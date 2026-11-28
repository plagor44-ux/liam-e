"""End-to-end and negative runtime tests for the production ``StudioMcpReadVerifier`` binding.

Drives the real ``get_read_access_verifier()`` (no dependency override), the real
``install_gateway_credentials`` middleware (``x-apps-token`` -> ``caller_credentials()``), the
real ``api.caller_identity.caller_identity`` lookup (mocked only at the outbound ``/v1/users/me``
HTTP boundary with ``respx``), and verifies the flagged ``/api/mcp`` route in ``mistral.json``.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from mistralai_capabilities.apps import credentials as gateway_credentials
from mistralai_capabilities.apps.credentials import install_gateway_credentials
from sqlalchemy.exc import OperationalError

import api.caller_identity as identity_module
from api.mcp.access import DenyReadAccess, get_read_access_verifier
from api.mcp.transport import MCP_MOUNT_PATH
from api.mcp.verifier import StudioMcpReadVerifier, mcp_owner_binding
from conftest import build_app
from db.accessors import agent_access, mail_account, mail_message, mail_sync_state, mcp_settings
from db.models.agent_access import AgentAccess
from db.models.mail_account import MailAccount
from db.models.mail_message import MailMessage
from db.models.mail_sync_state import MailSyncState
from env.audio import env as audio_env
from env.mcp import env as mcp_env

REPO_ROOT = Path(__file__).resolve().parents[3]
MISTRAL_JSON = REPO_ROOT / "mistral.json"

PROXY_URL = "https://gateway.example.test/mistral"
WHO_URL = f"{PROXY_URL}/v1/users/me"
OWNER_USER = UUID("11111111-1111-4111-8111-111111111111")
OWNER_ORG = UUID("22222222-2222-4222-8222-222222222222")
OWNER_WS = UUID("33333333-3333-4333-8333-333333333333")
OWNER_JSON = json.dumps(
    {
        "user_id": str(OWNER_USER),
        "organization_id": str(OWNER_ORG),
        "workspace_id": str(OWNER_WS),
    }
)
OWNER_ME = {
    "id": str(OWNER_USER),
    "organization": {"id": str(OWNER_ORG)},
    "workspace": {"id": str(OWNER_WS)},
    "api_key": None,
}
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _mcp_headers(method: str = "tools/call", name: str | None = "liam_mail_accounts", **extra: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    headers.update(extra)
    return headers


def _rpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    body_params = dict(params or {})
    body_params["_meta"] = MODERN_META
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": body_params}


def _accounts_rpc(req_id: int = 1) -> dict:
    return _rpc("tools/call", {"name": "liam_mail_accounts", "arguments": {}}, req_id=req_id)


def _gateway_app(monkeypatch):
    """Build the real app with gateway credential forwarding installed and NO verifier override."""
    monkeypatch.setattr(gateway_credentials.env, "apps_proxy_url", PROXY_URL)
    app = build_app()
    install_gateway_credentials(app)
    assert get_read_access_verifier not in app.dependency_overrides
    return app


def test_mistral_json_flags_api_mcp_without_tenant_ids() -> None:
    """mistral.json flags /api/mcp with mcp=true on the api module and carries no deployment block."""
    manifest = json.loads(MISTRAL_JSON.read_text(encoding="utf-8"))
    assert "deployment" not in manifest
    api_modules = [module for module in manifest["modules"] if module.get("name") == "api"]
    assert len(api_modules) == 1
    routes = api_modules[0]["routes"]
    assert {"path": "/api"} in routes
    flagged = [route for route in routes if route.get("mcp") is True]
    assert flagged == [{"path": MCP_MOUNT_PATH, "mcp": True}]


def test_binding_defaults_fail_closed_and_independent_from_audio(monkeypatch) -> None:
    """Unset, malformed, oversized, or audio-only bindings keep DenyReadAccess bound."""
    monkeypatch.setattr(mcp_env, "mcp_studio_owner", None)
    assert mcp_owner_binding() is None
    assert isinstance(get_read_access_verifier(), DenyReadAccess)

    # AUDIO_STUDIO_OWNER alone never activates the MCP verifier (D5 independence).
    monkeypatch.setattr(
        audio_env,
        "audio_studio_owner",
        json.dumps(
            {
                "user_id": str(OWNER_USER),
                "organization_id": str(OWNER_ORG),
                "workspace_id": str(OWNER_WS),
                "origin": "https://app.example.test",
            }
        ),
    )
    assert mcp_owner_binding() is None
    assert isinstance(get_read_access_verifier(), DenyReadAccess)

    # Extra fields (like origin), invalid JSON, bad UUIDs, and oversized values fail closed.
    for bad_value in (
        "not-json",
        json.dumps(
            {
                "user_id": str(OWNER_USER),
                "organization_id": str(OWNER_ORG),
                "workspace_id": str(OWNER_WS),
                "origin": "https://app.example.test",
            }
        ),
        json.dumps({"user_id": "not-a-uuid", "organization_id": str(OWNER_ORG), "workspace_id": str(OWNER_WS)}),
        " " * 1025,
    ):
        monkeypatch.setattr(mcp_env, "mcp_studio_owner", bad_value)
        assert mcp_owner_binding() is None
        assert isinstance(get_read_access_verifier(), DenyReadAccess)

    # Valid MCP_STUDIO_OWNER selects StudioMcpReadVerifier.
    monkeypatch.setattr(mcp_env, "mcp_studio_owner", OWNER_JSON)
    assert mcp_owner_binding() is not None
    assert isinstance(get_read_access_verifier(), StudioMcpReadVerifier)


def test_e2e_real_verifier_binding_with_flagged_route_and_empty_grant(monkeypatch) -> None:
    """End-to-end: flagged /api/mcp + real gateway middleware + real StudioMcpReadVerifier + /v1/users/me."""
    manifest = json.loads(MISTRAL_JSON.read_text(encoding="utf-8"))
    api_routes = next(m["routes"] for m in manifest["modules"] if m["name"] == "api")
    flagged_path = next(r["path"] for r in api_routes if r.get("mcp") is True)
    assert flagged_path == MCP_MOUNT_PATH

    account_id = str(uuid4())
    message_id = str(uuid4())
    account = MailAccount(
        id=account_id,
        provider="imap",
        auth_type="imap_password",
        email_address="owner@example.org",
        display_name="Owner Inbox",
    )
    row = MailMessage(
        id=message_id,
        account_id=account_id,
        provider_uid="1",
        subject="Synthetic update",
        sender="sender@example.org",
        snippet="Short synthetic snippet",
        body_text="Synthetic inbox body",
        internal_date=datetime(2026, 2, 1, tzinfo=UTC),
        is_read=False,
    )
    sync_state = MailSyncState(account_id=account_id, status="ok", last_sync_at=datetime(2026, 2, 1, tzinfo=UTC))
    grant_box = {"ids": frozenset({account_id})}

    monkeypatch.setattr(mcp_env, "mcp_studio_owner", OWNER_JSON)
    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(mcp_settings, "effective_account_ids", AsyncMock(side_effect=lambda: grant_box["ids"]))
    monkeypatch.setattr(mail_account, "list_for_ids", AsyncMock(side_effect=lambda scope: [account] if account_id in scope else []))
    monkeypatch.setattr(mail_sync_state, "list_for_accounts", AsyncMock(side_effect=lambda scope: [sync_state] if account_id in scope else []))
    monkeypatch.setattr(mail_message, "list_page", AsyncMock(side_effect=lambda **kw: [row] if account_id in kw["account_ids"] else []))
    monkeypatch.setattr(mail_message, "get_scoped_message", AsyncMock(side_effect=lambda mid, scope, **kw: row if mid == message_id and account_id in scope else None))
    monkeypatch.setattr(mail_message, "inbox_counts", AsyncMock(side_effect=lambda scope: {account_id: (1, 1)} if account_id in scope else {}))
    monkeypatch.setattr(
        agent_access,
        "state",
        AsyncMock(return_value=AgentAccess(enabled=True, revision=2, changed_at=datetime(2026, 2, 1, tzinfo=UTC))),
    )

    recorded: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            recorded.append(record)

    handler = _Handler(logging.DEBUG)
    observed = [logging.getLogger("api.mcp.verifier"), logging.getLogger("api.caller_identity")]
    for logger in observed:
        logger.addHandler(handler)

    try:
        with respx.mock(assert_all_called=False) as router:
            who = router.get(WHO_URL).mock(return_value=httpx.Response(200, json=OWNER_ME))
            app = _gateway_app(monkeypatch)
            with TestClient(app, base_url="http://localhost:3000") as client:
                token_hdr = {"x-apps-token": "owner-mcp-token-test-only"}

                # tools/list works with no Origin header (server-side client like Vibe Code).
                listed = client.post(
                    flagged_path,
                    headers=_mcp_headers("tools/list", None, **token_hdr),
                    json=_rpc("tools/list", req_id=1),
                )
                assert listed.status_code == 200
                assert [t["name"] for t in listed.json()["result"]["tools"]] == [
                    "liam_mail_accounts",
                    "liam_mail_search",
                    "liam_mail_read",
                    "liam_mail_summary",
                ]
                assert who.call_count == 1
                assert who.calls.last.request.headers["authorization"] == "Bearer owner-mcp-token-test-only"

                # Granted account returns indexed mail through the real verifier binding.
                accounts_res = client.post(flagged_path, headers=_mcp_headers(**token_hdr), json=_accounts_rpc(2))
                assert accounts_res.status_code == 200
                assert [a["id"] for a in accounts_res.json()["result"]["structuredContent"]["accounts"]] == [account_id]

                read_res = client.post(
                    flagged_path,
                    headers=_mcp_headers("tools/call", "liam_mail_read", **token_hdr),
                    json=_rpc("tools/call", {"name": "liam_mail_read", "arguments": {"message_id": message_id}}, req_id=3),
                )
                assert read_res.status_code == 200
                assert read_res.json()["result"]["structuredContent"]["untrusted_message"]["id"] == message_id

                # Empty grant returns empty results on the very next call.
                grant_box["ids"] = frozenset()
                empty_accounts = client.post(flagged_path, headers=_mcp_headers(**token_hdr), json=_accounts_rpc(4))
                assert empty_accounts.status_code == 200
                assert empty_accounts.json()["result"]["structuredContent"]["accounts"] == []

                empty_summary = client.post(
                    flagged_path,
                    headers=_mcp_headers("tools/call", "liam_mail_summary", **token_hdr),
                    json=_rpc("tools/call", {"name": "liam_mail_summary", "arguments": {}}, req_id=5),
                )
                assert empty_summary.status_code == 200
                assert empty_summary.json()["result"]["structuredContent"]["total"] == 0

                # agent_access_get reports verifier_configured=True when MCP_STUDIO_OWNER is configured.
                status_resp = client.get("/api/v1/settings/agent-access", headers=token_hdr)
                assert status_resp.status_code == 200
                assert status_resp.json()["verifier_configured"] is True
    finally:
        for logger in observed:
            logger.removeHandler(handler)

    rendered = "\n".join(rec.getMessage() for rec in recorded)
    for sensitive in (str(OWNER_USER), str(OWNER_ORG), str(OWNER_WS), "owner-mcp-token-test-only", "Synthetic inbox body"):
        assert sensitive not in rendered


def test_negative_switch_off_refuses_403_before_verifier_or_users_me(monkeypatch) -> None:
    """Switch off -> 403 agent_access_disabled with zero outbound /v1/users/me calls."""
    monkeypatch.setattr(mcp_env, "mcp_studio_owner", OWNER_JSON)
    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=False))

    with respx.mock(assert_all_called=False) as router:
        who = router.get(WHO_URL).mock(return_value=httpx.Response(200, json=OWNER_ME))
        app = _gateway_app(monkeypatch)
        with TestClient(app, base_url="http://localhost:3000") as client:
            response = client.post(
                MCP_MOUNT_PATH,
                headers=_mcp_headers(**{"x-apps-token": "owner-mcp-token-test-only"}),
                json=_accounts_rpc(),
            )
            assert response.status_code == 403
            assert response.json() == {"detail": "agent_access_disabled"}
            assert response.headers.get("cache-control") == "no-store"
            assert "www-authenticate" not in response.headers
        assert who.call_count == 0


def test_negative_no_binding_returns_401_before_users_me(monkeypatch) -> None:
    """Missing MCP_STUDIO_OWNER keeps DenyReadAccess -> 401 without calling /v1/users/me."""
    monkeypatch.setattr(mcp_env, "mcp_studio_owner", None)
    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=True))

    with respx.mock(assert_all_called=False) as router:
        who = router.get(WHO_URL).mock(return_value=httpx.Response(200, json=OWNER_ME))
        app = _gateway_app(monkeypatch)
        with TestClient(app, base_url="http://localhost:3000") as client:
            response = client.post(
                MCP_MOUNT_PATH,
                headers=_mcp_headers(**{"x-apps-token": "owner-mcp-token-test-only"}),
                json=_accounts_rpc(),
            )
            assert response.status_code == 401
            assert response.json() == {"detail": "unauthorized"}
            assert response.headers.get("cache-control") == "no-store"
        assert who.call_count == 0


@pytest.mark.parametrize(
    ("case_id", "headers", "me_response"),
    [
        ("x-api-key-header", {"x-apps-token": "tok-key-hdr", "x-api-key": "workspace-key-test-only"}, httpx.Response(200, json=OWNER_ME)),
        ("workspace-api-key-kind", {"x-apps-token": "tok-api-key"}, httpx.Response(200, json={**OWNER_ME, "api_key": {"id": "55555555-5555-4555-8555-555555555555"}})),
        ("foreign-user", {"x-apps-token": "tok-foreign-user"}, httpx.Response(200, json={**OWNER_ME, "id": "99999999-9999-4999-8999-999999999999"})),
        ("foreign-org", {"x-apps-token": "tok-foreign-org"}, httpx.Response(200, json={**OWNER_ME, "organization": {"id": "99999999-9999-4999-8999-999999999999"}})),
        ("foreign-workspace", {"x-apps-token": "tok-foreign-ws"}, httpx.Response(200, json={**OWNER_ME, "workspace": {"id": "99999999-9999-4999-8999-999999999999"}})),
        ("users-me-401-rejected", {"x-apps-token": "tok-rejected"}, httpx.Response(401)),
        ("users-me-500-unavailable", {"x-apps-token": "tok-500"}, httpx.Response(500)),
        ("missing-apps-token", {}, httpx.Response(200, json=OWNER_ME)),
    ],
    ids=lambda item: item if isinstance(item, str) else "",
)
def test_negative_workspace_key_foreign_rejected_and_missing_callers_return_401(
    monkeypatch, case_id: str, headers: dict[str, str], me_response: httpx.Response
) -> None:
    """Workspace keys, foreign users, rejected/unavailable lookups, and missing tokens all get 401."""
    monkeypatch.setattr(mcp_env, "mcp_studio_owner", OWNER_JSON)
    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        mcp_settings,
        "effective_account_ids",
        AsyncMock(side_effect=AssertionError("mcp_settings must not be queried for refused callers")),
    )

    with respx.mock(assert_all_called=False) as router:
        who = router.get(WHO_URL).mock(return_value=me_response)
        app = _gateway_app(monkeypatch)
        with TestClient(app, base_url="http://localhost:3000") as client:
            response = client.post(MCP_MOUNT_PATH, headers=_mcp_headers(**headers), json=_accounts_rpc())
            assert response.status_code == 401, case_id
            assert response.json() == {"detail": "unauthorized"}
            assert response.headers.get("cache-control") == "no-store"
        if case_id in {"x-api-key-header", "missing-apps-token"}:
            assert who.call_count == 0


def test_negative_stale_cached_owner_is_never_accepted_after_lookup_outage(monkeypatch) -> None:
    """StudioMcpReadVerifier passes allow_stale=False: once fresh TTL expires, an outage gets 401."""
    now = [1000.0]
    monkeypatch.setattr(identity_module, "_now", lambda: now[0])
    monkeypatch.setattr(mcp_env, "mcp_studio_owner", OWNER_JSON)
    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(mcp_settings, "effective_account_ids", AsyncMock(return_value=frozenset()))

    with respx.mock(assert_all_called=False) as router:
        who = router.get(WHO_URL).mock(return_value=httpx.Response(200, json=OWNER_ME))
        app = _gateway_app(monkeypatch)
        with TestClient(app, base_url="http://localhost:3000") as client:
            hdrs = _mcp_headers(**{"x-apps-token": "stale-check-token-test-only"})
            ok = client.post(MCP_MOUNT_PATH, headers=hdrs, json=_accounts_rpc(1))
            assert ok.status_code == 200

            # Expire fresh TTL and fail /v1/users/me: must NOT use stale cached USER verdict.
            now[0] += identity_module.FRESH_SECONDS[identity_module.CallerKind.USER] + 5.0
            who.mock(return_value=httpx.Response(503))
            refused = client.post(MCP_MOUNT_PATH, headers=hdrs, json=_accounts_rpc(2))
            assert refused.status_code == 401
            assert refused.json() == {"detail": "unauthorized"}


def test_negative_grant_database_error_fails_closed_with_503(monkeypatch) -> None:
    """If mcp_settings cannot be read, StudioMcpReadVerifier fails closed with 503."""
    monkeypatch.setattr(mcp_env, "mcp_studio_owner", OWNER_JSON)
    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        mcp_settings,
        "effective_account_ids",
        AsyncMock(side_effect=OperationalError("SELECT 1", {}, Exception("db-secret-test-only"))),
    )

    with respx.mock(assert_all_called=False) as router:
        router.get(WHO_URL).mock(return_value=httpx.Response(200, json=OWNER_ME))
        app = _gateway_app(monkeypatch)
        with TestClient(app, base_url="http://localhost:3000") as client:
            response = client.post(
                MCP_MOUNT_PATH,
                headers=_mcp_headers(**{"x-apps-token": "db-err-token-test-only"}),
                json=_accounts_rpc(),
            )
            assert response.status_code == 503
            assert response.json() == {"detail": "mcp_grant_unavailable"}
            assert response.headers.get("cache-control") == "no-store"
            assert "db-secret-test-only" not in response.text
