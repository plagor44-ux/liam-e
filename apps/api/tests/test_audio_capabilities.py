"""The owner-only capability contract is independent from provider calls."""

import json
from typing import Any

from fastapi.testclient import TestClient
import pytest
import respx

from conftest import build_app

PATH = "/api/v1/audio/capabilities"
CHECKED_AT = "2026-01-01T10:00:00Z"
STT_LIMITS = {"max_upload_bytes": 10_485_760, "max_duration_seconds": 120, "max_concurrent_requests": 1}
TTS_LIMITS = {"max_input_characters": 500, "max_response_bytes": 1_000_000, "timeout_seconds": 20}


@pytest.mark.parametrize(
    ("headers", "expected_status", "expected_detail"),
    [
        ({}, 401, "unauthorized"),
        ({"x-apps-token": "unverified-test-only", "x-api-key": "unverified-test-only"}, 403, "agent_access_disabled"),
        # A gateway token on an app without a configured gateway cannot be checked: fail closed.
        ({"authorization": "Bearer unverified-test-only", "x-space-token": "unverified-test-only"}, 503, "caller_identity_unavailable"),
    ],
)
def test_audio_capabilities_rejects_unverified_owner(
    client: TestClient, headers: dict[str, str], expected_status: int, expected_detail: str
) -> None:
    response = client.get(PATH, headers=headers, params={"owner": "true"})
    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}


@pytest.fixture
def owner_client() -> TestClient:
    from api.audio.access import get_audio_owner_verifier

    async def verified_owner(request):
        return True

    app = build_app()
    app.dependency_overrides[get_audio_owner_verifier] = lambda: verified_owner
    return TestClient(app)


@pytest.fixture
def audio_settings(monkeypatch):
    from env.audio import env

    monkeypatch.setattr(env, "audio_stt_model", "voxtral-mini-transcribe-3")
    monkeypatch.setattr(env, "audio_tts_model", "voxtral-mini-tts-3-0")
    monkeypatch.setattr(env, "audio_stt_verification", None)
    monkeypatch.setattr(env, "audio_tts_verification", None)
    return env


def verification(model: str, **values: Any) -> str:
    return json.dumps({"model": model, "checked_at": CHECKED_AT, **values})


def test_audio_capabilities_default_to_unknown_without_provider_traffic(
    owner_client: TestClient, audio_settings, monkeypatch
) -> None:
    from env.mistral import env as mistral_settings

    monkeypatch.setattr(mistral_settings, "mistral_api_key", "provider-secret-test-only")
    with respx.mock(assert_all_mocked=True) as outbound:
        response = owner_client.get(PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "provider-secret-test-only" not in response.text
    assert not outbound.calls
    assert response.json() == {
        "stt": {
            "status": "unknown",
            "reason": "verification_missing_or_invalid",
            "model": "voxtral-mini-transcribe-3",
            "resolved_model": None,
            "formats": [],
            "app_limits": {
                "max_upload_bytes": 10_485_760,
                "max_duration_seconds": 120,
                "max_concurrent_requests": 1,
            },
            "checked_at": None,
        },
        "tts": {
            "status": "unknown",
            "reason": "verification_missing_or_invalid",
            "model": "voxtral-mini-tts-3-0",
            "resolved_model": None,
            "formats": [],
            "app_limits": None,
            "checked_at": None,
        },
    }


def test_a_failed_tts_verification_does_not_disable_verified_stt(owner_client: TestClient, audio_settings) -> None:
    audio_settings.audio_stt_verification = verification(
        "voxtral-mini-transcribe-3", outcome="verified", scope="studio_release",
        resolved_model="voxtral-mini-transcribe-3-test", formats=["audio/webm;codecs=opus"], app_limits=STT_LIMITS,
    )
    audio_settings.audio_tts_verification = verification("voxtral-mini-tts-3-0", outcome="failed")

    body = owner_client.get(PATH).json()

    assert body["stt"]["status"] == "available"
    assert body["stt"]["reason"] is None
    assert body["stt"]["formats"] == ["audio/webm;codecs=opus"]
    assert body["stt"]["resolved_model"] == "voxtral-mini-transcribe-3-test"
    assert body["stt"]["checked_at"] == CHECKED_AT
    assert body["tts"]["status"] == "unavailable"
    assert body["tts"]["reason"] == "verification_failed"
    assert body["tts"]["formats"] == []
    assert body["tts"]["checked_at"] == CHECKED_AT


def test_a_failed_stt_verification_does_not_disable_verified_tts(owner_client: TestClient, audio_settings) -> None:
    audio_settings.audio_stt_verification = verification("voxtral-mini-transcribe-3", outcome="failed")
    audio_settings.audio_tts_verification = verification(
        "voxtral-mini-tts-3-0", outcome="verified", scope="studio_release",
        formats=["audio/mpeg"], app_limits=TTS_LIMITS,
    )

    body = owner_client.get(PATH).json()

    assert body["stt"]["status"] == "unavailable"
    assert body["tts"]["status"] == "available"
    assert body["tts"]["formats"] == ["audio/mpeg"]
    assert body["tts"]["app_limits"] == TTS_LIMITS


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "voxtral-mini-transcribe-3-test"},
        {"formats": []},
        {"formats": ["audio/anything"]},
        {"reason": "private-probe-notes-test-only"},
        {"checked_at": "2999-01-01T00:00:00Z"},
        {"scope": None},
        {"scope": "provider"},
        {"app_limits": None},
        {"app_limits": {}},
        {"app_limits": {**STT_LIMITS, "max_upload_bytes": 1_000_000}},
        {"app_limits": {**STT_LIMITS, "max_duration_seconds": 60}},
        {"app_limits": {**STT_LIMITS, "max_concurrent_requests": 2}},
    ],
)
def test_unbound_or_incomplete_evidence_does_not_enable_stt(
    owner_client: TestClient, audio_settings, overrides: dict[str, Any]
) -> None:
    release = {
        "model": "voxtral-mini-transcribe-3", "checked_at": CHECKED_AT,
        "outcome": "verified", "scope": "studio_release", "formats": ["audio/webm;codecs=opus"],
        "app_limits": STT_LIMITS,
    }
    audio_settings.audio_stt_verification = json.dumps(release | overrides)

    response = owner_client.get(PATH)

    assert response.status_code == 200
    assert "private-probe-notes-test-only" not in response.text
    assert response.json()["stt"]["status"] == "unknown"
    assert response.json()["stt"]["formats"] == []


def test_provider_success_without_release_scope_keeps_both_branches_unknown(
    owner_client: TestClient, audio_settings
) -> None:
    audio_settings.audio_stt_verification = verification(
        "voxtral-mini-transcribe-3", outcome="verified", formats=["audio/webm;codecs=opus"],
    )
    audio_settings.audio_tts_verification = verification(
        "voxtral-mini-tts-3-0", outcome="verified", formats=["audio/mpeg"], app_limits=TTS_LIMITS,
    )

    body = owner_client.get(PATH).json()

    assert [body[capability]["status"] for capability in ("stt", "tts")] == ["unknown", "unknown"]


@pytest.mark.parametrize("scope", [None, "provider"])
def test_provider_evidence_cannot_enable_tts(owner_client: TestClient, audio_settings, scope: str | None) -> None:
    audio_settings.audio_tts_verification = verification(
        "voxtral-mini-tts-3-0", outcome="verified", scope=scope, formats=["audio/mpeg"], app_limits=TTS_LIMITS,
    )

    assert owner_client.get(PATH).json()["tts"]["status"] == "unknown"


def test_unbounded_speech_verification_cannot_enable_tts(owner_client: TestClient, audio_settings) -> None:
    audio_settings.audio_tts_verification = verification(
        "voxtral-mini-tts-3-0", outcome="verified", scope="studio_release", formats=["audio/mpeg"],
    )

    body = owner_client.get(PATH).json()

    assert body["tts"]["status"] == "unknown"
    assert body["tts"]["app_limits"] is None


def test_bad_model_configuration_is_not_reflected(owner_client: TestClient, audio_settings) -> None:
    audio_settings.audio_tts_model = "provider-secret-test-only damaged model"

    response = owner_client.get(PATH)

    assert "provider-secret-test-only" not in response.text
    assert response.json()["tts"]["model"] is None
    assert response.json()["tts"]["status"] == "unknown"
    assert response.json()["tts"]["reason"] == "invalid_model_configuration"
