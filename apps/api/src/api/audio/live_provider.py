"""Bounded Mistral realtime protocol, independent of the browser transport."""

import base64
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from websockets.asyncio.client import connect
from utils.mistral import mistral_app_token


class LiveAudioError(Exception):
    def __init__(self) -> None:
        super().__init__("audio_unavailable")


async def _event(socket: Any) -> dict:
    try:
        raw = await socket.recv()
        if not isinstance(raw, str) or len(raw) > 128_000:
            raise LiveAudioError()
        event = json.loads(raw)
        if not isinstance(event, dict) or event.get("type") == "error":
            raise LiveAudioError()
        return event
    except (ValueError, TypeError):
        raise LiveAudioError() from None


async def _send(socket: Any, value: dict) -> None:
    await socket.send(json.dumps(value, separators=(",", ":")))


class LiveTranscription:
    def __init__(self, socket: Any):
        self.socket = socket

    async def start(self) -> None:
        if (await _event(self.socket)).get("type") != "session.created":
            raise LiveAudioError()
        await _send(self.socket, {"type": "session.update", "session": {
            "audio_format": {"encoding": "pcm_s16le", "sample_rate": 24000},
            "target_streaming_delay_ms": 480, "turn_detection": {},
        }})
        event = await _event(self.socket)
        settings = event.get("session")
        if (event.get("type") != "session.updated" or not isinstance(settings, dict)
                or settings.get("audio_format") != {"encoding": "pcm_s16le", "sample_rate": 24000}
                or not isinstance(settings.get("turn_detection"), dict)):
            raise LiveAudioError()

    async def append(self, audio: bytes) -> None:
        if not audio or len(audio) > 24_000 or len(audio) % 2:
            raise LiveAudioError()
        await _send(self.socket, {"type": "input_audio.append", "audio": base64.b64encode(audio).decode("ascii")})

    async def finish(self) -> None:
        await _send(self.socket, {"type": "input_audio.end"})

    async def receive(self) -> dict:
        event = await _event(self.socket)
        kind = event.get("type")
        if kind in {"turn.start", "turn.end", "turn.eager_end", "turn.resume"}:
            time = event.get("time_s")
            if not isinstance(time, (int, float)) or isinstance(time, bool) or not 0 <= time <= 3600:
                raise LiveAudioError()
            return {"type": kind, "time_s": time}
        if kind in {"transcription.text.delta", "transcription.done"}:
            text = event.get("text")
            if not isinstance(text, str) or len(text) > 8000:
                raise LiveAudioError()
            return {"type": kind, "text": text}
        raise LiveAudioError()


class LiveSpeech:
    def __init__(self, socket: Any):
        self.socket = socket

    async def start(self) -> None:
        if (await _event(self.socket)).get("type") != "session.created":
            raise LiveAudioError()
        await _send(self.socket, {"type": "session.update", "session": {
            "voice": "voice_481", "response_format": "pcm", "streaming_mode": "responsive",
        }})
        event = await _event(self.socket)
        settings = event.get("session")
        if (event.get("type") != "session.updated" or not isinstance(settings, dict)
                or settings.get("response_format") != "pcm" or settings.get("voice") != "voice_481"):
            raise LiveAudioError()

    async def append(self, text: str) -> None:
        if not text or len(text) > 8000:
            raise LiveAudioError()
        await _send(self.socket, {"type": "input_text.append", "text": text})

    async def finish(self) -> None:
        await _send(self.socket, {"type": "input_text.flush"})

    async def receive(self) -> bytes | None:
        event = await _event(self.socket)
        if event.get("type") == "speech.audio.done":
            return None
        encoded = event.get("audio_data")
        if event.get("type") != "speech.audio.delta" or not isinstance(encoded, str) or len(encoded) > 96_000:
            raise LiveAudioError()
        try:
            audio = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise LiveAudioError() from None
        # PCM bytes may be split at any byte boundary between provider events.
        if not audio:
            raise LiveAudioError()
        return audio


@asynccontextmanager
async def mistral_live(kind: str) -> AsyncIterator[LiveTranscription | LiveSpeech]:
    if kind == "transcription":
        path, model, cls = "transcriptions", "voxtral-mini-transcribe-realtime-3", LiveTranscription
    elif kind == "speech":
        path, model, cls = "speech", "voxtral-mini-tts-3-0", LiveSpeech
    else:
        raise LiveAudioError()
    async with connect(
        f"wss://api.mistral.ai/v1/audio/{path}/realtime?model={model}",
        additional_headers={"Authorization": f"Bearer {mistral_app_token()}"},
        proxy=None, max_size=128_000, open_timeout=10, close_timeout=3,
    ) as socket:
        session = cls(socket)
        await session.start()
        yield session
