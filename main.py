import asyncio
import inspect
import logging
import os
import sys
import time
from dataclasses import dataclass
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
    from livekit.agents import Agent, AgentSession, RoomOutputOptions, WorkerOptions, cli
    from livekit.plugins import google
except ImportError as import_error:  # pragma: no cover - depends on local env
    Agent = AgentSession = RoomOutputOptions = WorkerOptions = cli = google = None  # type: ignore[assignment]
    _LIVEKIT_IMPORT_ERROR: Optional[ImportError] = import_error
else:
    _LIVEKIT_IMPORT_ERROR = None

if TYPE_CHECKING:
    from livekit.agents import JobContext as LivekitJobContext
else:  # pragma: no cover - runtime fallback
    LivekitJobContext = Any


@dataclass
class AgentConfig:
    """Configuration sourced from environment variables for the Gemini RealtimeModel."""

    instructions: str = (
        "You are a friendly Gemini-based voice assistant. Answer promptly, keep responses short, "
        "and speak Ukrainian whenever possible."
    )
    model: str = "gemini-1.5-pro"
    voice: str = "Charis"
    temperature: float = 0.8


def load_config() -> AgentConfig:
    defaults = AgentConfig()
    return AgentConfig(
        instructions=os.getenv("VOICE_AGENT_INSTRUCTIONS", defaults.instructions),
        model=os.getenv("GEMINI_MODEL", defaults.model),
        voice=os.getenv("GEMINI_TTS_VOICE", defaults.voice),
        temperature=float(os.getenv("GEMINI_TEMPERATURE", defaults.temperature)),
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
    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=config.model,
            voice=config.voice,
            temperature=config.temperature,
            api_key=_resolve_gemini_api_key(),
        ),
    )

    agent = Agent(instructions=config.instructions)

    await session.start(
        agent=agent,
        room=ctx.room,
        room_output_options=RoomOutputOptions(transcription_enabled=True),
    )


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
