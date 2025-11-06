import asyncio
import functools
from datetime import datetime, timezone, timedelta
import logging
import os
import re
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse
from html import unescape
from typing import Any, Optional, TYPE_CHECKING

from .browser_pool import BrowserContextConfig, get_browser_pool

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

if TYPE_CHECKING:
    from livekit.agents import AgentSession  # pragma: no cover
else:  # pragma: no cover - runtime fallback
    AgentSession = Any  # type: ignore


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


_VIDEO_LOGGER = logging.getLogger("voice-agent.video")
_BROWSER_LOGGER = logging.getLogger("voice-agent.browser")


class GeminiVisionAgent(AgentBase):
    """
    Custom agent that exposes tools for managing the room video feed.
    Video is consumed automatically when available, while still giving the user
    an explicit way to pause or resume it.
    """

    def __init__(self, *, instructions: str) -> None:
        super().__init__(instructions=instructions)
        self._video_toggle_lock = asyncio.Lock()

    @function_tool
    async def enable_video_feed(self, _: RunContext) -> str:
        """
        Увімкнути передачу відео з камери користувача, якщо її було вимкнено раніше.
        """

        async with self._video_toggle_lock:
            session: Optional[AgentSession] = getattr(self, "session", None)
            if session is None:
                return "Зараз не можу отримати відео, спробуйте пізніше."

            video_stream = session.input.video
            if video_stream is None:
                return "Відео від учасника недоступне. Переконайтеся, що камера увімкнена."

            if session.input.video_enabled:
                return "Відео вже увімкнене."

            session.input.set_video_enabled(True)
            _VIDEO_LOGGER.info("Video feed enabled by request")
            return "Добре, я бачу відео. Дайте знати, що саме потрібно показати."

    @function_tool
    async def disable_video_feed(self, _: RunContext) -> str:
        """
        Вимкнути захоплення відео, щоб зекономити ресурси або надати приватність на вимогу.
        """

        async with self._video_toggle_lock:
            session: Optional[AgentSession] = getattr(self, "session", None)
            if session is None or session.input.video is None:
                return "Зараз відеосигнал недоступний."

            if not session.input.video_enabled:
                return "Відео вже вимкнене."

            session.input.set_video_enabled(False)
            _VIDEO_LOGGER.info("Video feed disabled on request")
            return "Вимкнула відео. Якщо знадобиться знову, просто скажіть."

    @function_tool
    async def current_time_utc_plus3(self, _: RunContext) -> str:
        """
        Повідомити поточний час у часовому поясі UTC+3 (за замовчуванням Київ).
        """

        tz_name = os.getenv("VOICE_AGENT_TIMEZONE", "Europe/Kyiv").strip() or "Europe/Kyiv"
        offset_override = os.getenv("VOICE_AGENT_TIME_OFFSET_HOURS", "").strip()

        tzinfo = None
        try:
            from zoneinfo import ZoneInfo  # Python 3.9+

            tzinfo = ZoneInfo(tz_name)
        except Exception:
            tzinfo = None

        if tzinfo is None:
            try:
                hours = int(offset_override) if offset_override else 3
            except ValueError:
                hours = 3
            tzinfo = timezone(timedelta(hours=hours))

        now = datetime.now(tzinfo)
        offset = now.utcoffset() or timedelta()
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        abs_minutes = abs(total_minutes)
        offset_hours = abs_minutes // 60
        offset_minutes = abs_minutes % 60
        offset_str = f"UTC{sign}{offset_hours:02d}:{offset_minutes:02d}"

        formatted = now.strftime("%d.%m.%Y %H:%M:%S")
        return f"Зараз {formatted} ({offset_str})."

    @function_tool
    async def browse_web_page(
        self,
        _: RunContext,
        url: str,
        wait: str = "",
        max_chars: int | str = 0,
    ) -> str:
        """
        Відкрити вебсторінку у headless-браузері та повернути стислий текст.
        """

        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeout  # type: ignore
        except ImportError:  # pragma: no cover - playwright optional
            return (
                "Headless-браузер недоступний. Встановіть пакет playwright і виконайте "
                "`playwright install chromium`."
            )

        url_value = (url or "").strip()
        if not url_value:
            url_value = os.getenv("VOICE_AGENT_BROWSER_HOME", "").strip()
        if not url_value:
            return "Будь ласка, надайте URL сторінки або встановіть VOICE_AGENT_BROWSER_HOME."

        if not urlparse(url_value).scheme:
            url_value = f"https://{url_value}"

        parsed = urlparse(url_value)
        if not parsed.netloc:
            return "URL виглядає некоректним. Перевірте адресу і спробуйте ще раз."
        final_url = parsed.geturl()

        user_agent = (
            os.getenv(
                "VOICE_AGENT_BROWSER_USER_AGENT",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            ).strip()
            or None
        )
        locale = os.getenv("VOICE_AGENT_BROWSER_LOCALE", "uk-UA").strip() or "uk-UA"
        timeout_ms_raw = os.getenv("VOICE_AGENT_BROWSER_TIMEOUT_MS", "").strip()
        wait_default = os.getenv("VOICE_AGENT_BROWSER_WAIT_UNTIL", "networkidle").strip()
        max_chars_env = os.getenv("VOICE_AGENT_BROWSER_MAX_CHARS", "").strip()

        def _resolve_int(
            raw: int | str | None, fallback: int, minimum: int, maximum: int | None = None
        ) -> int:
            candidate = raw
            if candidate in (None, "", 0):
                candidate = fallback
            try:
                value = int(candidate)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                value = fallback
            value = max(minimum, value)
            if maximum is not None:
                value = min(maximum, value)
            return value

        timeout_ms = _resolve_int(timeout_ms_raw or None, fallback=15000, minimum=1000, maximum=60000)
        max_chars_val = _resolve_int(
            max_chars if isinstance(max_chars, (int, str)) else None,
            fallback=_resolve_int(max_chars_env or None, 2500, 500, 12000),
            minimum=500,
            maximum=12000,
        )
        viewport_width = _resolve_int(
            os.getenv("VOICE_AGENT_BROWSER_VIEWPORT_WIDTH", "").strip() or None,
            fallback=1280,
            minimum=640,
            maximum=2560,
        )
        viewport_height = _resolve_int(
            os.getenv("VOICE_AGENT_BROWSER_VIEWPORT_HEIGHT", "").strip() or None,
            fallback=720,
            minimum=480,
            maximum=1600,
        )

        chromium_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
        ]
        extra_args = os.getenv("VOICE_AGENT_BROWSER_CHROMIUM_ARGS", "").strip()
        if extra_args:
            chromium_args.extend(arg for arg in extra_args.split() if arg)

        def _parse_wait_value(value: str) -> Optional[int]:
            normalized = value.strip().lower()
            if not normalized:
                return None
            try:
                if normalized.endswith("ms"):
                    return max(0, int(float(normalized[:-2])))
                if normalized.endswith("s"):
                    return max(0, int(float(normalized[:-1]) * 1000))
                if normalized.replace(".", "", 1).isdigit():
                    return max(0, int(float(normalized) * 1000))
            except ValueError:
                return None
            return None

        wait_condition = wait_default or "networkidle"
        extra_wait_ms = 0

        extra_wait_env = os.getenv("VOICE_AGENT_BROWSER_EXTRA_WAIT_MS", "").strip()
        extra_wait_ms = 2000  # sensible default delay for dynamic pages
        if extra_wait_env:
            parsed_env_wait = _parse_wait_value(extra_wait_env)
            if parsed_env_wait is not None:
                extra_wait_ms = parsed_env_wait

        wait_value = (wait or "").strip()
        if wait_value:
            parsed_wait = _parse_wait_value(wait_value)
            if parsed_wait is not None:
                extra_wait_ms = parsed_wait
            else:
                wait_condition = wait_value

        idle_timeout_raw = os.getenv("VOICE_AGENT_BROWSER_IDLE_SECONDS", "60").strip()
        try:
            idle_timeout = float(idle_timeout_raw) if idle_timeout_raw else 60.0
        except ValueError:
            idle_timeout = 60.0
        idle_timeout = max(0.0, min(idle_timeout, 3600.0))

        pool = get_browser_pool()
        page = None
        text_result = ""
        error_message = ""
        main_text = ""
        meta_title = ""
        meta_desc = ""

        try:
            page = await pool.acquire_page(
                config=BrowserContextConfig(
                    chromium_args=tuple(chromium_args),
                    user_agent=user_agent,
                    locale=locale,
                    timezone_id=os.getenv("VOICE_AGENT_BROWSER_TIMEZONE", "Europe/Kyiv"),
                    viewport=(viewport_width, viewport_height),
                ),
                launch_timeout_ms=timeout_ms,
                idle_timeout_s=idle_timeout,
            )
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)
            await page.goto(final_url, wait_until=wait_condition or "networkidle", timeout=timeout_ms)
            if extra_wait_ms > 0:
                await page.wait_for_timeout(extra_wait_ms)
            try:
                main_text = await page.evaluate(
                    """() => {
                        const walker = document.createTreeWalker(
                            document.body,
                            NodeFilter.SHOW_TEXT,
                            {
                                acceptNode: (node) => {
                                    if (!node || !node.parentElement) return NodeFilter.FILTER_REJECT;
                                    const parent = node.parentElement;
                                    const tag = parent.tagName || '';
                                    if (['SCRIPT','STYLE','NOSCRIPT','IFRAME'].includes(tag)) {
                                        return NodeFilter.FILTER_REJECT;
                                    }
                                    const text = node.textContent || '';
                                    return text.trim().length ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
                                }
                            },
                            false
                        );
                        const parts = [];
                        while(walker.nextNode()){
                            parts.push(walker.currentNode.textContent.trim());
                        }
                        return parts.join('\\n');
                    }"""
                )
            except Exception as exc:  # pragma: no cover - page-specific quirks
                _BROWSER_LOGGER.debug("DOM walker failed for %s: %s", final_url, exc)
                main_text = ""
            if not main_text:
                try:
                    main_text = await page.inner_text("body")
                except Exception as exc:  # pragma: no cover - minimal fallback
                    _BROWSER_LOGGER.debug("inner_text fallback failed for %s: %s", final_url, exc)
                    main_text = ""
            try:
                meta_title = await page.title()
            except Exception as exc:
                _BROWSER_LOGGER.debug("title() failed for %s: %s", final_url, exc)
                meta_title = ""
            try:
                meta_desc = await page.evaluate(
                    """() => {
                        const tag = document.querySelector('meta[name="description"], meta[property="og:description"]');
                        return tag ? tag.content : '';
                    }"""
                )
            except Exception as exc:
                _BROWSER_LOGGER.debug("meta description lookup failed for %s: %s", final_url, exc)
                meta_desc = ""
        except RuntimeError as exc:
            _BROWSER_LOGGER.error("Playwright runtime error for %s: %s", final_url, exc)
            return str(exc)
        except PlaywrightTimeout as exc:
            error_message = "Перевищено час очікування завантаження сторінки."
            _BROWSER_LOGGER.warning("Playwright timeout for %s: %s", final_url, exc)
        except Exception as exc:
            error_message = f"Не вдалося завантажити сторінку: {exc}"
            _BROWSER_LOGGER.exception("Playwright failed for %s", final_url)
        else:
            chunks = []
            if meta_title:
                chunks.append(meta_title.strip())
            if meta_desc:
                chunks.append(meta_desc.strip())
            if main_text:
                cleaned = re.sub(r"\s+", " ", main_text)
                chunks.append(cleaned.strip())
            text_result = "\n".join(filter(None, chunks)).strip()
        finally:
            if page is not None:
                await pool.release_page(page)

        if error_message:
            return error_message
        if not text_result:
            return "Сторінка не повернула текстового вмісту."

        if len(text_result) > max_chars_val:
            text_result = text_result[: max_chars_val - 3].rstrip() + "..."

        return text_result
    @function_tool
    async def fetch_rss_news(
        self, _: RunContext, feed_url: str = "", limit: int | str = 3
    ) -> str:
        """
        Прочитати останні новини з RSS-стрічки та коротко переказати посилання.
        """

        try:
            import feedparser  # type: ignore
        except ImportError:
            return "Модуль для читання RSS наразі не встановлений."

        feed_url_value = ""
        if isinstance(feed_url, str):
            feed_url_value = feed_url.strip()
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


__all__ = [
    "GeminiVisionAgent",
    "AgentBase",
    "RunContext",
    "function_tool",
    "LIVEKIT_IMPORT_ERROR",
]
