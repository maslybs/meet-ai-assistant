import inspect
import logging
from typing import Any


def _ensure_importlib_compat() -> None:
    """
    google.api_core expects importlib.metadata.packages_distributions which is
    only available in the stdlib starting with Python 3.10. When running on
    Python 3.9 we mirror the backport implementation to avoid runtime errors.
    """

    try:
        import importlib.metadata as stdlib_metadata  # type: ignore
    except ImportError:  # pragma: no cover - older interpreters
        return

    try:
        import importlib_metadata as backport  # type: ignore
    except ImportError:
        return

    if not hasattr(stdlib_metadata, "packages_distributions") and hasattr(
        backport, "packages_distributions"
    ):
        setattr(
            stdlib_metadata,
            "packages_distributions",
            backport.packages_distributions,
        )  # type: ignore[attr-defined]


def _patch_aiohttp_proxy_kwarg() -> None:
    """
    livekit-agents < 1.3.0 still calls aiohttp.ClientSession with the removed
    ``proxy=`` keyword (aiohttp 3.10+). Patch the ctor to ignore that keyword.
    """

    try:
        import aiohttp  # type: ignore
    except ImportError:  # pragma: no cover
        return

    original_init = aiohttp.ClientSession.__init__
    signature = inspect.signature(original_init)

    if "proxy" in signature.parameters:
        return

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("proxy", None)
        return original_init(self, *args, **kwargs)

    aiohttp.ClientSession.__init__ = _patched_init  # type: ignore[assignment]


def _patch_livekit_room_event() -> None:
    """
    Work around a race in livekit-rtc where local track events may arrive before
    the publication map is populated. Skip known-bad events instead of raising.
    """

    try:
        from livekit.rtc import room as rtc_room  # type: ignore
    except ImportError:  # pragma: no cover
        return

    original = rtc_room.Room._on_room_event
    logger = logging.getLogger("voice-agent.livekit")

    def _patched(self, event):  # type: ignore[no-untyped-def]
        try:
            return original(self, event)
        except KeyError as error:
            which = event.WhichOneof("message")
            if which in {"local_track_published", "local_track_subscribed"}:
                sid = getattr(getattr(event, which), "track_sid", "<unknown>")
                logger.warning(
                    "Skipped LiveKit event %s for missing local track %s",
                    which,
                    sid,
                )
                return
            raise

    rtc_room.Room._on_room_event = _patched  # type: ignore[assignment]


def _patch_google_realtime_autostart() -> None:
    """
    Gemini sometimes streams LiveServerContent before emitting a generation_created event.
    Ensure the LiveKit Google realtime session always starts a generation so the agent
    does not get stuck waiting for a response.
    """

    try:
        from livekit.plugins.google.realtime import realtime_api  # type: ignore
    except ImportError:  # pragma: no cover - plugin not available locally
        return

    original = getattr(realtime_api.RealtimeSession, "_handle_server_content", None)
    if original is None:
        return
    if getattr(realtime_api.RealtimeSession, "_voice_agent_patched", False):
        return

    def _has_content(server_content: Any) -> bool:
        model_turn = getattr(server_content, "model_turn", None)
        if model_turn and getattr(model_turn, "parts", None):
            for part in model_turn.parts:
                if getattr(part, "text", None):
                    return True
                inline = getattr(part, "inline_data", None)
                if inline and getattr(inline, "data", None):
                    return True
        output_transcription = getattr(server_content, "output_transcription", None)
        if output_transcription and getattr(output_transcription, "text", None):
            return True
        input_transcription = getattr(server_content, "input_transcription", None)
        if input_transcription and getattr(input_transcription, "text", None):
            return True
        return False

    logger = logging.getLogger("voice-agent.gemini")

    def _patched(self, server_content):  # type: ignore[no-untyped-def]
        try:
            needs_generation = (
                getattr(self, "_current_generation", None) is None
                or getattr(getattr(self, "_current_generation", None), "_done", False)
            )
            if needs_generation:
                try:
                    setattr(self, "_current_generation_event", None)
                    self._start_new_generation()  # type: ignore[attr-defined]
                    if _has_content(server_content):
                        logger.debug(
                            "Gemini autostart: primed generation before server content."
                        )
                except Exception as exc:  # pragma: no cover - best effort guard
                    logger.warning("Failed to auto-start Gemini generation: %s", exc)
        except Exception:  # pragma: no cover - defensive
            logger.debug(
                "Gemini realtime autostart probe failed; continuing with original handler."
            )

        result = original(self, server_content)

        try:
            pending = getattr(self, "_pending_generation_fut", None)
            current = getattr(self, "_current_generation", None)
            if (
                pending is not None
                and not pending.done()
                and current is not None
                and getattr(current, "message_ch", None) is not None
            ):
                if _has_content(server_content):
                    logger.debug(
                        "Gemini autostart: resolving pending generation after content."
                    )
                    try:
                        response_id = getattr(current, "response_id", None) or "GR_FALLBACK"
                        setattr(current, "response_id", response_id)
                        event = getattr(self, "_current_generation_event", None)
                        if event is None:
                            from livekit.agents import llm as _llm  # type: ignore

                            event = _llm.GenerationCreatedEvent(
                                message_stream=current.message_ch,
                                function_stream=current.function_ch,
                                user_initiated=True,
                                response_id=response_id,
                            )
                            setattr(self, "_current_generation_event", event)
                        pending.set_result(event)
                        setattr(self, "_pending_generation_fut", None)
                    except Exception as exc:  # pragma: no cover
                        logger.warning("Gemini autostart fallback failed: %s", exc)
        except Exception:  # pragma: no cover
            logger.debug("Gemini autostart post-hook failed.")

        return result

    realtime_api.RealtimeSession._handle_server_content = _patched  # type: ignore[assignment]
    realtime_api.RealtimeSession._voice_agent_patched = True  # type: ignore[attr-defined]


def bootstrap() -> None:
    """
    Apply all runtime compatibility patches needed for the agent to function.
    Separated into a dedicated call so imports remain side-effect free for tests.
    """

    _ensure_importlib_compat()
    _patch_aiohttp_proxy_kwarg()
    _patch_livekit_room_event()
