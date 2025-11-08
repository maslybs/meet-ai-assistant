import os
from datetime import datetime, timezone, timedelta
from typing import Any


async def current_time_utc_plus3(_: Any) -> str:
    """Return the current time in the configured timezone (defaults to UTC+3)."""

    tz_name = os.getenv("VOICE_AGENT_TIMEZONE", "Europe/Kyiv").strip() or "Europe/Kyiv"
    offset_override = os.getenv("VOICE_AGENT_TIME_OFFSET_HOURS", "").strip()

    tzinfo = None
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+

        tzinfo = ZoneInfo(tz_name)
    except Exception:  # pragma: no cover - zoneinfo may be unavailable
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
