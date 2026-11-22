"""Protocol tests use synthetic frames and never contact Mistral."""
import json

import pytest

from api.audio.live_provider import LiveAudioError, LiveTranscription, LiveSpeech


class Socket:
    def __init__(self, events):
        self.events = iter(json.dumps(event) for event in events)
        self.sent = []

    async def recv(self):
        return next(self.events)

    async def send(self, value):
        self.sent.append(json.loads(value))


async def test_transcription_configures_pcm_and_forwards_bounded_audio():
    socket = Socket([{"type": "session.created"}, {"type": "session.updated", "session": {"audio_format": {"encoding": "pcm_s16le", "sample_rate": 24000}, "turn_detection": {}}}, {"type": "turn.start", "time_s": 1.0}, {"type": "transcription.text.delta", "text": "Hallo"}])
    session = LiveTranscription(socket)
    await session.start()
    await session.append(b"\x00\x01")
    assert socket.sent == [
        {"type": "session.update", "session": {"audio_format": {"encoding": "pcm_s16le", "sample_rate": 24000}, "target_streaming_delay_ms": 480, "turn_detection": {}}},
        {"type": "input_audio.append", "audio": "AAE="},
    ]
    assert await session.receive() == {"type": "turn.start", "time_s": 1.0}
    assert await session.receive() == {"type": "transcription.text.delta", "text": "Hallo"}
    with pytest.raises(LiveAudioError):
        await session.append(b"x" * 50000)


async def test_speech_streams_validated_audio_and_does_not_echo_provider_error():
    socket = Socket([{"type": "session.created"}, {"type": "session.updated", "session": {"response_format": "pcm", "voice": "voice_481"}}, {"type": "speech.audio.delta", "audio_data": "AAA="}, {"type": "speech.audio.delta", "audio_data": "AD8="}, {"type": "error", "error": {"message": "private content"}}])
    session = LiveSpeech(socket)
    await session.start()
    await session.append("Hallo")
    await session.finish()
    assert socket.sent == [{"type": "session.update", "session": {"voice": "voice_481", "response_format": "pcm", "streaming_mode": "responsive"}}, {"type": "input_text.append", "text": "Hallo"}, {"type": "input_text.flush"}]
    assert await session.receive() + await session.receive() == b"\x00\x00\x00\x3f"
    with pytest.raises(LiveAudioError, match="audio_unavailable") as error:
        await session.receive()
    assert "private content" not in str(error.value)
