"""A temporary device check uses the owner boundary; only its explicit smoke runs inference."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import logging

import pytest
import respx
from fastapi.testclient import TestClient

from api.audio.access import get_audio_owner_verifier
from api.audio.errors import audio_error
from api.audio.provider import get_audio_provider
from api.routers.api.v1.audio.device_test import route as device_route
from api.routers.api.v1.audio.transcriptions import get_transcription_slot
from conftest import build_app
from env.audio import env
from test_audio_transcriptions import REQUEST_ID, recording

PATH = "/api/v1/audio/device-test"


@pytest.fixture
def owner(monkeypatch) -> TestClient:
    async def verified_owner(request):
        return True

    @asynccontextmanager
    async def available():
        yield True

    def forbidden_provider():
        pytest.fail("The device test must not use the live audio provider")

    monkeypatch.setattr(env, "audio_device_test_until",
                        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    monkeypatch.setattr(env, "audio_stt_verification", None)
    monkeypatch.setattr(env, "audio_tts_verification", None)
    app = build_app()
    app.dependency_overrides[get_audio_owner_verifier] = lambda: verified_owner
    app.dependency_overrides[get_transcription_slot] = lambda: available
    app.dependency_overrides[get_audio_provider] = forbidden_provider
    return TestClient(app)


def test_active_device_check_does_not_release_regular_audio(owner) -> None:
    response = owner.get(PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["format"] == "audio/webm;codecs=opus"
    assert response.json()["sample_text"] == "Eine gelbe Ente schwimmt im Teich."
    capabilities = owner.get("/api/v1/audio/capabilities").json()
    assert capabilities["stt"]["status"] == capabilities["tts"]["status"] == "unknown"


@pytest.mark.parametrize("until", [None, "invalid", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00"])
def test_all_device_routes_close_when_disabled_or_expired(owner, monkeypatch, until) -> None:
    monkeypatch.setattr(env, "audio_device_test_until", until)
    for path in (PATH, PATH + "/sample"):
        response = owner.get(path)
        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"
    assert owner.post(PATH + "/recording", content=b"ignored").status_code == 404
    assert owner.post(PATH + "/inference").status_code == 404


def test_device_check_routes_reject_unverified_callers(client) -> None:
    assert client.get(PATH).status_code == 401
    assert client.get(PATH + "/sample").status_code == 401
    assert client.post(PATH + "/recording", content=b"ignored").status_code == 401
    with respx.mock() as outbound:
        assert client.post(PATH + "/inference").status_code == 401
    assert not outbound.calls


def test_recording_uses_real_validation_but_returns_only_the_fixed_sample(owner, recording) -> None:
    with respx.mock(assert_all_mocked=True) as outbound:
        response = owner.post(
            PATH + "/recording", files={"file": ("sample.webm", recording, "audio/webm")},
            data={"request_id": REQUEST_ID, "language": "de"},
        )
    assert response.status_code == 200
    assert response.json() == {"request_id": REQUEST_ID, "text": "Eine gelbe Ente schwimmt im Teich."}
    assert response.headers["cache-control"] == "no-store"
    assert not outbound.calls


def test_invalid_recording_is_rejected_on_the_device_path(owner) -> None:
    response = owner.post(
        PATH + "/recording", files={"file": ("invalid.webm", b"not media", "audio/webm")},
        data={"request_id": REQUEST_ID},
    )
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "audio_format_unsupported"


def test_sample_download_serves_local_mpeg_audio(owner) -> None:
    with respx.mock(assert_all_mocked=True) as outbound:
        response = owner.get(PATH + "/sample")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "audio/mpeg"
    assert 1000 < len(response.content) < 32 * 1024
    assert not outbound.calls


SAMPLE = "Eine gelbe Ente schwimmt im Teich."
TRANSCRIPT = "eine gelbe Ente schwimmt im Teich"
MP3 = b"ID3" + b"\x00" * 2045


class FakeAudio:
    """Records the smoke's provider calls. Mistral is never contacted."""

    def __init__(self, *, transcript=TRANSCRIPT, stt_error=None, tts_error=None):
        self.transcript, self.stt_error, self.tts_error = transcript, stt_error, tts_error
        self.transcriptions: list[dict] = []
        self.syntheses: list[dict] = []

    async def transcribe(self, data, *, kind, model, language):
        self.transcriptions.append({"data": data, "kind": kind, "model": model, "language": language})
        if self.stt_error:
            raise audio_error(self.stt_error)
        return self.transcript, "voxtral-mini-transcribe-3-0"

    async def synthesize(self, text, *, model, limits, locale="en", pace="normal", voice=None):
        self.syntheses.append({"text": text, "model": model, "limits": limits.model_dump(), "locale": locale,
                               "pace": pace, "voice": voice})
        if self.tts_error:
            raise audio_error(self.tts_error)
        return MP3


@pytest.fixture
def smoke(owner, monkeypatch):
    """The owner client with a fake provider. The validator is stubbed; one test runs the real one."""

    def install(audio: FakeAudio, *, report=None) -> FakeAudio:
        monkeypatch.setattr(device_route, "check_recording",
                            lambda kind, file: report or {"status": "accepted", "probe": kind})
        owner.app.dependency_overrides[get_audio_provider] = lambda: audio
        return audio

    return install


def run_smoke(owner):
    with respx.mock() as outbound:
        response = owner.post(PATH + "/inference")
    assert not outbound.calls
    return response


def test_inference_smoke_sends_only_the_bundled_fixtures_and_returns_no_content(owner, smoke, caplog) -> None:
    audio = smoke(FakeAudio())
    caplog.set_level(logging.DEBUG)
    response = run_smoke(owner)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["stt"] == {
        "outcome": "passed", "code": None, "model": "voxtral-mini-transcribe-3",
        "elapsed_ms": body["stt"]["elapsed_ms"], "reported_model": "voxtral-mini-transcribe-3-0",
        "matches_sample": True,
    }
    assert body["tts"] == {
        "outcome": "passed", "code": None, "model": "voxtral-mini-tts-3-0",
        "elapsed_ms": body["tts"]["elapsed_ms"], "audio_bytes": len(MP3),
    }
    assert body["stt"]["elapsed_ms"] >= 0 and body["tts"]["elapsed_ms"] >= 0
    assert datetime.fromisoformat(body["checked_at"]).utcoffset() is not None
    # Neither the transcript nor the sample sentence leaves the server or reaches the logs.
    for text in (TRANSCRIPT, SAMPLE, "gelbe"):
        assert text not in response.text
        assert text not in caplog.text

    fixture = device_route._SAMPLE_RECORDING.read_bytes()
    assert audio.transcriptions == [
        {"data": fixture, "kind": "webm", "model": "voxtral-mini-transcribe-3", "language": "de"},
    ]
    assert audio.syntheses == [{
        "text": SAMPLE, "model": "voxtral-mini-tts-3-0", "locale": "de", "pace": "normal", "voice": None,
        "limits": {"max_input_characters": 500, "max_response_bytes": 1_000_000, "timeout_seconds": 20},
    }]
    # The smoke never releases the regular routes.
    capabilities = owner.get("/api/v1/audio/capabilities").json()
    assert capabilities["stt"]["status"] == capabilities["tts"]["status"] == "unknown"


def test_a_different_transcript_is_reported_without_its_text(owner, smoke) -> None:
    smoke(FakeAudio(transcript="Ein grüner Frosch quakt synthetisch."))
    response = run_smoke(owner)
    assert response.json()["stt"]["outcome"] == "passed"
    assert response.json()["stt"]["matches_sample"] is False
    assert "Frosch" not in response.text


@pytest.mark.parametrize(("stt_error", "tts_error"), [
    ("audio_timeout", None),
    (None, "audio_unavailable"),
    ("audio_rate_limited", "audio_timeout"),
])
def test_each_capability_fails_independently(owner, smoke, stt_error, tts_error) -> None:
    audio = smoke(FakeAudio(stt_error=stt_error, tts_error=tts_error))
    response = run_smoke(owner)
    assert response.status_code == 200
    stt, tts = response.json()["stt"], response.json()["tts"]
    assert (stt["outcome"], stt["code"]) == (("failed", stt_error) if stt_error else ("passed", None))
    assert (tts["outcome"], tts["code"]) == (("failed", tts_error) if tts_error else ("passed", None))
    if stt_error:
        assert stt["reported_model"] is None and stt["matches_sample"] is None
    if tts_error:
        assert tts["audio_bytes"] is None
    assert len(audio.transcriptions) == len(audio.syntheses) == 1


def test_invalid_model_configuration_skips_only_that_provider_call(owner, smoke, monkeypatch) -> None:
    audio = smoke(FakeAudio())
    monkeypatch.setattr(env, "audio_stt_model", "not-a-voxtral-model")
    body = run_smoke(owner).json()
    assert body["stt"] == {
        "outcome": "failed", "code": "invalid_model_configuration", "model": None, "elapsed_ms": 0,
        "reported_model": None, "matches_sample": None,
    }
    assert body["tts"]["outcome"] == "passed"
    assert not audio.transcriptions and len(audio.syntheses) == 1


def test_a_rejected_fixture_is_never_sent(owner, smoke) -> None:
    audio = smoke(FakeAudio(), report={"status": "rejected", "reason": "tool_unavailable"})
    body = run_smoke(owner).json()
    assert (body["stt"]["outcome"], body["stt"]["code"], body["stt"]["elapsed_ms"]) == ("failed", "recording_rejected", 0)
    assert body["tts"]["outcome"] == "passed"
    assert not audio.transcriptions


def test_a_busy_or_unavailable_slot_stops_before_any_provider_call(owner, smoke) -> None:
    audio = smoke(FakeAudio())

    @asynccontextmanager
    async def busy():
        yield False

    owner.app.dependency_overrides[get_transcription_slot] = lambda: busy
    response = run_smoke(owner)
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "audio_rate_limited"

    @asynccontextmanager
    async def broken():
        raise OSError("lock unavailable")
        yield True

    owner.app.dependency_overrides[get_transcription_slot] = lambda: broken
    response = run_smoke(owner)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "audio_unavailable"
    assert not audio.transcriptions and not audio.syntheses


def test_bundled_recording_passes_the_production_validator(recording) -> None:
    # `recording` is requested only for its skip: the validator needs FFmpeg with the fd protocol.
    with device_route._SAMPLE_RECORDING.open("rb") as fixture:
        report = device_route.check_recording("webm", fixture)
    assert report["status"] == "accepted"
    assert 1 < report["decoded_seconds"] < 5
    assert report["bytes"] < 32 * 1024
