import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

_LOGGER = logging.getLogger("voice-agent.radio")

_SAMPLE_RATE = int(os.getenv("VOICE_AGENT_RADIO_SAMPLE_RATE", "48000"))
_NUM_CHANNELS = int(os.getenv("VOICE_AGENT_RADIO_CHANNELS", "1"))
_FRAME_MS = int(os.getenv("VOICE_AGENT_RADIO_FRAME_MS", "20"))
_QUEUE_SIZE_MS = int(os.getenv("VOICE_AGENT_RADIO_QUEUE_SIZE_MS", "1500"))
_BYTES_PER_SAMPLE = 2
_CHUNK_SIZE = int(_SAMPLE_RATE * _NUM_CHANNELS * _BYTES_PER_SAMPLE * (_FRAME_MS / 1000))


@dataclass
class PlaybackState:
    status: str = "idle"
    title: str = ""
    audio_url: str = ""
    page_url: str = ""
    started_at: float = 0.0
    error: str = ""
    track_sid: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class RadioPlaybackController:
    """Publish RSS/podcast audio into the LiveKit room as a separate audio track."""

    def __init__(self, room: Any, session: Any = None) -> None:
        self._room = room
        self._session = session
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task[None]] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._source: Any = None
        self._publication: Any = None
        self._state = PlaybackState()

    @property
    def state(self) -> PlaybackState:
        return self._state

    def status_json(self) -> str:
        return json.dumps(self._state.__dict__, ensure_ascii=False)

    async def play(self, *, audio_url: str, title: str = "", page_url: str = "", metadata: Optional[dict[str, Any]] = None) -> str:
        audio_url = (audio_url or "").strip()
        if not audio_url:
            return "Немає audio_url для програвання."

        async with self._lock:
            await self._stop_locked(reason="replace")
            self._state = PlaybackState(
                status="starting",
                title=title or "Аудіо",
                audio_url=audio_url,
                page_url=page_url or "",
                started_at=asyncio.get_running_loop().time(),
                metadata=metadata or {},
            )
            self._task = asyncio.create_task(self._playback_loop(), name="radio-playback")
            return json.dumps(
                {
                    "action": "radio.playing",
                    "title": self._state.title,
                    "audio_url": audio_url,
                    "page_url": page_url or "",
                    "message": "Аудіо запущено. Не озвучуй зміст поверх нього. Чекай команди користувача або завершення програвання.",
                },
                ensure_ascii=False,
            )

    async def stop(self) -> str:
        async with self._lock:
            was_playing = self._state.status in {"starting", "playing"}
            await self._stop_locked(reason="user_stop")
            return "Зупинила відтворення." if was_playing else "Зараз нічого не відтворюється."

    async def aclose(self) -> None:
        async with self._lock:
            await self._stop_locked(reason="close")

    async def _stop_locked(self, *, reason: str) -> None:
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()

        process = self._process
        self._process = None
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        if self._source is not None:
            try:
                self._source.clear_queue()
            except Exception:
                pass

        if task and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                _LOGGER.warning("Radio playback task failed while stopping: %s", exc)

        await self._unpublish_track()
        if self._state.status in {"starting", "playing"}:
            self._state.status = "stopped"
            self._state.error = reason

    async def _unpublish_track(self) -> None:
        publication = self._publication
        self._publication = None
        track_sid = getattr(publication, "sid", "") if publication is not None else ""
        if track_sid:
            try:
                await self._room.local_participant.unpublish_track(track_sid)
            except Exception as exc:
                _LOGGER.warning("Failed to unpublish radio track %s: %s", track_sid, exc)
        if self._source is not None:
            try:
                await self._source.aclose()
            except Exception:
                pass
            self._source = None

    async def _publish_track(self) -> Any:
        from livekit import rtc  # type: ignore

        self._source = rtc.AudioSource(_SAMPLE_RATE, _NUM_CHANNELS, queue_size_ms=_QUEUE_SIZE_MS)
        track = rtc.LocalAudioTrack.create_audio_track("radio-playback", self._source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        publication = await self._room.local_participant.publish_track(track, options)
        self._publication = publication
        self._state.track_sid = getattr(publication, "sid", "") or ""
        return self._source

    async def _start_ffmpeg(self) -> asyncio.subprocess.Process:
        ffmpeg_bin = os.getenv("VOICE_AGENT_FFMPEG_BIN", "ffmpeg")
        url = self._state.audio_url
        args = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            os.getenv("VOICE_AGENT_FFMPEG_LOGLEVEL", "error"),
            "-nostdin",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-i",
            url,
            "-vn",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            str(_NUM_CHANNELS),
            "-ar",
            str(_SAMPLE_RATE),
            "pipe:1",
        ]
        return await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _playback_loop(self) -> None:
        from livekit import rtc  # type: ignore

        try:
            source = await self._publish_track()
            process = await self._start_ffmpeg()
            self._process = process
            self._state.status = "playing"

            if process.stdout is None:
                raise RuntimeError("ffmpeg stdout is not available")

            while True:
                chunk = await process.stdout.readexactly(_CHUNK_SIZE)
                frame = rtc.AudioFrame(
                    data=chunk,
                    sample_rate=_SAMPLE_RATE,
                    num_channels=_NUM_CHANNELS,
                    samples_per_channel=int(_SAMPLE_RATE * (_FRAME_MS / 1000)),
                )
                await source.capture_frame(frame)
        except asyncio.IncompleteReadError:
            self._state.status = "finished"
        except asyncio.CancelledError:
            raise
        except FileNotFoundError:
            self._state.status = "error"
            self._state.error = "ffmpeg не знайдено. Встанови ffmpeg або задай VOICE_AGENT_FFMPEG_BIN."
            _LOGGER.exception("ffmpeg binary not found")
        except Exception as exc:
            self._state.status = "error"
            self._state.error = str(exc)
            _LOGGER.exception("Radio playback failed")
        finally:
            process = self._process
            self._process = None
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

            final_status = self._state.status
            if self._source is not None:
                try:
                    await self._source.wait_for_playout()
                except Exception:
                    pass
            await self._unpublish_track()
            if final_status == "finished":
                await self._announce_finished()

    async def _announce_finished(self) -> None:
        if os.getenv("VOICE_AGENT_RADIO_ANNOUNCE_FINISHED", "1").strip().lower() in {"0", "false", "no", "off"}:
            return
        if self._session is None or not hasattr(self._session, "say"):
            return
        try:
            await self._session.say("Відтворення завершено. Увімкнути щось інше?")
        except Exception as exc:
            _LOGGER.warning("Failed to announce radio playback finish: %s", exc)


__all__ = ["RadioPlaybackController", "PlaybackState"]
