import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .resources import read_instructions
from .tools import radio, rss


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
    voice: str = "Achernar"
    temperature: float = 0.8
    enable_search: bool = False


def load_config() -> AgentConfig:
    instructions = os.getenv("VOICE_AGENT_INSTRUCTIONS")

    if not instructions:
        prompt_path = Path(os.getenv("VOICE_AGENT_PROMPT_FILE", "prompt.md"))
        instructions = read_instructions(prompt_path)

    instructions = _append_rss_catalog_section(instructions)
    instructions = _append_radio_catalog_section(instructions)

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
    return "Achernar"


_RSS_CATALOG_HEADER = "### Каталог RSS із rss_feeds.json"


def _append_rss_catalog_section(instructions: str) -> str:
    """
    Ensure the base prompt always contains the current RSS catalog so the LLM
    never invents feed URLs or categories.
    """

    catalog_text = rss.describe_feed_catalog().strip()
    if not catalog_text:
        return instructions

    if _RSS_CATALOG_HEADER in instructions:
        return instructions

    advisory = (
        "Коли розповідаєш новини, спочатку перелічи категорії нижче і "
        "використовуй ТІЛЬКИ наведені ID або URL. Якщо потрібної категорії "
        "нема, повідом про це й запропонуй вибрати з каталогу. Коли користувач "
        "просить змінити стрічку, вибери відповідну категорію саме за її title "
        "та description і підстав її URL або ID у виклик fetch_rss_news."
    )
    base_text = instructions.rstrip()
    section_lines = [
        base_text,
        "",
        _RSS_CATALOG_HEADER,
        advisory,
        catalog_text,
    ] if base_text else [
        _RSS_CATALOG_HEADER,
        advisory,
        catalog_text,
    ]
    result = "\n".join(section_lines)
    article_advisory = (
        "\n\n### Читання повних новинних статей\n"
        "Коли користувач просить прочитати конкретну новину повністю, спочатку візьми link із RSS, "
        "а потім викликай read_full_article(url=link, max_chars=100000). Не обмежуйся summary з RSS, "
        "особливо для 24 Каналу, бо його RSS часто містить тільки короткий анонс. Для Української правди RSS "
        "може містити content:encoded, але якщо користувач просить повністю або детально — усе одно відкрий link через read_full_article."
    )
    return result + article_advisory


_RADIO_CATALOG_HEADER = "### Каталог радіо та аудіо RSS"


def _append_radio_catalog_section(instructions: str) -> str:
    catalog_text = radio.describe_radio_catalog().strip()
    if not catalog_text:
        return instructions
    if _RADIO_CATALOG_HEADER in instructions:
        return instructions

    advisory = (
        "Коли користувач просить послухати радіо, передачу, випуск, музику або аудіо з RSS, "
        "не переказуй епізод замість програвання. Спочатку використай list_radio_feeds, "
        "get_radio_episodes або search_radio_episodes, потім play_radio_episode або play_audio_url. "
        "Поки аудіо грає, не говори поверх нього. Якщо користувач просить голосніше, тихіше або змінити гучність, викликай make_radio_louder, make_radio_quieter, change_radio_volume або set_radio_volume. "
        "Якщо користувач каже 'пауза', 'постав на паузу', 'призупини' — викликай pause_radio_playback, не stop_radio_playback. "
        "Якщо користувач каже 'продовж', 'віднови', 'грай далі' — викликай resume_radio_playback. "
        "Якщо користувач просить прискорити/уповільнити або поставити швидкість 1.25x/1.5x/2x — викликай set_radio_playback_speed. Якщо просить нормальну швидкість — reset_radio_playback_speed. Якщо просить перемотати вперед/назад на секунди або хвилини — викликай seek_radio_relative з кількістю секунд, для назад використовуй відʼємне число. Якщо просить перейти на хвилину/секунду від початку — seek_radio_to_seconds. Якщо просить перейти на відсоток запису — seek_radio_to_percent. 
        "Якщо користувач каже 'вимкни', 'зупини повністю', 'закрий', 'стоп повністю' — викликай stop_radio_playback. Якщо у каталозі для потрібної стрічки ще немає URL, "
        "скажи коротко, що треба додати RSS URL у voice_agent/data/radio_feeds.json або передати прямий RSS URL."
    )
    base_text = instructions.rstrip()
    section_lines = [
        base_text,
        "",
        _RADIO_CATALOG_HEADER,
        advisory,
        catalog_text,
    ] if base_text else [
        _RADIO_CATALOG_HEADER,
        advisory,
        catalog_text,
    ]
    return "\n".join(section_lines)
