import asyncio
import contextlib
import logging
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
        self._shutdown_delay = max(0.0, shutdown_delay)
        self._greeting_delay = max(0.0, greeting_delay)
        self._greeted_identities: set[str] = set()
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
        """
        Periodically reconcile remote participants to guard against missed events.
        """

        try:
            while True:
                await asyncio.sleep(interval)
                participants_snapshot = list(self._ctx.room.remote_participants.values())
                for participant in participants_snapshot:
                    identity = getattr(participant, "identity", None)
                    if (
                        not identity
                        or identity in self._greeted_identities
                        or identity in self._inflight_initializations
                    ):
                        continue
                    self._handle_participant_connected(participant)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            _VIDEO_LOGGER.debug("Remote participant poll failed: %s", exc)
            await asyncio.sleep(interval)

    def _handle_participant_connected(self, participant: Any) -> None:
        if self._shutdown_task:
            self._shutdown_task.cancel()
            self._shutdown_task = None

        identity = getattr(participant, "identity", None)
        if identity is None:
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

        asyncio.create_task(self._initialize_participant(identity))

    async def _initialize_participant(self, identity: str) -> None:
        try:
            if not self._session.input.audio_enabled:
                self._session.input.set_audio_enabled(True)
        except Exception as exc:  # pragma: no cover - defensive
            _VIDEO_LOGGER.debug("Failed to ensure audio input enabled before wait: %s", exc)

        try:
            await self._wait_for_media_ready(identity, broadcast=self._broadcast_mode)
            media_ready = True
        except TimeoutError as exc:
            _VIDEO_LOGGER.warning("Media for %s not ready: %s", identity, exc)
            media_ready = False
        except Exception as exc:  # pragma: no cover - best effort logging
            _VIDEO_LOGGER.warning("Unexpected media wait failure for %s: %s", identity, exc)
            media_ready = False
        else:
            await asyncio.sleep(0)

        try:
            if not self._session.input.audio_enabled:
                self._session.input.set_audio_enabled(True)
        except Exception as exc:  # pragma: no cover - defensive
            _VIDEO_LOGGER.debug("Failed to ensure audio input enabled: %s", exc)

        if identity in self._greeted_identities:
            return

        self._inflight_initializations.add(identity)

        if self._greeting_delay:
            await asyncio.sleep(self._greeting_delay)

        if not media_ready:
            _VIDEO_LOGGER.debug(
                "Greeting %s without confirmed media readiness.", identity
            )
        greeted = await self._send_greeting(identity)
        if greeted:
            self._greeted_identities.add(identity)
        self._inflight_initializations.discard(identity)

    async def _wait_for_media_ready(
        self,
        identity: str,
        timeout: float = 10.0,
        *,
        broadcast: bool,
    ) -> None:
        """
        Wait until RoomIO links to the participant and the media pipelines are live.
        This avoids greeting the user before audio input/output are ready.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            linked = self._room_io.linked_participant
            audio_input = self._room_io.audio_input

            audio_ready = audio_input is not None
            if audio_input is not None:
                audio_ready = True
                if _lk_rtc is not None:
                    source = getattr(audio_input, "publication_source", None)
                    if source is None:
                        audio_ready = False
                forward_task = getattr(audio_input, "_forward_atask", None)
                if forward_task is None:
                    audio_ready = False

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
                raise TimeoutError(
                    f"Timed out waiting for media streams for participant '{identity}'."
                )
            await asyncio.sleep(0.1)

        subscribed = self._room_io.subscribed_fut
        if subscribed is not None and not subscribed.done():
            try:
                await asyncio.wait_for(asyncio.shield(subscribed), timeout)
            except asyncio.TimeoutError:
                _VIDEO_LOGGER.warning(
                    "Timed out waiting for LiveKit to subscribe to agent audio for %s",
                    identity,
                )

    async def _send_greeting(self, identity: str) -> bool:
        """
        Deliver the welcome message to the participant.
        Retries if the realtime backend is still spinning up.
        Returns True if the greeting was delivered.
        """

        max_attempts = 3
        fallback_used = False
        for attempt in range(1, max_attempts + 1):
            try:
                if fallback_used:
                    handle = self._session.say(self._greeting_text)
                else:
                    handle = self._session.generate_reply(user_input=self._greeting_text)
                await handle.wait_for_playout()
                return True
            except RealtimeError as exc:
                backoff = 0.6 * attempt
                _VIDEO_LOGGER.warning(
                    "Greeting attempt %s for %s failed due to realtime timeout: %s (retry in %.1fs)",
                    attempt,
                    identity,
                    exc,
                    backoff if attempt < max_attempts else 0.0,
                )
                if attempt == max_attempts - 1 and not fallback_used:
                    fallback_used = True
                    continue
                if attempt >= max_attempts:
                    return False
                await asyncio.sleep(backoff)
            except Exception as exc:  # pragma: no cover - best effort logging
                _VIDEO_LOGGER.warning("Failed to send greeting to %s: %s", identity, exc)
                return False
        return False

    def _handle_participant_disconnected(self, participant: Any) -> None:
        identity = getattr(participant, "identity", None)
        if identity is None:
            return

        self._greeted_identities.discard(identity)
        self._inflight_initializations.discard(identity)
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
                        _VIDEO_LOGGER.info(
                            "Closed LiveKit room '%s' after last participant left.",
                            self._ctx.room.name,
                        )
                    except Exception as exc:  # pragma: no cover - best effort logging
                        _VIDEO_LOGGER.warning(
                            "Failed to close room '%s': %s", self._ctx.room.name, exc
                        )
                else:
                    _VIDEO_LOGGER.info(
                        "All participants left the room; shutting down the agent worker."
                    )
                self._ctx.shutdown("room-empty")
            finally:
                self._shutdown_task = None

        self._shutdown_task = asyncio.create_task(_shutdown(), name="voice-agent.shutdown")


__all__ = ["ParticipantGreeter"]
