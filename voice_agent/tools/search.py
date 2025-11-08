import asyncio
import json
import os
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


async def google_search_api(_: Any, query: str, limit: int | str = 5) -> str:
    """Call the Google Custom Search JSON API and summarise the top results."""

    api_key = os.getenv("GOOGLE_SEARCH_API_KEY", "").strip()
    engine_id = os.getenv("GOOGLE_SEARCH_ENGINE_ID", "").strip()

    if not api_key:
        return "GOOGLE_SEARCH_API_KEY не заданий. Додайте його у середовище."
    if not engine_id:
        return "GOOGLE_SEARCH_ENGINE_ID не заданий. Укажіть ID пошукової системи Google Programmable Search."

    query_value = (query or "").strip()
    if not query_value:
        return "Потрібен пошуковий запит."

    try:
        limit_value = max(1, min(int(limit), 10))
    except (TypeError, ValueError):
        limit_value = 5

    params = {
        "key": api_key,
        "cx": engine_id,
        "q": query_value,
        "num": limit_value,
        "safe": os.getenv("GOOGLE_SEARCH_SAFE", "off"),
        "hl": os.getenv("GOOGLE_SEARCH_LANG", "uk"),
    }
    params.update(
        _extract_optional_param("GOOGLE_SEARCH_SITE_RESTRICT", "siteSearch"),
    )
    params.update(
        _extract_optional_param("GOOGLE_SEARCH_DATE_RESTRICT", "dateRestrict"),
    )

    encoded = urllib_parse.urlencode({k: v for k, v in params.items() if v})
    url = f"https://www.googleapis.com/customsearch/v1?{encoded}"

    loop = asyncio.get_running_loop()

    def _fetch() -> dict[str, Any]:
        req = urllib_request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": os.getenv(
                    "VOICE_AGENT_BROWSER_USER_AGENT",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
                ),
            },
        )
        with urllib_request.urlopen(req, timeout=15) as response:
            return json.load(response)

    try:
        payload = await loop.run_in_executor(None, _fetch)
    except (urllib_error.HTTPError, urllib_error.URLError, TimeoutError) as exc:
        return f"Google Search API недоступний ({exc})."

    error_info = payload.get("error")
    if error_info:
        message = error_info.get("message") or json.dumps(error_info, ensure_ascii=False)
        return f"Google Search API повернув помилку: {message}"

    items = payload.get("items") or []
    if not items:
        return "За цим запитом результатів немає."

    lines: list[str] = []
    for idx, item in enumerate(items, 1):
        title = (item.get("title") or "Без назви").strip()
        snippet = (item.get("snippet") or item.get("htmlSnippet") or "").strip()
        link = item.get("link") or item.get("formattedUrl") or ""

        entry = [f"{idx}. {title}"]
        if link:
            entry.append(link)
        if snippet:
            entry.append(snippet.replace("\n", " ").strip())

        lines.append(" — ".join(part for part in entry if part))

    return "\n".join(lines)


def _extract_optional_param(env_name: str, param: str) -> dict[str, str]:
    value = os.getenv(env_name, "").strip()
    return {param: value} if value else {}
