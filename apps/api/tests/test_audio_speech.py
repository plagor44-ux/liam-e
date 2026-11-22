"""Only the visible, finished assistant response may be sent to the speech provider."""

import base64
import json
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import Response

from conftest import build_app
from db.accessors import chat_context, chat_conversation, chat_message
from db.models.chat_context import ChatContext
from db.models.chat_conversation import ChatConversation
from db.models.chat_message import ChatMessage

PATH = "/api/v1/audio/speech"
MODEL = "voxtral-mini-tts-3-0"
CONVERSATION = "test-conversation"
MESSAGE = "test-message"
REFERENCE = {"conversation_id": CONVERSATION, "message_id": MESSAGE}
REPLY = "# Nachricht\n\nDas ist **synthetisch**. [Ansehen](https://example.org/test)\n\n<script>unsichtbar</script>"
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00synthetic-media-fixture"


@pytest.fixture
def speech_client(monkeypatch) -> TestClient:
    from api.audio.access import get_audio_owner_verifier
    from env.audio import env
    from env.mistral import env as mistral

    async def owner(request):
        return True

    monkeypatch.setattr(env, "audio_stt_verification", None)
    monkeypatch.setattr(env, "audio_tts_model", MODEL)
    monkeypatch.setattr(env, "audio_tts_verification", json.dumps({
        "model": MODEL, "checked_at": "2026-01-01T10:00:00Z", "scope": "studio_release",
        "outcome": "verified", "formats": ["audio/mpeg"],
        "app_limits": {"max_input_characters": 500, "max_response_bytes": 1_000_000, "timeout_seconds": 20},
    }))
    monkeypatch.setattr(mistral, "mistral_api_key", "test-only")
    monkeypatch.setattr(chat_conversation, "get_conversation", AsyncMock(return_value=ChatConversation(id=CONVERSATION)))
    monkeypatch.setattr(chat_message, "get_message", AsyncMock(return_value=ChatMessage(
        id=MESSAGE, conversation_id=CONVERSATION, role="assistant", context_id="test-context", content=REPLY,
    )))
    monkeypatch.setattr(chat_context, "get_context", AsyncMock(return_value=ChatContext(
        id="test-context", conversation_id=CONVERSATION, revoked=False, mail_scope_version=1,
    )))
    monkeypatch.setattr(chat_context, "is_live", AsyncMock(return_value=True))
    app = build_app()
    app.dependency_overrides[get_audio_owner_verifier] = lambda: owner
    return TestClient(app)


def test_speech_rejects_unverified_owner(client: TestClient):
    response = client.post(PATH, content=b"synthetic invalid body")

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_live_speech_requires_test_opt_in_and_still_uses_saved_reply(speech_client, monkeypatch):
    from contextlib import asynccontextmanager
    from env.audio import env
    from api.routers.api.v1.audio.live import speech

    calls = []
    class FakeSpeech:
        async def append(self, text): calls.append(text)
        async def finish(self): pass
        async def receive(self):
            return next(self.frames)
        frames = iter([b"\x00\x01", None])

    @asynccontextmanager
    async def fake_live(kind, **kwargs):
        assert kind == "speech"
        yield FakeSpeech()

    monkeypatch.setattr(speech, "mistral_live", fake_live)
    monkeypatch.setattr(env, 'audio_studio_owner', json.dumps({'user_id': '00000000-0000-0000-0000-000000000001', 'organization_id': '00000000-0000-0000-0000-000000000002', 'workspace_id': '00000000-0000-0000-0000-000000000003', 'origin': 'https://test.example'}))
    monkeypatch.setattr(env, 'audio_live_test_origin', 'https://test.example')
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(env, 'audio_live_test_until', (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    headers = {'origin': 'https://test.example'}
    assert speech_client.post('/api/v1/audio/live/speech', json=REFERENCE, headers=headers).status_code == 503
    monkeypatch.setattr(env, "audio_live_test_enabled", True)
    assert speech_client.post('/api/v1/audio/live/speech', json=REFERENCE, headers={'origin': 'https://other.example'}).status_code == 503
    response = speech_client.post('/api/v1/audio/live/speech', json=REFERENCE, headers=headers)
    assert response.status_code == 200
    assert response.content == b"\x00\x01"
    assert calls == ["Nachricht Das ist synthetisch. Ansehen"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.anyio
@pytest.mark.parametrize('over_limit', [False, True])
async def test_live_speech_float_pcm_byte_limit(monkeypatch, over_limit):
    from contextlib import asynccontextmanager
    from api.routers.api.v1.audio.live import speech

    class FakeSpeech:
        def __init__(self):
            # Two chunks cross the previous 12 MB limit; an optional extra sample crosses 24 MB.
            self.frames = iter([b'\0' * 12_000_000, b'\0' * 12_000_000] + ([b'\0' * 4] if over_limit else []) + [None])
        async def append(self, text): pass
        async def finish(self): pass
        async def receive(self): return next(self.frames)

    @asynccontextmanager
    async def fake_live(kind, **kwargs):
        assert kind == 'speech'
        yield FakeSpeech()

    monkeypatch.setattr(speech, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(speech, '_speech_reference', AsyncMock(return_value=speech.SpeechReference(**REFERENCE)))
    monkeypatch.setattr(speech, '_visible_reply', AsyncMock(return_value='Synthetic reply'))
    monkeypatch.setattr(speech, 'mistral_live', fake_live)
    response = await speech.synthesize(None)
    sizes = []
    if over_limit:
        with pytest.raises(RuntimeError, match='audio_unavailable'):
            async for chunk in response.body_iterator:
                sizes.append(len(chunk))
    else:
        async for chunk in response.body_iterator:
            sizes.append(len(chunk))
    assert sizes == [12_000_000, 12_000_000]


def test_speech_release_gate_is_independent_and_fails_closed(speech_client, monkeypatch):
    from env.audio import env

    monkeypatch.setattr(env, "audio_tts_verification", None)
    response = speech_client.post(PATH, content=b"synthetic invalid body")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "audio_unavailable"


def test_speech_sends_visible_markdown_as_text_and_serves_mp3(speech_client):
    with respx.mock(assert_all_mocked=True) as outbound:
        provider = outbound.post("https://api.mistral.ai/v1/audio/speech").mock(return_value=Response(
            200, json={"audio_data": base64.b64encode(MP3).decode()},
        ))
        response = speech_client.post(PATH, json=REFERENCE)

    assert response.status_code == 200
    assert response.content == MP3
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["cache-control"] == "no-store"
    assert provider.call_count == 1
    assert json.loads(provider.calls[0].request.content) == {
        "input": "Nachricht Das ist synthetisch. Ansehen",
        "voice_id": "voice_478", "model": MODEL, "response_format": "mp3",
    }


@pytest.mark.parametrize("changes", [
    {"role": "user"}, {"role": "proposal"}, {"stopped": True}, {"tool_payload_json": "[]"},
    {"conversation_id": "another-conversation"}, {"content": ""}, {"context_id": None},
])
def test_ineligible_replies_never_reach_the_provider(speech_client, changes):
    lookup = chat_message.get_message
    lookup.return_value = lookup.return_value.model_copy(update=changes)
    with respx.mock(assert_all_mocked=True) as outbound:
        response = speech_client.post(PATH, json=REFERENCE)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "audio_message_unavailable"
    assert not outbound.calls


def test_revoked_message_never_reaches_the_provider(speech_client, monkeypatch):
    chat_message.get_message.return_value.context_id = "test-context"
    monkeypatch.setattr(chat_context, "get_context", AsyncMock(return_value=ChatContext(
        id="test-context", conversation_id=CONVERSATION, revoked=True,
    )))
    with respx.mock(assert_all_mocked=True) as outbound:
        response = speech_client.post(PATH, json=REFERENCE)
    assert response.status_code == 404
    assert not outbound.calls


@pytest.mark.parametrize("body", [
    REFERENCE | {"text": "A request cannot supply speech text."},
    REFERENCE | {"voice_id": "an-arbitrary-voice"},
])
def test_speech_does_not_accept_text_or_provider_configuration(speech_client, body):
    with respx.mock(assert_all_mocked=True) as outbound:
        response = speech_client.post(PATH, json=body)

    assert response.status_code == 422
    assert "A request cannot supply speech text." not in response.text
    assert "an-arbitrary-voice" not in response.text
    assert not outbound.calls


def test_speech_rejects_visible_text_over_the_limit_without_truncating(speech_client):
    chat_message.get_message.return_value.content = "x" * 501
    with respx.mock(assert_all_mocked=True) as outbound:
        response = speech_client.post(PATH, json=REFERENCE)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "audio_text_too_long"
    assert not outbound.calls


@pytest.mark.parametrize(("upstream", "status", "code"), [
    (Response(200, json={"audio_data": "invalid base64"}), 503, "audio_unavailable"),
    (Response(429, json={"detail": "provider-only"}), 429, "audio_rate_limited"),
    (Response(504, json={"detail": "provider-only"}), 504, "audio_timeout"),
])
def test_speech_redacts_provider_failures(speech_client, upstream, status, code):
    with respx.mock(assert_all_mocked=True) as outbound:
        provider = outbound.post("https://api.mistral.ai/v1/audio/speech").mock(return_value=upstream)
        response = speech_client.post(PATH, json=REFERENCE)

    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert "provider-only" not in response.text
    assert provider.call_count == 1


def test_speech_rejects_a_response_larger_than_the_attested_limit(speech_client, monkeypatch):
    from env.audio import env

    attestation = json.loads(env.audio_tts_verification)
    attestation["app_limits"]["max_response_bytes"] = len(MP3) - 1
    monkeypatch.setattr(env, "audio_tts_verification", json.dumps(attestation))
    with respx.mock(assert_all_mocked=True) as outbound:
        outbound.post("https://api.mistral.ai/v1/audio/speech").mock(return_value=Response(
            200, json={"audio_data": base64.b64encode(MP3).decode()},
        ))
        response = speech_client.post(PATH, json=REFERENCE)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "audio_unavailable"

@pytest.mark.anyio
async def test_rest_speech_rejects_tag_over_attested_limit(monkeypatch):
    from api.audio import provider
    from api.audio.capabilities import SpeechLimitsOut
    request = AsyncMock()
    monkeypatch.setattr(provider, '_audio_request', request)
    limits = SpeechLimitsOut(max_input_characters=500, max_response_bytes=1000, timeout_seconds=20)
    with pytest.raises(HTTPException) as error:
        await provider.MistralAudio().synthesize('x' * 500, model=MODEL, limits=limits, locale='de', pace='slow')
    assert error.value.detail["code"] == "audio_text_too_long"
    request.assert_not_awaited()


@pytest.mark.anyio
async def test_live_speech_rejects_tag_over_provider_limit(monkeypatch, ui_locale_in_browser_mode):
    from api.routers.api.v1.audio.live import speech
    ui_locale_in_browser_mode.return_value.speech_pace = 'slow'
    monkeypatch.setattr(speech, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(speech, '_speech_reference', AsyncMock(return_value=speech.SpeechReference(**REFERENCE)))
    monkeypatch.setattr(speech, '_visible_reply', AsyncMock(return_value='x' * 8000))
    with pytest.raises(HTTPException) as error:
        await speech.synthesize(None)
    assert error.value.detail["code"] == "audio_text_too_long"

@pytest.mark.parametrize(('pace', 'tag'), [('slow', '[pace=slow]'), ('normal', ''), ('fast', '[pace=fast]')])
def test_pace_tag_counts_towards_limit_at_exact_boundary(pace, tag):
    from api.audio.pace import paced_text
    assert len(paced_text('x' * (500 - len(tag)), pace, 500)) == 500
    with pytest.raises(ValueError, match='audio_text_too_long'):
        paced_text('x' * (501 - len(tag)), pace, 500)
