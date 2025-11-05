import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from .agent import LIVEKIT_IMPORT_ERROR
from .config import AgentConfig, load_config, load_dotenv
from .runtime.entrypoint import run_job
from .compat import bootstrap as bootstrap_compat


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
        f"Інструкції асистента: {config.instructions}",
        file=sys.stderr,
    )
    print(
        "Немає активного з'єднання з LiveKit, тому відповіді генеруються не будуть.",
        file=sys.stderr,
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
        from livekit import api  # type: ignore

        start = asyncio.get_running_loop().time()
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

                elapsed = asyncio.get_running_loop().time() - start
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


def _apply_env_cli_defaults() -> None:
    """
    Allow a shorthand mode where the developer only sets env vars and runs
    `python main.py` without passing CLI args. When VOICE_AGENT_ROOM is set, we
    pivot to the `connect` command with env-provided defaults. Optionally wait
    until the room is already occupied so the agent does not become the host.
    """

    if len(sys.argv) > 1:
        return

    autostart_mode = (
        os.getenv("VOICE_AGENT_AUTOSTART_MODE", "dispatch").strip().lower() or "dispatch"
    )
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


def run_cli() -> None:
    load_dotenv()
    config = load_config()

    bootstrap_compat()

    root_dir = str(Path(__file__).resolve().parents[1])
    pythonpath = os.environ.get("PYTHONPATH", "")
    paths = [p for p in pythonpath.split(os.pathsep) if p]
    if root_dir not in paths:
        paths.insert(0, root_dir)
        os.environ["PYTHONPATH"] = os.pathsep.join(paths)

    _apply_env_cli_defaults()

    if LIVEKIT_IMPORT_ERROR is not None:
        _handle_missing_livekit(LIVEKIT_IMPORT_ERROR, config)
        return

    from livekit.agents import WorkerOptions, cli  # type: ignore

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=run_job,
            agent_name=config.agent_name,
            initialize_process_timeout=float(os.getenv("VOICE_AGENT_INIT_TIMEOUT", "15")),
        )
    )


__all__ = ["run_cli"]
