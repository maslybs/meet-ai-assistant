import asyncio
import json
import logging
import os
import random
import re
from typing import Any, Optional, Sequence
from urllib.parse import urlparse
from urllib import request as urllib_request
from urllib import error as urllib_error

from ..browser_pool import BrowserContextConfig, ProxyConfig, get_browser_pool


_BROWSER_LOGGER = logging.getLogger("voice-agent.browser")
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
    """Fetch textual content from a web page using Playwright."""

    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeout  # type: ignore
    except ImportError:  # pragma: no cover - optional dependency
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
    max_chars_env = os.getenv("VOICE_AGENT_BROWSER_MAX_CHARS", "").strip()
    proxy_server = os.getenv("VOICE_AGENT_BROWSER_PROXY_SERVER", "").strip()
    proxy_username = os.getenv("VOICE_AGENT_BROWSER_PROXY_USERNAME", "").strip() or None
    proxy_password = os.getenv("VOICE_AGENT_BROWSER_PROXY_PASSWORD", "").strip() or None
    proxy_bypass = os.getenv("VOICE_AGENT_BROWSER_PROXY_BYPASS", "").strip() or None

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
    viewport_width_env = os.getenv("VOICE_AGENT_BROWSER_VIEWPORT_WIDTH", "").strip()
    viewport_height_env = os.getenv("VOICE_AGENT_BROWSER_VIEWPORT_HEIGHT", "").strip()
    if viewport_width_env or viewport_height_env:
        viewport_width = _resolve_int(
            viewport_width_env or None,
            fallback=1280,
            minimum=640,
            maximum=2560,
        )
        viewport_height = _resolve_int(
            viewport_height_env or None,
            fallback=720,
            minimum=480,
            maximum=1600,
        )
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

    allowed_wait_conditions = {"load", "domcontentloaded", "networkidle", "commit"}
    wait_default_normalized = (wait_default or "").lower()
    wait_condition = (
        wait_default_normalized if wait_default_normalized in allowed_wait_conditions else "networkidle"
    )
    extra_wait_ms = 2000

    extra_wait_env = os.getenv("VOICE_AGENT_BROWSER_EXTRA_WAIT_MS", "").strip()
    if extra_wait_env:
        parsed_wait = _parse_wait_value(extra_wait_env)
        if parsed_wait is not None:
            extra_wait_ms = parsed_wait

    def _coerce_wait_ms(value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return max(0, int(float(value) * 1000))
        if isinstance(value, dict):
            for key, factor in (
                ("milliseconds", 1),
                ("ms", 1),
                ("seconds", 1000),
                ("s", 1000),
            ):
                if key in value:
                    try:
                        return max(0, int(float(value[key]) * factor))
                    except (TypeError, ValueError):
                        continue
            # Fallback: try to parse any first value as string
            try:
                first_val = next(iter(value.values()))
            except StopIteration:
                return None
            return _parse_wait_value(str(first_val).strip())
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                return None
            lowered = normalized.lower()
            if lowered in allowed_wait_conditions:
                return None
            return _parse_wait_value(normalized)
        # Fallback: attempt to parse string representation
        return _parse_wait_value(str(value).strip())

    random_jitter_env = os.getenv("VOICE_AGENT_BROWSER_RANDOM_DELAY_RANGE", "").strip()
    if random_jitter_env:
        try:
            parts = [float(p) for p in re.split(r"[,;:/\-]+", random_jitter_env) if p.strip()]
        except ValueError:
            parts = []
        if len(parts) == 1:
            extra_wait_ms += max(0, int(parts[0] * 1000))
        elif len(parts) >= 2:
            low = max(0.0, min(parts[0], parts[1]))
            high = max(parts[0], parts[1])
            extra_wait_ms = int(random.uniform(low, high) * 1000)
    elif not extra_wait_env:
        extra_wait_ms = int(random.uniform(1.0, 3.5) * 1000)

    if isinstance(wait, str):
        lowered = wait.strip().lower()
        if lowered in allowed_wait_conditions:
            wait_condition = lowered
        else:
            parsed_wait = _coerce_wait_ms(wait)
            if parsed_wait is not None:
                extra_wait_ms = parsed_wait

    else:
        parsed_wait = _coerce_wait_ms(wait)
        if parsed_wait is not None:
            extra_wait_ms = parsed_wait

    idle_timeout_raw = os.getenv("VOICE_AGENT_BROWSER_IDLE_SECONDS", "60").strip()
    try:
        idle_timeout = float(idle_timeout_raw) if idle_timeout_raw else 60.0
    except ValueError:
        idle_timeout = 60.0
    idle_timeout = max(0.0, min(idle_timeout, 3600.0))

    if proxy_server:
        proxy = ProxyConfig(
            server=proxy_server,
            username=proxy_username,
            password=proxy_password,
            bypass=proxy_bypass,
        )
    else:
        proxy = None

    if proxy is None:
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
            async def _route_handler(route):  # type: ignore[no-untyped-def]
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

        await page.goto(final_url, wait_until=wait_condition or "networkidle", timeout=timeout_ms)
        if extra_wait_ms > 0:
            await page.wait_for_timeout(extra_wait_ms)

        try:
            meta_title = await page.title()
        except Exception:
            meta_title = ""
        try:
            meta_desc = await page.evaluate(
                """() => {
                    const tag = document.querySelector('meta[name="description"], meta[property="og:description"]');
                    return tag ? tag.content : '';
                }"""
            )
        except Exception:
            meta_desc = ""
        try:
            main_text = await page.inner_text("body")
        except Exception:
            main_text = ""

        chunks = []
        if meta_title:
            chunks.append(meta_title.strip())
        if meta_desc:
            chunks.append(meta_desc.strip())
        if main_text:
            cleaned = re.sub(r"\s+", " ", main_text)
            chunks.append(cleaned.strip())
        text_result = "\n".join(filter(None, chunks)).strip()
    except RuntimeError as exc:
        _BROWSER_LOGGER.error("Playwright runtime error for %s: %s", final_url, exc)
        return str(exc)
    except PlaywrightTimeout as exc:
        error_message = "Перевищено час очікування завантаження сторінки."
        _BROWSER_LOGGER.warning("Playwright timeout for %s: %s", final_url, exc)
        return error_message
    except Exception as exc:
        error_message = f"Не вдалося завантажити сторінку: {exc}"
        _BROWSER_LOGGER.exception("Playwright failed for %s", final_url)
        return error_message
    finally:
        if page is not None:
            await pool.release_page(page)

    if not text_result:
        return "Сторінка не повернула текстового вмісту."

    if len(text_result) > max_chars_val:
        text_result = text_result[: max_chars_val - 3].rstrip() + "..."

    return text_result


async def _maybe_fetch_webshare_proxy() -> Optional[ProxyConfig]:
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
