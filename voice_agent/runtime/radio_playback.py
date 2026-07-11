import asyncio
import audioop
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

_LOGGER = logging.getLogger("voice-agent.radio")

_SAMPLE_RATE = int(os.getenv("VOICE_AGENT_RADIO_SAMPLE_RATE", "48000"))
_NUM_CHANNELS = int(os.getenv("VOICE_AGENT_RADIO_CHANNELS", "2"))
_FRAME_MS = int(os.getenv("VOICE_AGENT_RADIO_FRAME_MS", "20"))
_QUEUE_SIZE_MS = int(os.getenv("VOICE_AGENT_RADIO_QUEUE_SIZE_MS", "1500"))
_BYTES_PER_SAMPLE = 2
_CHUNK_SIZE = int(_SAMPLE_RATE * _NUM_CHANNELS * _BYTES_PER_SAMPLE * (_FRAME_MS / 1000))
_MIN_VOLUME = float(os.getenv("VOICE_AGENT_RADIO_MIN_VOLUME", "0.0"))
_MAX_VOLUME = float(os.getenv("VOICE_AGENT_RADIO_MAX_VOLUME", "10.0"))
_DEFAULT_VOLUME = float(os.getenv("VOICE_AGENT_RADIO_DEFAULT_VOLUME", "1.0"))
_PLAYBACK_MODE = os.getenv("VOICE_AGENT_RADIO_PLAYBACK_MODE", "client").strip().lower()
_RADIO_TOPIC = os.getenv("VOICE_AGENT_RADIO_DATA_TOPIC", "radio-control")


def _clamp_volume(value: float) -> float:
    return max(_MIN_VOLUME, min(_MAX_VOLUME, value))


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
    volume: float = _DEFAULT_VOLUME
    playback_rate: float = 1.0


class RadioPlaybackController:
    """Control RSS/podcast audio playback.

    Default mode is client/browser playback: the agent publishes LiveKit data-channel
    commands and the web client plays audio directly in an HTMLAudioElement.
    Server-side LiveKit audio-track playback remains available through
    VOICE_AGENT_RADIO_PLAYBACK_MODE=server.
    """

    def __init__(self, room: Any, session: Any = None) -> None:
        self._room = room
        self._session = session
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task[None]] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._source: Any = None
        self._publication: Any = None
        self._volume = _clamp_volume(_DEFAULT_VOLUME)
        self._state = PlaybackState(volume=self._volume)

    @property
    def state(self) -> PlaybackState:
        return self._state

    def volume_percent(self) -> int:
        return int(round(self._volume * 100))

    def status_json(self) -> str:
        self._state.volume = self._volume
        data = dict(self._state.__dict__)
        data["playback_mode"] = _PLAYBACK_MODE
        data["volume_percent"] = self.volume_percent()
        return json.dumps(data, ensure_ascii=False)

    async def set_playback_rate(self, rate: float | int | str) -> str:
        try:
            raw = float(str(rate).replace("x", "").replace("×", "").strip())
        except Exception:
            return "Не зрозуміла швидкість. Скажи, наприклад: 1.25, 1.5, 2 або нормальна швидкість."
        safe_rate = max(0.25, min(3.0, raw))
        async with self._lock:
            self._state.playback_rate = safe_rate
            if self._client_mode():
                await self._publish_client_command("radio.rate", playback_rate=safe_rate, rate=safe_rate)
            return json.dumps({"action": "radio.rate", "playback_rate": safe_rate, "message": f"Швидкість відтворення: {safe_rate:g}x"}, ensure_ascii=False)

    async def seek_relative(self, seconds: float | int | str) -> str:
        try:
            value = float(str(seconds).replace("сек", "").replace("s", "").strip())
        except Exception:
            return "Не зрозуміла, на скільки перемотати. Скажи, наприклад: вперед на 30 секунд або назад на 2 хвилини."
        async with self._lock:
            if self._client_mode():
                await self._publish_client_command("radio.seek.by", seconds=value)
            return json.dumps({"action": "radio.seek.by", "seconds": value, "message": f"Перемотую на {value:g} секунд."}, ensure_ascii=False)

    async def seek_to_seconds(self, seconds: float | int | str) -> str:
        try:
            value = float(str(seconds).replace("сек", "").replace("s", "").strip())
        except Exception:
            return "Не зрозуміла позицію для перемотки."
        async with self._lock:
            if self._client_mode():
                await self._publish_client_command("radio.seek.to", seconds=max(0, value))
            return json.dumps({"action": "radio.seek.to", "seconds": max(0, value), "message": f"Переходжу на {max(0, value):g} секунд від початку."}, ensure_ascii=False)

    async def seek_to_percent(self, percent: float | int | str) -> str:
        try:
            value = float(str(percent).replace("%", "").strip())
        except Exception:
            return "Не зрозуміла відсоток для перемотки."
        if value > 1:
            value = value / 100.0
        value = max(0.0, min(1.0, value))
        async with self._lock:
            if self._client_mode():
                await self._publish_client_command("radio.seek.percent", percent=value)
            return json.dumps({"action": "radio.seek.percent", "percent": value, "message": f"Переходжу на {round(value * 100)}% запису."}, ensure_ascii=False)

    async def _publish_client_command(self, command: str, **payload: Any) -> None:
        message = {
            "source": "voice-agent",
            "type": command,
            "command": command,
            **payload,
        }
        await self._room.local_participant.publish_data(
            json.dumps(message, ensure_ascii=False),
            reliable=True,
            topic=_RADIO_TOPIC,
        )
        _LOGGER.info("Published client radio command: %s", command)

    def _client_mode(self) -> bool:
        return _PLAYBACK_MODE in {"client", "browser", "frontend"}

    async def _broadcast_volume_locked(self) -> None:
        if self._client_mode():
            await self._publish_client_command(
                "radio.volume",
                volume=self._volume,
                volume_percent=self.volume_percent(),
                title=self._state.title,
                audio_url=self._state.audio_url,
                page_url=self._state.page_url,
            )

    async def set_volume(self, volume: float | int | str) -> str:
        try:
            raw = float(str(volume).replace("%", "").strip())
        except Exception:
            return "Не зрозуміла рівень гучності. Скажи, наприклад: 50%, 100%, 200%, голосніше або тихіше."
        if raw > _MAX_VOLUME:
            raw = raw / 100.0
        async with self._lock:
            previous = self._volume
            self._volume = _clamp_volume(raw)
            self._state.volume = self._volume
            _LOGGER.info("Radio volume set: %.2f -> %.2f", previous, self._volume)
            await self._broadcast_volume_locked()
            return json.dumps(
                {
                    "action": "radio.volume",
                    "volume": self._volume,
                    "volume_percent": self.volume_percent(),
                    "message": f"Гучність відтворення: {self.volume_percent()}%",
                },
                ensure_ascii=False,
            )

    async def change_volume(self, delta: float | int | str) -> str:
        try:
            raw_delta = float(str(delta).replace("%", "").strip())
        except Exception:
            return "Не зрозуміла зміну гучності. Скажи: голосніше, тихіше, +50% або -50%."
        if abs(raw_delta) > 1:
            raw_delta = raw_delta / 100.0
        async with self._lock:
            previous = self._volume
            self._volume = _clamp_volume(self._volume + raw_delta)
            self._state.volume = self._volume
            _LOGGER.info("Radio volume changed additively: %.2f -> %.2f", previous, self._volume)
            await self._broadcast_volume_locked()
            return json.dumps(
                {
                    "action": "radio.volume",
                    "volume": self._volume,
                    "volume_percent": self.volume_percent(),
                    "message": f"Гучність відтворення: {self.volume_percent()}%",
                },
                ensure_ascii=False,
            )

    async def multiply_volume(self, factor: float | int | str) -> str:
        try:
            raw_factor = float(str(factor).replace("x", "").replace("×", "").strip())
        except Exception:
            return "Не зрозуміла множник гучності."
        async with self._lock:
            previous = self._volume
            self._volume = _clamp_volume(self._volume * raw_factor)
            self._state.volume = self._volume
            _LOGGER.info("Radio volume changed multiplicatively: %.2f -> %.2f", previous, self._volume)
            await self._broadcast_volume_locked()
            return json.dumps(
                {
                    "action": "radio.volume",
                    "volume": self._volume,
                    "volume_percent": self.volume_percent(),
                    "message": f"Гучність відтворення: {self.volume_percent()}%",
                },
                ensure_ascii=False,
            )

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
                volume=self._volume,
                playback_rate=self._state.playback_rate,
            )

            if self._client_mode():
                self._state.status = "playing"
                await self._publish_client_command(
                    "radio.play",
                    title=self._state.title,
                    audio_url=audio_url,
                    page_url=page_url or "",
                    volume=self._volume,
                    volume_percent=self.volume_percent(),
                    playback_rate=self._state.playback_rate,
                    metadata=metadata or {},
                )
                return json.dumps(
                    {
                        "action": "radio.playing_in_browser",
                        "title": self._state.title,
                        "audio_url": audio_url,
                        "page_url": page_url or "",
                        "volume_percent": self.volume_percent(),
                        "message": "Аудіо запущено у браузерному плеєрі. Не озвучуй зміст поверх нього.",
                    },
                    ensure_ascii=False,
                )

            self._task = asyncio.create_task(self._playback_loop(), name="radio-playback")
            return json.dumps(
                {
                    "action": "radio.playing",
                    "title": self._state.title,
                    "audio_url": audio_url,
                    "page_url": page_url or "",
                    "volume_percent": self.volume_percent(),
                    "message": "Аудіо запущено через LiveKit audio track.",
                },
                ensure_ascii=False,
            )

    async def stop(self) -> str:
        async with self._lock:
            was_playing = self._state.status in {"starting", "playing", "paused"}
            await self._stop_locked(reason="user_stop")
            if self._client_mode():
                await self._publish_client_command("radio.stop")
            return "Зупинила відтворення." if was_playing else "Зараз нічого не відтворюється."

    async def pause(self) -> str:
        async with self._lock:
            self._state.status = "paused"
            if self._client_mode():
                await self._publish_client_command("radio.pause")
            return "Поставила на паузу."

    async def resume(self) -> str:
        async with self._lock:
            self._state.status = "playing"
            if self._client_mode():
                await self._publish_client_command("radio.resume")
            return "Продовжую відтворення."

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
        if self._state.status in {"starting", "playing", "paused"}:
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
        options.source = rtc.TrackSource.SOURCE_SCREENSHARE_AUDIO
        options.dtx = False
        options.red = False
        publication = await self._room.local_participant.publish_track(track, options)
        self._publication = publication
        self._state.track_sid = getattr(publication, "sid", "") or ""
        return self._source

    async def _start_ffmpeg(self) -> asyncio.subprocess.Process:
        ffmpeg_bin = os.getenv("VOICE_AGENT_FFMPEG_BIN", "ffmpeg")
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
            self._state.audio_url,
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
                volume = self._volume
                if abs(volume - 1.0) > 0.001:
                    chunk = audioop.mul(chunk, _BYTES_PER_SAMPLE, volume)
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
