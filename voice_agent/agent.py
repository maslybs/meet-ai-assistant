import asyncio
from typing import Any, Optional

from .tools import browser, rss, search, time_tools, video

try:
    from livekit.agents import Agent as _AgentBase, RunContext as _RunContext
    from livekit.agents.llm import function_tool as _function_tool
except ImportError as exc:  # pragma: no cover - local dev without LiveKit
    _AgentBase = None  # type: ignore[assignment]
    _RunContext = None  # type: ignore[assignment]
    _function_tool = None  # type: ignore[assignment]
    LIVEKIT_IMPORT_ERROR: Optional[ImportError] = exc
else:
    LIVEKIT_IMPORT_ERROR = None


class _AgentStub:
    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError(
            "LiveKit agents are not available; install the dependencies referenced in requirements.txt."
        )


AgentBase = _AgentBase if _AgentBase is not None else _AgentStub
RunContext = _RunContext if _RunContext is not None else Any  # type: ignore


def function_tool(func):  # type: ignore[misc]
    if _function_tool is None:  # pragma: no cover - fallback
        return func
    return _function_tool(func)


class GeminiVisionAgent(AgentBase):
    """Agent that exposes a small set of reusable function tools."""

    def __init__(self, *, instructions: str) -> None:
        super().__init__(instructions=instructions)
        self._video_toggle_lock = asyncio.Lock()

    # Video tools commented out to prevent hallucinations about controlling user hardware
    # @function_tool
    # async def enable_video_feed(self, _: RunContext) -> str:
    #     async with self._video_toggle_lock:
    #         return await video.enable_video_feed(self)

    # @function_tool
    # async def disable_video_feed(self, _: RunContext) -> str:
    #     async with self._video_toggle_lock:
    #         return await video.disable_video_feed(self)

    @function_tool
    async def current_time_utc_plus3(self, _: RunContext) -> str:
        return await time_tools.current_time_utc_plus3(None)

    @function_tool
    async def browse_web_page(
        self,
        _: RunContext,
        url: str,
        wait: Any = "",
        max_chars: int | str = 0,
    ) -> str:
        return await browser.browse_web_page(None, url, wait=wait, max_chars=max_chars)

    @function_tool
    async def fetch_rss_news(
        self, _: RunContext, feed_url: str = "", limit: int | str = 3
    ) -> str:
        return await rss.fetch_rss_news(None, feed_url=feed_url, limit=limit)
    fetch_rss_news.__doc__ = rss.describe_feed_catalog()

    @function_tool
    async def google_search_api(self, _: RunContext, query: str, limit: int | str = 5) -> str:
        return await search.google_search_api(None, query=query, limit=limit)


__all__ = [
    "GeminiVisionAgent",
    "AgentBase",
    "RunContext",
    "function_tool",
    "LIVEKIT_IMPORT_ERROR",
]
