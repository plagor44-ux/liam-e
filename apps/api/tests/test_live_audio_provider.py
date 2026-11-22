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
    session = LiveTranscription(socket, "de")
    await session.start()
    await session.append(b"\x00\x01")
    assert socket.sent == [
        {"type": "session.update", "session": {"audio_format": {"encoding": "pcm_s16le", "sample_rate": 24000}, "target_streaming_delay_ms": 480, "turn_detection": {}, "language": "de"}},
        {"type": "input_audio.append", "audio": "AAE="},
    ]
    assert await session.receive() == {"type": "turn.start", "time_s": 1.0}
    assert await session.receive() == {"type": "transcription.text.delta", "text": "Hallo"}
    with pytest.raises(LiveAudioError):
        await session.append(b"x" * 50000)


async def test_speech_streams_validated_audio_and_does_not_echo_provider_error():
    socket = Socket([{"type": "session.created"}, {"type": "session.updated", "session": {"response_format": "pcm", "voice": "voice_481"}}, {"type": "speech.audio.delta", "audio_data": "AAA="}, {"type": "speech.audio.delta", "audio_data": "AD8="}, {"type": "error", "error": {"message": "private content"}}])
    session = LiveSpeech(socket, "de")
    await session.start()
    await session.append("Hallo")
    await session.finish()
    assert socket.sent == [{"type": "session.update", "session": {"voice": "voice_481", "response_format": "pcm", "streaming_mode": "responsive"}}, {"type": "input_text.append", "text": "Hallo"}, {"type": "input_text.flush"}]
    assert await session.receive() + await session.receive() == b"\x00\x00\x00\x3f"
    with pytest.raises(LiveAudioError, match="audio_unavailable") as error:
        await session.receive()
    assert "private content" not in str(error.value)


async def test_speech_accepts_one_second_float32_provider_chunks():
    import base64
    pcm = bytes(24_000 * 4)
    encoded = base64.b64encode(pcm).decode('ascii')
    socket = Socket([{'type': 'speech.audio.delta', 'audio_data': encoded}] * 2 + [{'type': 'speech.audio.done'}])
    session = LiveSpeech(socket)
    assert await session.receive() == pcm
    assert await session.receive() == pcm
    assert await session.receive() is None


async def test_speech_rejects_a_chunk_over_the_bounded_float32_limit():
    import base64
    pcm = bytes(576_001)
    session = LiveSpeech(Socket([{'type': 'speech.audio.delta', 'audio_data': base64.b64encode(pcm).decode('ascii')}]))
    with pytest.raises(LiveAudioError):
        await session.receive()


async def test_speech_failure_logs_only_safe_classification(monkeypatch, caplog):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from api.routers.api.v1.audio.live import speech

    class Provider:
        async def append(self, text): pass
        async def finish(self): pass
        async def receive(self): raise ValueError('private provider contents')
    @asynccontextmanager
    async def fake_live(kind, **kwargs):
        yield Provider()
    monkeypatch.setattr(speech, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(speech, '_speech_reference', AsyncMock(return_value=speech.SpeechReference(conversation_id='c', message_id='m')))
    monkeypatch.setattr(speech, '_visible_reply', AsyncMock(return_value='Synthetic response'))
    monkeypatch.setattr(speech, 'mistral_live', fake_live)
    response = await speech.synthesize(object())
    with pytest.raises(RuntimeError, match='audio_unavailable'):
        _ = [chunk async for chunk in response.body_iterator]
    assert 'code=stream_failure class=ValueError' in caplog.text
    assert 'private provider contents' not in caplog.text
    assert 'Synthetic response' not in caplog.text

@pytest.mark.parametrize(('locale', 'voice'), [('de', 'voice_481'), ('en', 'voice_478')])
async def test_language_and_voice_are_fixed_before_audio(locale, voice):
    transcription = Socket([{'type': 'session.created'}, {'type': 'session.updated', 'session': {'audio_format': {'encoding': 'pcm_s16le', 'sample_rate': 24000}, 'turn_detection': {}}}])
    session = LiveTranscription(transcription, locale)
    await session.start()
    await session.append(b'\x00\x01')
    assert transcription.sent[0]['session']['language'] == locale
    assert [event['type'] for event in transcription.sent] == ['session.update', 'input_audio.append']
    speech = Socket([{'type': 'session.created'}, {'type': 'session.updated', 'session': {'response_format': 'pcm', 'voice': voice}}])
    await LiveSpeech(speech, locale).start()
    assert speech.sent[0]['session']['voice'] == voice

async def test_invalid_live_language_is_rejected():
    with pytest.raises(LiveAudioError):
        LiveTranscription(Socket([]), 'fr')
