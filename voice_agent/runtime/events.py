import asyncio
import contextlib
import logging
import os
from typing import Any, Optional

_VIDEO_LOGGER = logging.getLogger("voice-agent.video")

try:
    from livekit import api as _lk_api  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    _lk_api = None  # type: ignore[assignment]

try:
    from livekit import rtc as _lk_rtc  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    _lk_rtc = None  # type: ignore[assignment]

try:
    from livekit.agents.types import ATTRIBUTE_PUBLISH_ON_BEHALF
except ImportError:  # pragma: no cover - fallback when livekit missing
    ATTRIBUTE_PUBLISH_ON_BEHALF = "lk.publish_on_behalf"  # type: ignore[assignment]

try:
    from livekit.agents.llm.realtime import RealtimeError  # type: ignore
except ImportError:  # pragma: no cover - used only when realtime is available
    RealtimeError = Exception  # type: ignore[assignment]


class ParticipantGreeter:
    """
    Manage participant greetings and media readiness checks for the session.
    Encapsulates event subscriptions and cleanup to keep the entrypoint lean.
    """

    def __init__(
        self,
        *,
        ctx: Any,
        session: Any,
        room_io: Any,
        broadcast_mode: bool,
        greeting_text: str,
        terminate_on_empty: bool,
        close_room_on_empty: bool,
        shutdown_delay: float,
        greeting_delay: float,
    ) -> None:
        self._ctx = ctx
        self._session = session
        self._room_io = room_io
        self._broadcast_mode = broadcast_mode
        self._greeting_text = greeting_text
        self._terminate_on_empty = terminate_on_empty
        self._close_room_on_empty = close_room_on_empty
        
        self._shutdown_delay = 5.0 if shutdown_delay < 5.0 else shutdown_delay

        # Default greeting delay is minimal
        self._greeting_delay = max(0.0, greeting_delay)
        
        self._greeted_sids: set[str] = set()
        self._inflight_initializations: set[str] = set()
        self._participant_poll_task: Optional[asyncio.Task[Any]] = None
        self._shutdown_task: Optional[asyncio.Task[None]] = None

    def attach(self) -> None:
        room = self._ctx.room
        room.on("participant_connected", self._handle_participant_connected)
        room.on("participant_disconnected", self._handle_participant_disconnected)

        for participant in room.remote_participants.values():
            self._handle_participant_connected(participant)

        self._participant_poll_task = asyncio.create_task(
            self._poll_remote_participants(), name="voice-agent.participant-poll"
        )
        self._ctx.add_shutdown_callback(self._cleanup_callbacks)

    async def _cleanup_callbacks(self) -> None:
        room = self._ctx.room
        room.off("participant_connected", self._handle_participant_connected)
        room.off("participant_disconnected", self._handle_participant_disconnected)
        if self._participant_poll_task:
            self._participant_poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._participant_poll_task
        if self._shutdown_task:
            self._shutdown_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._shutdown_task
            self._shutdown_task = None

    async def _poll_remote_participants(self, interval: float = 5.0) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                participants_snapshot = list(self._ctx.room.remote_participants.values())
                for participant in participants_snapshot:
                    sid = getattr(participant, "sid", None)
                    if (
                        not sid
                        or sid in self._greeted_sids
                        or sid in self._inflight_initializations
                    ):
                        continue
                    self._handle_participant_connected(participant)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _VIDEO_LOGGER.debug("Remote participant poll failed: %s", exc)
            await asyncio.sleep(interval)

    def _handle_participant_connected(self, participant: Any) -> None:
        if self._shutdown_task:
            self._shutdown_task.cancel()
            self._shutdown_task = None

        identity = getattr(participant, "identity", None)
        sid = getattr(participant, "sid", None)
        if identity is None or sid is None:
            return

        local_identity = getattr(self._ctx.room.local_participant, "identity", None)
        attributes = getattr(participant, "attributes", {}) or {}
        if attributes.get(ATTRIBUTE_PUBLISH_ON_BEHALF) == local_identity:
            return

        if _lk_rtc is not None:
            configured_kinds = getattr(
                getattr(self._room_io, "_input_options", None), "participant_kinds", None
            )
            if isinstance(configured_kinds, list) and configured_kinds:
                allowed_kinds = set(configured_kinds)
            else:
                allowed_kinds = {
                    getattr(_lk_rtc.ParticipantKind, "PARTICIPANT_KIND_STANDARD", None),
                    getattr(_lk_rtc.ParticipantKind, "PARTICIPANT_KIND_SIP", None),
                }
            participant_kind = getattr(participant, "kind", None)
            if participant_kind not in allowed_kinds:
                return

        linked = self._room_io.linked_participant
        target_identity = getattr(self._room_io, "_participant_identity", None)

        should_follow = False
        if linked is None:
            should_follow = True
        elif getattr(linked, "identity", None) == identity:
            should_follow = True
        elif target_identity is None:
            should_follow = True
        elif target_identity == identity:
            should_follow = True

        if not should_follow:
            return

        if not self._broadcast_mode:
            self._room_io.set_participant(identity)

        asyncio.create_task(self._initialize_participant(identity, sid))

    async def _initialize_participant(self, identity: str, sid: str) -> None:
        # Attempt to enable audio, but don't block
        try:
            if not self._session.input.audio_enabled:
                self._session.input.set_audio_enabled(True)
        except Exception:
            pass

        # CRITICAL FIX: Minimal wait. If media isn't ready in 0.5s, we proceed to greet anyway.
        # This ensures we don't get stuck waiting for a muted mic.
        try:
            await self._wait_for_media_ready(identity, broadcast=self._broadcast_mode, timeout=0.5)
        except TimeoutError:
            _VIDEO_LOGGER.info("Media not ready instantly for %s, proceeding to greet anyway.", identity)
        except Exception:
            pass
        
        if sid in self._greeted_sids:
            return

        self._inflight_initializations.add(sid)

        # Small delay to ensure connection stability
        await asyncio.sleep(1.0)

        greeted = await self._send_greeting(identity)
        if greeted:
            self._greeted_sids.add(sid)
        self._inflight_initializations.discard(sid)

    async def _wait_for_media_ready(
        self,
        identity: str,
        timeout: float = 10.0,
        *,
        broadcast: bool,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            linked = self._room_io.linked_participant
            audio_input = self._room_io.audio_input
            audio_ready = False

            if audio_input is not None:
                audio_ready = True
                # Don't over-validate source/task, just existence is enough for greeting trigger
            
            if broadcast:
                if audio_ready:
                    break
            else:
                if (
                    linked is not None
                    and getattr(linked, "identity", None) == identity
                    and audio_ready
                ):
                    break

            if loop.time() > deadline:
                raise TimeoutError(f"Timeout waiting for media {identity}")
            await asyncio.sleep(0.1)

    async def _send_greeting(self, identity: str) -> bool:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                # Use conversation.item.create for a more direct injection if possible, 
                # but sticking to generate_reply with text prompt for stability.
                # Important: We add "system_instruction" context implicitly by asking it to say specific text.
                
                # Simply triggering response_create might be cleaner if the model has instructions to greet.
                # But forcing it ensures it happens.
                
                _VIDEO_LOGGER.info("Sending greeting to %s (attempt %d)", identity, attempt)
                handle = self._session.generate_reply(
                    user_input=f"Say exactly: {self._greeting_text}"
                )
                await handle.wait_for_playout()
                return True
            except RealtimeError:
                await asyncio.sleep(0.5)
            except Exception as exc:
                _VIDEO_LOGGER.warning("Failed to greet %s: %s", identity, exc)
                return False
        return False

    def _handle_participant_disconnected(self, participant: Any) -> None:
        identity = getattr(participant, "identity", None)
        sid = getattr(participant, "sid", None)
        if identity is None:
            return

        if sid:
            self._greeted_sids.discard(sid)
            self._inflight_initializations.discard(sid)
        
        linked = self._room_io.linked_participant
        if linked is None:
            return

        if getattr(linked, "identity", None) == identity:
            self._room_io.unset_participant()

        self._maybe_schedule_shutdown()

    def _maybe_schedule_shutdown(self) -> None:
        if not self._terminate_on_empty:
            return

        connected_participants = [
            participant
            for participant in self._ctx.room.remote_participants.values()
            if getattr(participant, "is_connected", True)
        ]
        if connected_participants:
            return

        if self._shutdown_task is not None:
            return

        async def _shutdown() -> None:
            try:
                if self._shutdown_delay:
                    await asyncio.sleep(self._shutdown_delay)
                remaining = [
                    participant
                    for participant in self._ctx.room.remote_participants.values()
                    if getattr(participant, "is_connected", True)
                ]
                if remaining:
                    return
                if self._close_room_on_empty and _lk_api is not None:
                    try:
                        await self._ctx.api.room.delete_room(
                            _lk_api.DeleteRoomRequest(room=self._ctx.room.name)
                        )
                    except Exception:
                        pass
                self._ctx.shutdown("room-empty")
            finally:
                self._shutdown_task = None

        self._shutdown_task = asyncio.create_task(_shutdown(), name="voice-agent.shutdown")


__all__ = ["ParticipantGreeter"]
