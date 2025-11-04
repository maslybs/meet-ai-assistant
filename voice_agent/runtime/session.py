import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from ..config import AgentConfig, _resolve_voice_override, _is_truthy

_GEMINI_LOGGER = logging.getLogger("voice-agent.gemini")
_VIDEO_LOGGER = logging.getLogger("voice-agent.video")


@dataclass
class SessionSettings:
    instructions: str
    model: str
    voice: str
    temperature: float
    enable_search: bool
    gemini_api_key: Optional[str]


@dataclass
class SessionArtifacts:
    session: Any
    room_input_options: Any
    room_output_options: Any
    video_sampler: Optional[Any]


def normalize_room_name(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def derive_session_settings(
    base_config: AgentConfig,
    job_metadata: dict[str, Any],
    *,
    room_name: str,
    env_managed_rooms: set[str],
    default_api_key: Optional[str],
) -> SessionSettings:
    voice_override_raw = job_metadata.get("voice")
    voice_override = voice_override_raw.strip() if isinstance(voice_override_raw, str) else None
    temperature_override = job_metadata.get("temperature")
    search_override_raw = job_metadata.get("enable_search")
    if search_override_raw is None:
        search_override_raw = job_metadata.get("search_enabled")

    instructions_override = job_metadata.get("instructions")
    model_override = job_metadata.get("model")

    effective_instructions = (
        (instructions_override or base_config.instructions).strip() or base_config.instructions
    )
    effective_model = (model_override or base_config.model).strip() or base_config.model
    effective_voice = (
        (voice_override or base_config.voice).strip()
        or _resolve_voice_override(base_config.voice)
    )
    effective_temperature = float(temperature_override or base_config.temperature)
    effective_search_enabled = base_config.enable_search
    if search_override_raw is not None:
        effective_search_enabled = _is_truthy(search_override_raw)

    gemini_key_override = job_metadata.get("gemini_api_key")
    normalized_room = room_name.casefold() if room_name else ""
    use_env_key = bool(normalized_room and normalized_room in env_managed_rooms)
    gemini_api_key = default_api_key
    if not use_env_key:
        gemini_api_key = gemini_key_override or gemini_api_key

    return SessionSettings(
        instructions=effective_instructions,
        model=effective_model,
        voice=effective_voice,
        temperature=effective_temperature,
        enable_search=effective_search_enabled,
        gemini_api_key=gemini_api_key,
    )


def _resolve_gemini_api_key() -> Optional[str]:
    """
    Support both GOOGLE_API_KEY (default expected by the plugin)
    and GEMINI_API_KEY (commonly used in docs for Gemini).
    """

    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


def _resolve_gemini_tools(*, enable_search: bool) -> list[Any]:
    if not enable_search:
        return []

    try:
        from livekit.plugins import google  # type: ignore
    except ImportError:
        _GEMINI_LOGGER.warning(
            "Gemini search requested but livekit.plugins.google is unavailable; skipping tool setup.",
        )
        return []

    try:
        from google.genai import types as google_types  # type: ignore
    except ImportError as exc:
        _GEMINI_LOGGER.warning(
            "Gemini search requested but google.genai.types is missing: %s", exc
        )
        return []

    try:
        return [google_types.GoogleSearch()]
    except Exception as exc:  # pragma: no cover - defensive guard
        _GEMINI_LOGGER.warning("Failed to configure Gemini Google Search tool: %s", exc)
        return []


def _resolve_video_sampler() -> Optional[Any]:
    """
    Build a voice-activity-aware video sampler so we only forward frames when needed.
    Allows overriding defaults via environment variables.
    """

    try:
        from livekit.agents import voice  # type: ignore
    except ImportError:
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

    speaking = getattr(sampler, "speaking_fps", "unknown")
    silent = getattr(sampler, "silent_fps", "unknown")
    _VIDEO_LOGGER.info(
        "Video sampler configured (speaking_fps=%s, silent_fps=%s).",
        speaking,
        silent,
    )


def build_agent_session(settings: SessionSettings) -> SessionArtifacts:
    from livekit.agents import AgentSession, RoomInputOptions, RoomOutputOptions  # type: ignore
    from livekit.plugins import google  # type: ignore

    video_sampler = _resolve_video_sampler()
    agent_session_kwargs: dict[str, Any] = {}
    if video_sampler is not None:
        agent_session_kwargs["video_sampler"] = video_sampler

    gemini_tools = _resolve_gemini_tools(enable_search=settings.enable_search)
    llm_kwargs: dict[str, Any] = {
        "model": settings.model,
        "voice": settings.voice,
        "temperature": settings.temperature,
        "api_key": settings.gemini_api_key,
    }
    if gemini_tools:
        llm_kwargs["_gemini_tools"] = gemini_tools
        _GEMINI_LOGGER.info("Google Search tool enabled for Gemini Realtime session.")

    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            **llm_kwargs,
        ),
        user_away_timeout=None,
        **agent_session_kwargs,
    )

    _log_video_sampler_settings(video_sampler)
    room_input_options = RoomInputOptions(
        video_enabled=True,
        close_on_disconnect=False,
    )
    room_output_options = RoomOutputOptions(transcription_enabled=True)
    return SessionArtifacts(
        session=session,
        room_input_options=room_input_options,
        room_output_options=room_output_options,
        video_sampler=video_sampler,
    )


__all__ = [
    "SessionSettings",
    "derive_session_settings",
    "normalize_room_name",
    "build_agent_session",
    "SessionArtifacts",
    "_resolve_gemini_api_key",
]
