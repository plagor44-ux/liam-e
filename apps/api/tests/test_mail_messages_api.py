from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from db.models.mail_message import MailMessage

from api.routers.api.v1.mail import messages


def message(**kwargs):
    return MailMessage(id="m1", account_id="a1", provider_uid="1", **kwargs)


@pytest.fixture
def access(monkeypatch):
    page = AsyncMock(return_value=[])
    get = AsyncMock(return_value=None)
    insights = AsyncMock(return_value={})
    accounts = AsyncMock(return_value=[])
    monkeypatch.setattr(messages.message_access, "list_page", page)
    monkeypatch.setattr(messages.message_access, "get_message", get)
    monkeypatch.setattr(messages.mail_reference_snapshot, "read", AsyncMock(return_value=None))
    monkeypatch.setattr(messages.insight_access, "get_for_messages", insights)
    monkeypatch.setattr(messages.account_access, "list_accounts", accounts)
    monkeypatch.setattr(messages.filing_access, "for_messages", AsyncMock(return_value={}))
    return SimpleNamespace(page=page, get=get, insights=insights, accounts=accounts)


def test_cursor_roundtrip():
    for dt in (datetime(2026, 1, 2, tzinfo=UTC), None):
        row = message(internal_date=dt)
        assert messages.decode_cursor(messages.encode_cursor(row)) == (dt or datetime(1970, 1, 1, tzinfo=UTC), row.id)


@pytest.mark.parametrize("cursor", ["!!!", "bm90LWRhdGV8aWQ", "MjAyNi0wMS0wMXw", "MjAyNi0wMS0wMXxpZA"])
def test_messages_list_rejects_bad_cursor(client, access, cursor):
    response = client.get("/api/v1/mail/messages", params={"cursor": cursor})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "cursor_invalid"
    access.page.assert_not_awaited()


def test_messages_list_maps_filters_to_accessor(client, access):
    response = client.get("/api/v1/mail/messages", params=[("account_id", "a1"), ("account_id", "a2"), ("unread", "true"), ("important", "true"), ("attachments", "true"), ("q", "Rechnung"), ("folder", "ARCHIVE")])
    assert response.status_code == 200
    access.page.assert_awaited_once_with(folder="ARCHIVE", account_ids=["a1", "a2"], unread_only=True, important_only=True, with_attachments=True, query="Rechnung", before=None, limit=50, hide_filed=False, app_folder_id=None, app_label_id=None)


def test_messages_list_sets_next_cursor_only_when_page_full(client, access):
    access.page.return_value = [message()]
    response = client.get("/api/v1/mail/messages", params={"limit": 1})
    assert response.status_code == 200
    cursor = response.json()["next_cursor"]
    assert messages.decode_cursor(cursor) == (datetime(1970, 1, 1, tzinfo=UTC), "m1")
    response = client.get("/api/v1/mail/messages", params={"limit": 2, "cursor": cursor})
    assert response.json()["next_cursor"] is None
    assert access.page.await_args.kwargs["before"] == (datetime(1970, 1, 1, tzinfo=UTC), "m1")
    assert access.page.await_args.kwargs["account_ids"] is None


def test_message_list_and_detail_expose_filing(client, access):
    access.page.return_value = [message()]
    access.get.return_value = message()
    messages.filing_access.for_messages.return_value = {"m1": messages.filing_access.Filing("folder", ["label"])}
    for data in (client.get('/api/v1/mail/messages').json()['items'][0], client.get('/api/v1/mail/messages/m1').json()):
        assert data['app_folder_id'] == 'folder'
        assert data['app_label_ids'] == ['label']


def test_message_get_includes_attachments_and_insight_deadline(client, access):
    access.get.return_value = message(sender="Ada <ada@example.test>", sender_address="ada@example.test", recipients_json='["bob@example.test"]', cc_json='["c@example.test"]', attachments_json='[{"name":"test.txt","size":12,"content_type":"text/plain"}]', list_unsubscribe="<https://example.test/unsubscribe>")
    access.insights.return_value = {"m1": SimpleNamespace(deadline=date(2026, 2, 3), needs_reply=True)}
    response = client.get("/api/v1/mail/messages/m1")
    assert response.status_code == 200
    data = response.json()
    assert data["sender_name"] == "Ada"
    assert data["sender_address"] == "ada@example.test"
    assert data["to"] == ["bob@example.test"] and data["cc"] == ["c@example.test"]
    assert data["attachments"] == [{"name": "test.txt", "size": 12, "content_type": "text/plain"}]
    assert data["deadline"] == "2026-02-03" and data["needs_reply"] is True
    assert data["can_unsubscribe"] is True


def test_missing_message_and_invalid_limits(client, access):
    assert client.get("/api/v1/mail/messages/missing").status_code == 404
    for limit in (0, 101):
        assert client.get("/api/v1/mail/messages", params={"limit": limit}).status_code == 422
