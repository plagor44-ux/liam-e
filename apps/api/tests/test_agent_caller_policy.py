"""Who counts as an agent on ``/api/v1`` behind the Studio gateway (#197 D1 = B, #200).

The gateway forwards neither ``x-api-key`` nor ``Authorization``, so the caller gate asks the
Users API what the forwarded caller token belongs to. The app, the gate and the identity lookup
are the real code; the Users API, the switch and the prompt store are stand-ins.
"""

from collections.abc import Iterator
import logging
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
from mistralai_capabilities.apps import credentials as gateway_credentials
import pytest
import respx
from starlette.websockets import WebSocketDisconnect

import api.caller_identity as identity_module
from api.caller_identity import FRESH_SECONDS, STALE_SECONDS, CallerKind
from db.accessors import agent_access, prompt_template

PROXY = "https://gateway.example.test/mistral"
TOKEN = "forwarded-caller-token-test-only"
HEADERS = {"x-apps-token": TOKEN}
USER = {
    "id": "11111111-1111-4111-8111-111111111111",
    "organization": {"id": "33333333-3333-4333-8333-333333333333"},
    "workspace": {"id": "44444444-4444-4444-8444-444444444444"},
    "api_key": None,
}
KEY = {**USER, "api_key": {"id": "55555555-5555-4555-8555-555555555555"}}

READ_ONLY = "/api/v1/prompts"  # in the agent read inventory
BROWSER_ONLY = "/api/v1/settings/agent-access"  # a GET agents never get
MUTATION = "/api/v1/prompts"  # POST; an empty body answers 422 once past the gate
DISABLE = "/api/v1/settings/agent-access/disable"


class Switch:
    def __init__(self) -> None:
        self.enabled = False
        self.reads = 0

    async def is_enabled(self) -> bool:
        self.reads += 1
        return self.enabled


@pytest.fixture
def switch(monkeypatch) -> Switch:
    fake = Switch()
    monkeypatch.setattr(agent_access, "is_enabled", fake.is_enabled)

    async def state():
        return SimpleNamespace(enabled=fake.enabled, revision=2, changed_at=None)

    async def disable():
        fake.enabled = False
        return SimpleNamespace(enabled=False, revision=3, changed_at=None)

    async def no_prompts():
        return []

    monkeypatch.setattr(agent_access, "state", state)
    monkeypatch.setattr(agent_access, "disable", disable)
    monkeypatch.setattr(prompt_template, "list_templates", no_prompts)
    return fake


@pytest.fixture
def users_api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(PROXY + "/v1/users/me", name="who").mock(return_value=httpx.Response(200, json=USER))
        yield router


@pytest.fixture
def who(users_api) -> respx.Route:
    return users_api.routes["who"]


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(identity_module, "_now", lambda: now[0])
    return now


def _app(monkeypatch, proxy: str | None = PROXY):
    from api.main import create_app

    monkeypatch.setattr(gateway_credentials.env, "apps_proxy_url", proxy)
    return create_app()


@pytest.fixture
def client(monkeypatch, switch, users_api) -> TestClient:
    return TestClient(_app(monkeypatch))


def _refused(response, detail: str) -> None:
    assert response.status_code == 403
    assert response.json() == {"detail": detail}
    assert response.headers["cache-control"] == "no-store"


def test_a_user_session_passes_without_reading_the_switch(client, switch, who):
    assert client.get(READ_ONLY, headers=HEADERS).status_code == 200
    assert client.get(BROWSER_ONLY, headers=HEADERS).status_code == 200
    assert client.post(MUTATION, headers=HEADERS, json={}).status_code == 422
    assert switch.reads == 0
    assert who.call_count == 1


def _key_caller(who):
    who.mock(return_value=httpx.Response(200, json=KEY))
    return HEADERS


def _rejected_caller(who):
    who.mock(return_value=httpx.Response(401))
    return HEADERS


def _tokenless_caller(who):
    return {}


@pytest.mark.parametrize(("caller", "path_group"), [
    (_key_caller, "v1-api-key"),
    (_rejected_caller, "v1-rejected"),
    (_tokenless_caller, "v1-no-token"),
])
def test_agent_callers_get_the_switch_and_only_the_read_inventory(client, switch, who, caplog, caller, path_group):
    headers = caller(who)
    caplog.set_level(logging.INFO)

    # Off (the default): every agent request is refused, reads included.
    _refused(client.get(READ_ONLY, headers=headers), "agent_access_disabled")
    _refused(client.post(MUTATION, headers=headers, json={}), "agent_access_disabled")
    assert f"agent_access=refused path_group={path_group}" in caplog.text

    # On: the read inventory only.
    switch.enabled = True
    assert client.get(READ_ONLY, headers=headers).status_code == 200
    _refused(client.get(BROWSER_ONLY, headers=headers), "agent_operation_refused")
    _refused(client.post(MUTATION, headers=headers, json={}), "agent_operation_refused")
    _refused(client.post("/api/v1/settings/agent-access/enable", headers=headers, json={"expected_revision": 2}),
             "agent_operation_refused")
    assert TOKEN not in caplog.text


def test_the_x_api_key_header_still_marks_an_agent_without_a_lookup(client, switch, users_api):
    headers = {**HEADERS, "x-api-key": "workspace-key-test-only"}
    _refused(client.get(READ_ONLY, headers=headers), "agent_access_disabled")
    switch.enabled = True
    assert client.get(READ_ONLY, headers=headers).status_code == 200
    _refused(client.post(MUTATION, headers=headers, json={}), "agent_operation_refused")
    assert not users_api.calls


@pytest.mark.parametrize("headers", [
    HEADERS,
    {},
    {**HEADERS, "x-api-key": "workspace-key-test-only"},
])
def test_anyone_may_turn_access_off_without_a_lookup(client, switch, users_api, who, headers):
    who.mock(return_value=httpx.Response(200, json=KEY))
    switch.enabled = True
    response = client.post(DISABLE, headers=headers)
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert not users_api.calls


@pytest.mark.parametrize("failure", [httpx.Response(500), httpx.ConnectTimeout("down")])
def test_without_any_verdict_reads_pass_and_changes_wait(client, switch, who, caplog, failure):
    """A Users API outage keeps the owner's browser readable and stops every change."""
    if isinstance(failure, Exception):
        who.mock(side_effect=failure)
    else:
        who.mock(return_value=failure)
    caplog.set_level(logging.INFO)

    assert client.get(READ_ONLY, headers=HEADERS).status_code == 200
    assert client.get(BROWSER_ONLY, headers=HEADERS).status_code == 200
    assert "agent_caller=unverified outcome=read_allowed" in caplog.text

    response = client.post(MUTATION, headers=HEADERS, json={})
    assert response.status_code == 503
    assert response.json() == {"detail": "caller_identity_unavailable"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "5"
    assert "agent_caller=unverified outcome=refused" in caplog.text
    assert switch.reads == 0
    assert TOKEN not in caplog.text


def test_a_known_user_keeps_working_through_an_outage(client, switch, who, clock, caplog):
    assert client.get(READ_ONLY, headers=HEADERS).status_code == 200
    clock[0] += FRESH_SECONDS[CallerKind.USER] + 1
    who.mock(return_value=httpx.Response(503))
    caplog.set_level(logging.INFO)
    assert client.post(MUTATION, headers=HEADERS, json={}).status_code == 422
    assert "caller_identity=stale kind=user" in caplog.text
    assert switch.reads == 0


def test_a_known_key_stays_refused_through_an_outage(client, switch, who, clock):
    who.mock(return_value=httpx.Response(200, json=KEY))
    _refused(client.get(READ_ONLY, headers=HEADERS), "agent_access_disabled")
    clock[0] += FRESH_SECONDS[CallerKind.API_KEY] + 1
    who.mock(return_value=httpx.Response(503))
    _refused(client.get(READ_ONLY, headers=HEADERS), "agent_access_disabled")
    _refused(client.get(BROWSER_ONLY, headers=HEADERS), "agent_access_disabled")


def test_a_stale_user_verdict_ends(client, switch, who, clock):
    assert client.get(READ_ONLY, headers=HEADERS).status_code == 200
    clock[0] += STALE_SECONDS
    who.mock(return_value=httpx.Response(503))
    assert client.post(MUTATION, headers=HEADERS, json={}).status_code == 503


def test_without_a_gateway_requests_pass_unchanged(monkeypatch, switch, users_api):
    client = TestClient(_app(monkeypatch, proxy=None))
    assert client.get(READ_ONLY).status_code == 200
    assert client.post(MUTATION, json={}).status_code == 422
    assert switch.reads == 0
    assert not users_api.calls


def test_a_gateway_token_without_a_configured_gateway_cannot_change_anything(monkeypatch, switch, users_api):
    client = TestClient(_app(monkeypatch, proxy=None))
    assert client.get(READ_ONLY, headers=HEADERS).status_code == 200
    assert client.post(MUTATION, headers=HEADERS, json={}).status_code == 503
    assert not users_api.calls


@pytest.mark.parametrize("enabled", [False, True])
def test_a_key_identity_cannot_open_the_live_websocket(client, switch, who, enabled):
    who.mock(return_value=httpx.Response(200, json=KEY))
    switch.enabled = enabled
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/audio/live/transcription", headers=HEADERS):
            pass
    assert who.call_count == 1
