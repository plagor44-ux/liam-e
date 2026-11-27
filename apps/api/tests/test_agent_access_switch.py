"""HTTP contracts of the owner's agent access switch (#197).

Reading and turning off need nothing beyond the gateway; turning on needs the bound owner's own
browser session and the current revision. Storage is an in-memory stand-in for the accessor,
whose live contracts are in ``packages/py/db/tests/test_agent_access_pg.py``. The owner check
is the real shared verifier with the Studio identity lookup mocked.
"""

from collections.abc import Iterator
from datetime import datetime, timezone
import json
import logging

from env.audio import env
from fastapi.testclient import TestClient
import httpx
from mistralai_capabilities.apps import credentials as gateway_credentials
import pytest
import respx
from sqlalchemy.exc import OperationalError
from utils.mistral import install_caller_credentials

import api.caller_identity as identity_module
from db.accessors import agent_access
from db.models.agent_access import AgentAccess

PATH = "/api/v1/settings/agent-access"
ORIGIN = "https://app.example.test"
PROXY = "https://gateway.example.test/mistral"
OWNER = "11111111-1111-4111-8111-111111111111"
ORGANIZATION = "33333333-3333-4333-8333-333333333333"
WORKSPACE = "44444444-4444-4444-8444-444444444444"
OTHER = "22222222-2222-4222-8222-222222222222"
TOKEN = "forwarded-user-session-test-only"
HEADERS = {"x-apps-token": TOKEN}
OWNER_POST = {**HEADERS, "origin": ORIGIN}
IDENTITY = {"id": OWNER, "organization": {"id": ORGANIZATION}, "workspace": {"id": WORKSPACE}, "api_key": None}
BINDING = json.dumps({"user_id": OWNER, "organization_id": ORGANIZATION, "workspace_id": WORKSPACE, "origin": ORIGIN})
CHANGED = datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc)


class Store:
    """The accessor's observable behaviour without a database: no row means off."""

    def __init__(self, row: AgentAccess | None = None):
        self.row = row
        self.failure: Exception | None = None

    def _check(self):
        if self.failure is not None:
            raise self.failure

    async def is_enabled(self) -> bool:
        self._check()
        return bool(self.row and self.row.enabled)

    async def state(self) -> AgentAccess:
        self._check()
        return self.row.model_copy() if self.row else AgentAccess()

    def turn_off(self) -> None:
        """Every turn-off advances the revision; a missing row becomes revision 2, off."""
        current = self.row or AgentAccess()
        changed_at = CHANGED if current.enabled else current.changed_at
        self.row = AgentAccess(enabled=False, revision=current.revision + 1, changed_at=changed_at)

    async def disable(self) -> AgentAccess:
        self._check()
        self.turn_off()
        return await self.state()

    async def enable(self, expected_revision: int) -> AgentAccess:
        self._check()
        current = self.row or AgentAccess()
        if current.revision != expected_revision:
            raise agent_access.StaleRevision(expected_revision)
        if not current.enabled:
            current = AgentAccess(enabled=True, revision=current.revision + 1, changed_at=CHANGED)
        self.row = current
        return current.model_copy()


@pytest.fixture
def store(monkeypatch) -> Store:
    fake = Store()
    for name in ("is_enabled", "state", "disable", "enable"):
        monkeypatch.setattr(agent_access, name, getattr(fake, name))
    return fake


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    """The identity cache's clock; ``clock[0] += ...`` lets cached answers expire."""
    now = [1000.0]
    monkeypatch.setattr(identity_module, "_now", lambda: now[0])
    return now


@pytest.fixture
def client(monkeypatch, store) -> Iterator[TestClient]:
    from api.main import create_app

    monkeypatch.setattr(env, "audio_studio_owner", BINDING)
    monkeypatch.setattr(gateway_credentials.env, "apps_proxy_url", PROXY)
    app = create_app()
    try:
        yield TestClient(app)
    finally:
        install_caller_credentials(None)


def _unbound(monkeypatch):
    monkeypatch.setattr(env, "audio_studio_owner", None)


def _code(response) -> str:
    return response.json()["detail"]["code"]


def test_get_reports_off_by_default_without_creating_a_row(client, store, monkeypatch):
    _unbound(monkeypatch)
    response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "enabled": False, "revision": 1, "changed_at": None, "verifier_configured": False, "owner_bound": False,
    }
    assert response.headers["cache-control"] == "no-store"
    assert store.row is None


def test_get_reports_binding_and_verifier_presence_but_no_values(client, store):
    from api.mcp.access import ReadGrant, get_read_access_verifier

    class Granting:
        async def verify(self, request):
            return ReadGrant(frozenset({"acct-test"}))

    client.app.dependency_overrides[get_read_access_verifier] = Granting
    store.row = AgentAccess(enabled=True, revision=2, changed_at=CHANGED)
    response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert (body["enabled"], body["revision"], body["verifier_configured"], body["owner_bound"]) == (True, 2, True, True)
    assert datetime.fromisoformat(body["changed_at"]) == CHANGED
    for secret in (OWNER, ORGANIZATION, WORKSPACE, ORIGIN):
        assert secret not in response.text


@pytest.mark.parametrize("bound", [True, False])
@pytest.mark.parametrize("body", [None, {"expected_revision": 99}])
def test_disable_needs_no_identity_revision_or_binding(client, store, monkeypatch, bound, body):
    from api.routers.api.v1.settings.agent_access.route import get_agent_access_owner_verifier

    def refuse_lookup():
        raise AssertionError("turning access off must not look up an identity")

    if not bound:
        _unbound(monkeypatch)
    client.app.dependency_overrides[get_agent_access_owner_verifier] = refuse_lookup
    store.row = AgentAccess(enabled=True, revision=4, changed_at=None)
    with respx.mock() as outbound:
        first = client.post(PATH + "/disable", headers=HEADERS, json=body)
        second = client.post(PATH + "/disable", headers=HEADERS)
    assert not outbound.calls
    assert first.status_code == second.status_code == 200
    assert (first.json()["enabled"], first.json()["revision"]) == (False, 5)
    assert first.json()["changed_at"] is not None
    assert (second.json()["enabled"], second.json()["revision"]) == (False, 6)
    assert second.json()["changed_at"] == first.json()["changed_at"]
    assert first.headers["cache-control"] == "no-store"


def test_disable_without_a_row_stays_off_and_advances_past_the_default(client, store):
    response = client.post(PATH + "/disable", headers=HEADERS)
    assert response.status_code == 200
    assert (response.json()["enabled"], response.json()["revision"], response.json()["changed_at"]) == (False, 2, None)
    assert store.row is not None and store.row.enabled is False


@pytest.mark.parametrize("stored", [None, AgentAccess(enabled=False, revision=3, changed_at=CHANGED)])
def test_a_turn_off_during_the_identity_lookup_wins_over_the_pending_turn_on(client, store, stored):
    """Review finding on #198: an off switch must still invalidate a confirmed, pending turn-on."""
    store.row = stored
    page = client.get(PATH, headers=HEADERS).json()
    assert page["enabled"] is False

    def turn_off_in_another_tab(request):
        store.turn_off()
        return httpx.Response(200, json=IDENTITY)

    with respx.mock(assert_all_called=False) as outbound:
        who = outbound.get(PROXY + "/v1/users/me").mock(side_effect=turn_off_in_another_tab)
        response = client.post(PATH + "/enable", headers=OWNER_POST, json={"expected_revision": page["revision"]})
    assert who.call_count == 1
    assert response.status_code == 409
    assert _code(response) == "agent_access_conflict"
    assert store.row.enabled is False and store.row.revision == page["revision"] + 1


def test_the_verified_owner_turns_access_on_with_the_current_revision(client, store):
    with respx.mock(assert_all_called=False) as outbound:
        who = outbound.get(PROXY + "/v1/users/me").mock(return_value=httpx.Response(200, json=IDENTITY))
        response = client.post(PATH + "/enable", headers=OWNER_POST, json={"expected_revision": 1})
    assert response.status_code == 200
    assert (response.json()["enabled"], response.json()["revision"]) == (True, 2)
    assert response.headers["cache-control"] == "no-store"
    assert who.call_count == 1
    assert who.calls.last.request.headers["authorization"] == "Bearer " + TOKEN
    assert "x-api-key" not in who.calls.last.request.headers


@pytest.mark.parametrize("change", [
    {"id": OTHER}, {"organization": {"id": OTHER}}, {"workspace": {"id": OTHER}}, {"api_key": {"id": OWNER}},
])
def test_another_identity_cannot_turn_access_on(client, store, change):
    with respx.mock(assert_all_called=False) as outbound:
        outbound.get(PROXY + "/v1/users/me").mock(return_value=httpx.Response(200, json={**IDENTITY, **change}))
        response = client.post(PATH + "/enable", headers=OWNER_POST, json={"expected_revision": 1})
    assert response.status_code == 403
    assert _code(response) == "agent_access_owner_required"
    assert response.headers["cache-control"] == "no-store"
    assert store.row is None


@pytest.mark.parametrize("headers", [
    HEADERS,
    {**HEADERS, "origin": "null"},
    {**HEADERS, "origin": "https://other.example.test"},
])
def test_a_workspace_key_or_foreign_origin_never_reaches_the_identity_lookup(client, store, headers):
    with respx.mock() as outbound:
        response = client.post(PATH + "/enable", headers=headers, json={"expected_revision": 1})
    assert response.status_code == 403
    assert _code(response) == "agent_access_owner_required"
    assert not outbound.calls
    assert store.row is None


def test_a_workspace_key_cannot_enable_switch_and_never_reaches_the_identity_lookup(client, store):
    with respx.mock() as outbound:
        response = client.post(
            PATH + "/enable",
            headers={**OWNER_POST, "x-api-key": "workspace-key-test-only"},
            json={"expected_revision": 1},
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "agent_access_disabled"}
    assert response.headers["cache-control"] == "no-store"
    assert not outbound.calls
    assert store.row is None


@pytest.mark.parametrize("answer", [httpx.Response(500), httpx.TimeoutException("slow"), httpx.Response(302, headers={"location": "https://other.example.test/"})])
def test_a_failed_identity_lookup_fails_closed(client, store, answer):
    with respx.mock(assert_all_called=False) as outbound:
        route = outbound.get(PROXY + "/v1/users/me")
        if isinstance(answer, Exception):
            route.mock(side_effect=answer)
        else:
            route.mock(return_value=answer)
        response = client.post(PATH + "/enable", headers=OWNER_POST, json={"expected_revision": 1})
    assert response.status_code == 403
    assert _code(response) == "agent_access_owner_required"
    assert store.row is None


@pytest.mark.parametrize("binding", [None, "", "{", json.dumps({"user_id": OWNER})])
def test_without_an_owner_binding_access_cannot_be_turned_on(client, store, monkeypatch, binding):
    monkeypatch.setattr(env, "audio_studio_owner", binding)
    with respx.mock() as outbound:
        response = client.post(PATH + "/enable", headers=OWNER_POST, json={"expected_revision": 1})
    assert response.status_code == 409
    assert _code(response) == "agent_access_owner_unbound"
    assert not outbound.calls
    assert store.row is None


def test_a_stale_revision_is_a_conflict(client, store):
    store.row = AgentAccess(enabled=False, revision=3, changed_at=CHANGED)
    with respx.mock(assert_all_called=False) as outbound:
        outbound.get(PROXY + "/v1/users/me").mock(return_value=httpx.Response(200, json=IDENTITY))
        response = client.post(PATH + "/enable", headers=OWNER_POST, json={"expected_revision": 2})
    assert response.status_code == 409
    assert _code(response) == "agent_access_conflict"
    assert store.row.enabled is False and store.row.revision == 3


@pytest.mark.parametrize("body", [
    {},
    {"expected_revision": 0},
    {"expected_revision": "1"},
    {"expected_revision": True},
    {"expected_revision": 1.5},
    {"expected_revision": 1, "owner_scope": "other-scope-test-only"},
])
def test_an_invalid_body_is_refused_without_echo_or_lookup(client, store, body):
    with respx.mock() as outbound:
        response = client.post(PATH + "/enable", headers=OWNER_POST, json=body)
    assert response.status_code == 422
    assert _code(response) == "agent_access_invalid"
    assert "other-scope-test-only" not in response.text
    assert not outbound.calls
    assert store.row is None


@pytest.mark.parametrize("failure", [OperationalError("select", {}, Exception("down")), ConnectionRefusedError()])
@pytest.mark.parametrize("call", [
    lambda c: c.get(PATH, headers=HEADERS),
    lambda c: c.post(PATH + "/disable", headers=HEADERS),
])
def test_a_storage_failure_answers_503_and_never_passes(client, store, failure, call):
    store.failure = failure
    response = call(client)
    assert response.status_code == 503
    assert _code(response) == "agent_access_unavailable"
    assert response.headers["cache-control"] == "no-store"


def test_a_storage_failure_while_enabling_answers_503(client, store):
    store.failure = OperationalError("update", {}, Exception("down"))
    with respx.mock(assert_all_called=False) as outbound:
        outbound.get(PROXY + "/v1/users/me").mock(return_value=httpx.Response(200, json=IDENTITY))
        response = client.post(PATH + "/enable", headers=OWNER_POST, json={"expected_revision": 1})
    assert response.status_code == 503
    assert _code(response) == "agent_access_unavailable"


def test_switch_logs_carry_no_identity_or_token(client, store, clock, caplog):
    caplog.set_level(logging.INFO)
    with respx.mock(assert_all_called=False) as outbound:
        outbound.get(PROXY + "/v1/users/me").mock(return_value=httpx.Response(200, json={**IDENTITY, "id": OTHER}))
        client.post(PATH + "/enable", headers=OWNER_POST, json={"expected_revision": 1})
        clock[0] += identity_module.FRESH_SECONDS[identity_module.CallerKind.USER] + 1
        outbound.get(PROXY + "/v1/users/me").mock(return_value=httpx.Response(200, json=IDENTITY))
        client.post(PATH + "/enable", headers=OWNER_POST, json={"expected_revision": 1})
    client.post(PATH + "/disable", headers=HEADERS)
    assert "agent_access=enabled" in caplog.text and "agent_access=disabled" in caplog.text
    for value in (OWNER, OTHER, ORGANIZATION, WORKSPACE, ORIGIN, TOKEN):
        assert value not in caplog.text
