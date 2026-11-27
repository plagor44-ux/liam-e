"""Runtime contracts for the independently protected, read-only mail facade."""

import logging
import socket
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from api.mail import insights, models_catalog, original
from api.mcp import mail as service
from api.mcp.access import ReadGrant, get_read_access_verifier
from api.mcp.access_log import RedactAgentReadQuery
from conftest import build_app
from db.accessors import agent_access, mail_account, mail_message, mail_sync_state
from db.models.mail_account import MailAccount
from db.models.mail_message import MailMessage
from db.models.mail_sync_state import MailSyncState


@pytest.fixture
def indexed(monkeypatch):
    ids = [str(uuid4()) for _ in range(3)]
    accounts = [MailAccount(id=id, provider="imap", auth_type="imap_password", email_address=f"{i}@example.org", display_name=f"Account {i}") for i, id in enumerate(ids)]
    rows = [MailMessage(id=str(uuid4()), account_id=ids[i], provider_uid=str(n), subject="Example", sender="Sender", snippet="x" * 501,
                        body_text="a" * 20001, internal_date=datetime(2026, 1, n + 1, tzinfo=UTC))
            for i, n in [(0, 0), (0, 1), (1, 2), (2, 3)]]
    state = MailSyncState(account_id=ids[0], status="ok", last_sync_at=datetime(2026, 1, 1, tzinfo=UTC))
    calls = []

    async def accounts_for(scope):
        calls.append("accounts")
        return [a for a in accounts if a.id in scope]

    async def states_for(scope):
        calls.append("states")
        return [state] if ids[0] in scope else []

    async def page(*, folder, account_ids, query, before, limit):
        calls.append("page")
        found = sorted((r for r in rows if r.account_id in account_ids and r.folder == folder and query.lower() in r.subject.lower()),
                       key=lambda r: (r.internal_date, r.id), reverse=True)
        if before:
            found = [r for r in found if (r.internal_date, r.id) < before]
        return found[:limit]

    async def get(id, scope, *, folder):
        calls.append("get")
        return next((r for r in rows if r.id == id and r.account_id in scope and r.folder == folder), None)

    async def counts(scope):
        calls.append("counts")
        return {id: (sum(r.account_id == id for r in rows), sum(r.account_id == id and not r.is_read for r in rows)) for id in scope}

    def forbidden(*args, **kwargs):
        pytest.fail("Provider, model or write operation was called")

    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(mail_account, "list_for_ids", accounts_for)
    monkeypatch.setattr(mail_sync_state, "list_for_accounts", states_for)
    monkeypatch.setattr(mail_message, "list_page", page)
    monkeypatch.setattr(mail_message, "get_scoped_message", get)
    monkeypatch.setattr(mail_message, "inbox_counts", counts)
    monkeypatch.setattr(Session, "flush", forbidden)
    monkeypatch.setattr(Session, "commit", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(insights, "get_or_compute", forbidden)
    monkeypatch.setattr(original, "fetch", forbidden)
    monkeypatch.setattr(models_catalog, "list_chat_models", forbidden)
    return ids, rows, calls


@pytest.fixture
def authorized(indexed):
    ids, rows, calls = indexed
    app = build_app()

    class Verifier:
        async def verify(self, request):
            return ReadGrant(frozenset(ids[:2]))

    app.dependency_overrides[get_read_access_verifier] = Verifier
    return TestClient(app), ids, rows, calls


BASE = "/api/agent-read/v1/mail"


def test_uvicorn_access_log_hides_query_without_changing_other_routes():
    redact = RedactAgentReadQuery()
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %s',
                               ("client", "GET", BASE + "/messages?query=private-text", "1.1", 200), None)
    assert redact.filter(record)
    assert "private-text" not in record.getMessage() and len(record.args) == 5
    other = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %s',
                              ("client", "GET", "/api/health?x=1", "1.1", 200), None)
    assert redact.filter(other) and "?x=1" in other.getMessage()


def test_access_log_redacts_query_in_an_unexpected_record_shape():
    redact = RedactAgentReadQuery()
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%(client)s "%(line)s" %(status)s',
                               ({"client": "client", "line": "GET " + BASE + "/messages?query=private-text HTTP/1.1", "status": 200},), None)
    assert redact.filter(record)
    assert "private-text" not in record.getMessage() and BASE + "/messages" in record.getMessage()
    plain = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "GET " + BASE + "/summary?account_ids=x 200", None, None)
    assert redact.filter(plain) and "account_ids" not in plain.getMessage()
    other = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "GET /api/health?x=1 200", None, None)
    assert redact.filter(other) and other.getMessage() == "GET /api/health?x=1 200"
    quoted = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "GET " + BASE + "/messages?query=he\"llo it's HTTP/1.1 200", None, None)
    assert redact.filter(quoted) and "llo" not in quoted.getMessage() and quoted.getMessage().startswith("GET " + BASE + "/messages ")
    moved = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '"%s %s" %s - %s %s',
                              ("GET", BASE + "/summary?account_ids=private", 200, "client", "1.1"), None)
    assert redact.filter(moved) and "private" not in moved.getMessage() and len(moved.args) == 5
    split = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '"%s?%s" %s', (BASE + "/summary", "account_ids=private", 200), None)
    assert redact.filter(split) and "private" not in split.getMessage()
    rendered = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "GET " + BASE + "/messages?query=private %s", (200,), None)
    assert redact.filter(rendered) and "private" not in rendered.getMessage() and rendered.getMessage().endswith(" 200")
    raw = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s %s", ((BASE + "/messages?query=private").encode(), 200), None)
    assert redact.filter(raw) and "private" not in raw.getMessage()


def test_default_denies_before_any_service(indexed, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("service reached without access")
    for name in ("liam_mail_accounts", "liam_mail_search", "liam_mail_read", "liam_mail_summary"):
        monkeypatch.setattr(service, name, forbidden)
    client = TestClient(build_app())
    for suffix in ("/accounts", "/messages", "/messages/invalid", "/summary"):
        assert client.get(BASE + suffix).status_code == 401
    assert indexed[2] == []


def test_malformed_grant_is_denied_before_services(indexed, monkeypatch):
    app = build_app()

    class InvalidVerifier:
        async def verify(self, request):
            return ReadGrant(frozenset({"x" * 65}))

    app.dependency_overrides[get_read_access_verifier] = InvalidVerifier
    assert TestClient(app).get(BASE + "/accounts").status_code == 401
    assert indexed[2] == []


def test_four_reads_scope_pagination_and_missing_state(authorized):
    client, ids, rows, calls = authorized
    data = client.get(BASE + "/accounts").json()
    assert {a["id"] for a in data["accounts"]} == set(ids[:2])
    assert data["schema_version"] == 1 and data["scope"] == sorted(ids[:2])
    assert all(i["incomplete"] and i["stale"] for i in data["index"])
    assert "index_incomplete" in next(i for i in data["index"] if i["account_id"] == ids[1])["warnings"]
    assert "index_completeness_unknown" in next(i for i in data["index"] if i["account_id"] == ids[0])["warnings"]
    first = client.get(BASE + "/messages", params={"query": "Example", "limit": 1}).json()
    assert len(first["untrusted_messages"]) == 1 and first["truncated"]
    assert first["untrusted_messages"][0]["truncated"]
    assert len(first["untrusted_messages"][0]["snippet"]) == 500
    second = client.get(BASE + "/messages", params={"query": "Example", "limit": 1, "cursor": first["next_cursor"]}).json()
    assert first["untrusted_messages"][0]["id"] != second["untrusted_messages"][0]["id"]
    assert client.get(BASE + "/messages", params={"query": "different", "cursor": first["next_cursor"]}).status_code == 422
    assert client.get(BASE + "/messages", params={"query": "Example", "account_ids": ids[0], "cursor": first["next_cursor"]}).status_code == 422
    assert client.get(BASE + "/messages", params={"account_ids": ids[2]}).status_code == 403
    assert client.get(BASE + "/summary", params={"account_ids": ids[2]}).status_code == 403
    assert client.get(BASE + "/summary", params={"account_ids": ids[2]}).json() == client.get(BASE + "/summary", params={"account_ids": str(uuid4())}).json()
    assert client.get(BASE + "/messages", params={"cursor": "not-base64!"}).status_code == 422
    assert client.get(BASE + "/messages/" + rows[3].id).json() == client.get(BASE + "/messages/" + str(uuid4())).json() == {"detail": "message_not_found"}
    detail = client.get(BASE + "/messages/" + rows[0].id).json()
    assert len(detail["body_text"]) == 20000 and detail["truncated"]
    assert detail["untrusted_message"]["app_link"] == "/mail/" + rows[0].id
    summary = client.get(BASE + "/summary").json()
    assert summary["total"] == 3 and summary["unread"] == 3
    assert client.get(BASE + "/messages", params={"query": "nothing"}).json()["untrusted_messages"] == []
    assert client.get(BASE + "/messages", params={"limit": 51}).status_code == 422
    assert client.get(BASE + "/messages", params={"limit": 0}).status_code == 422
    invalid = client.get(BASE + "/messages", params={"query": "s" * 10000})
    assert invalid.status_code == 422 and invalid.json() == {"detail": "invalid_request"}
    assert len(invalid.content) < 100
    rows[0].body_text = ""
    empty = client.get(BASE + "/messages/" + rows[0].id).json()
    assert empty["untrusted_message"]["folder"] == "INBOX" and not empty["body_available"]
    rows[0].folder = "ARCHIVE"
    archived = client.get(BASE + "/messages/" + rows[0].id)
    missing = client.get(BASE + "/messages/" + str(uuid4()))
    assert archived.status_code == missing.status_code == 404 and archived.json() == missing.json()
    assert set(calls) == {"accounts", "states", "page", "get", "counts"}


def test_response_byte_limit_and_explicit_truncation(authorized):
    client, ids, rows, calls = authorized
    for row in rows[:3]:
        row.body_text = "\u0001" * 20000
        row.snippet = "\u0001" * 1000
        row.subject = "\u0001" * 1000
        row.sender = "\u0001" * 1000
    response = client.get(BASE + "/messages/" + rows[0].id)
    assert response.status_code == 200 and len(response.content) <= service.MAX_RESPONSE_BYTES
    assert response.json()["truncated"]
    for i in range(45):
        rows.append(MailMessage(id=str(uuid4()), account_id=ids[0], provider_uid=f"extra-{i}",
                                subject="\u0001" * 200, sender="\u0001" * 200, snippet="\u0001" * 500,
                                internal_date=datetime(2026, 2, 1, tzinfo=UTC)))
    seen = []
    cursor = None
    while True:
        response = client.get(BASE + "/messages", params={"cursor": cursor} if cursor else {})
        assert response.status_code == 200 and len(response.content) <= service.MAX_RESPONSE_BYTES
        data = response.json()
        assert data["truncated"]
        seen.extend(item["id"] for item in data["untrusted_messages"])
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 48
