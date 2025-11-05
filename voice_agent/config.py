import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .resources import read_instructions


try:
    from dotenv import load_dotenv as _load_dotenv  # type: ignore
except ImportError:  # pragma: no cover - optional dependency

    def _load_dotenv() -> None:
        """Fallback no-op if python-dotenv is not installed."""
        return


def load_dotenv() -> None:
    """Public wrapper to keep imports lazy in callers."""

    _load_dotenv()


def _is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


@dataclass
class AgentConfig:
    instructions: str
    agent_name: str
    model: str = "gemini-1.5-pro"
    voice: str = "Charis"
    temperature: float = 0.8
    enable_search: bool = False


def load_config() -> AgentConfig:
    instructions = os.getenv("VOICE_AGENT_INSTRUCTIONS")

    if not instructions:
        prompt_path = Path(os.getenv("VOICE_AGENT_PROMPT_FILE", "prompt.md"))
        instructions = read_instructions(prompt_path)

    search_flag = os.getenv("GEMINI_ENABLE_SEARCH")

    return AgentConfig(
        instructions=instructions,
        agent_name=os.getenv("VOICE_AGENT_NAME", "Hanna").strip() or "Hanna",
        model=os.getenv(
            "GEMINI_MODEL", "gemini-2.5-flash-native-audio-preview-09-2025"
        ),
        voice=os.getenv("GEMINI_TTS_VOICE", ""),
        temperature=float(os.getenv("GEMINI_TEMPERATURE", 0.8)),
        enable_search=_is_truthy(search_flag) if search_flag is not None else False,
    )


def _resolve_voice_override(default: Optional[str] = None) -> str:
    """
    Provide a final fallback when neither the environment nor job metadata specify a voice.
    """

    override = os.getenv("GEMINI_TTS_VOICE_DEFAULT") or ""
    override = override.strip()
    if override:
        return override
    if default:
        return default.strip()
    return "Charis"

