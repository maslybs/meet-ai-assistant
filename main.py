import asyncio
import inspect
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional


def _ensure_importlib_compat() -> None:
    """
    google.api_core expects importlib.metadata.packages_distributions which is
    only available in the stdlib starting with Python 3.10. When running on
    Python 3.9 we mirror the backport implementation to avoid runtime errors.
    """

    try:
        import importlib.metadata as stdlib_metadata  # type: ignore
    except ImportError:  # pragma: no cover - older interpreters
        return

    try:
        import importlib_metadata as backport  # type: ignore
    except ImportError:
        return

    if not hasattr(stdlib_metadata, "packages_distributions") and hasattr(
        backport, "packages_distributions"
    ):
        setattr(stdlib_metadata, "packages_distributions", backport.packages_distributions)  # type: ignore[attr-defined]


_ensure_importlib_compat()


def _patch_aiohttp_proxy_kwarg() -> None:
    """
    livekit-agents < 1.3.0 still calls aiohttp.ClientSession with the removed
    ``proxy=`` keyword (aiohttp 3.10+). Patch the ctor to ignore that keyword.
    """

    try:
        import aiohttp  # type: ignore
    except ImportError:  # pragma: no cover
        return

    original_init = aiohttp.ClientSession.__init__
    signature = inspect.signature(original_init)

    if "proxy" in signature.parameters:
        return

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("proxy", None)
        return original_init(self, *args, **kwargs)

    aiohttp.ClientSession.__init__ = _patched_init  # type: ignore[assignment]


_patch_aiohttp_proxy_kwarg()


def _patch_livekit_room_event() -> None:
    """
    Work around a race in livekit-rtc where local track events may arrive before
    the publication map is populated. Skip known-bad events instead of raising.
    """

    try:
        from livekit.rtc import room as rtc_room  # type: ignore
    except ImportError:  # pragma: no cover
        return

    original = rtc_room.Room._on_room_event
    logger = logging.getLogger("voice-agent.livekit")

    def _patched(self, event):  # type: ignore[no-untyped-def]
        try:
            return original(self, event)
        except KeyError as error:
            which = event.WhichOneof("message")
            if which in {"local_track_published", "local_track_subscribed"}:
                sid = getattr(getattr(event, which), "track_sid", "<unknown>")
                logger.warning(
                    "Skipped LiveKit event %s for missing local track %s",
                    which,
                    sid,
                )
                return
            raise

    rtc_room.Room._on_room_event = _patched  # type: ignore[assignment]


_patch_livekit_room_event()

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:  # type: ignore[no-redef]
        """Fallback no-op if python-dotenv is not installed."""
        return

try:
    from livekit.agents import (
        Agent,
        AgentSession,
        RoomInputOptions,
        RoomOutputOptions,
        RunContext,
        WorkerOptions,
        cli,
        voice,
    )
    from livekit.agents.llm import function_tool
    from livekit.plugins import google
except ImportError as import_error:  # pragma: no cover - depends on local env
    Agent = (
        AgentSession
    ) = RoomInputOptions = RoomOutputOptions = RunContext = WorkerOptions = cli = voice = None  # type: ignore[assignment]
    function_tool = None  # type: ignore[assignment]
    google = None  # type: ignore[assignment]
    _LIVEKIT_IMPORT_ERROR: Optional[ImportError] = import_error
else:
    _LIVEKIT_IMPORT_ERROR = None

if TYPE_CHECKING:
    from livekit.agents import JobContext as LivekitJobContext
else:  # pragma: no cover - runtime fallback
    LivekitJobContext = Any

if RunContext is None:  # type: ignore[truthy-bool]
    RunContext = Any  # type: ignore[assignment]

if function_tool is None:  # type: ignore[misc]
    def function_tool(func):  # type: ignore[no-redef]
        return func


@dataclass
class AgentConfig:
    """Configuration sourced from environment variables for the Gemini RealtimeModel."""

    instructions: str
    model: str = "gemini-1.5-pro"
    voice: str = "Charis"
    temperature: float = 0.8


_VIDEO_LOGGER = logging.getLogger("voice-agent.video")


class GeminiVisionAgent(Agent):
    """
    Custom agent that exposes tools for managing the room video feed.
    Video is consumed automatically when available, while still giving the user
    an explicit way to pause or resume it.
    """

    def __init__(self, *, instructions: str) -> None:
        super().__init__(instructions=instructions)
        self._video_toggle_lock = asyncio.Lock()

    @function_tool
    async def enable_video_feed(self, _: RunContext) -> str:
        """
        Увімкнути передачу відео з камери користувача, якщо її було вимкнено раніше.
        """

        async with self._video_toggle_lock:
            session = self.session
            if session is None:
                return "Зараз не можу отримати відео, спробуйте пізніше."

            video_stream = session.input.video
            if video_stream is None:
                return "Відео від учасника недоступне. Переконайтеся, що камера увімкнена."

            if session.input.video_enabled:
                return "Відео вже увімкнене."

            session.input.set_video_enabled(True)
            _VIDEO_LOGGER.info("Video feed enabled by request")
            return "Добре, я бачу відео. Дайте знати, що саме потрібно показати."

    @function_tool
    async def disable_video_feed(self, _: RunContext) -> str:
        """
        Вимкнути захоплення відео, щоб зекономити ресурси або надати приватність на вимогу.
        """

        async with self._video_toggle_lock:
            session = self.session
            if session is None or session.input.video is None:
                return "Зараз відеосигнал недоступний."

            if not session.input.video_enabled:
                return "Відео вже вимкнене."

            session.input.set_video_enabled(False)
            _VIDEO_LOGGER.info("Video feed disabled on request")
            return "Вимкнула відео. Якщо знадобиться знову, просто скажіть."


def load_config() -> AgentConfig:
    instructions = os.getenv("VOICE_AGENT_INSTRUCTIONS")

    if not instructions:
        prompt_path = Path(os.getenv("VOICE_AGENT_PROMPT_FILE", "prompt.md"))
        try:
            instructions = prompt_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Prompt file '{prompt_path}' is missing. Provide VOICE_AGENT_PROMPT_FILE or VOICE_AGENT_INSTRUCTIONS."
            ) from exc

    return AgentConfig(
        instructions=instructions,
        model=os.getenv("GEMINI_MODEL", "gemini-1.5-pro"),
        voice=os.getenv("GEMINI_TTS_VOICE", "Charis"),
        temperature=float(os.getenv("GEMINI_TEMPERATURE", 0.8)),
    )


async def entrypoint(ctx: "LivekitJobContext") -> None:
    """
    Launch a LiveKit session powered by Gemini RealtimeModel. The realtime model
    handles transcription, response generation, and voice synthesis in one stream.
    """

    if _LIVEKIT_IMPORT_ERROR is not None:
        raise RuntimeError(
            "LiveKit agents are not available; fallback handler should have run instead."
        ) from _LIVEKIT_IMPORT_ERROR

    config = load_config()
    video_sampler = _resolve_video_sampler()
    agent_session_kwargs: dict[str, Any] = {}
    if video_sampler is not None:
        agent_session_kwargs["video_sampler"] = video_sampler

    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=config.model,
            voice=config.voice,
            temperature=config.temperature,
            api_key=_resolve_gemini_api_key(),
        ),
        user_away_timeout=None,
        **agent_session_kwargs,
    )

    _log_video_sampler_settings(video_sampler)
    agent = GeminiVisionAgent(instructions=config.instructions)

    await session.start(
        agent=agent,
        room=ctx.room,
        room_input_options=RoomInputOptions(
            video_enabled=True,
            close_on_disconnect=False,
        ),
        room_output_options=RoomOutputOptions(transcription_enabled=True),
    )
    _VIDEO_LOGGER.info("Session started; video capture enabled while user camera is active.")

    room_io = getattr(session, "_room_io", None)

    if room_io is not None:

        def _handle_participant_connected(participant: Any) -> None:
            linked = room_io.linked_participant
            target_identity = getattr(room_io, "_participant_identity", None)
            identity = getattr(participant, "identity", None)
            if identity is None:
                return

            if linked is None:
                room_io.set_participant(identity)
            elif getattr(linked, "identity", None) == identity:
                room_io.set_participant(identity)
            elif target_identity is None:
                # default behaviour is to follow the first participant
                room_io.set_participant(identity)

            async def _greet() -> None:
                try:
                    await session.generate_reply(
                        instructions="Привітай користувача, ввічливо назви себе Ганною та коротко запропонуй допомогу."
                    )
                except Exception as exc:  # pragma: no cover - best effort logging
                    _VIDEO_LOGGER.warning("Failed to send greeting: %s", exc)

            asyncio.create_task(_greet())

        def _handle_participant_disconnected(participant: Any) -> None:
            linked = room_io.linked_participant
            identity = getattr(participant, "identity", None)
            if linked is None or identity is None:
                return
            if getattr(linked, "identity", None) == identity:
                room_io.unset_participant()

        ctx.room.on("participant_connected", _handle_participant_connected)
        ctx.room.on("participant_disconnected", _handle_participant_disconnected)

        for participant in ctx.room.remote_participants.values():
            _handle_participant_connected(participant)

        def _cleanup_callbacks() -> None:
            ctx.room.off("participant_connected", _handle_participant_connected)
            ctx.room.off("participant_disconnected", _handle_participant_disconnected)

        ctx.add_shutdown_callback(_cleanup_callbacks)


def _handle_missing_livekit(error: ImportError, config: AgentConfig) -> None:
    """
    Provide a clear message when LiveKit (or its plugins) are unavailable locally.
    This keeps the script runnable even in offline/dev environments.
    """

    print("LiveKit агенти недоступні в цій системі.", file=sys.stderr)
    print(
        "Переконайтеся, що залежності встановлені командою "
        "`python3 -m pip install -r requirements.txt`.",
        file=sys.stderr,
    )
    print(f"Деталі: {error}", file=sys.stderr)
    print()
    print("Режим демонстрації:", file=sys.stderr)
    print(
        f"Інструкції асистента: {config.instructions}", file=sys.stderr
    )
    print(
        "Немає активного з'єднання з LiveKit, тому відповіді генеруються не будуть.",
        file=sys.stderr,
    )


def main() -> None:
    load_dotenv()

    config = load_config()

    _apply_env_cli_defaults()

    if _LIVEKIT_IMPORT_ERROR is not None:
        _handle_missing_livekit(_LIVEKIT_IMPORT_ERROR, config)
        return

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
        )
    )


def _apply_env_cli_defaults() -> None:
    """
    Allow a shorthand mode where the developer only sets env vars and runs
    `python main.py` without passing CLI args. When VOICE_AGENT_ROOM is set, we
    pivot to the `connect` command with env-provided defaults. Optionally wait
    until the room is already occupied so the agent does not become the host.
    """

    if len(sys.argv) > 1:
        return

    room = os.getenv("VOICE_AGENT_ROOM")
    if not room:
        return

    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    watch_flag = os.getenv("VOICE_AGENT_WATCH", "").strip().lower()

    cli_args = ["connect", "--room", room]

    if url:
        cli_args.extend(["--url", url])
    if api_key:
        cli_args.extend(["--api-key", api_key])
    if api_secret:
        cli_args.extend(["--api-secret", api_secret])
    if watch_flag in {"0", "false", "no"}:
        cli_args.append("--no-watch")

    wait_for_occupant = os.getenv("VOICE_AGENT_WAIT_FOR_OCCUPANT", "true").strip().lower() not in {
        "",
        "0",
        "false",
        "no",
    }
    if wait_for_occupant:
        if not (url and api_key and api_secret):
            print(
                "[voice-agent] VOICE_AGENT_WAIT_FOR_OCCUPANT is enabled but LIVEKIT_URL/API_KEY/API_SECRET "
                "are missing. Skipping occupancy check.",
                file=sys.stderr,
            )
        else:
            try:
                _wait_for_room_participants(room, url, api_key, api_secret)
            except Exception as exc:  # pragma: no cover - best effort guard
                print(
                    f"[voice-agent] Failed to wait for room occupants: {exc}. Continuing without guard.",
                    file=sys.stderr,
                )

    print(
        f"[voice-agent] VOICE_AGENT_ROOM detected. Defaulting to `python main.py {' '.join(cli_args)}`.",
        file=sys.stderr,
    )
    sys.argv.extend(cli_args)


def _resolve_gemini_api_key() -> Optional[str]:
    """
    Support both GOOGLE_API_KEY (default expected by the plugin)
    and GEMINI_API_KEY (commonly used in docs for Gemini).
    """

    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


def _resolve_video_sampler() -> Optional[Any]:
    """
    Build a voice-activity-aware video sampler so we only forward frames when needed.
    Allows overriding defaults via environment variables.
    """

    if voice is None:
        return None

    speaking_fps_raw = os.getenv("VOICE_AGENT_VIDEO_FPS_SPEAKING", "1.0")
    silent_fps_raw = os.getenv("VOICE_AGENT_VIDEO_FPS_SILENT", "0.3")

    try:
        speaking_fps = max(0.0, float(speaking_fps_raw))
        silent_fps = max(0.0, float(silent_fps_raw))
    except ValueError:
        _VIDEO_LOGGER.warning(
            "Invalid VOICE_AGENT_VIDEO_FPS_* values (%s, %s); falling back to defaults.",
            speaking_fps_raw,
            silent_fps_raw,
        )
        speaking_fps, silent_fps = 1.0, 0.3

    return voice.VoiceActivityVideoSampler(
        speaking_fps=speaking_fps,
        silent_fps=silent_fps,
    )


def _log_video_sampler_settings(sampler: Optional[Any]) -> None:
    if sampler is None:
        _VIDEO_LOGGER.info("Video sampler not configured (using SDK defaults).")
        return

    # VoiceActivityVideoSampler exposes attributes for the configured fps.
    speaking = getattr(sampler, "speaking_fps", "unknown")
    silent = getattr(sampler, "silent_fps", "unknown")
    _VIDEO_LOGGER.info(
        "Video sampler configured (speaking_fps=%s, silent_fps=%s).",
        speaking,
        silent,
    )


def _wait_for_room_participants(
    room: str,
    url: str,
    api_key: str,
    api_secret: str,
) -> None:
    """
    Poll the LiveKit RoomService until the target room has at least one
    participant. This prevents the agent from being the first joiner/host.
    """

    poll_seconds = float(os.getenv("VOICE_AGENT_POLL_SECONDS", "2.0"))
    timeout_seconds = float(os.getenv("VOICE_AGENT_WAIT_TIMEOUT", "0"))

    async def _wait_loop() -> None:
        from livekit import api

        start = time.monotonic()
        attempt = 0

        async with api.LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret) as lkapi:
            while True:
                attempt += 1
                try:
                    response = await lkapi.room.list_participants(
                        api.ListParticipantsRequest(room=room)
                    )
                    participants = response.participants
                except api.TwirpError as err:
                    if err.code == api.TwirpErrorCode.not_found:
                        participants = []
                    else:
                        raise

                if participants:
                    identities = ", ".join(
                        participant.identity or participant.name or "<unknown>"
                        for participant in participants
                    )
                    print(
                        f"[voice-agent] Room '{room}' has active participants ({identities}); connecting.",
                        file=sys.stderr,
                    )
                    return

                elapsed = time.monotonic() - start
                if timeout_seconds and elapsed > timeout_seconds:
                    raise TimeoutError(
                        f"Timed out after {timeout_seconds}s waiting for participants in room '{room}'."
                    )

                if attempt == 1:
                    print(
                        f"[voice-agent] Waiting for participants in room '{room}' before connecting...",
                        file=sys.stderr,
                    )
                await asyncio.sleep(poll_seconds)

    asyncio.run(_wait_loop())


if __name__ == "__main__":
    main()
