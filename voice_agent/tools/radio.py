import asyncio
import functools
import json
import os
import re
from html import unescape
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote, urlencode

_DEFAULT_RADIO_CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "radio_feeds.json"
_RADIO_CATALOG_ENV = "VOICE_AGENT_RADIO_CATALOG_FILE"
_UKR_RADIO_BASE_URL = os.getenv("VOICE_AGENT_UKR_RADIO_BASE_URL", "https://ukr-radio.bgdn.dev").rstrip("/")


def _load_catalog() -> list[dict[str, Any]]:
    path = Path(os.getenv(_RADIO_CATALOG_ENV, str(_DEFAULT_RADIO_CATALOG_PATH))).expanduser()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not isinstance(raw, list):
        return []

    catalog: list[dict[str, Any]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        feed_url = str(item.get("url") or "").strip()
        page_url = str(item.get("page_url") or "").strip()
        if not feed_url and not page_url:
            continue

        aliases = item.get("aliases") or []
        if not isinstance(aliases, list):
            aliases = []

        catalog.append(
            {
                "id": str(item.get("id") or f"radio_{idx}").strip(),
                "title": str(item.get("title") or "Radio feed").strip(),
                "description": str(item.get("description") or "").strip(),
                "url": feed_url,
                "page_url": page_url,
                "aliases": [str(alias).strip() for alias in aliases if str(alias).strip()],
                "kind": str(item.get("kind") or "program").strip(),
                "source": str(item.get("source") or "ukr-radio.bgdn.dev").strip(),
            }
        )
    return catalog


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _looks_like_url(value: str) -> bool:
    normalized = value.strip().casefold()
    return normalized.startswith("http://") or normalized.startswith("https://")


def _match_feed(feed: str, catalog: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidate = _norm(feed)
    if not candidate:
        return None

    if _looks_like_url(feed):
        return {
            "id": feed,
            "title": "Direct RSS feed",
            "description": "Direct RSS URL provided by the user.",
            "url": feed.strip(),
            "page_url": "",
            "aliases": [],
            "kind": "direct",
            "source": "direct",
        }

    for item in catalog:
        exact_values = [item.get("id", ""), item.get("title", ""), *(item.get("aliases") or [])]
        if any(_norm(value) == candidate for value in exact_values if value):
            return item

    for item in catalog:
        fuzzy_values = [item.get("title", ""), item.get("description", ""), *(item.get("aliases") or [])]
        if any(candidate and candidate in _norm(value) for value in fuzzy_values if value):
            return item
    return None


def _clean_html(value: Any, *, max_chars: int = 600) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if max_chars and len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _extract_audio_url(item: dict[str, Any]) -> str:
    audio_ext = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac")

    def _is_audio(href: Any, media_type: Any = "", rel: Any = "") -> bool:
        href_text = str(href or "").strip()
        media_text = str(media_type or "").casefold()
        rel_text = str(rel or "").casefold()
        if not href_text:
            return False
        return (
            media_text.startswith("audio/")
            or rel_text == "enclosure"
            or href_text.casefold().split("?", 1)[0].endswith(audio_ext)
        )

    for enclosure in item.get("enclosures") or []:
        href = enclosure.get("href") or enclosure.get("url")
        if _is_audio(href, enclosure.get("type")):
            return str(href).strip()

    for link in item.get("links") or []:
        href = link.get("href")
        if _is_audio(href, link.get("type"), link.get("rel")):
            return str(href).strip()

    for media in item.get("media_content") or []:
        href = media.get("url")
        if _is_audio(href, media.get("type")):
            return str(href).strip()

    return ""


async def _fetch_feed(feed_url: str) -> Any:
    import feedparser

    loop = asyncio.get_running_loop()

    def _download() -> bytes:
        req = urllib_request.Request(
            feed_url,
            headers={
                "User-Agent": os.getenv("VOICE_AGENT_RADIO_USER_AGENT", "VoiceAgentRadio/1.0"),
                "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
            },
        )
        with urllib_request.urlopen(req, timeout=float(os.getenv("VOICE_AGENT_RADIO_FETCH_TIMEOUT", "20"))) as response:
            return response.read()

    data = await loop.run_in_executor(None, _download)
    return await loop.run_in_executor(None, functools.partial(feedparser.parse, data))


def _entry_to_episode(item: dict[str, Any], *, feed_id: str = "", feed_title: str = "") -> dict[str, Any]:
    summary = (
        item.get("summary")
        or item.get("description")
        or (item.get("summary_detail", {}).get("value") if isinstance(item.get("summary_detail"), dict) else "")
        or ""
    )
    return {
        "title": str(item.get("title") or "Без назви").strip(),
        "published": item.get("published") or item.get("updated") or "",
        "description": _clean_html(summary),
        "page_url": item.get("link") or "",
        "audio_url": _extract_audio_url(item),
        "feed_id": feed_id,
        "feed_title": feed_title,
    }


def _episodes_as_json(episodes: list[dict[str, Any]]) -> str:
    return json.dumps(episodes, ensure_ascii=False)


def describe_radio_catalog() -> str:
    catalog = _load_catalog()
    if not catalog:
        return "Каталог радіо RSS порожній. Можна передати прямий RSS URL у параметр feed."

    lines = ["Доступні радіо/RSS стрічки для аудіо:"]
    for item in catalog:
        aliases = ", ".join(item.get("aliases") or [])
        suffix = f"; aliases: {aliases}" if aliases else ""
        url_hint = item.get("url") or item.get("page_url") or ""
        lines.append(
            f"- {item['id']}: {item['title']} ({item['kind']}) — {item['description']}{suffix}; source: {url_hint}"
        )
    return "\n".join(lines)


async def list_radio_feeds(_: Any = None) -> str:
    catalog = _load_catalog()
    if not catalog:
        return "Каталог радіо RSS порожній. Додай стрічки у voice_agent/data/radio_feeds.json або передай прямий RSS URL."

    return json.dumps(
        [
            {
                "id": item["id"],
                "title": item["title"],
                "description": item["description"],
                "aliases": item["aliases"],
                "kind": item["kind"],
                "source": item["source"],
                "url_configured": bool(item.get("url")),
                "page_url": item.get("page_url", ""),
            }
            for item in catalog
        ],
        ensure_ascii=False,
    )


async def get_radio_episodes(_: Any = None, feed: str = "", limit: int | str = 5) -> str:
    catalog = _load_catalog()
    entry = _match_feed(feed, catalog)
    if not entry:
        return f"Не знайшла радіо-стрічку '{feed}'. Використай list_radio_feeds або передай прямий RSS URL."

    feed_url = str(entry.get("url") or "").strip()
    if not feed_url:
        return (
            f"Для '{entry.get('title')}' ще не прописаний RSS URL. "
            f"Відкрий {entry.get('page_url') or 'сторінку каталогу'} і скопіюй RSS у voice_agent/data/radio_feeds.json."
        )

    try:
        limit_value = max(1, min(int(limit), 20))
    except Exception:
        limit_value = 5

    try:
        parsed = await _fetch_feed(feed_url)
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError, OSError) as exc:
        return f"Не вдалося завантажити RSS '{feed_url}': {exc}"

    parsed_feed = getattr(parsed, "feed", {}) or {}
    feed_title = parsed_feed.get("title") or entry.get("title") or feed
    episodes = [
        _entry_to_episode(item, feed_id=entry.get("id", ""), feed_title=feed_title)
        for item in (getattr(parsed, "entries", []) or [])[:limit_value]
    ]

    return _episodes_as_json(episodes)


async def search_radio_episodes(
    _: Any = None,
    query: str = "",
    feed: str = "",
    limit: int | str = 5,
) -> str:
    catalog = _load_catalog()
    feeds = catalog

    if feed:
        matched = _match_feed(feed, catalog)
        if not matched:
            return f"Не знайшла радіо-стрічку '{feed}'."
        feeds = [matched]

    try:
        limit_value = max(1, min(int(limit), 20))
    except Exception:
        limit_value = 5

    q = _norm(query)
    if not q:
        return "Потрібен query для пошуку передачі або епізоду."

    results: list[dict[str, Any]] = []
    for feed_entry in feeds[: int(os.getenv("VOICE_AGENT_RADIO_SEARCH_MAX_FEEDS", "20"))]:
        feed_url = str(feed_entry.get("url") or "").strip()
        if not feed_url:
            continue
        try:
            parsed = await _fetch_feed(feed_url)
        except Exception:
            continue

        parsed_feed = getattr(parsed, "feed", {}) or {}
        feed_title = parsed_feed.get("title") or feed_entry.get("title") or ""
        for item in getattr(parsed, "entries", []) or []:
            episode = _entry_to_episode(item, feed_id=feed_entry.get("id", ""), feed_title=feed_title)
            haystack = _norm(" ".join([episode["title"], episode["description"], episode["feed_title"]]))
            if q in haystack:
                results.append(episode)
                if len(results) >= limit_value:
                    return _episodes_as_json(results)

    return _episodes_as_json(results)


async def resolve_radio_episode(_: Any = None, feed: str = "", query: str = "latest") -> str:
    raw = await get_radio_episodes(None, feed=feed, limit=20)
    try:
        episodes = json.loads(raw)
    except Exception:
        return raw

    if not isinstance(episodes, list) or not episodes:
        return "Не знайшла епізодів у цій стрічці."

    selected = None
    normalized_query = _norm(query)
    if not normalized_query or normalized_query in {"latest", "останній", "свіжий", "новий"}:
        selected = episodes[0]
    else:
        for episode in episodes:
            haystack = _norm(" ".join([episode.get("title", ""), episode.get("description", "")]))
            if normalized_query in haystack:
                selected = episode
                break

    if not selected:
        return f"Не знайшла епізод за запитом '{query}' у стрічці '{feed}'."

    if not selected.get("audio_url"):
        return f"Знайшла епізод '{selected.get('title')}', але в RSS немає audio_url/enclosure."

    return json.dumps(
        {
            "action": "radio.resolve",
            "title": selected.get("title", ""),
            "audio_url": selected.get("audio_url", ""),
            "page_url": selected.get("page_url", ""),
            "published": selected.get("published", ""),
            "feed_id": selected.get("feed_id", ""),
            "feed_title": selected.get("feed_title", ""),
        },
        ensure_ascii=False,
    )


def build_ukr_radio_search_url(query: str = "", *, channel: str = "") -> str:
    params: dict[str, str] = {}
    if query.strip():
        params["q"] = query.strip()
    if channel.strip():
        params["channel"] = channel.strip()
    suffix = f"?{urlencode(params, quote_via=quote)}" if params else ""
    return f"{_UKR_RADIO_BASE_URL}/programs/{suffix}"


__all__ = [
    "describe_radio_catalog",
    "list_radio_feeds",
    "get_radio_episodes",
    "search_radio_episodes",
    "resolve_radio_episode",
    "build_ukr_radio_search_url",
]
