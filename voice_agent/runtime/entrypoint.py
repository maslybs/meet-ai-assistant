import json
import logging
import os
from typing import Any, Optional

from ..agent import GeminiVisionAgent, LIVEKIT_IMPORT_ERROR
from ..config import AgentConfig, load_config, _is_truthy
from ..compat import bootstrap as bootstrap_compat
from .events import ParticipantGreeter
from .session import (
    SessionArtifacts,
    SessionSettings,
    build_agent_session,
    derive_session_settings,
    normalize_room_name,
    _resolve_gemini_api_key,
)

_VIDEO_LOGGER = logging.getLogger("voice-agent.video")


def _load_job_metadata(ctx: Any) -> dict[str, Any]:
    job_metadata_raw = getattr(ctx.job, "metadata", "") or "{}"
    try:
        return json.loads(job_metadata_raw)
    except json.JSONDecodeError:
        return {}


def _determine_room(ctx: Any, job_metadata: dict[str, Any]) -> str:
    room_name = normalize_room_name(getattr(ctx.room, "name", ""))
    if not room_name:
        room_name = normalize_room_name(job_metadata.get("room"))
    if not room_name:
        room_name = normalize_room_name(job_metadata.get("roomName"))
    return room_name


def _compute_env_managed_rooms() -> set[str]:
    default_room = os.getenv("VOICE_AGENT_DEFAULT_ROOM", "").strip()
    demo_room = os.getenv("VOICE_AGENT_DEMO_ROOM", "").strip()
    return {
        value.casefold()
        for value in (default_room, demo_room)
        if isinstance(value, str) and value.strip()
    }


def _resolve_broadcast_mode(job_metadata: dict[str, Any]) -> bool:
    broadcast_default = os.getenv("VOICE_AGENT_MULTI_PARTICIPANT", "false").strip().lower()
    broadcast_mode = broadcast_default not in {"", "0", "false", "no"}
    if "multi_participant" in job_metadata:
        broadcast_mode = bool(job_metadata.get("multi_participant"))
    return broadcast_mode


def _should_terminate_on_empty(job_metadata: dict[str, Any]) -> bool:
    terminate_default = os.getenv("VOICE_AGENT_TERMINATE_ON_EMPTY", "true")
    terminate = _is_truthy(terminate_default)
    if "terminate_on_empty" in job_metadata:
        terminate = _is_truthy(job_metadata.get("terminate_on_empty"))
    return terminate


def _should_close_room_on_empty(job_metadata: dict[str, Any]) -> bool:
    close_default = os.getenv("VOICE_AGENT_CLOSE_ROOM_ON_EMPTY", "true")
    close_room = _is_truthy(close_default)
    if "close_room_on_empty" in job_metadata:
        close_room = _is_truthy(job_metadata.get("close_room_on_empty"))
    return close_room


def _resolve_room_empty_delay(job_metadata: dict[str, Any]) -> float:
    delay_raw = os.getenv("VOICE_AGENT_ROOM_EMPTY_SHUTDOWN_DELAY", "3.0")
    if "room_empty_shutdown_delay" in job_metadata:
        delay_raw = str(job_metadata.get("room_empty_shutdown_delay"))
    try:
        return max(0.0, float(delay_raw))
    except (TypeError, ValueError):
        _VIDEO_LOGGER.warning(
            "Invalid shutdown delay value '%s'; defaulting to 3.0 seconds.", delay_raw
        )
        return 3.0


def _resolve_greeting_delay(job_metadata: dict[str, Any]) -> float:
    delay_raw = os.getenv("VOICE_AGENT_GREETING_DELAY", "0.5")
    if "greeting_delay" in job_metadata:
        delay_raw = str(job_metadata.get("greeting_delay"))
    try:
        return max(0.0, float(delay_raw))
    except (TypeError, ValueError):
        _VIDEO_LOGGER.warning(
            "Invalid greeting delay value '%s'; defaulting to 0.5 seconds.", delay_raw
        )
        return 0.5


def _create_participant_greeter(
    ctx: Any,
    session_artifacts: SessionArtifacts,
    *,
    broadcast_mode: bool,
    terminate_on_empty: bool,
    close_room_on_empty: bool,
    shutdown_delay: float,
    greeting_delay: float,
) -> Optional[ParticipantGreeter]:
    room_io = getattr(session_artifacts.session, "_room_io", None)
    if room_io is None:
        return None

    if broadcast_mode:
        try:
            room_io.set_participant(None)
        except Exception as exc:
            _VIDEO_LOGGER.warning("Failed to enable multi-participant mode: %s", exc)
        else:
            _VIDEO_LOGGER.info(
                "RoomIO switched to broadcast mode; agent listening to all participants."
            )

    greeting_text = (
        "Привітай користувача, ввічливо назви себе Ганною та коротко запропонуй допомогу"
    )

    greeter = ParticipantGreeter(
        ctx=ctx,
        session=session_artifacts.session,
        room_io=room_io,
        broadcast_mode=broadcast_mode,
        greeting_text=greeting_text,
        terminate_on_empty=terminate_on_empty,
        close_room_on_empty=close_room_on_empty,
        shutdown_delay=shutdown_delay,
        greeting_delay=greeting_delay,
    )
    greeter.attach()
    return greeter


async def run_job(ctx: Any) -> None:
    if LIVEKIT_IMPORT_ERROR is not None:
        raise RuntimeError(
            "LiveKit agents are not available; fallback handler should have run instead."
        ) from LIVEKIT_IMPORT_ERROR

    bootstrap_compat()

    config: AgentConfig = load_config()
    job_metadata = _load_job_metadata(ctx)
    room_name = _determine_room(ctx, job_metadata)
    env_managed_rooms = _compute_env_managed_rooms()
    default_api_key = _resolve_gemini_api_key()

    settings: SessionSettings = derive_session_settings(
        config,
        job_metadata,
        room_name=room_name,
        env_managed_rooms=env_managed_rooms,
        default_api_key=default_api_key,
    )

    session_artifacts = build_agent_session(settings)
    agent = GeminiVisionAgent(instructions=settings.instructions)

    await session_artifacts.session.start(
        agent=agent,
        room=ctx.room,
        room_input_options=session_artifacts.room_input_options,
        room_output_options=session_artifacts.room_output_options,
    )
    _VIDEO_LOGGER.info(
        "Session started; video capture enabled while user camera is active."
    )

    broadcast_mode = _resolve_broadcast_mode(job_metadata)
    terminate_on_empty = _should_terminate_on_empty(job_metadata)
    close_room_on_empty = _should_close_room_on_empty(job_metadata)
    shutdown_delay = _resolve_room_empty_delay(job_metadata)
    greeting_delay = _resolve_greeting_delay(job_metadata)

    async def _stop_session(_: str) -> None:
        await session_artifacts.session.aclose()

    ctx.add_shutdown_callback(_stop_session)

    _create_participant_greeter(
        ctx,
        session_artifacts,
        broadcast_mode=broadcast_mode,
        terminate_on_empty=terminate_on_empty,
        close_room_on_empty=close_room_on_empty,
        shutdown_delay=shutdown_delay,
        greeting_delay=greeting_delay,
    )


__all__ = ["run_job"]
