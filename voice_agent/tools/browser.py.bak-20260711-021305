import asyncio
import json
import logging
import os
import random
import re
from typing import Any, Optional, Sequence
from urllib.parse import urlparse, quote
from urllib import request as urllib_request
from urllib import error as urllib_error

from ..browser_pool import BrowserContextConfig, ProxyConfig, get_browser_pool


_BROWSER_LOGGER = logging.getLogger("voice-agent.browser")

# Jina Reader configuration
_JINA_BASE_URL = "https://r.jina.ai/"
_JINA_TIMEOUT = 45  # Increased for e-commerce sites with dynamic content

_DEFAULT_USER_AGENTS: Sequence[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15; rv:117.0) Gecko/20100101 Firefox/117.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.78 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 6.1; WOW64; rv:60.0) Gecko/20100101 Firefox/60.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/117.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.86 Safari/537.36",
)
_DEFAULT_VIEWPORTS: Sequence[tuple[int, int]] = (
    (1920, 1080),
    (1680, 1050),
    (1600, 900),
    (1536, 864),
    (1440, 900),
    (1366, 768),
)


async def browse_web_page(
    _: Any,
    url: str,
    wait: Any = "",
    max_chars: int | str = 0,
) -> str:
    """
    Fetch textual content from a web page.
    
    Uses Jina Reader API as primary method (fast, handles anti-bot protection).
    Falls back to Playwright if Jina fails or is disabled.
    
    Returns Markdown content with links preserved for navigation.
    """
    _BROWSER_LOGGER.info("=" * 60)
    _BROWSER_LOGGER.info("BROWSE_WEB_PAGE CALLED")
    _BROWSER_LOGGER.info("URL parameter: %s", url)
    _BROWSER_LOGGER.info("=" * 60)
    
    url_value = (url or "").strip()
    if not url_value:
        url_value = os.getenv("VOICE_AGENT_BROWSER_HOME", "").strip()
    if not url_value:
        _BROWSER_LOGGER.warning("No URL provided and VOICE_AGENT_BROWSER_HOME not set")
        return "Будь ласка, надайте URL сторінки або встановіть VOICE_AGENT_BROWSER_HOME."

    if not urlparse(url_value).scheme:
        url_value = f"https://{url_value}"

    parsed = urlparse(url_value)
    if not parsed.netloc:
        _BROWSER_LOGGER.warning("Invalid URL: %s", url_value)
        return "URL виглядає некоректним. Перевірте адресу і спробуйте ще раз."
    final_url = parsed.geturl()
    
    _BROWSER_LOGGER.info("Final URL to fetch: %s", final_url)

    # Resolve max_chars - default 20000 for rich content (prices, descriptions)
    max_chars_env = os.getenv("VOICE_AGENT_BROWSER_MAX_CHARS", "").strip()
    max_chars_val = _resolve_int(
        max_chars if isinstance(max_chars, (int, str)) else None,
        fallback=_resolve_int(max_chars_env or None, 20000, 500, 100000),
        minimum=500,
        maximum=100000,
    )
    _BROWSER_LOGGER.info("Max chars: %d", max_chars_val)

    # Check if Jina is enabled (default: yes)
    jina_enabled = os.getenv("VOICE_AGENT_JINA_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }

    result = None
    
    # Try Jina Reader first
    if jina_enabled:
        _BROWSER_LOGGER.info("Attempting Jina Reader for: %s", final_url)
        result = await _fetch_via_jina(final_url, max_chars_val)
        if result:
            _BROWSER_LOGGER.info("Jina Reader SUCCESS - got %d chars", len(result))
            _BROWSER_LOGGER.info("Content preview (first 500 chars): %s", result[:500])
            return result
        _BROWSER_LOGGER.warning("Jina Reader returned no content")

    # Fallback to Playwright
    playwright_enabled = os.getenv("VOICE_AGENT_PLAYWRIGHT_FALLBACK", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }
    
    if playwright_enabled:
        _BROWSER_LOGGER.info("Attempting Playwright fallback for: %s", final_url)
        result = await _fetch_via_playwright(final_url, wait, max_chars_val)
        if result:
            _BROWSER_LOGGER.info("Playwright SUCCESS - got %d chars", len(result))
            _BROWSER_LOGGER.info("Content preview (first 500 chars): %s", result[:500])
            return result
        _BROWSER_LOGGER.warning("Playwright returned no content")

    _BROWSER_LOGGER.error("FAILED to fetch page with all methods: %s", final_url)
    return "Не вдалося завантажити сторінку жодним із доступних методів."


async def _fetch_via_jina(url: str, max_chars: int) -> Optional[str]:
    """Fetch page content using Jina Reader API with retry logic."""
    
    _BROWSER_LOGGER.info("[JINA] Starting fetch for: %s", url)
    
    jina_api_key = os.getenv("JINA_API_KEY", "").strip()
    timeout_raw = os.getenv("VOICE_AGENT_JINA_TIMEOUT", str(_JINA_TIMEOUT)).strip()
    max_retries = int(os.getenv("VOICE_AGENT_JINA_RETRIES", "3"))
    
    try:
        timeout = max(5.0, min(float(timeout_raw), 120.0))
    except ValueError:
        timeout = _JINA_TIMEOUT
    
    _BROWSER_LOGGER.info("[JINA] Timeout: %s sec, Max retries: %d, API key: %s", 
                         timeout, max_retries, "SET" if jina_api_key else "NOT SET")

    # Build Jina URL
    jina_url = f"{_JINA_BASE_URL}{url}"
    _BROWSER_LOGGER.info("[JINA] Full Jina URL: %s", jina_url)
    
    headers = {
        "Accept": "text/markdown",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    
    # Add API key if available (increases rate limits)
    if jina_api_key:
        headers["Authorization"] = f"Bearer {jina_api_key}"
    
    # Request links summary (free tier feature)
    headers["x-with-links-summary"] = "true"
    
    loop = asyncio.get_running_loop()

    def _fetch() -> str:
        req = urllib_request.Request(jina_url, headers=headers)
        with urllib_request.urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    content = None
    last_error = None
    
    for attempt in range(max_retries):
        _BROWSER_LOGGER.info("[JINA] Attempt %d/%d for %s", attempt + 1, max_retries, url)
        try:
            content = await loop.run_in_executor(None, _fetch)
            if content and content.strip():
                _BROWSER_LOGGER.info("[JINA] SUCCESS on attempt %d - received %d chars", attempt + 1, len(content))
                break
            _BROWSER_LOGGER.warning("[JINA] Empty content on attempt %d for %s", attempt + 1, url)
        except urllib_error.HTTPError as exc:
            last_error = exc
            _BROWSER_LOGGER.error("[JINA] HTTP Error %d on attempt %d for %s", exc.code, attempt + 1, url)
            if exc.code == 429:  # Rate limited
                wait_time = min(2 ** attempt, 10)
                _BROWSER_LOGGER.warning("[JINA] Rate limited, waiting %ds", wait_time)
                await asyncio.sleep(wait_time)
            elif exc.code >= 500:  # Server error - retry
                wait_time = min(2 ** attempt, 8)
                _BROWSER_LOGGER.warning("[JINA] Server error, retrying in %ds", wait_time)
                await asyncio.sleep(wait_time)
            else:  # Client error (4xx except 429) - don't retry
                _BROWSER_LOGGER.error("[JINA] Client error %d, not retrying", exc.code)
                return None
        except (urllib_error.URLError, TimeoutError) as exc:
            last_error = exc
            wait_time = min(2 ** attempt, 8)
            _BROWSER_LOGGER.error("[JINA] Network/timeout error on attempt %d: %s", attempt + 1, exc)
            await asyncio.sleep(wait_time)
        except Exception as exc:
            last_error = exc
            _BROWSER_LOGGER.exception("[JINA] Unexpected error on attempt %d", attempt + 1)
            await asyncio.sleep(1)

    if not content or not content.strip():
        _BROWSER_LOGGER.error("[JINA] FAILED after %d attempts for %s. Last error: %s", max_retries, url, last_error)
        return None

    # Truncate if needed
    original_len = len(content)
    if len(content) > max_chars:
        content = content[:max_chars - 3].rstrip() + "..."
        _BROWSER_LOGGER.info("[JINA] Truncated from %d to %d chars", original_len, len(content))

    _BROWSER_LOGGER.info("[JINA] Final content length: %d chars", len(content))
    return content


async def _fetch_via_playwright(
    url: str,
    wait: Any,
    max_chars: int,
) -> Optional[str]:
    """Fetch page content using Playwright (fallback method)."""

    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeout
    except ImportError:
        _BROWSER_LOGGER.warning("Playwright not available for fallback")
        return None

    user_agent_setting = os.getenv("VOICE_AGENT_BROWSER_USER_AGENT", "").strip()
    user_agent_choices: Sequence[str]
    if user_agent_setting:
        separators = (",", "|", "\n")
        normalized = user_agent_setting
        for sep in separators:
            normalized = normalized.replace(sep, "\n")
        user_agent_choices = tuple(
            ua.strip() for ua in normalized.splitlines() if ua.strip()
        )
        if not user_agent_choices:
            user_agent_choices = _DEFAULT_USER_AGENTS
    else:
        user_agent_choices = _DEFAULT_USER_AGENTS
    user_agent = random.choice(user_agent_choices)

    locale = os.getenv("VOICE_AGENT_BROWSER_LOCALE", "uk-UA").strip() or "uk-UA"
    timeout_ms_raw = os.getenv("VOICE_AGENT_BROWSER_TIMEOUT_MS", "").strip()
    wait_default = os.getenv("VOICE_AGENT_BROWSER_WAIT_UNTIL", "networkidle").strip()
    proxy_server = os.getenv("VOICE_AGENT_BROWSER_PROXY_SERVER", "").strip()
    proxy_username = os.getenv("VOICE_AGENT_BROWSER_PROXY_USERNAME", "").strip() or None
    proxy_password = os.getenv("VOICE_AGENT_BROWSER_PROXY_PASSWORD", "").strip() or None
    proxy_bypass = os.getenv("VOICE_AGENT_BROWSER_PROXY_BYPASS", "").strip() or None

    timeout_ms = _resolve_int(timeout_ms_raw or None, fallback=15000, minimum=1000, maximum=60000)
    
    viewport_width_env = os.getenv("VOICE_AGENT_BROWSER_VIEWPORT_WIDTH", "").strip()
    viewport_height_env = os.getenv("VOICE_AGENT_BROWSER_VIEWPORT_HEIGHT", "").strip()
    if viewport_width_env or viewport_height_env:
        viewport_width = _resolve_int(viewport_width_env or None, fallback=1280, minimum=640, maximum=2560)
        viewport_height = _resolve_int(viewport_height_env or None, fallback=720, minimum=480, maximum=1600)
    else:
        viewport_width, viewport_height = random.choice(_DEFAULT_VIEWPORTS)

    chromium_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-gpu",
    ]
    extra_args = os.getenv("VOICE_AGENT_BROWSER_CHROMIUM_ARGS", "").strip()
    if extra_args:
        chromium_args.extend(arg for arg in extra_args.split() if arg)

    allowed_wait_conditions = {"load", "domcontentloaded", "networkidle", "commit"}
    wait_default_normalized = (wait_default or "").lower()
    wait_condition = (
        wait_default_normalized if wait_default_normalized in allowed_wait_conditions else "networkidle"
    )
    extra_wait_ms = _resolve_extra_wait(wait)

    idle_timeout_raw = os.getenv("VOICE_AGENT_BROWSER_IDLE_SECONDS", "60").strip()
    try:
        idle_timeout = float(idle_timeout_raw) if idle_timeout_raw else 60.0
    except ValueError:
        idle_timeout = 60.0
    idle_timeout = max(0.0, min(idle_timeout, 3600.0))

    proxy_enabled = os.getenv("VOICE_AGENT_BROWSER_ENABLE_PROXY", "1").strip().lower() not in {
        "", "0", "false", "no", "off",
    }

    proxy = None
    if proxy_enabled:
        if proxy_server:
            proxy = ProxyConfig(
                server=proxy_server,
                username=proxy_username,
                password=proxy_password,
                bypass=proxy_bypass,
            )
        else:
            proxy = await _maybe_fetch_webshare_proxy()

    pool = get_browser_pool()
    page = None
    text_result = ""

    try:
        page = await pool.acquire_page(
            config=BrowserContextConfig(
                chromium_args=tuple(chromium_args),
                user_agent=user_agent,
                locale=locale,
                timezone_id=os.getenv("VOICE_AGENT_BROWSER_TIMEZONE", "Europe/Kyiv"),
                viewport=(viewport_width, viewport_height),
                proxy=proxy,
            ),
            launch_timeout_ms=timeout_ms,
            idle_timeout_s=idle_timeout,
        )
        page.set_default_timeout(timeout_ms)
        page.set_default_navigation_timeout(timeout_ms)

        block_resources_setting = os.getenv("VOICE_AGENT_BROWSER_BLOCK_RESOURCES", "1").strip().lower()
        block_resources = block_resources_setting not in {"", "0", "false", "no"}
        blocked_types = {"image", "media", "font"}
        blocked_extensions = tuple(
            ext.strip().lower()
            for ext in os.getenv("VOICE_AGENT_BROWSER_BLOCK_EXT", ".ico,.png,.jpg,.jpeg,.gif,.svg,.webp,.mp4,.webm").split(",")
            if ext.strip()
        )

        if block_resources:
            async def _route_handler(route):
                try:
                    req = route.request
                    resource_type = req.resource_type
                    req_url = req.url.lower()
                    if resource_type == "subframe":
                        await route.abort()
                        return
                    if resource_type in blocked_types:
                        await route.abort()
                        return
                    if blocked_extensions and any(req_url.endswith(ext) for ext in blocked_extensions):
                        await route.abort()
                        return
                    await route.continue_()
                except Exception:
                    await route.continue_()

            await page.route("**/*", _route_handler)

        await page.goto(url, wait_until=wait_condition or "networkidle", timeout=timeout_ms)
        if extra_wait_ms > 0:
            await page.wait_for_timeout(extra_wait_ms)

        # Extract content with links preserved
        text_result = await _extract_markdown_content(page)
        
    except RuntimeError as exc:
        _BROWSER_LOGGER.error("Playwright runtime error for %s: %s", url, exc)
        return None
    except PlaywrightTimeout:
        _BROWSER_LOGGER.warning("Playwright timeout for %s", url)
        return None
    except Exception as exc:
        _BROWSER_LOGGER.exception("Playwright failed for %s", url)
        return None
    finally:
        if page is not None:
            await pool.release_page(page)

    if not text_result:
        return None

    if len(text_result) > max_chars:
        text_result = text_result[:max_chars - 3].rstrip() + "..."

    return text_result


async def _extract_markdown_content(page: Any) -> str:
    """Extract page content as Markdown with links preserved."""
    
    try:
        # Get page title and URL
        title = await page.title() or ""
        current_url = page.url
        
        # Extract links with their text
        links_data = await page.evaluate("""() => {
            const links = [];
            document.querySelectorAll('a[href]').forEach((a, idx) => {
                const text = a.innerText.trim();
                const href = a.href;
                if (text && href && !href.startsWith('javascript:') && text.length < 200) {
                    links.push({text: text, href: href});
                }
            });
            return links.slice(0, 100);  // Limit to 100 links
        }""")
        
        # Get main text content
        main_text = await page.inner_text("body")
        main_text = re.sub(r"\s+", " ", main_text).strip()
        
        # Build Markdown output
        parts = []
        parts.append(f"Title: {title}")
        parts.append(f"URL Source: {current_url}")
        parts.append("")
        parts.append("Markdown Content:")
        parts.append(f"# {title}")
        parts.append("")
        
        # Add main content (truncated)
        if main_text:
            parts.append(main_text[:3000])
        
        # Add links section
        if links_data:
            parts.append("")
            parts.append("---")
            parts.append("## Посилання на сторінці:")
            seen_hrefs = set()
            for link in links_data:
                href = link.get("href", "")
                text = link.get("text", "").replace("\n", " ").strip()
                if href and href not in seen_hrefs and text:
                    seen_hrefs.add(href)
                    parts.append(f"- [{text}]({href})")
        
        return "\n".join(parts)
        
    except Exception as exc:
        _BROWSER_LOGGER.warning("Failed to extract markdown content: %s", exc)
        # Fallback to simple text extraction
        try:
            return await page.inner_text("body")
        except Exception:
            return ""


def _resolve_int(
    raw: int | str | None, fallback: int, minimum: int, maximum: int | None = None
) -> int:
    candidate = raw
    if candidate in (None, "", 0):
        candidate = fallback
    try:
        value = int(candidate)
    except (TypeError, ValueError):
        value = fallback
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _resolve_extra_wait(wait: Any) -> int:
    """Resolve extra wait time in milliseconds."""
    extra_wait_env = os.getenv("VOICE_AGENT_BROWSER_EXTRA_WAIT_MS", "").strip()
    random_jitter_env = os.getenv("VOICE_AGENT_BROWSER_RANDOM_DELAY_RANGE", "").strip()
    
    extra_wait_ms = 2000
    
    if extra_wait_env:
        try:
            extra_wait_ms = int(float(extra_wait_env))
        except ValueError:
            pass
    
    if random_jitter_env:
        try:
            parts = [float(p) for p in re.split(r"[,;:/\-]+", random_jitter_env) if p.strip()]
            if len(parts) >= 2:
                low = max(0.0, min(parts[0], parts[1]))
                high = max(parts[0], parts[1])
                extra_wait_ms = int(random.uniform(low, high) * 1000)
            elif len(parts) == 1:
                extra_wait_ms += int(parts[0] * 1000)
        except ValueError:
            pass
    elif not extra_wait_env:
        extra_wait_ms = int(random.uniform(1.0, 3.5) * 1000)
    
    # Override with explicit wait parameter
    if isinstance(wait, (int, float)):
        extra_wait_ms = int(float(wait) * 1000)
    elif isinstance(wait, str) and wait.strip():
        try:
            extra_wait_ms = int(float(wait.strip()) * 1000)
        except ValueError:
            pass
    
    return max(0, extra_wait_ms)


async def _maybe_fetch_webshare_proxy() -> Optional[ProxyConfig]:
    """Fetch a random proxy from Webshare API."""
    api_key = os.getenv("VOICE_AGENT_WEBSHARE_API_KEY", "").strip()
    if not api_key:
        return None

    endpoint = os.getenv(
        "VOICE_AGENT_WEBSHARE_ENDPOINT",
        "https://proxy.webshare.io/api/proxy/list/",
    ).strip()
    query = os.getenv("VOICE_AGENT_WEBSHARE_QUERY", "mode=direct&limit=20").strip()
    if query:
        separator = "&" if "?" in endpoint else "?"
        url = f"{endpoint}{separator}{query}"
    else:
        url = endpoint

    headers = {
        "Authorization": api_key,
        "Accept": "application/json",
    }

    timeout_raw = os.getenv("VOICE_AGENT_WEBSHARE_TIMEOUT", "10").strip()
    try:
        timeout = max(3.0, float(timeout_raw))
    except ValueError:
        timeout = 10.0

    loop = asyncio.get_running_loop()

    def _fetch() -> bytes:
        req = urllib_request.Request(url, headers=headers)
        with urllib_request.urlopen(req, timeout=timeout) as response:
            return response.read()

    try:
        payload = await loop.run_in_executor(None, _fetch)
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError) as exc:
        _BROWSER_LOGGER.warning("Webshare proxy request failed: %s", exc)
        return None

    try:
        data = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        _BROWSER_LOGGER.warning("Webshare proxy response is not JSON: %s", exc)
        return None

    results = data.get("results") or data.get("data") or []
    if not isinstance(results, list) or not results:
        _BROWSER_LOGGER.warning("Webshare proxy list is empty.")
        return None

    entry = random.choice(results)
    host = entry.get("proxy_address") or entry.get("ip") or entry.get("host")
    port = entry.get("port") or entry.get("proxy_port")
    scheme = entry.get("protocol") or entry.get("scheme") or "http"
    username = entry.get("username") or entry.get("user")
    password = entry.get("password") or entry.get("pass") or entry.get("pwd")

    if not host or not port:
        _BROWSER_LOGGER.warning("Webshare proxy entry missing host/port; skipping.")
        return None

    try:
        port_int = int(str(port))
    except ValueError:
        _BROWSER_LOGGER.warning("Webshare proxy entry has invalid port: %s", port)
        return None

    server = f"{scheme}://{host}:{port_int}"
    return ProxyConfig(
        server=server,
        username=username,
        password=password,
        bypass=None,
    )
