import asyncio
import json
from typing import Any, Optional

from .tools import browser, radio, rss, search, time_tools, video

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

    def __init__(self, *, instructions: str, radio_playback: Any = None) -> None:
        super().__init__(instructions=instructions)
        self._video_toggle_lock = asyncio.Lock()
        self._radio_playback = radio_playback

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
    async def read_full_article(
        self,
        _: RunContext,
        url: str,
        max_chars: int | str = 100000,
    ) -> str:
        """Open a news article URL and extract the full readable article text without menus, footer, ads, or cookie banners."""
        return await browser.browse_web_page(None, url=url, wait="domcontentloaded", max_chars=max_chars)

    @function_tool
    async def fetch_rss_news(
        self, _: RunContext, feed_url: str = "", limit: int | str = 3
    ) -> str:
        return await rss.fetch_rss_news(None, feed_url=feed_url, limit=limit)
    fetch_rss_news.__doc__ = rss.describe_feed_catalog()

    @function_tool
    async def list_radio_feeds(self, _: RunContext) -> str:
        """List configured radio/podcast RSS feeds that can be searched or played."""
        return await radio.list_radio_feeds(None)

    @function_tool
    async def get_radio_episodes(
        self,
        _: RunContext,
        feed: str,
        limit: int | str = 5,
    ) -> str:
        """Read recent episodes from a configured radio RSS feed or a direct RSS URL."""
        return await radio.get_radio_episodes(None, feed=feed, limit=limit)

    @function_tool
    async def search_radio_episodes(
        self,
        _: RunContext,
        query: str,
        feed: str = "",
        limit: int | str = 5,
    ) -> str:
        """Search radio RSS episodes by title, description, program, channel, or topic."""
        return await radio.search_radio_episodes(None, query=query, feed=feed, limit=limit)

    @function_tool
    async def search_ukr_radio_programs(
        self,
        _: RunContext,
        query: str,
        limit: int | str = 10,
    ) -> str:
        """Search ukr-radio.bgdn.dev builder catalog for programs/channels by keyword, title, channel, or ID."""
        return await radio.search_ukr_radio_programs(None, query=query, limit=limit)

    @function_tool
    async def build_ukr_radio_rss(
        self,
        _: RunContext,
        query: str = "",
        program_ids: str = "",
        channel_ids: str = "",
    ) -> str:
        """Build a custom Українське Радіо RSS URL using builder-style query/program_ids/channel_ids."""
        return await radio.build_ukr_radio_rss(None, query=query, program_ids=program_ids, channel_ids=channel_ids)

    @function_tool
    async def search_ukr_radio_audio(
        self,
        _: RunContext,
        query: str,
        limit: int | str = 10,
    ) -> str:
        """Search all Українське Радіо audio episodes through builder keyword RSS and return playable episodes."""
        return await radio.search_ukr_radio_audio(None, query=query, limit=limit)

    @function_tool
    async def play_ukr_radio_search(
        self,
        _: RunContext,
        query: str,
        episode_query: str = "latest",
    ) -> str:
        """Search Українське Радіо by keyword through builder RSS and immediately play the best/latest matching episode."""
        resolved = await radio.resolve_ukr_radio_search_episode(None, query=query, episode_query=episode_query)
        try:
            payload = json.loads(resolved)
        except Exception:
            return resolved

        audio_url = payload.get("audio_url") if isinstance(payload, dict) else ""
        if not audio_url:
            return resolved
        if self._radio_playback is None:
            return json.dumps(
                {
                    "action": "radio.play",
                    "title": payload.get("title", ""),
                    "audio_url": audio_url,
                    "page_url": payload.get("page_url", ""),
                },
                ensure_ascii=False,
            )
        return await self._radio_playback.play(
            audio_url=audio_url,
            title=payload.get("title", ""),
            page_url=payload.get("page_url", ""),
            metadata=payload,
        )

    @function_tool
    async def play_radio_episode(
        self,
        _: RunContext,
        feed: str,
        query: str = "latest",
    ) -> str:
        """Find an episode in RSS and immediately play its audio in the room."""
        resolved = await radio.resolve_radio_episode(None, feed=feed, query=query)
        try:
            payload = json.loads(resolved)
        except Exception:
            return resolved

        audio_url = payload.get("audio_url") if isinstance(payload, dict) else ""
        if not audio_url:
            return resolved
        if self._radio_playback is None:
            return json.dumps(
                {
                    "action": "radio.play",
                    "title": payload.get("title", ""),
                    "audio_url": audio_url,
                    "page_url": payload.get("page_url", ""),
                    "message": "Playback controller is not attached; client should play audio_url.",
                },
                ensure_ascii=False,
            )
        return await self._radio_playback.play(
            audio_url=audio_url,
            title=payload.get("title", ""),
            page_url=payload.get("page_url", ""),
            metadata=payload,
        )

    @function_tool
    async def play_audio_url(
        self,
        _: RunContext,
        audio_url: str,
        title: str = "Аудіо",
        page_url: str = "",
    ) -> str:
        """Immediately play a direct audio URL, for example an RSS enclosure URL."""
        if self._radio_playback is None:
            return json.dumps(
                {
                    "action": "radio.play",
                    "title": title,
                    "audio_url": audio_url,
                    "page_url": page_url,
                    "message": "Playback controller is not attached; client should play audio_url.",
                },
                ensure_ascii=False,
            )
        return await self._radio_playback.play(
            audio_url=audio_url,
            title=title,
            page_url=page_url,
            metadata={"source": "direct"},
        )

    @function_tool
    async def set_radio_volume(self, _: RunContext, volume: str) -> str:
        """Set radio/music playback volume. Accepts values like 50%, 100%, 150%, or 1.5."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return await self._radio_playback.set_volume(volume)

    @function_tool
    async def change_radio_volume(self, _: RunContext, delta: str) -> str:
        """Change radio/music playback volume relatively. Use +20% for louder or -20% for quieter."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return await self._radio_playback.change_volume(delta)

    @function_tool
    async def make_radio_louder(self, _: RunContext) -> str:
        """Make currently playing radio/music louder by the default step."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return await self._radio_playback.multiply_volume("1.5")

    @function_tool
    async def make_radio_quieter(self, _: RunContext) -> str:
        """Make currently playing radio/music quieter by the default step."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return await self._radio_playback.multiply_volume("0.5")

    @function_tool
    async def set_radio_playback_speed(self, _: RunContext, speed: str) -> str:
        """Set browser radio/audio playback speed. Accepts 0.5, 1, 1.25, 1.5, 2, 3."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return await self._radio_playback.set_playback_rate(speed)

    @function_tool
    async def reset_radio_playback_speed(self, _: RunContext) -> str:
        """Reset browser radio/audio playback speed to normal 1x."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return await self._radio_playback.set_playback_rate("1")

    @function_tool
    async def seek_radio_relative(self, _: RunContext, seconds: str) -> str:
        """Seek current browser radio/audio by relative seconds. Positive means forward, negative means backward."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return await self._radio_playback.seek_relative(seconds)

    @function_tool
    async def seek_radio_to_seconds(self, _: RunContext, seconds: str) -> str:
        """Seek current browser radio/audio to an absolute timestamp in seconds from the beginning."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return await self._radio_playback.seek_to_seconds(seconds)

    @function_tool
    async def seek_radio_to_percent(self, _: RunContext, percent: str) -> str:
        """Seek current browser radio/audio to a percentage of total duration, e.g. 50%."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return await self._radio_playback.seek_to_percent(percent)

    @function_tool
    async def pause_radio_playback(self, _: RunContext) -> str:
        """Pause currently playing radio, music, or podcast audio without clearing the current audio source."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return await self._radio_playback.pause()

    @function_tool
    async def resume_radio_playback(self, _: RunContext) -> str:
        """Resume paused radio, music, or podcast audio from the same position."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return await self._radio_playback.resume()

    @function_tool
    async def stop_radio_playback(self, _: RunContext) -> str:
        """Stop currently playing radio, music, or podcast audio."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return await self._radio_playback.stop()

    @function_tool
    async def what_is_playing_now(self, _: RunContext) -> str:
        """Tell what radio/music episode is currently playing now, including title, feed/program, publication time, and volume."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return self._radio_playback.status_json()

    @function_tool
    async def radio_playback_status(self, _: RunContext) -> str:
        """Return current radio/music playback state."""
        if self._radio_playback is None:
            return "Playback controller is not attached."
        return self._radio_playback.status_json()

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
