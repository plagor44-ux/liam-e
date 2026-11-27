"""The Studio gateway's caller token is verified before it can authorize audio.

Every ``/api/v1`` request first passes the caller gate (``api.mcp.exposure``), which looks the
same token up once (#200); the owner check reuses that answer.
"""

from collections.abc import Iterator
import json

from env.audio import env
from fastapi.testclient import TestClient
import httpx
from mistralai_capabilities.apps import credentials as gateway_credentials
import pytest
import respx
from starlette.websockets import WebSocketDisconnect
from utils.mistral import install_caller_credentials

CAPABILITIES = "/api/v1/audio/capabilities"
TRANSCRIPTIONS = "/api/v1/audio/transcriptions"
ORIGIN = "https://audio.example.test"
PROXY = "https://gateway.example.test/mistral"
OWNER = "11111111-1111-4111-8111-111111111111"
ORGANIZATION = "33333333-3333-4333-8333-333333333333"
WORKSPACE = "44444444-4444-4444-8444-444444444444"
OTHER = "22222222-2222-4222-8222-222222222222"
HEADERS = {"x-apps-token": "forwarded-user-session-test-only"}
IDENTITY = {
    "id": OWNER,
    "organization": {"id": ORGANIZATION},
    "workspace": {"id": WORKSPACE},
    "api_key": None,
}


@pytest.fixture
def studio_client(monkeypatch) -> Iterator[TestClient]:
    from api.main import create_app

    binding = {"user_id": OWNER, "organization_id": ORGANIZATION, "workspace_id": WORKSPACE, "origin": ORIGIN}
    monkeypatch.setattr(env, "audio_studio_owner", json.dumps(binding))
    monkeypatch.setattr(env, "audio_stt_verification", None)
    monkeypatch.setattr(env, "audio_tts_verification", None)
    monkeypatch.setattr(gateway_credentials.env, "apps_proxy_url", PROXY)
    app = create_app()
    try:
        yield TestClient(app)
    finally:
        install_caller_credentials(None)


def test_verified_gateway_owner_can_read_the_independent_capabilities(studio_client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as outbound:
        who = outbound.get(PROXY + "/v1/users/me").mock(return_value=httpx.Response(200, json=IDENTITY))
        response = studio_client.get(CAPABILITIES, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["stt"]["status"] == response.json()["tts"]["status"] == "unknown"
    assert response.headers["cache-control"] == "no-store"
    assert who.call_count == 1
    assert who.calls.last.request.headers["authorization"] == "Bearer " + HEADERS["x-apps-token"]
    assert "x-api-key" not in who.calls.last.request.headers


@pytest.mark.parametrize(("change", "expected_status", "expected_detail"), [
    ({"id": OTHER}, 401, "unauthorized"),
    ({"organization": {"id": OTHER}}, 401, "unauthorized"),
    ({"workspace": {"id": OTHER}}, 401, "unauthorized"),
    ({"organization": None}, 401, "unauthorized"),
    # Any non-null api_key marks a key caller, which the caller gate refuses first.
    ({"api_key": {"id": OWNER}}, 403, "agent_access_disabled"),
    ({"api_key": False}, 403, "agent_access_disabled"),
])
def test_wrong_principal_context_or_api_key_is_denied(
    studio_client: TestClient, change: dict, expected_status: int, expected_detail: str
) -> None:
    with respx.mock(assert_all_called=False) as outbound:
        who = outbound.get(PROXY + "/v1/users/me").mock(
            return_value=httpx.Response(200, json={**IDENTITY, **change})
        )
        response = studio_client.get(CAPABILITIES, headers=HEADERS)
    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert who.call_count == 1


def test_missing_credential_type_is_not_proof_of_a_session(studio_client: TestClient) -> None:
    body = {k: v for k, v in IDENTITY.items() if k != "api_key"}
    with respx.mock(assert_all_called=False) as outbound:
        outbound.get(PROXY + "/v1/users/me").mock(return_value=httpx.Response(200, json=body))
        response = studio_client.get(CAPABILITIES, headers=HEADERS)
    assert response.status_code == 401


def test_identity_redirect_does_not_send_the_gateway_token_elsewhere(studio_client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as outbound:
        who = outbound.get(PROXY + "/v1/users/me").mock(
            return_value=httpx.Response(302, headers={"location": "https://other.example.test/"})
        )
        response = studio_client.get(CAPABILITIES, headers=HEADERS)
    assert response.status_code == 401
    assert who.call_count == 1


@pytest.mark.parametrize(("headers", "expected_status", "expected_detail"), [
    # Without a caller token the request counts as an agent (#200), so the switch answers first.
    ({"Authorization": "Bearer request-token-test-only"}, 403, "agent_access_disabled"),
    ({**HEADERS, "x-api-key": "workspace-key-test-only"}, 403, "agent_access_disabled"),
])
def test_studio_owner_never_falls_back_to_other_credentials(
    studio_client: TestClient, headers: dict, expected_status: int, expected_detail: str
) -> None:
    with respx.mock() as outbound:
        response = studio_client.get(CAPABILITIES, headers=headers)
    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert not outbound.calls


@pytest.mark.parametrize("origin", [None, "null", "https://other.example.test"])
def test_audio_posts_require_the_configured_browser_origin(studio_client: TestClient, origin: str | None) -> None:
    headers = {**HEADERS, **({"origin": origin} if origin else {})}
    with respx.mock(assert_all_called=False) as outbound:
        who = outbound.get(PROXY + "/v1/users/me").mock(return_value=httpx.Response(200, json=IDENTITY))
        response = studio_client.post(TRANSCRIPTIONS, headers=headers)
    assert response.status_code == 401
    # The owner's own identity is not enough without the bound origin; only the gate looked it up.
    assert who.call_count == 1


@pytest.mark.parametrize(("headers", "expected_status", "expected_detail", "lookups"), [
    (HEADERS, 401, "unauthorized", 1),
    ({**HEADERS, "origin": "null"}, 401, "unauthorized", 1),
    ({**HEADERS, "origin": "https://other.example.test"}, 401, "unauthorized", 1),
    ({"x-api-key": "workspace-key-test-only", "origin": ORIGIN}, 403, "agent_access_disabled", 0),
])
def test_paid_device_inference_smoke_requires_owner_and_origin(
    studio_client: TestClient, monkeypatch, headers: dict, expected_status: int, expected_detail: str, lookups: int
) -> None:
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(env, "audio_device_test_until", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    with respx.mock(assert_all_called=False) as outbound:
        who = outbound.get(PROXY + "/v1/users/me").mock(return_value=httpx.Response(200, json=IDENTITY))
        response = studio_client.post("/api/v1/audio/device-test/inference", headers=headers)
    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert who.call_count == lookups


def test_same_origin_owner_reaches_the_transcription_release_check(studio_client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as outbound:
        outbound.get(PROXY + "/v1/users/me").mock(return_value=httpx.Response(200, json=IDENTITY))
        response = studio_client.post(TRANSCRIPTIONS, headers={**HEADERS, "origin": ORIGIN})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "audio_unavailable"


def test_live_websocket_enforces_gateway_identity_and_origin(studio_client: TestClient, monkeypatch) -> None:
    from contextlib import asynccontextmanager
    import asyncio
    from datetime import datetime, timedelta, timezone
    from api.routers.api.v1.audio.live import transcription

    class Provider:
        async def receive(self):
            await asyncio.sleep(0.01)
            return {'type': 'turn.start', 'time_s': 1}
        async def append(self, data): pass
    @asynccontextmanager
    async def fake_live(_, **kwargs): yield Provider()
    monkeypatch.setattr(transcription, 'mistral_live', fake_live)
    monkeypatch.setattr(env, 'audio_live_test_enabled', True)
    monkeypatch.setattr(env, 'audio_live_test_origin', ORIGIN)
    monkeypatch.setattr(env, 'audio_live_test_until', (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    with respx.mock(assert_all_called=False) as outbound:
        who = outbound.get(PROXY + '/v1/users/me').mock(return_value=httpx.Response(200, json=IDENTITY))
        with pytest.raises(WebSocketDisconnect):
            with studio_client.websocket_connect('/api/v1/audio/live/transcription', headers={**HEADERS, 'origin': 'https://other.example.test'}): pass
        assert who.call_count == 1  # the caller gate; the owner check stopped at the origin
        with studio_client.websocket_connect('/api/v1/audio/live/transcription', headers={**HEADERS, 'origin': ORIGIN}) as ws:
            assert ws.receive_json()['type'] == 'turn.start'
            ws.close()
        assert who.call_count == 1  # the same token's answer is still fresh
