import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple

try:
    from playwright.async_api import async_playwright  # type: ignore
except ImportError:  # pragma: no cover - playwright optional
    async_playwright = None  # type: ignore[assignment]

_STEALTH_SNIPPET = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
const originalQuery = window.navigator.permissions?.query;
if (originalQuery) {
  window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
      ? Promise.resolve({ state: 'denied' })
      : originalQuery(parameters);
}
"""


@dataclass(frozen=True)
class ProxyConfig:
    server: str
    username: Optional[str]
    password: Optional[str]
    bypass: Optional[str]


@dataclass(frozen=True)
class BrowserContextConfig:
    chromium_args: Tuple[str, ...]
    user_agent: Optional[str]
    locale: str
    timezone_id: str
    viewport: Tuple[int, int]
    proxy: Optional[ProxyConfig]


class PlaywrightBrowserPool:
    """
    Lazily launch a headless Chromium instance and reuse it across tool calls.
    Automatically tears down the browser after a configurable idle timeout.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._context_config: Optional[BrowserContextConfig] = None
        self._chromium_args: Tuple[str, ...] | None = None
        self._active_pages = 0
        self._idle_timeout = 60.0
        self._last_used = 0.0
        self._idle_task: Optional[asyncio.Task[None]] = None

    async def acquire_page(
        self,
        *,
        config: BrowserContextConfig,
        launch_timeout_ms: int,
        idle_timeout_s: float,
    ) -> Any:
        if async_playwright is None:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Playwright is not installed. Run `pip install playwright` and "
                "`python -m playwright install chromium`."
            )

        # Ensure browser/context exist with the requested configuration.
        async with self._lock:
            self._idle_timeout = max(0.0, idle_timeout_s)
            await self._ensure_browser_locked(config=config, launch_timeout_ms=launch_timeout_ms)
            self._active_pages += 1
            self._last_used = time.monotonic()
            context = self._context

        page = await context.new_page()
        return page

    async def release_page(self, page: Any) -> None:
        try:
            await page.close()
        except Exception:  # pragma: no cover - defensive cleanup
            pass

        async with self._lock:
            self._active_pages = max(0, self._active_pages - 1)
            self._last_used = time.monotonic()

            if self._active_pages == 0:
                if self._idle_timeout <= 0.0:
                    browser = self._browser
                    context = self._context
                    playwright = self._playwright
                    self._reset_locked()
                    await self._shutdown_objects(context, browser, playwright)
                elif self._idle_task is None:
                    self._idle_task = asyncio.create_task(self._idle_cleanup())

    async def _ensure_browser_locked(
        self,
        *,
        config: BrowserContextConfig,
        launch_timeout_ms: int,
    ) -> None:
        assert async_playwright is not None  # guarded by caller

        launch_timeout_ms = max(1000, launch_timeout_ms)

        need_new_browser = (
            self._browser is None
            or self._chromium_args != config.chromium_args
        )
        browser_to_close = None
        context_to_close = None
        playwright_to_stop = None

        if need_new_browser and self._browser is not None:
            browser_to_close = self._browser
            context_to_close = self._context
            playwright_to_stop = self._playwright
            self._context = None
            self._browser = None
            self._playwright = None
            self._context_config = None
            self._chromium_args = None

        if browser_to_close or context_to_close or playwright_to_stop:
            await self._shutdown_objects(context_to_close, browser_to_close, playwright_to_stop)

        if self._browser is None:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            launch_params = dict(
                headless=True,
                args=list(config.chromium_args),
                timeout=launch_timeout_ms,
            )
            if config.proxy is not None:
                launch_params["proxy"] = {
                    "server": config.proxy.server,
                }
                if config.proxy.username:
                    launch_params["proxy"]["username"] = config.proxy.username
                if config.proxy.password:
                    launch_params["proxy"]["password"] = config.proxy.password
                if config.proxy.bypass:
                    launch_params["proxy"]["bypass"] = config.proxy.bypass
            self._browser = await self._playwright.chromium.launch(**launch_params)
            self._chromium_args = config.chromium_args

        if self._context is None or self._context_config != config:
            if self._context is not None:
                await self._context.close()
            viewport_width, viewport_height = config.viewport
            self._context = await self._browser.new_context(
                user_agent=config.user_agent,
                locale=config.locale,
                viewport={"width": viewport_width, "height": viewport_height},
                timezone_id=config.timezone_id,
            )
            await self._context.add_init_script(_STEALTH_SNIPPET)
            self._context_config = config

        if self._idle_task is not None and self._active_pages == 0:
            # Cancel pending idle shutdown if a new page is requested while idle.
            self._idle_task.cancel()
            self._idle_task = None

    async def _idle_cleanup(self) -> None:
        try:
            while True:
                timeout = self._idle_timeout
                await asyncio.sleep(timeout)
                async with self._lock:
                    now = time.monotonic()
                    if self._active_pages > 0 or self._browser is None:
                        continue
                    if (now - self._last_used) < timeout:
                        continue
                    browser = self._browser
                    context = self._context
                    playwright = self._playwright
                    self._reset_locked()
                await self._shutdown_objects(context, browser, playwright)
                break
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            raise
        finally:
            async with self._lock:
                self._idle_task = None

    def _reset_locked(self) -> None:
        self._browser = None
        self._context = None
        self._playwright = None
        self._context_config = None
        self._chromium_args = None
        self._last_used = time.monotonic()

    @staticmethod
    async def _shutdown_objects(context: Any, browser: Any, playwright: Any) -> None:
        if context is not None:
            try:
                await context.close()
            except Exception:  # pragma: no cover - defensive cleanup
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass

    async def warmup(
        self,
        *,
        config: BrowserContextConfig,
        launch_timeout_ms: int,
        idle_timeout_s: float,
    ) -> None:
        page = await self.acquire_page(
            config=config,
            launch_timeout_ms=launch_timeout_ms,
            idle_timeout_s=idle_timeout_s,
        )
        await self.release_page(page)

    async def shutdown(self) -> None:
        async with self._lock:
            browser = self._browser
            context = self._context
            playwright = self._playwright
            if browser is None and context is None and playwright is None:
                return
            if self._idle_task:
                self._idle_task.cancel()
                self._idle_task = None
            self._reset_locked()
        await self._shutdown_objects(context, browser, playwright)


_POOL: Optional[PlaywrightBrowserPool] = None


def get_browser_pool() -> PlaywrightBrowserPool:
    global _POOL
    if _POOL is None:
        _POOL = PlaywrightBrowserPool()
    return _POOL


__all__ = ["BrowserContextConfig", "PlaywrightBrowserPool", "ProxyConfig", "get_browser_pool"]
