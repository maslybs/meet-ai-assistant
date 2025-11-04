import asyncio
import logging
from typing import Any, Optional, TYPE_CHECKING

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


__all__ = [
    "GeminiVisionAgent",
    "AgentBase",
    "RunContext",
    "function_tool",
    "LIVEKIT_IMPORT_ERROR",
]
