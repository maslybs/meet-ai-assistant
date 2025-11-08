import asyncio
import functools
import os
import re
from typing import Any

from html import unescape
from urllib import error as urllib_error
from urllib import request as urllib_request


async def fetch_rss_news(_: Any, feed_url: str = "", limit: int | str = 3) -> str:
    """Fetch and summarise entries from an RSS feed."""

    try:
        import feedparser  # type: ignore
    except ImportError:
        return "Модуль для читання RSS наразі не встановлений."

    feed_url_value = feed_url.strip() if isinstance(feed_url, str) else ""
    env_feed_default = os.getenv("VOICE_AGENT_RSS_FEED", "").strip()
    allow_override_raw = os.getenv("VOICE_AGENT_RSS_ALLOW_OVERRIDE", "").strip().lower()
    allow_override = allow_override_raw not in {"", "0", "false", "no"}
    if env_feed_default:
        if not allow_override:
            feed_url_value = env_feed_default
        elif not feed_url_value:
            feed_url_value = env_feed_default
    if not feed_url_value:
        return "Будь ласка, надайте повний URL RSS-стрічки або встановіть VOICE_AGENT_RSS_FEED."

    env_limit_raw = os.getenv("VOICE_AGENT_RSS_LIMIT", "").strip()

    def _resolve_limit(raw: int | str | None) -> int:
        candidate: int | str | None = raw
        if not allow_override or (candidate is None or candidate == ""):
            candidate = env_limit_raw or candidate
        if candidate in ("", None):
            candidate = 3
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
        req = urllib_request.Request(feed_url_value, headers=headers)
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
    for item in entries[:limit_value]:
        title = (item.get("title") or "Без заголовка").strip()
        published = item.get("published") or item.get("updated") or ""
        link = item.get("link") or ""
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
        if link:
            header_parts.append(f"— {link}")
        entry_lines.append(" ".join(header_parts).strip())
        if summary:
            cleaned = re.sub(r"<[^>]+>", " ", summary)
            cleaned = unescape(cleaned)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if cleaned:
                entry_lines.append(cleaned)
        entries_output.append("\n".join(entry_lines))

    return "\n\n".join(entries_output)
