"""Owner-controlled agent access gate for the read facade and direct read services."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from api.mcp import mail as service
from api.mcp.access import (
    ReadGrant,
    ensure_agent_access,
    get_read_access_verifier,
    require_read_access,
)
from conftest import build_app
from db.accessors import agent_access, mail_account, mail_message, mail_sync_state
from db.models.mail_account import MailAccount
from db.models.mail_sync_state import MailSyncState

BASE = "/api/agent-read/v1/mail"
REPO_ROOT = Path(__file__).resolve().parents[3]
MISTRAL_JSON = REPO_ROOT / "mistral.json"
GATE_CALLABLES = frozenset({require_read_access, ensure_agent_access})


def _dependency_callables(dependant: Dependant) -> set[object]:
    found: set[object] = set()
    stack = list(dependant.dependencies)
    while stack:
        item = stack.pop()
        if item.call is not None:
            found.add(item.call)
        stack.extend(item.dependencies)
    return found


def _mcp_route_prefixes(manifest: dict) -> list[str]:
    prefixes: list[str] = []
    for module in manifest.get("modules", ()):
        for route in module.get("routes", ()):
            if route.get("mcp") is True and isinstance(route.get("path"), str):
                prefixes.append(route["path"])
    return prefixes


def _matches_prefix(path: str, prefix: str) -> bool:
    normalized = prefix.rstrip("/")
    return path == normalized or path.startswith(normalized + "/")


def _unprotected_agent_routes(app: FastAPI, manifest: dict) -> list[str]:
    mcp_prefixes = _mcp_route_prefixes(manifest)
    missing: list[str] = []
    for ctx in iter_route_contexts(app.routes):
        route = ctx.original_route
        path = ctx.path
        if path is None or not isinstance(route, (APIRoute, APIWebSocketRoute)):
            continue
        covered = path.startswith("/api/agent-read/") or any(
            _matches_prefix(path, prefix) for prefix in mcp_prefixes
        )
        if not covered:
            continue
        callables = _dependency_callables(route.dependant)
        effective = getattr(ctx, "route", None)
        if effective is not None and hasattr(effective, "dependant"):
            callables |= _dependency_callables(effective.dependant)
        for dep in getattr(ctx, "dependencies", ()):
            if getattr(dep, "dependency", None) is not None:
                callables.add(dep.dependency)
        if not (callables & GATE_CALLABLES):
            missing.append(path)
    return missing


def _assert_disabled_response(response) -> None:
    assert response.status_code == 403
    assert response.json() == {"detail": "agent_access_disabled"}
    assert response.headers.get("cache-control") == "no-store"
    assert "www-authenticate" not in response.headers


def test_default_off_refuses_every_facade_route_before_verifier_or_services(monkeypatch) -> None:
    """Case 1: default off answers 403 agent_access_disabled without running verifier or services."""
    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=False))

    verifier_calls = 0

    class SpyVerifier:
        async def verify(self, request):
            nonlocal verifier_calls
            verifier_calls += 1
            pytest.fail("verifier reached while agent access is disabled")

    def forbidden(*args, **kwargs):
        pytest.fail("service reached while agent access is disabled")

    for name in ("liam_mail_accounts", "liam_mail_search", "liam_mail_read", "liam_mail_summary"):
        monkeypatch.setattr(service, name, forbidden)

    app = build_app()
    app.dependency_overrides[get_read_access_verifier] = SpyVerifier
    client = TestClient(app)

    for suffix in ("/accounts", "/messages", f"/messages/{uuid4()}", "/summary"):
        _assert_disabled_response(client.get(BASE + suffix))
    assert verifier_calls == 0


def test_enabled_switch_delegates_to_default_denying_verifier(monkeypatch) -> None:
    """Case 2: when on, DenyReadAccess answers 401 as before (gate first, then verifier)."""
    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=True))
    client = TestClient(build_app())

    for suffix in ("/accounts", "/messages", f"/messages/{uuid4()}", "/summary"):
        response = client.get(BASE + suffix)
        assert response.status_code == 401
        assert response.json() == {"detail": "unauthorized"}


def test_on_then_off_refuses_next_request_on_same_client(monkeypatch) -> None:
    """Case 3: turning off takes effect on the next request in the same process and client."""
    switch = {"enabled": True}
    account_id = str(uuid4())
    account = MailAccount(
        id=account_id,
        provider="imap",
        auth_type="imap_password",
        email_address="owner@example.org",
        display_name="Primary Example",
    )
    sync_state = MailSyncState(
        account_id=account_id, status="ok", last_sync_at=datetime(2026, 1, 1, tzinfo=UTC)
    )

    async def read_switch() -> bool:
        return switch["enabled"]

    async def accounts_for(scope):
        return [account] if account_id in scope else []

    async def states_for(scope):
        return [sync_state] if account_id in scope else []

    monkeypatch.setattr(agent_access, "is_enabled", read_switch)
    monkeypatch.setattr(mail_account, "list_for_ids", accounts_for)
    monkeypatch.setattr(mail_sync_state, "list_for_accounts", states_for)

    app = build_app()

    class GrantingVerifier:
        async def verify(self, request):
            return ReadGrant(frozenset({account_id}))

    app.dependency_overrides[get_read_access_verifier] = GrantingVerifier
    client = TestClient(app)

    first = client.get(BASE + "/accounts")
    assert first.status_code == 200
    assert [item["id"] for item in first.json()["accounts"]] == [account_id]

    switch["enabled"] = False
    second = client.get(BASE + "/accounts")
    _assert_disabled_response(second)
    assert account_id not in second.text
    assert "Primary Example" not in second.text


def test_two_app_instances_observe_disable_without_local_cache(monkeypatch) -> None:
    """Case 4 (in-process counterpart): a second app instance sees disable immediately."""
    switch = {"enabled": True}
    account_id = str(uuid4())
    account = MailAccount(
        id=account_id,
        provider="imap",
        auth_type="imap_password",
        email_address="owner@example.org",
        display_name="Shared Example",
    )

    async def read_switch() -> bool:
        return switch["enabled"]

    async def accounts_for(scope):
        return [account] if account_id in scope else []

    async def states_for(scope):
        return []

    monkeypatch.setattr(agent_access, "is_enabled", read_switch)
    monkeypatch.setattr(mail_account, "list_for_ids", accounts_for)
    monkeypatch.setattr(mail_sync_state, "list_for_accounts", states_for)

    class GrantingVerifier:
        async def verify(self, request):
            return ReadGrant(frozenset({account_id}))

    app_a = build_app()
    app_b = build_app()
    app_a.dependency_overrides[get_read_access_verifier] = GrantingVerifier
    app_b.dependency_overrides[get_read_access_verifier] = GrantingVerifier
    client_a = TestClient(app_a)
    client_b = TestClient(app_b)

    assert client_a.get(BASE + "/accounts").status_code == 200
    assert client_b.get(BASE + "/accounts").status_code == 200

    switch["enabled"] = False
    _assert_disabled_response(client_b.get(BASE + "/accounts"))


@pytest.mark.anyio
async def test_direct_read_services_refuse_when_switch_is_off(monkeypatch) -> None:
    """Case 5: calling the four mail services directly with a valid grant still fails closed."""
    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=False))

    def forbidden(*args, **kwargs):
        pytest.fail("database mail accessor called while agent access is disabled")

    monkeypatch.setattr(mail_account, "list_for_ids", forbidden)
    monkeypatch.setattr(mail_sync_state, "list_for_accounts", forbidden)
    monkeypatch.setattr(mail_message, "list_page", forbidden)
    monkeypatch.setattr(mail_message, "get_scoped_message", forbidden)
    monkeypatch.setattr(mail_message, "inbox_counts", forbidden)

    grant = ReadGrant(frozenset({str(uuid4())}))
    calls = (
        service.liam_mail_accounts(grant),
        service.liam_mail_search(grant, query="Example"),
        service.liam_mail_read(grant, str(uuid4())),
        service.liam_mail_summary(grant),
    )
    for call in calls:
        with pytest.raises(HTTPException) as exc_info:
            await call
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "agent_access_disabled"
        assert exc_info.value.headers == {"Cache-Control": "no-store"}


def _capture_access_logs():
    records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _ListHandler(logging.INFO)
    logger = logging.getLogger("api.mcp.access")
    logger.addHandler(handler)
    return logger, handler, records


def test_database_failure_fails_closed_with_503_before_verifier(monkeypatch) -> None:
    """Case 6: a switch lookup error returns 503 agent_access_unavailable without running verifier."""

    async def broken_switch() -> bool:
        raise OperationalError("SELECT 1", {}, Exception("dsn=postgres://secret-test-only"))

    monkeypatch.setattr(agent_access, "is_enabled", broken_switch)

    class SpyVerifier:
        async def verify(self, request):
            pytest.fail("verifier reached after switch read failure")

    app = build_app()
    app.dependency_overrides[get_read_access_verifier] = SpyVerifier
    client = TestClient(app)

    logger, handler, records = _capture_access_logs()
    try:
        for suffix in ("/accounts", "/messages", f"/messages/{uuid4()}", "/summary"):
            response = client.get(BASE + suffix)
            assert response.status_code == 503
            assert response.json() == {"detail": "agent_access_unavailable"}
            assert response.headers.get("cache-control") == "no-store"
            assert "www-authenticate" not in response.headers
            assert "secret-test-only" not in response.text
    finally:
        logger.removeHandler(handler)

    rendered = "\n".join(record.getMessage() for record in records)
    assert "agent_access=unavailable path_group=agent-read" in rendered
    assert "secret-test-only" not in rendered


def test_every_agent_and_mcp_route_has_access_gate_in_dependency_tree() -> None:
    """Case 7: structural check over /api/agent-read/ and mistral.json mcp=true routes."""
    app = build_app()
    manifest = json.loads(MISTRAL_JSON.read_text(encoding="utf-8"))

    agent_routes = [
        ctx.path
        for ctx in iter_route_contexts(app.routes)
        if ctx.path is not None
        and isinstance(ctx.original_route, (APIRoute, APIWebSocketRoute))
        and ctx.path.startswith("/api/agent-read/")
    ]
    assert len(agent_routes) == 4
    assert _unprotected_agent_routes(app, manifest) == []

    # Negative self-checks: an unprotected agent route or mcp=true route must be flagged.
    unprotected_app = build_app()

    @unprotected_app.get("/api/agent-read/v1/unprotected")
    async def unprotected_agent():
        return {}

    assert _unprotected_agent_routes(unprotected_app, manifest) == ["/api/agent-read/v1/unprotected"]

    @unprotected_app.get("/api/mcp/unprotected")
    async def unprotected_mcp():
        return {}

    synthetic_manifest = {
        "modules": [{"name": "api", "routes": [{"path": "/api/mcp", "mcp": True}]}]
    }
    assert sorted(_unprotected_agent_routes(unprotected_app, synthetic_manifest)) == [
        "/api/agent-read/v1/unprotected",
        "/api/mcp/unprotected",
    ]


def test_refusal_log_contains_no_path_params_query_identity_or_headers(monkeypatch) -> None:
    """Case 13: refused requests log only the fixed summary line without caller data."""
    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=False))
    client = TestClient(build_app())

    message_id = "11111111-1111-4111-8111-111111111111"
    sensitive_values = (
        message_id,
        "private-query-test-only",
        "bearer-secret-test-only",
        "apps-token-test-only",
        "user-id-test-only",
        "workspace-key-test-only",
    )

    logger, handler, records = _capture_access_logs()
    try:
        response = client.get(
            f"{BASE}/messages/{message_id}",
            params={"query": "private-query-test-only"},
            headers={
                "Authorization": "Bearer bearer-secret-test-only",
                "x-apps-token": "apps-token-test-only",
                "x-user-id": "user-id-test-only",
                "x-api-key": "workspace-key-test-only",
            },
        )
    finally:
        logger.removeHandler(handler)

    _assert_disabled_response(response)
    messages = [record.getMessage() for record in records]
    assert messages == ["agent_access=refused path_group=agent-read"]
    rendered = "\n".join(messages)
    for value in sensitive_values:
        assert value not in rendered


def test_v1_key_caller_policy_enforces_switch_and_read_only_inventory(monkeypatch) -> None:
    """Case 10: x-api-key callers on /api/v1 respect the switch and read-only GET set."""
    from db.accessors import prompt_template

    switch = {"enabled": False}
    switch_reads = 0

    async def read_switch() -> bool:
        nonlocal switch_reads
        switch_reads += 1
        return switch["enabled"]

    async def fake_prompts():
        return []

    monkeypatch.setattr(agent_access, "is_enabled", read_switch)
    monkeypatch.setattr(prompt_template, "list_templates", fake_prompts)

    client = TestClient(build_app())
    key_headers = {"x-api-key": "workspace-key-test-only"}

    # Without x-api-key: browser calls pass unchanged and never read the switch.
    browser_get = client.get("/api/v1/prompts")
    assert browser_get.status_code == 200
    assert switch_reads == 0

    # Switch off: x-api-key on a read-only GET is refused with 403 agent_access_disabled.
    logger, handler, records = _capture_access_logs()
    try:
        off_get = client.get("/api/v1/prompts", headers=key_headers)
    finally:
        logger.removeHandler(handler)
    _assert_disabled_response(off_get)
    assert switch_reads == 1
    assert [record.getMessage() for record in records] == ["agent_access=refused path_group=v1-key"]
    assert "workspace-key-test-only" not in "\n".join(record.getMessage() for record in records)

    # Switch on: read-only GET passes; browser-only GET and mutations answer 403 agent_operation_refused.
    switch["enabled"] = True
    on_get = client.get("/api/v1/prompts", headers=key_headers)
    assert on_get.status_code == 200

    exposure_logger = logging.getLogger("api.mcp.exposure")
    exposure_records: list[logging.LogRecord] = []

    class _ExposureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            exposure_records.append(record)

    exposure_handler = _ExposureHandler(logging.INFO)
    exposure_logger.addHandler(exposure_handler)
    try:
        refused_calls = (
            client.get("/api/v1/settings", headers=key_headers),
            client.get("/api/v1/settings/agent-access", headers=key_headers),
            client.get("/api/v1/audio/capabilities", headers=key_headers),
            client.post("/api/v1/settings/agent-access/disable", headers=key_headers),
            client.post("/api/v1/settings/agent-access/enable", headers=key_headers, json={"expected_revision": 1}),
            client.post("/api/v1/prompts", headers=key_headers, json={}),
            client.patch("/api/v1/settings/ui", headers=key_headers, json={}),
            client.delete(f"/api/v1/prompts/{uuid4()}", headers=key_headers),
        )
    finally:
        exposure_logger.removeHandler(exposure_handler)

    for response in refused_calls:
        assert response.status_code == 403
        assert response.json() == {"detail": "agent_operation_refused"}
        assert response.headers.get("cache-control") == "no-store"
        assert "www-authenticate" not in response.headers
    assert [record.getMessage() for record in exposure_records] == [
        "agent_access=refused path_group=v1-key"
    ] * len(refused_calls)
    assert "workspace-key-test-only" not in "\n".join(record.getMessage() for record in exposure_records)


@pytest.mark.anyio
async def test_v1_key_caller_policy_fallback_when_scope_route_is_missing(monkeypatch) -> None:
    """Case 10 (route-lookup fallback): matches read-only GETs and fails closed on unknown routes."""
    from starlette.requests import HTTPConnection

    from api.mcp.exposure import agent_caller_policy

    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=True))
    app = build_app()

    # Known read-only GET and HEAD (including parameterized paths) resolve via router fallback.
    allowed_scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/api/v1/prompts",
        "raw_path": b"/api/v1/prompts",
        "query_string": b"",
        "headers": [(b"x-api-key", b"workspace-key-test-only")],
    }
    await agent_caller_policy(HTTPConnection(allowed_scope))
    await agent_caller_policy(HTTPConnection({**allowed_scope, "method": "HEAD"}))
    await agent_caller_policy(
        HTTPConnection({**allowed_scope, "path": f"/api/v1/prompts/{uuid4()}"})
    )

    # Browser-only GET without request.scope["route"] is refused.
    browser_only_scope = {**allowed_scope, "path": "/api/v1/settings", "raw_path": b"/api/v1/settings"}
    with pytest.raises(HTTPException) as exc_info:
        await agent_caller_policy(HTTPConnection(browser_only_scope))
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "agent_operation_refused"

    # Unknown path fails closed.
    unknown_scope = {**allowed_scope, "path": "/api/v1/does-not-exist", "raw_path": b"/api/v1/does-not-exist"}
    with pytest.raises(HTTPException) as exc_info:
        await agent_caller_policy(HTTPConnection(unknown_scope))
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "agent_operation_refused"


@pytest.mark.parametrize("enabled", [False, True])
def test_v1_live_websocket_refuses_x_api_key_handshake(monkeypatch, enabled: bool) -> None:
    """Case 10 (WebSocket): x-api-key on /api/v1 live audio websocket refuses the handshake."""
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=enabled))
    client = TestClient(build_app())

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/v1/audio/live/transcription",
            headers={"x-api-key": "workspace-key-test-only"},
        ):
            pass
