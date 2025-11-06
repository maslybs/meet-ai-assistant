import logging
from typing import Any


_VIDEO_LOGGER = logging.getLogger("voice-agent.video")


async def enable_video_feed(agent: Any) -> str:
    """Enable participant video stream if available."""

    session = getattr(agent, "session", None)
    if session is None:
        return "Зараз не можу отримати відео, спробуйте пізніше."

    video_stream = getattr(getattr(session, "input", None), "video", None)
    if video_stream is None:
        return "Відео від учасника недоступне. Переконайтеся, що камера увімкнена."

    if session.input.video_enabled:
        return "Відео вже увімкнене."

    session.input.set_video_enabled(True)
    _VIDEO_LOGGER.info("Video feed enabled by request")
    return "Добре, я бачу відео. Дайте знати, що саме потрібно показати."


async def disable_video_feed(agent: Any) -> str:
    """Disable participant video stream if currently enabled."""

    session = getattr(agent, "session", None)
    if session is None or getattr(getattr(session, "input", None), "video", None) is None:
        return "Зараз відеосигнал недоступний."

    if not session.input.video_enabled:
        return "Відео вже вимкнене."

    session.input.set_video_enabled(False)
    _VIDEO_LOGGER.info("Video feed disabled on request")
    return "Вимкнула відео. Якщо знадобиться знову, просто скажіть."
