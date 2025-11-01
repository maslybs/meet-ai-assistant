import asyncio
import inspect
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
import json


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
    from livekit.agents.types import ATTRIBUTE_PUBLISH_ON_BEHALF
    from livekit.plugins import google
    from livekit.agents.llm.realtime import RealtimeError
    try:
        from livekit import rtc as lk_rtc
    except ImportError:  # pragma: no cover - rtc may be optional
        lk_rtc = None  # type: ignore[assignment]
except ImportError as import_error:  # pragma: no cover - depends on local env
    Agent = (
        AgentSession
    ) = RoomInputOptions = RoomOutputOptions = RunContext = WorkerOptions = cli = voice = None  # type: ignore[assignment]
    function_tool = None  # type: ignore[assignment]
    google = None  # type: ignore[assignment]
    RealtimeError = Exception  # type: ignore[assignment]
    ATTRIBUTE_PUBLISH_ON_BEHALF = "lk.publish_on_behalf"  # type: ignore[assignment]
    lk_rtc = None  # type: ignore[assignment]
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
    agent_name: str
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
        agent_name=os.getenv("VOICE_AGENT_NAME", "hanna-agent").strip() or "hanna-agent",
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-native-audio-preview-09-2025"),
        voice=os.getenv("GEMINI_TTS_VOICE", ""),
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

    job_metadata_raw = getattr(ctx.job, "metadata", "") or "{}"
    try:
        job_metadata: dict[str, Any] = json.loads(job_metadata_raw)
    except json.JSONDecodeError:
        job_metadata = {}

    room_name = getattr(ctx.room, "name", "") or job_metadata.get("room") or ""
    default_room = os.getenv("VOICE_AGENT_DEFAULT_ROOM", "").strip()

    gemini_key_override = job_metadata.get("gemini_api_key")
    use_env_key = bool(default_room and room_name == default_room)
    gemini_api_key = _resolve_gemini_api_key()
    if not use_env_key:
        gemini_api_key = gemini_key_override or gemini_api_key

    instructions_override = job_metadata.get("instructions")
    model_override = job_metadata.get("model")
    voice_override_raw = job_metadata.get("voice")
    voice_override = voice_override_raw.strip() if isinstance(voice_override_raw, str) else None
    temperature_override = job_metadata.get("temperature")

    effective_instructions = (instructions_override or config.instructions).strip() or config.instructions
    effective_model = (model_override or config.model).strip() or config.model
    effective_voice = (voice_override or config.voice).strip() or _resolve_voice_override()
    effective_temperature = float(temperature_override or config.temperature)

    video_sampler = _resolve_video_sampler()
    agent_session_kwargs: dict[str, Any] = {}
    if video_sampler is not None:
        agent_session_kwargs["video_sampler"] = video_sampler

    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=effective_model,
            voice=effective_voice,
            temperature=effective_temperature,
            api_key=gemini_api_key,
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
        broadcast_default = os.getenv("VOICE_AGENT_MULTI_PARTICIPANT", "true").strip().lower()
        broadcast_mode = broadcast_default not in {"", "0", "false", "no"}
        if "multi_participant" in job_metadata:
            broadcast_mode = bool(job_metadata.get("multi_participant"))

        if broadcast_mode:
            try:
                room_io.set_participant(None)
            except Exception as exc:
                _VIDEO_LOGGER.warning("Failed to enable multi-participant mode: %s", exc)
            else:
                _VIDEO_LOGGER.info("RoomIO switched to broadcast mode; agent listening to all participants.")

        async def _wait_for_media_ready(identity: str, timeout: float = 10.0, *, broadcast: bool) -> None:
            """
            Wait until RoomIO links to the participant and the media pipelines are live.
            This avoids greeting the user before audio input/output are ready.
            """

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout

            while True:
                linked = room_io.linked_participant
                audio_input = room_io.audio_input

                audio_ready = audio_input is not None
                if audio_input is not None:
                    audio_ready = True
                    if lk_rtc is not None:
                        source = getattr(audio_input, "publication_source", None)
                        if source is None:
                            audio_ready = False

                if broadcast:
                    if audio_ready:
                        break
                else:
                    if (
                        linked is not None
                        and getattr(linked, "identity", None) == identity
                        and audio_ready
                    ):
                        break

                if loop.time() > deadline:
                    raise TimeoutError(
                        f"Timed out waiting for media streams for participant '{identity}'."
                    )
                await asyncio.sleep(0.1)

            subscribed = room_io.subscribed_fut
            if subscribed is not None and not subscribed.done():
                try:
                    await asyncio.wait_for(asyncio.shield(subscribed), timeout)
                except asyncio.TimeoutError:
                    _VIDEO_LOGGER.warning(
                        "Timed out waiting for LiveKit to subscribe to agent audio for %s",
                        identity,
                    )

        greeted_identities: set[str] = set()

        async def _send_greeting(identity: str) -> bool:
            """
            Deliver the welcome message to the participant.
            Retries if the realtime backend is still spinning up.
            Returns True if the greeting was delivered.
            """

            greeting_text = (
                "Привітай користувача, ввічливо назви себе Ганною та коротко запропонуй допомогу."
            )
            max_attempts = 3
            fallback_used = False
            for attempt in range(1, max_attempts + 1):
                try:
                    if fallback_used:
                        handle = session.say(greeting_text)
                    else:
                        handle = session.generate_reply(user_input=greeting_text)
                    await handle.wait_for_playout()
                    return True
                except RealtimeError as exc:
                    backoff = 0.6 * attempt
                    _VIDEO_LOGGER.warning(
                        "Greeting attempt %s for %s failed due to realtime timeout: %s (retry in %.1fs)",
                        attempt,
                        identity,
                        exc,
                        backoff if attempt < max_attempts else 0.0,
                    )
                    if attempt == max_attempts - 1 and not fallback_used:
                        fallback_used = True
                        continue
                    if attempt >= max_attempts:
                        return False
                    await asyncio.sleep(backoff)
                except Exception as exc:  # pragma: no cover - best effort logging
                    _VIDEO_LOGGER.warning("Failed to send greeting to %s: %s", identity, exc)
                    return False
            return False

        def _handle_participant_connected(participant: Any) -> None:
            identity = getattr(participant, "identity", None)
            if identity is None:
                return

            local_identity = getattr(ctx.room.local_participant, "identity", None)
            attributes = getattr(participant, "attributes", {}) or {}
            if attributes.get(ATTRIBUTE_PUBLISH_ON_BEHALF) == local_identity:
                return

            if lk_rtc is not None:
                configured_kinds = getattr(getattr(room_io, "_input_options", None), "participant_kinds", None)
                if isinstance(configured_kinds, list) and configured_kinds:
                    allowed_kinds = set(configured_kinds)
                else:
                    allowed_kinds = {
                        getattr(lk_rtc.ParticipantKind, "PARTICIPANT_KIND_STANDARD", None),
                        getattr(lk_rtc.ParticipantKind, "PARTICIPANT_KIND_SIP", None),
                    }
                participant_kind = getattr(participant, "kind", None)
                if participant_kind not in allowed_kinds:
                    return

            linked = room_io.linked_participant
            target_identity = getattr(room_io, "_participant_identity", None)

            should_follow = False
            if linked is None:
                should_follow = True
            elif getattr(linked, "identity", None) == identity:
                should_follow = True
            elif target_identity is None:
                should_follow = True
            elif target_identity == identity:
                should_follow = True

            if not should_follow:
                return

            if not broadcast_mode:
                room_io.set_participant(identity)

            async def _initialize_participant() -> None:
                try:
                    if not session.input.audio_enabled:
                        session.input.set_audio_enabled(True)
                except Exception as exc:  # pragma: no cover - defensive
                    _VIDEO_LOGGER.debug("Failed to ensure audio input enabled before wait: %s", exc)

                try:
                    await _wait_for_media_ready(identity, broadcast=broadcast_mode)
                except TimeoutError as exc:
                    _VIDEO_LOGGER.warning("Media for %s not ready: %s", identity, exc)
                except Exception as exc:  # pragma: no cover - best effort logging
                    _VIDEO_LOGGER.warning("Unexpected media wait failure for %s: %s", identity, exc)

                try:
                    if not session.input.audio_enabled:
                        session.input.set_audio_enabled(True)
                except Exception as exc:  # pragma: no cover - defensive
                    _VIDEO_LOGGER.debug("Failed to ensure audio input enabled: %s", exc)

                if identity in greeted_identities:
                    return

                greeted = await _send_greeting(identity)
                if greeted:
                    greeted_identities.add(identity)

            asyncio.create_task(_initialize_participant())

        def _handle_participant_disconnected(participant: Any) -> None:
            identity = getattr(participant, "identity", None)
            if identity is None:
                return

            greeted_identities.discard(identity)
            linked = room_io.linked_participant
            if linked is None:
                return

            if getattr(linked, "identity", None) == identity:
                room_io.unset_participant()

        ctx.room.on("participant_connected", _handle_participant_connected)
        ctx.room.on("participant_disconnected", _handle_participant_disconnected)

        for participant in ctx.room.remote_participants.values():
            _handle_participant_connected(participant)

        async def _cleanup_callbacks() -> None:
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
            agent_name=config.agent_name,
            initialize_process_timeout=float(os.getenv("VOICE_AGENT_INIT_TIMEOUT", "15")),
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

    autostart_mode = os.getenv("VOICE_AGENT_AUTOSTART_MODE", "dispatch").strip().lower() or "dispatch"
    if autostart_mode not in {"dispatch", "connect"}:
        print(
            f"[voice-agent] Unknown VOICE_AGENT_AUTOSTART_MODE '{autostart_mode}', falling back to dispatch.",
            file=sys.stderr,
        )
        autostart_mode = "dispatch"

    room = os.getenv("VOICE_AGENT_ROOM")
    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    watch_flag = os.getenv("VOICE_AGENT_WATCH", "").strip().lower()

    if autostart_mode == "connect":
        if not room:
            print(
                "[voice-agent] VOICE_AGENT_AUTOSTART_MODE=connect requires VOICE_AGENT_ROOM.",
                file=sys.stderr,
            )
            return

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
        return

    # dispatch mode (default)
    cli_args = ["dev"]
    if watch_flag in {"0", "false", "no"}:
        cli_args.append("--no-watch")
    if url:
        cli_args.extend(["--url", url])
    if api_key:
        cli_args.extend(["--api-key", api_key])
    if api_secret:
        cli_args.extend(["--api-secret", api_secret])

    if room:
        print(
            f"[voice-agent] Dispatch mode enabled. Waiting for AgentDispatch requests targeting room '{room}'.",
            file=sys.stderr,
        )
    else:
        print(
            "[voice-agent] Dispatch mode enabled. Waiting for AgentDispatch requests.",
            file=sys.stderr,
        )
    print(
        f"[voice-agent] Defaulting to `python main.py {' '.join(cli_args)}`.",
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
                    if err.code == api.TwirpErrorCode.NOT_FOUND:
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
