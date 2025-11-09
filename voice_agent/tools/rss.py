import asyncio
import functools
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable, List

from html import unescape
from urllib import error as urllib_error
from urllib import request as urllib_request


_DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "rss_feeds.json"
_CATALOG_ENV_VAR = "VOICE_AGENT_RSS_CATALOG_FILE"
_RSS_LOGGER = logging.getLogger("voice-agent.rss")

_FEED_CACHE: List[dict[str, Any]] | None = None
_FEED_CACHE_PATH: Path | None = None


def _read_catalog_file(path: Path) -> list[dict[str, Any]]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _RSS_LOGGER.warning("RSS catalog file '%s' не знайдено. Використовую стандартну стрічку.", path)
        return []
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        _RSS_LOGGER.warning("Невалідний JSON у каталозі RSS '%s': %s", path, exc)
        return []
    if not isinstance(data, list):
        _RSS_LOGGER.warning("Каталог RSS має бути списком об'єктів. Файл '%s' проігноровано.", path)
        return []
    entries: list[dict[str, Any]] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            _RSS_LOGGER.debug("Пропускаю запис #%s у каталозі RSS: не словник.", idx)
            continue
        url = str(item.get("url", "")).strip()
        if not url:
            _RSS_LOGGER.debug("Пропускаю запис #%s у каталозі RSS: немає 'url'.", idx)
            continue
        entry = {
            "id": str(item.get("id") or item.get("key") or f"feed_{idx}").strip(),
            "title": str(item.get("title") or item.get("name") or "RSS стрічка").strip(),
            "description": str(item.get("description") or "").strip(),
            "url": url,
            "aliases": [alias.strip() for alias in item.get("aliases", []) if isinstance(alias, str)],
        }
        entries.append(entry)
    return entries


def _load_feed_catalog() -> list[dict[str, Any]]:
    global _FEED_CACHE, _FEED_CACHE_PATH

    catalog_override = os.getenv(_CATALOG_ENV_VAR, "").strip()
    path = Path(catalog_override).expanduser() if catalog_override else _DEFAULT_CATALOG_PATH

    if _FEED_CACHE is not None and _FEED_CACHE_PATH == path:
        return _FEED_CACHE

    catalog = _read_catalog_file(path)
    if not catalog:
        catalog = [
            {
                "id": "headlines",
                "title": "Головні новини",
                "description": "Усі оперативні матеріали 24 Каналу.",
                "url": "https://24tv.ua/rss/all.xml",
                "aliases": ["новини"],
            }
        ]

    _FEED_CACHE = catalog
    _FEED_CACHE_PATH = path
    return catalog


def _normalize_token(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.strip())
    return cleaned.casefold()


def _match_catalog_entry(candidate: str, catalog: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    normalized = _normalize_token(candidate)
    for entry in catalog:
        identifiers = [entry.get("id", ""), entry.get("title", "")]
        identifiers.extend(entry.get("aliases", []) or [])
        for identifier in identifiers:
            if not identifier:
                continue
            if _normalize_token(identifier) == normalized:
                return entry
    return None


def describe_feed_catalog() -> str:
    catalog = _load_feed_catalog()
    if not catalog:
        return (
            "Озвучує останні новини з RSS. Обов'язково передай аргумент feed_url як "
            "повний URL (https://...) або ідентифікатор із каталогу."
        )

    lines = [
        "Зачитує останні публікації з підготовленого каталогу RSS. "
        "Обов'язково вкажи аргумент feed_url як повний URL або ідентифікатор з переліку:",
    ]
    for entry in catalog:
        description = f" — {entry['description']}" if entry.get("description") else ""
        lines.append(f"- {entry['title']} (`{entry.get('id', '')}`) → {entry['url']}{description}")
    lines.append(
        f"Редагуй каталог у файлі {os.getenv(_CATALOG_ENV_VAR, str(_DEFAULT_CATALOG_PATH))}."
    )
    return "\n".join(lines)


async def fetch_rss_news(_: Any, feed_url: str = "", limit: int | str = 3) -> str:
    """Fetch and summarise entries from an RSS feed defined in the catalog."""

    try:
        import feedparser  # type: ignore
    except ImportError:
        return "Модуль для читання RSS наразі не встановлений."

    catalog = _load_feed_catalog()
    feed_arg = feed_url.strip() if isinstance(feed_url, str) else ""
    target_url = ""

    if feed_arg:
        if feed_arg.lower().startswith(("http://", "https://")):
            target_url = feed_arg
        else:
            entry = _match_catalog_entry(feed_arg, catalog)
            if entry:
                target_url = entry["url"]
            else:
                return (
                    f"Не впізнала категорію '{feed_arg}'. Ось доступні варіанти:\n{describe_feed_catalog()}"
                )

    if not target_url:
        return (
            "Не вдалося визначити RSS-стрічку. Назви категорію з каталогу або передай повний RSS-URL.\n"
            f"{describe_feed_catalog()}"
        )

    def _resolve_limit(raw: int | str | None) -> int:
        candidate: int | str | None = raw
        if candidate in ("", None):
            candidate = os.getenv("VOICE_AGENT_RSS_LIMIT", "").strip() or 3
        try:
            value = int(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            value = 3
        return max(1, min(value, 10))

    provided_limit: int | str | None = limit if isinstance(limit, (int, str)) else None
    limit_value = _resolve_limit(provided_limit)

    loop = asyncio.get_running_loop()

    def _download_feed() -> bytes:
        headers = {
            "User-Agent": os.getenv(
                "VOICE_AGENT_RSS_USER_AGENT",
                "VoiceAgentRSS/1.0 (+https://livekit.io)",
            ),
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        }
        req = urllib_request.Request(target_url, headers=headers)
        with urllib_request.urlopen(req, timeout=15) as response:
            return response.read()

    try:
        feed_bytes = await loop.run_in_executor(None, _download_feed)
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError) as exc:
        return f"Не вдалося завантажити RSS ({exc})."

    parsed = await loop.run_in_executor(
        None,
        functools.partial(feedparser.parse, feed_bytes),
    )
    if getattr(parsed, "bozo", False):
        error = getattr(parsed, "bozo_exception", None)
        return f"Не вдалося розібрати RSS: {error!r}" if error else "Не вдалося розібрати RSS."

    entries = getattr(parsed, "entries", []) or []
    if not entries:
        status = getattr(parsed, "status", None)
        if status and status != 200:
            return f"Стрічка повернула статус {status}, записи відсутні."
        return "У стрічці немає публікацій."

    entries_output: list[str] = []

    def _clean_text(value: str) -> str:
        stripped = re.sub(r"\s+", " ", value).strip()
        return stripped

    for item in entries[:limit_value]:
        title = (item.get("title") or "Без заголовка").strip()
        published = item.get("published") or item.get("updated") or ""
        link = item.get("link") or ""
        guid = item.get("id") or item.get("guid") or ""

        media_entries = item.get("media_content") or []
        if not isinstance(media_entries, list):
            media_entries = []

        summary = ""
        summary_candidates = [
            item.get("summary"),
            item.get("summary_detail", {}).get("value") if isinstance(item.get("summary_detail"), dict) else None,
            item.get("content", [{}])[0].get("value") if item.get("content") else None,
            item.get("description"),
        ]
        for candidate in summary_candidates:
            if isinstance(candidate, str) and candidate.strip():
                summary = candidate.strip()
                break
        entry_lines: list[str] = []
        header_parts: list[str] = [title]
        if published:
            header_parts.append(f"({published})")
        entry_lines.append(" ".join(header_parts).strip())
        if link:
            entry_lines.append(f"Посилання: {link}")
        if guid and guid != link:
            entry_lines.append(f"GUID: {guid}")
        if summary:
            cleaned = re.sub(r"<[^>]+>", " ", summary)
            cleaned = unescape(cleaned)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if cleaned:
                entry_lines.append(f"Коротко: {cleaned}")

        content_html = ""
        content_candidates_html: list[str] = []
        if item.get("content"):
            for content_block in item["content"]:
                if isinstance(content_block, dict):
                    candidate_html = content_block.get("value") or ""
                    if isinstance(candidate_html, str) and candidate_html.strip():
                        content_candidates_html.append(candidate_html)
        encoded = item.get("content_encoded") or item.get("content:encoded")
        if isinstance(encoded, str) and encoded.strip():
            content_candidates_html.append(encoded)
        for candidate in content_candidates_html:
            if candidate.strip():
                content_html = candidate.strip()
                break

        if content_html:
            entry_lines.append("Повний контент (HTML із content:encoded):")
            entry_lines.append(content_html)

        if media_entries:
            media_lines: list[str] = []
            for media in media_entries:
                if not isinstance(media, dict):
                    continue
                media_url = media.get("url") or ""
                if not media_url:
                    continue
                width = media.get("width")
                height = media.get("height")
                media_type = media.get("type") or ""
                size_info = []
                if width:
                    size_info.append(f"w={width}")
                if height:
                    size_info.append(f"h={height}")
                if media_type:
                    size_info.append(media_type)
                suffix = f" ({', '.join(size_info)})" if size_info else ""
                desc = ""
                media_desc = media.get("description") or ""
                if isinstance(media_desc, str) and media_desc.strip():
                    desc = f" — {_clean_text(media_desc)}"
                media_lines.append(f"- {media_url}{suffix}{desc}")
            if media_lines:
                entry_lines.append("Медіа з <media:content>:")
                entry_lines.extend(media_lines)

        entries_output.append("\n".join(entry_lines))

    return "\n\n".join(entries_output)
