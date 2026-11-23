"""Incremental inbox sync contracts; all providers are faked."""

import imaplib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from api.mail import secrets, sync, sync_connector, sync_gmail, sync_graph, sync_imap, sync_types
from support.fake_imap import FakeIMAP
from utils.mistral import MistralNotConfiguredError


@pytest.fixture(autouse=True)
def empty_connector_archive(monkeypatch):
    monkeypatch.setattr(sync_connector.message_access, "trash_origins", AsyncMock(return_value={}))
    monkeypatch.setattr(sync_connector.message_access, "trash_conflicts", AsyncMock(return_value={}))
    monkeypatch.setattr(sync_connector.message_access, "archived_connector_members", AsyncMock(return_value=[]))


def account(provider="imap", auth_type="imap_password"):
    return SimpleNamespace(id="a", provider=provider, auth_type=auth_type, email_address="a@example.org")


async def imap_run(monkeypatch, server, cursor="", known=None, limit=3):
    async def credentials(_):
        return {"imap_host": "example.org", "password": "secret"}
    async def password(_):
        return "secret"
    async def check(_):
        pass
    async def uids(*_):
        return known or {}
    monkeypatch.setattr(sync_imap.service, "load_credentials", credentials)
    monkeypatch.setattr(sync_imap.service, "imap_password", password)
    monkeypatch.setattr(sync_imap.transport, "check_mail_host", check)
    monkeypatch.setattr(sync_imap.transport, "open_imap", lambda *_: server)
    monkeypatch.setattr(sync_imap.message_access, "known_uids", uids)
    return await sync_imap.fetch_changes(account(), cursor=cursor, folders=["INBOX"], limit=limit, max_body_chars=100)


@pytest.mark.anyio
async def test_imap_first_sync_takes_newest_limit(monkeypatch):
    changes = await imap_run(monkeypatch, FakeIMAP(range(1, 7)))
    assert [m["provider_uid"] for m in changes.upserts] == ["4", "5", "6"]
    assert changes.full_rebuild


@pytest.mark.anyio
async def test_imap_incremental_fetches_only_new_uids(monkeypatch):
    changes = await imap_run(monkeypatch, FakeIMAP(range(118, 124)), "7:120")
    assert [m["provider_uid"] for m in changes.upserts] == ["121", "122", "123"]


@pytest.mark.anyio
async def test_imap_incremental_cursor_stops_at_limit(monkeypatch):
    changes = await imap_run(monkeypatch, FakeIMAP(range(118, 124)), "7:120", limit=2)
    assert [m["provider_uid"] for m in changes.upserts] == ["121", "122"]
    assert changes.cursor == "7:122"


@pytest.mark.anyio
async def test_imap_uidvalidity_change_rebuilds(monkeypatch):
    assert (await imap_run(monkeypatch, FakeIMAP([1], validity=8), "7:120")).full_rebuild


@pytest.mark.anyio
async def test_imap_flags_update_read_state_and_detect_moved(monkeypatch):
    server = FakeIMAP([118, 120], flags={118: r"\Seen \Flagged"})
    changes = await imap_run(monkeypatch, server, "7:120", {"118": "a", "119": "b", "120": "c"})
    assert changes.read_updates["118"] is True
    assert changes.flag_updates["118"] is True
    assert changes.moved_out == ["119"]


@pytest.mark.anyio
async def test_imap_fetch_never_sets_seen(monkeypatch):
    server = FakeIMAP([1, 2], flags={2: r"\Seen \Flagged"})
    changes = await imap_run(monkeypatch, server)
    fetches = [str(c) for c in server.commands if c[0] == "fetch"]
    assert all("BODY.PEEK[]" in c and "RFC822)" not in c for c in fetches)
    assert changes.upserts[-1]["is_read"] and changes.upserts[-1]["is_flagged"]


def mock_client(monkeypatch, module, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


async def token_setup(monkeypatch, module):
    async def credentials(_): return {}
    async def token(*_): return "test"
    monkeypatch.setattr(module.service, "load_credentials", credentials)
    monkeypatch.setattr(module.service, "valid_access_token", token)


@pytest.mark.anyio
async def test_gmail_history_404_triggers_full_rebuild(monkeypatch):
    await token_setup(monkeypatch, sync_gmail)
    def handler(request):
        if request.url.path.endswith("/history"):
            return httpx.Response(404)
        if request.url.path.endswith("/messages"):
            assert request.url.params["q"] == "in:inbox"
            return httpx.Response(200, json={"messages": []})
        return httpx.Response(200, json={"historyId": "42"})
    mock_client(monkeypatch, sync_gmail, handler)
    changes = await sync_gmail.fetch_changes(account("google", "oauth2"), cursor="1", folders=["INBOX"], limit=10, max_body_chars=100)
    assert changes.full_rebuild


@pytest.mark.anyio
async def test_graph_delta_removed_marks_moved_out(monkeypatch):
    await token_setup(monkeypatch, sync_graph)
    mock_client(monkeypatch, sync_graph, lambda request: httpx.Response(200, json={"value": [{"id": "old", "@removed": {"reason": "deleted"}}], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/next"}))
    changes = await sync_graph.fetch_changes(account("microsoft", "oauth2"), cursor="https://graph.microsoft.com/v1.0/delta", folders=["INBOX"], limit=10, max_body_chars=100)
    assert changes.moved_out == ["old"]


@pytest.mark.anyio
async def test_connector_uses_after_query_and_reconciles_unread(monkeypatch):
    queries = []
    async def call(provider, tool, args):
        queries.append(args["query"])
        return {"threads": [{"id": "t"}]} if "after:" not in args["query"] else {"threads": []}
    async def known(*_): return {"m": "local"}
    async def get(_): return SimpleNamespace(thread_id="t", internal_date=None)
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", call)
    monkeypatch.setattr(sync_connector.message_access, "known_uids", known)
    monkeypatch.setattr(sync_connector.message_access, "get_message", get)
    changes = await sync_connector.fetch_changes(account("gmail", "connector"), cursor="123", folders=["INBOX"], limit=10, max_body_chars=100)
    assert "in:inbox after:122" in queries
    assert changes.read_updates["m"] is False


@pytest.mark.anyio
async def test_sync_account_applies_changes_and_reports_new_ids(monkeypatch, fake_store):
    stored = {}
    async def get_account(_): return account()
    async def known(*_, session=None): return {"old": "local-old"}
    async def fetch(*_, **__):
        return sync_types.SyncChanges(upserts=[{"folder": "INBOX", "provider_uid": "old"}, {"folder": "INBOX", "provider_uid": "new"}], read_updates={"old": True}, flag_updates={"old": True}, moved_out=[], cursor="7:2", full_rebuild=False)
    async def upsert(**kwargs): return SimpleNamespace(id="local-new")
    async def set_state(id, *, session=None, **kwargs): stored[id] = kwargs
    monkeypatch.setattr(sync.account_access, "get_account", get_account)
    monkeypatch.setattr(sync.message_access, "known_uids", known)
    monkeypatch.setattr(sync.message_access, "upsert_message", upsert)
    monkeypatch.setattr(sync.message_access, "set_state", set_state)
    monkeypatch.setattr(sync.message_access, "stale_body_texts", AsyncMock(return_value={}))
    monkeypatch.setattr(sync.message_access, "stale_bodies", AsyncMock(return_value=[]))
    monkeypatch.setattr(sync_imap, "fetch_changes", fetch)
    result = await sync.sync_account("a")
    assert result.new_message_ids == ["local-new"]
    assert stored["local-old"] == {"is_read": True, "is_flagged": True, "imap_uidvalidity": "7"}


@pytest.mark.anyio
async def test_sync_moved_out_leaves_spam_untouched(monkeypatch, fake_store):
    from unittest.mock import AsyncMock
    from db.models.mail_message import MailMessage

    fake_store.accounts["a"] = account()
    fake_store.messages["m"] = MailMessage(id="m", account_id="a", provider_uid="old", folder="SPAM")
    async def known(account_id, folder, *, session=None):
        return {row.provider_uid: row.id for row in fake_store.messages.values() if row.account_id == account_id and row.folder == folder}
    monkeypatch.setattr(sync.message_access, "known_uids", known)
    monkeypatch.setattr(sync_imap, "fetch_changes", AsyncMock(return_value=sync_types.SyncChanges(moved_out=["old"], cursor="1")))
    result = await sync.sync_account("a")
    assert result.status == "ok"
    assert fake_store.messages["m"].folder == "SPAM"
    assert await sync.message_access.known_uids("a", "INBOX") == {}


@pytest.mark.anyio
async def test_connector_paginates_past_empty_threads(monkeypatch):
    calls = []
    async def call(provider, tool, args):
        calls.append((tool, args.copy()))
        if tool == "get_thread":
            return {"id": args["threadId"], "messages": [] if args["threadId"] == "empty" else [{"id": "m", "date": "2026-11-13T10:00:00+00:00", "labelIds": ["INBOX"]}]}
        if not args.get("pageToken"):
            return {"threads": [{"id": "empty"}], "nextPageToken": "next"}
        return {"threads": [{"id": "full"}]}
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", call)
    async def known(*_): return {}
    monkeypatch.setattr(sync_connector.message_access, "known_uids", known)
    result = await sync_connector.fetch_changes(account("gmail", "connector"), cursor="", folders=["INBOX"], limit=1, max_body_chars=100)
    assert [m["provider_uid"] for m in result.upserts] == ["m"]
    assert any(args.get("pageToken") == "next" for tool, args in calls if tool == "search_threads")


@pytest.mark.anyio
async def test_gmail_follows_all_history_pages(monkeypatch):
    await token_setup(monkeypatch, sync_gmail)
    requests = []
    def gmail(request):
        requests.append(request)
        if request.url.path.endswith("/history"):
            if not request.url.params.get("pageToken"):
                return httpx.Response(200, json={"historyId": "8", "nextPageToken": "p"})
            return httpx.Response(200, json={"historyId": "9", "history": [{"messagesDeleted": [{"message": {"id": "gone"}}]}]})
        return httpx.Response(404)
    mock_client(monkeypatch, sync_gmail, gmail)
    result = await sync_gmail.fetch_changes(account("google", "oauth2"), cursor="7", folders=["INBOX"], limit=2, max_body_chars=100)
    assert result.cursor == "9" and result.moved_out == ["gone"]
    assert any(r.url.params.get("pageToken") == "p" for r in requests)


@pytest.mark.anyio
async def test_graph_follows_next_link_until_delta(monkeypatch):
    await token_setup(monkeypatch, sync_graph)
    requests = []
    def graph(request):
        requests.append(str(request.url))
        if len(requests) == 1:
            return httpx.Response(200, json={"value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2"})
        return httpx.Response(200, json={"value": [{"id": "gone", "@removed": {}}], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta2"})
    mock_client(monkeypatch, sync_graph, graph)
    result = await sync_graph.fetch_changes(account("microsoft", "oauth2"), cursor="https://graph.microsoft.com/v1.0/delta", folders=["INBOX"], limit=1, max_body_chars=100)
    assert len(requests) == 2 and result.cursor.endswith("delta2")
    assert result.moved_out == ["gone"]


@pytest.mark.anyio
async def test_graph_first_sync_selects_reply_to(monkeypatch):
    await token_setup(monkeypatch, sync_graph)
    requests = []
    def graph(request):
        requests.append(request)
        return httpx.Response(200, json={"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta"})
    mock_client(monkeypatch, sync_graph, graph)
    await sync_graph.fetch_changes(account("microsoft", "oauth2"), cursor="", folders=["INBOX"], limit=5, max_body_chars=100)
    assert "replyTo" in requests[0].url.params["$select"].split(",")


@pytest.mark.anyio
async def test_connector_backlog_and_boundary_progress(monkeypatch):
    from datetime import UTC, datetime
    import re
    known = {"old": "old"}
    messages = {str(i): i for i in range(101, 106)}
    messages["old"] = 100
    async def known_uids(*_): return dict(known)
    async def get(_): return None
    async def call(provider, tool, args):
        if tool == "get_thread":
            uid = args["threadId"]
            date = datetime.fromtimestamp(messages[uid], UTC).isoformat()
            return {"id": uid, "messages": [{"id": uid, "date": date, "labelIds": ["INBOX"]}]}
        query = args["query"]
        if "newer_than" in query:
            return {"threads": []}
        after = re.search(r"after:(-?\d+)", query)
        before = re.search(r"before:(-?\d+)", query)
        ids = [uid for uid, ts in sorted(messages.items(), key=lambda item: -item[1]) if (not after or ts > int(after.group(1))) and (not before or ts < int(before.group(1)))]
        return {"threads": [{"id": uid} for uid in ids]}
    monkeypatch.setattr(sync_connector.message_access, "known_uids", known_uids)
    monkeypatch.setattr(sync_connector.message_access, "get_message", get)
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", call)
    cursor = "100"
    for _ in range(3):
        changes = await sync_connector.fetch_changes(account("gmail", "connector"), cursor=cursor, folders=["INBOX"], limit=2, max_body_chars=100)
        assert len(changes.upserts) <= 2
        known.update({m["provider_uid"]: m["provider_uid"] for m in changes.upserts})
        cursor = changes.cursor
    assert set(known) == set(messages)
    assert cursor == "105"
    known.clear()
    known.update({"105": "105", "104": "104"})
    changes = await sync_connector.fetch_changes(account("gmail", "connector"), cursor="105:100:104", folders=["INBOX"], limit=2, max_body_chars=100)
    assert {m["provider_uid"] for m in changes.upserts} == {"102", "103"}
    assert changes.cursor == "105:100:102"
    messages["same-second"] = 105
    known.update({"102": "102", "103": "103"})
    changes = await sync_connector.fetch_changes(account("gmail", "connector"), cursor="105", folders=["INBOX"], limit=2, max_body_chars=100)
    assert [m["provider_uid"] for m in changes.upserts] == ["same-second"]


@pytest.mark.anyio
async def test_connector_invalid_cursor_rescans_without_removing_links(monkeypatch):
    async def known(*_): return {}
    async def call(*_): return {"threads": []}
    monkeypatch.setattr(sync_connector.message_access, "known_uids", known)
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", call)
    result = await sync_connector.fetch_changes(account("gmail", "connector"), cursor="not-a-cursor", folders=["INBOX"], limit=2, max_body_chars=100)
    assert not result.full_rebuild and result.cursor == "0"


@pytest.mark.anyio
async def test_gmail_stops_at_history_page_boundary(monkeypatch):
    await token_setup(monkeypatch, sync_gmail)
    def handler(request):
        if request.url.path.endswith("/history"):
            assert request.url.params["maxResults"] == "1"
            return httpx.Response(200, json={"historyId": "99", "history": [{"id": "11", "messagesDeleted": [{"message": {"id": "gone"}}]}], "nextPageToken": "next"})
        return httpx.Response(404)
    mock_client(monkeypatch, sync_gmail, handler)
    result = await sync_gmail.fetch_changes(account("google", "oauth2"), cursor="10", folders=["INBOX"], limit=1, max_body_chars=100)
    assert result.cursor == "11" and result.moved_out == ["gone"]


@pytest.mark.anyio
async def test_graph_stops_at_page_boundary(monkeypatch):
    await token_setup(monkeypatch, sync_graph)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"value": [{"id": "a", "@removed": {}}, {"id": "b", "@removed": {}}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"})
    mock_client(monkeypatch, sync_graph, handler)
    result = await sync_graph.fetch_changes(account("microsoft", "oauth2"), cursor="https://graph.microsoft.com/v1.0/delta", folders=["INBOX"], limit=1, max_body_chars=100)
    assert result.cursor.endswith("/next") and result.moved_out == ["a", "b"]
    assert len(calls) == 1 and calls[0].headers["Prefer"] == "odata.maxpagesize=1"


@pytest.mark.anyio
async def test_graph_rejects_network_path_delta_link(monkeypatch):
    await token_setup(monkeypatch, sync_graph)
    requests = []
    def graph(request):
        requests.append(request)
        return httpx.Response(200, json={"value": [], "@odata.nextLink": "//evil.example/path"})
    mock_client(monkeypatch, sync_graph, graph)
    with pytest.raises(ValueError):
        await sync_graph.fetch_changes(account("microsoft", "oauth2"), cursor="", folders=["INBOX"], limit=1, max_body_chars=100)
    assert len(requests) == 1


@pytest.mark.anyio
async def test_sync_error_stores_code_not_provider_text(fake_store, monkeypatch):
    async def get_account(_): return account()
    async def fetch(*_, **__): raise ValueError("token=abc failed")
    monkeypatch.setattr(sync.account_access, "get_account", get_account)
    monkeypatch.setattr(sync_imap, "fetch_changes", fetch)
    result = await sync.sync_account("a")
    assert result.error_code == "provider_error"
    assert fake_store.states["a"].last_error == "provider_error"


@pytest.mark.anyio
@pytest.mark.parametrize("limit", [1, 2])
async def test_connector_whole_thread_frontier_preserves_backlog(monkeypatch, limit):
    from datetime import UTC, datetime
    import re

    threads = {"x": [("outside", 900), ("new", 2000)], "y": [("backlog", 1900)]}
    known = {}
    async def known_uids(*_): return dict(known)
    async def get(_): return None
    async def call(provider, tool, args):
        if tool == "get_thread":
            return {"messages": [{"id": uid, "date": datetime.fromtimestamp(ts, UTC).isoformat(), "labelIds": ["INBOX"]} for uid, ts in threads[args["threadId"]]]}
        query = args["query"]
        if "newer_than" in query:
            return {"threads": []}
        after = re.search(r"after:(-?\d+)", query)
        before = re.search(r"before:(-?\d+)", query)
        matching = [(name, max(ts for _, ts in messages)) for name, messages in threads.items()
                    if any((not after or ts > int(after.group(1))) and (not before or ts < int(before.group(1))) for _, ts in messages)]
        return {"threads": [{"id": name} for name, _ in sorted(matching, key=lambda item: -item[1])]}
    monkeypatch.setattr(sync_connector.message_access, "known_uids", known_uids)
    monkeypatch.setattr(sync_connector.message_access, "get_message", get)
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", call)
    cursor = "1000"
    for _ in range(5):
        changes = await sync_connector.fetch_changes(account("gmail", "connector"), cursor=cursor, folders=["INBOX"], limit=limit, max_body_chars=100)
        known.update({m["provider_uid"]: m["provider_uid"] for m in changes.upserts})
        cursor = changes.cursor
        parts = cursor.split(":")
        if len(parts) == 3:
            assert int(parts[2]) >= int(parts[1])
        else:
            break
    assert set(known) == {"new", "backlog"}
    assert cursor == "2000"


@pytest.mark.anyio
async def test_connector_gap_skips_messages_outside_window_without_budget(monkeypatch):
    from datetime import UTC, datetime

    async def known(*_): return {}
    async def call(provider, tool, args):
        if tool == "get_thread":
            stamps = [("above", 3000), ("inside", 1500)] if args["threadId"] == "x" else [("other", 1400)]
            return {"messages": [{"id": uid, "date": datetime.fromtimestamp(ts, UTC).isoformat(), "labelIds": ["INBOX"]} for uid, ts in stamps]}
        query = args["query"]
        if "before:" in query:
            return {"threads": [{"id": "x"}, {"id": "y"}]}
        return {"threads": []}
    monkeypatch.setattr(sync_connector.message_access, "known_uids", known)
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", call)
    result = await sync_connector.fetch_changes(account("gmail", "connector"), cursor="2500:1000:2000", folders=["INBOX"], limit=2, max_body_chars=100)
    assert {m["provider_uid"] for m in result.upserts} == {"inside", "other"}
    assert result.cursor == "2500"


@pytest.mark.anyio
async def test_connector_one_second_gap_is_kept(monkeypatch):
    from datetime import UTC, datetime
    import re

    threads = {"t1": [("a", 1500)], "t2": [("1000b", 1000)], "t3": [("1000c", 1000)]}
    known = {}
    async def known_uids(*_): return dict(known)
    async def get(_): return None
    async def call(provider, tool, args):
        if tool == "get_thread":
            return {"messages": [{"id": uid, "date": datetime.fromtimestamp(ts, UTC).isoformat(), "labelIds": ["INBOX"]} for uid, ts in threads[args["threadId"]]]}
        query = args["query"]
        if "newer_than" in query:
            return {"threads": []}
        after = re.search(r"after:(-?\d+)", query)
        before = re.search(r"before:(-?\d+)", query)
        matching = [name for name, messages in threads.items()
                    if any((not after or ts > int(after.group(1))) and (not before or ts < int(before.group(1))) for _, ts in messages)]
        return {"threads": [{"id": name} for name in sorted(matching, key=lambda name: -max(ts for _, ts in threads[name]))]}
    monkeypatch.setattr(sync_connector.message_access, "known_uids", known_uids)
    monkeypatch.setattr(sync_connector.message_access, "get_message", get)
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", call)
    first = await sync_connector.fetch_changes(account("gmail", "connector"), cursor="1000", folders=["INBOX"], limit=2, max_body_chars=100)
    assert first.cursor == "1500:1000:1000"
    known.update({m["provider_uid"]: m["provider_uid"] for m in first.upserts})
    second = await sync_connector.fetch_changes(account("gmail", "connector"), cursor=first.cursor, folders=["INBOX"], limit=2, max_body_chars=100)
    assert [m["provider_uid"] for m in second.upserts] == ["1000c"]
    assert second.cursor == "1500"


@pytest.mark.anyio
@pytest.mark.parametrize(("exception_type", "code"), [(sync_imap.imaplib.IMAP4.abort, "unreachable"), (sync_imap.imaplib.IMAP4.readonly, "unreachable"), (sync_imap.imaplib.IMAP4.error, "auth_expired")])
async def test_imap_login_error_classification(fake_store, monkeypatch, exception_type, code):
    class Server:
        def login(self, *_): raise exception_type("login failed")
        def logout(self): pass
    async def get_account(_): return account()
    async def credentials(_): return {"imap_host": "example.org"}
    async def password(_): return "placeholder"
    async def check(_): pass
    monkeypatch.setattr(sync.account_access, "get_account", get_account)
    monkeypatch.setattr(sync_imap.service, "load_credentials", credentials)
    monkeypatch.setattr(sync_imap.service, "imap_password", password)
    monkeypatch.setattr(sync_imap.transport, "check_mail_host", check)
    monkeypatch.setattr(sync_imap.transport, "open_imap", lambda *_: Server())
    result = await sync.sync_account("a")
    assert result.error_code == code
    assert fake_store.states["a"].last_error == code


@pytest.mark.anyio
async def test_connector_skips_messages_without_inbox_label(monkeypatch):
    async def known(*_): return {}
    async def call(provider, tool, args):
        if tool == "get_thread":
            return {"id": "t", "messages": [
                {"id": "in", "date": "1970-01-01T00:01:40+00:00", "labelIds": ["INBOX"]},
                {"id": "sent", "date": "1970-01-01T00:03:20+00:00", "labelIds": ["SENT"]},
            ]}
        return {"threads": [{"id": "t"}]}
    monkeypatch.setattr(sync_connector.message_access, "known_uids", known)
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", call)
    result = await sync_connector.fetch_changes(account("gmail", "connector"), cursor="", folders=["INBOX"], limit=10, max_body_chars=100)
    assert [m["provider_uid"] for m in result.upserts] == ["in"]
    assert result.cursor == "200"


@pytest.mark.anyio
async def test_connector_scan_uses_per_message_listing_when_full_labels_are_missing(monkeypatch):
    async def known(*_): return {}
    async def call(provider, tool, args):
        if tool == "get_thread":
            return {"id": "t", "messages": [
                {"id": "missing", "date": "1970-01-01T00:01:40+00:00"},
                {"id": "none", "date": "1970-01-01T00:03:20+00:00", "labelIds": None},
            ]}
        return {"threads": [{"id": "t", "messages": [
            {"id": "missing", "labelIds": ["INBOX", "UNREAD", "STARRED"]}, {"id": "none", "labelIds": ["INBOX"]},
        ]}]}
    monkeypatch.setattr(sync_connector.message_access, "known_uids", known)
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", call)
    result = await sync_connector.fetch_changes(account("gmail", "connector"), cursor="", folders=["INBOX"], limit=10, max_body_chars=100)
    assert [m["provider_uid"] for m in result.upserts] == ["missing", "none"]
    assert [(row["is_read"], row["is_flagged"], json.loads(row["labels_json"])) for row in result.upserts] == [
        (False, True, ["INBOX", "UNREAD", "STARRED"]), (True, False, ["INBOX"]),
    ]


@pytest.mark.anyio
async def test_connector_history_archives_an_old_indexed_sibling(monkeypatch):
    stamp = datetime(2024, 1, 1, tzinfo=UTC)
    options = {"folders": ["INBOX"], "limit": 10, "max_body_chars": 100}
    monkeypatch.setattr(sync_connector.message_access, "known_uids", AsyncMock(return_value={"archived": "local"}))
    monkeypatch.setattr(sync_connector.message_access, "get_message", AsyncMock(return_value=SimpleNamespace(
        thread_id="historic", internal_date=stamp)))

    async def provider(_, tool, args):
        if tool == "search_threads":
            return {"threads": [{"id": "historic"}] if args.get("pageToken") == "older" else []}
        return {"id": "historic", "messages": [
            {"id": "archived", "date": stamp.isoformat(), "labelIds": ["STARRED"]},
            {"id": "still-here", "date": stamp.isoformat(), "labelIds": ["INBOX"]},
        ]}

    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", provider)
    changes = await sync_connector.fetch_changes(account("gmail", "connector"),
        cursor='initial:["1800000000", "older"]', **options)
    assert changes.cursor == "1800000000"
    assert changes.moved_out == ["archived"]
    assert [row["provider_uid"] for row in changes.upserts] == ["still-here"]


async def connector_reconciliation(monkeypatch, threads, unread_threads):
    queries = []
    async def known(*_): return {"a": "a", "b": "b"}
    async def get(_): return SimpleNamespace(thread_id="t", internal_date=None)
    async def call(provider, tool, args):
        query = args["query"]
        queries.append(query)
        if "after:" in query: return {"threads": []}
        if "is:unread" in query: return {"threads": unread_threads}
        return {"threads": threads}
    monkeypatch.setattr(sync_connector.message_access, "known_uids", known)
    monkeypatch.setattr(sync_connector.message_access, "get_message", get)
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", call)
    result = await sync_connector.fetch_changes(account("gmail", "connector"), cursor="123", folders=["INBOX"], limit=10, max_body_chars=100)
    return result, queries


@pytest.mark.anyio
async def test_connector_reconciles_read_state_per_message(monkeypatch):
    result, queries = await connector_reconciliation(monkeypatch, [{"id": "t", "messages": [
        {"id": "a", "labelIds": ["INBOX", "UNREAD"]}, {"id": "b", "labelIds": ["INBOX"]},
    ]}], [{"id": "t"}])
    assert result.read_updates == {"a": False, "b": True}
    assert not any("is:unread" in query for query in queries)


@pytest.mark.anyio
async def test_connector_archived_sibling_is_moved_out(monkeypatch):
    result, queries = await connector_reconciliation(monkeypatch, [{"id": "t", "messages": [
        {"id": "a", "labelIds": ["INBOX"]}, {"id": "b", "labelIds": []},
    ]}], [])
    assert result.moved_out == ["b"]
    assert result.read_updates == {"a": True}
    assert not any("is:unread" in query for query in queries)


@pytest.mark.anyio
async def test_connector_reconcile_unknown_labels_uses_thread_state(monkeypatch):
    result, queries = await connector_reconciliation(monkeypatch, [{"id": "t", "messages": [
        {"id": "a"}, {"id": "b", "labelIds": None},
    ]}], [{"id": "t"}])
    assert result.moved_out == []
    assert result.read_updates == {"a": False, "b": False}
    assert "is:unread in:inbox newer_than:14d" in queries


@pytest.mark.anyio
async def test_connector_falls_back_to_thread_state_without_messages(monkeypatch):
    result, queries = await connector_reconciliation(monkeypatch, [{"id": "t"}], [{"id": "t"}])
    assert result.read_updates == {"a": False, "b": False}
    assert "is:unread in:inbox newer_than:14d" in queries


@pytest.mark.parametrize(("failure", "code"), [
    (MistralNotConfiguredError("private data"), "mistral_not_configured"),
    (secrets.WorkspaceSecretAccessDeniedError("private data"), "secret_access_denied"),
])
def test_missing_mistral_access_is_not_an_expired_login(failure, code):
    assert sync._error_code(failure) == code


# Bodies stored by an older text extraction are re-read in place: same row, new text, insight dropped.
def stored(id, uid, *, version=0, body="", rfc="", thread="t1"):
    from db.models.mail_message import MailMessage
    return MailMessage(id=id, account_id="a", provider_uid=uid, folder="INBOX", body_text=body, snippet=body, body_version=version, rfc_message_id=rfc, thread_id=thread)


def refresh_store(monkeypatch, fake_store, rows, fetch_bodies, upserts=()):
    fake_store.accounts["a"] = account()
    for row in rows:
        fake_store.messages[row.id] = row
        fake_store.insights[row.id] = "old insight"
    async def known(account_id, folder, *, session=None):
        return {row.provider_uid: row.id for row in fake_store.messages.values() if row.account_id == account_id and row.folder == folder}
    async def upsert(*, account_id, provider_uid, body_version, drop_insight=False, **fields):
        row = next(row for row in fake_store.messages.values() if row.provider_uid == provider_uid)
        row.body_text, row.body_version = fields["body_text"], body_version
        if drop_insight:
            fake_store.insights.pop(row.id, None)
        return row
    monkeypatch.setattr(sync.message_access, "known_uids", known)
    monkeypatch.setattr(sync.message_access, "upsert_message", upsert)
    monkeypatch.setattr(sync_imap, "fetch_changes", AsyncMock(return_value=sync_types.SyncChanges(upserts=list(upserts), cursor="7:9")))
    monkeypatch.setattr(sync_imap, "fetch_bodies", fetch_bodies)


def read(text, rfc=""):
    return {"body_text": text, "snippet": text, "rfc_message_id": rfc}


@pytest.mark.anyio
async def test_html_refresh_failure_preserves_text_refresh_insight_signal(monkeypatch, fake_store, caplog):
    refresh_store(monkeypatch, fake_store, [stored("m1", "5", body="old")], AsyncMock(return_value={"5": read("new")}))
    monkeypatch.setattr(sync, "refresh_html_bodies", AsyncMock(side_effect=RuntimeError("private content")))
    result = await sync.sync_account("a")
    assert result.status == "ok" and result.refresh_insights
    assert "m1" not in fake_store.insights
    assert "RuntimeError" in caplog.text and "private content" not in caplog.text


@pytest.mark.anyio
async def test_sync_rereads_stale_bodies_and_drops_their_insights(monkeypatch, fake_store):
    fetch_bodies = AsyncMock(return_value={"5": read("Lesbarer Text", "<m@x>")})
    refresh_store(monkeypatch, fake_store, [stored("m1", "5", body="96 p{ margin:10px }", rfc="<m@x>"), stored("m2", "6", version=sync_types.BODY_VERSION, body="aktuell")], fetch_bodies)
    result = await sync.sync_account("a")
    assert result.status == "ok"
    assert [row.id for row in fetch_bodies.await_args.args[1]] == ["m1"]
    assert fake_store.messages["m1"].body_text == "Lesbarer Text" and fake_store.messages["m1"].snippet == "Lesbarer Text"
    assert fake_store.messages["m1"].body_version == sync_types.BODY_VERSION
    assert "m1" not in fake_store.insights and fake_store.insights["m2"] == "old insight"


@pytest.mark.anyio
async def test_sync_reread_with_same_text_keeps_the_insight(monkeypatch, fake_store):
    refresh_store(monkeypatch, fake_store, [stored("m1", "5", body="gleich")], AsyncMock(return_value={"5": read("gleich")}))
    await sync.sync_account("a")
    assert fake_store.messages["m1"].body_version == sync_types.BODY_VERSION
    assert fake_store.insights["m1"] == "old insight"


@pytest.mark.anyio
async def test_sync_reread_that_stays_empty_drops_the_insight_made_from_the_subject(monkeypatch, fake_store):
    refresh_store(monkeypatch, fake_store, [stored("m1", "5"), stored("m2", "6")], AsyncMock(return_value={"5": read(""), "6": None}))
    await sync.sync_account("a")
    assert "m1" not in fake_store.insights and "m2" not in fake_store.insights
    assert fake_store.messages["m2"].body_version == sync_types.BODY_VERSION


@pytest.mark.anyio
async def test_sync_reread_leaves_text_of_mail_the_provider_no_longer_has(monkeypatch, fake_store):
    refresh_store(monkeypatch, fake_store, [stored("m1", "5", body="alt")], AsyncMock(return_value={"5": None}))
    await sync.sync_account("a")
    # The version moves on so a vanished mail is not asked for again on every run.
    assert fake_store.messages["m1"].body_version == sync_types.BODY_VERSION
    assert fake_store.messages["m1"].body_text == "alt" and fake_store.insights["m1"] == "old insight"


@pytest.mark.anyio
async def test_sync_reread_keeps_mails_it_could_not_read_for_the_next_run(monkeypatch, fake_store):
    # One mail failed (no entry), the other was read: the read one is done, the failed one waits.
    refresh_store(monkeypatch, fake_store, [stored("m1", "5", body="alt"), stored("m2", "6", body="alt")], AsyncMock(return_value={"6": read("neu")}))
    await sync.sync_account("a")
    assert (fake_store.messages["m1"].body_version, fake_store.messages["m1"].body_text, fake_store.insights["m1"]) == (0, "alt", "old insight")
    assert (fake_store.messages["m2"].body_version, fake_store.messages["m2"].body_text) == (sync_types.BODY_VERSION, "neu")


@pytest.mark.anyio
async def test_sync_reread_lets_an_unreadable_mail_wait_instead_of_holding_up_the_others(monkeypatch, fake_store):
    # The list is read newest first: a mail that never answers (a deleted thread the connector reports
    # as text) must not take a place in every run, nor be asked for every five minutes.
    monkeypatch.setattr(sync, "BODY_REFRESH_BATCH", 1)
    fetch_bodies = AsyncMock(side_effect=lambda account, rows, **_: {"6": read("neu")} if rows[0].provider_uid == "6" else {})
    refresh_store(monkeypatch, fake_store, [stored("m1", "5", body="alt"), stored("m2", "6", body="alt")], fetch_bodies)
    await sync.sync_account("a")
    failures, retry_at = sync._reread_backoff["m1"]
    assert failures == 1 and retry_at - datetime.now(UTC) > sync.REREAD_WAIT - timedelta(minutes=1)
    await sync.sync_account("a")
    assert [call.args[1][0].id for call in fetch_bodies.await_args_list] == ["m1", "m2"]
    assert fake_store.messages["m2"].body_text == "neu"
    await sync.sync_account("a")  # m1 still waits, nothing else is stale
    assert fetch_bodies.await_count == 2
    # After its wait it is asked again; every failure doubles the wait, up to a day.
    sync._reread_backoff["m1"] = (1, datetime.now(UTC) - timedelta(seconds=1))
    await sync.sync_account("a")
    assert fetch_bodies.await_count == 3 and sync._reread_backoff["m1"][0] == 2
    assert sync._reread_backoff["m1"][1] - datetime.now(UTC) > 2 * sync.REREAD_WAIT - timedelta(minutes=1)
    sync._reread_backoff["m1"] = (40, datetime.now(UTC) - timedelta(seconds=1))
    await sync.sync_account("a")
    assert sync._reread_backoff["m1"][1] - datetime.now(UTC) <= sync.REREAD_WAIT_MAX


@pytest.mark.anyio
async def test_sync_reread_forgets_the_wait_of_a_mail_it_finally_reads(monkeypatch, fake_store):
    refresh_store(monkeypatch, fake_store, [stored("m1", "5", body="alt")], AsyncMock(return_value={"5": read("neu")}))
    sync._reread_backoff["m1"] = (3, datetime.now(UTC) - timedelta(seconds=1))
    await sync.sync_account("a")
    assert "m1" not in sync._reread_backoff and fake_store.messages["m1"].body_text == "neu"


@pytest.mark.anyio
async def test_sync_reread_never_takes_text_of_another_mail(monkeypatch, fake_store):
    refresh_store(monkeypatch, fake_store, [stored("m1", "5", body="alt", rfc="<m@x>")], AsyncMock(return_value={"5": read("fremd", "<other@x>")}))
    await sync.sync_account("a")
    assert fake_store.messages["m1"].body_text == "alt" and fake_store.insights["m1"] == "old insight"
    assert fake_store.messages["m1"].body_version == sync_types.BODY_VERSION


@pytest.mark.anyio
async def test_sync_reread_failure_keeps_sync_ok_and_retries_later(monkeypatch, fake_store):
    refresh_store(monkeypatch, fake_store, [stored("m1", "5")], AsyncMock(side_effect=httpx.ConnectError("down")))
    result = await sync.sync_account("a")
    assert result.status == "ok"
    assert fake_store.messages["m1"].body_version == 0 and fake_store.insights["m1"] == "old insight"


@pytest.mark.anyio
async def test_sync_reupsert_of_stale_row_drops_its_insight_only_when_the_text_changes(monkeypatch, fake_store):
    fetch_bodies = AsyncMock(return_value={})
    rows = [stored("m1", "5", body="alt"), stored("m2", "6", body="gleich")]
    refresh_store(monkeypatch, fake_store, rows, fetch_bodies, upserts=[{"folder": "INBOX", "provider_uid": "5", "body_text": "neu"}, {"folder": "INBOX", "provider_uid": "6", "body_text": "gleich"}])
    await sync.sync_account("a")
    assert fake_store.messages["m1"].body_version == fake_store.messages["m2"].body_version == sync_types.BODY_VERSION
    assert "m1" not in fake_store.insights and fake_store.insights["m2"] == "old insight"
    fetch_bodies.assert_not_awaited()


@pytest.mark.parametrize("old, new, outdated", [("a", "b", True), ("a", "a", False), ("", "", True), ("  ", "  ", True)])
def test_insight_is_outdated_when_the_text_changes_or_there_is_none(old, new, outdated):
    assert sync.insight_outdated(old, new) is outdated


class FlakyIMAP(FakeIMAP):
    """UID 6 answers NO; UID 8 loses the connection."""

    def uid(self, command, *args):
        if command.lower() == "fetch" and str(args[0]) == "6":
            self.commands.append(("fetch", *args))
            return "NO", [b"[UNAVAILABLE]"]
        if command.lower() == "fetch" and str(args[0]) == "8":
            raise imaplib.IMAP4.abort("socket error")
        return super().uid(command, *args)


async def imap_bodies(monkeypatch, server, uids):
    async def credentials(_): return {"imap_host": "example.org"}
    async def password(_): return "secret"
    async def check(_): pass
    monkeypatch.setattr(sync_imap.service, "load_credentials", credentials)
    monkeypatch.setattr(sync_imap.service, "imap_password", password)
    monkeypatch.setattr(sync_imap.transport, "check_mail_host", check)
    monkeypatch.setattr(sync_imap.transport, "open_imap", lambda *_: server)
    return await sync_imap.fetch_bodies(account(), [stored(f"m{uid}", uid) for uid in uids], max_body_chars=100)


@pytest.mark.anyio
async def test_imap_fetch_bodies_peeks_and_marks_vanished_uids_gone(monkeypatch):
    server = FakeIMAP([5])
    found = await imap_bodies(monkeypatch, server, ["5", "6", "x"])
    assert found["5"]["body_text"] == "body" and found["6"] is None and found["x"] is None
    fetches = [str(c) for c in server.commands if c[0] == "fetch"]
    assert fetches and all("BODY.PEEK[]" in c for c in fetches)
    assert ("select", '"INBOX"') in server.commands


@pytest.mark.anyio
async def test_imap_fetch_bodies_keeps_what_it_read_when_one_mail_or_the_connection_fails(monkeypatch):
    found = await imap_bodies(monkeypatch, FlakyIMAP([5, 6, 7, 8, 9]), ["5", "6", "7", "8", "9"])
    # 6 failed now (asked again next run); the lost connection at 8 leaves 8 and 9 for the next run.
    assert sorted(found) == ["5", "7"]


def gmail_or_graph(module):
    if module is sync_gmail:
        return httpx.Response(200, json={"id": "m/1", "payload": {"mimeType": "text/html", "headers": [], "body": {"data": "PHA-SGFsbG88L3A-"}}})
    return httpx.Response(200, json={"id": "m/1", "body": {"contentType": "html", "content": "<p>Hallo</p>"}})


@pytest.mark.anyio
@pytest.mark.parametrize("module, provider, path", [(sync_gmail, "google", "/messages/m%2F1"), (sync_graph, "microsoft", "/me/messages/m%2F1")])
async def test_api_fetch_bodies_marks_mails_the_provider_no_longer_has(monkeypatch, module, provider, path):
    await token_setup(monkeypatch, module)
    seen = []
    def handler(request):
        seen.append(request.url.raw_path.decode().split("?")[0])
        return httpx.Response(404) if seen[-1].endswith("gone") else gmail_or_graph(module)
    mock_client(monkeypatch, module, handler)
    found = await module.fetch_bodies(account(provider, "oauth2"), [stored("a1", "m/1"), stored("a2", "gone")], max_body_chars=100)
    assert found["m/1"]["body_text"] == "Hallo" and found["gone"] is None
    assert any(p.endswith(path) for p in seen)


@pytest.mark.anyio
@pytest.mark.parametrize("module, provider", [(sync_gmail, "google"), (sync_graph, "microsoft")])
async def test_api_fetch_bodies_skips_a_failing_mail_and_stops_when_rate_limited(monkeypatch, module, provider):
    await token_setup(monkeypatch, module)
    answers = {"ok1": None, "boom": 503, "bad": 400, "ok2": None, "limit": 429, "after": None}
    asked = []
    def handler(request):
        uid = request.url.raw_path.decode().split("?")[0].rsplit("/", 1)[-1]
        asked.append(uid)
        return httpx.Response(answers[uid]) if answers[uid] else gmail_or_graph(module)
    mock_client(monkeypatch, module, handler)
    found = await module.fetch_bodies(account(provider, "oauth2"), [stored(uid, uid) for uid in answers], max_body_chars=100)
    assert {uid: (parsed or {}).get("body_text") for uid, parsed in found.items()} == {"ok1": "Hallo", "bad": None, "ok2": "Hallo"}
    assert asked == ["ok1", "boom", "bad", "ok2", "limit"]


@pytest.mark.anyio
async def test_api_fetch_bodies_returns_what_it_read_when_the_provider_goes_away(monkeypatch):
    await token_setup(monkeypatch, sync_gmail)
    def handler(request):
        if request.url.raw_path.decode().endswith("down?format=full"):
            raise httpx.ConnectError("down")
        return gmail_or_graph(sync_gmail)
    mock_client(monkeypatch, sync_gmail, handler)
    found = await sync_gmail.fetch_bodies(account("google", "oauth2"), [stored("a", "first"), stored("b", "down"), stored("c", "never")], max_body_chars=100)
    assert list(found) == ["first"]


@pytest.mark.anyio
async def test_connector_fetch_bodies_reads_each_thread_once(monkeypatch):
    calls = []
    async def call(provider, tool, args):
        calls.append(args["threadId"])
        if args["threadId"] == "text":
            raise ValueError("not JSON")
        return {"id": "t1", "messages": [{"id": "x1", "htmlBody": "<p>Eins</p>"}, {"id": "x2", "plaintextBody": "Zwei"}, {"id": "x3", "plaintextBody": "nicht gefragt"}]}
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", call)
    found = await sync_connector.fetch_bodies(account("gmail", "connector"), [stored("a", "x1"), stored("b", "x2"), stored("c", "x9"), stored("d", "y", thread="text")], max_body_chars=100)
    # x9 is not in its thread any more (gone); a text answer may be a tool error, so y is asked again next run.
    assert {uid: (parsed or {}).get("body_text") for uid, parsed in found.items()} == {"x1": "Eins", "x2": "Zwei", "x9": None}
    assert calls == ["t1", "text"]


@pytest.mark.anyio
async def test_connector_fetch_bodies_returns_what_it_read_when_the_connector_fails(monkeypatch):
    async def call(provider, tool, args):
        if args["threadId"] == "t2":
            raise httpx.ConnectError("down")
        return {"messages": [{"id": "x1", "plaintextBody": "Eins"}]}
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", call)
    found = await sync_connector.fetch_bodies(account("gmail", "connector"), [stored("a", "x1"), stored("b", "x2", thread="t2"), stored("c", "x3", thread="t3")], max_body_chars=100)
    assert list(found) == ["x1"]


@pytest.mark.anyio
@pytest.mark.parametrize("bad", [None, [], {"id": "deferred"}, {"id": "wrong", "messages": []}])
async def test_malformed_full_thread_preserves_valid_initial_and_incremental_mail(monkeypatch, bad):
    from api.mail import sync
    client_account = account("gmail", "connector")
    monkeypatch.setattr(sync_connector.message_access, "known_uids", AsyncMock(return_value={}))
    stamp = "2026-01-20T10:00:00+00:00"
    async def provider(_, tool, args):
        if tool == "get_thread":
            if args["threadId"] == "deferred":
                return bad
            return {"id": "valid", "messages": [{"id": "new", "date": stamp, "labelIds": ["INBOX"]}]}
        if "newer_than" in args["query"]:
            return {"threads": []}
        return {"threads": [{"id": "deferred"}, {"id": "valid"}]}
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", provider)
    options = {"folders": ["INBOX"], "limit": 10, "max_body_chars": 100}
    first, pending = await sync._connector_changes(client_account, "", [], options)
    assert [row["provider_uid"] for row in first.upserts] == ["new"] and pending == ["deferred"]
    current, still_pending = await sync._connector_changes(client_account, first.cursor, pending, options)
    assert [row["provider_uid"] for row in current.upserts] == ["new"] and still_pending == ["deferred"]


@pytest.mark.anyio
@pytest.mark.parametrize("labels", [None, "missing"])
@pytest.mark.parametrize("listed,expected", [(False, []), (True, ["old"])])
async def test_retry_of_old_message_without_labels_requires_current_inbox_match(monkeypatch, labels, listed, expected):
    row = {"id": "old", "date": "2025-01-20T10:00:00+00:00"}
    if labels is None:
        row["labelIds"] = None
    async def provider(_, tool, args):
        if tool == "get_thread":
            return {"id": "held", "messages": [row]}
        assert args["query"].startswith("in:inbox after:") and "before:" in args["query"]
        return {"threads": [{"id": "held"}] if listed else []}
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", provider)
    changes = await sync_connector.retry_threads(account("gmail", "connector"), ["held"], 100)
    assert [message["provider_uid"] for message in changes.upserts] == expected
    assert changes.moved_out == ([] if listed else ["old"])
    assert changes.deferred_threads == []


@pytest.mark.anyio
async def test_retry_does_not_infer_membership_from_another_message_in_the_thread(monkeypatch):
    stamp = "2025-01-20T10:00:00+00:00"
    rows = [{"id": "archived", "date": stamp}, {"id": "visible", "date": stamp, "labelIds": ["INBOX"]}]
    async def provider(_, tool, args):
        if tool == "get_thread":
            return {"id": "held", "messages": rows}
        return {"threads": [{"id": "held", "messages": [rows[1]]}]}
    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", provider)
    changes = await sync_connector.retry_threads(account("gmail", "connector"), ["held"], 100)
    assert [message["provider_uid"] for message in changes.upserts] == ["visible"]
    assert changes.deferred_threads == ["held"]


@pytest.mark.anyio
@pytest.mark.parametrize("initial_labels", [None, ["INBOX"]])
@pytest.mark.parametrize("membership_timeout", [False, True])
async def test_regular_and_deferred_reads_only_publish_confirmed_siblings(monkeypatch, initial_labels, membership_timeout):
    stamp = "2026-06-01T11:00:00+00:00"
    visible = {"id": "visible", "date": stamp, "labelIds": ["INBOX"]}
    reads = 0

    async def provider(_, tool, args):
        nonlocal reads
        if tool == "get_thread":
            reads += 1
            archived = {"id": "archived", "date": stamp, "labelIds": initial_labels if reads == 1 else None}
            return {"id": "mixed", "messages": [archived, visible]}
        if membership_timeout and "before:" in args["query"]:
            raise httpx.ReadTimeout("Inbox lookup timed out")
        return {"threads": [{"id": "mixed", "messages": [visible]}]}

    monkeypatch.setattr(sync_connector.connectors, "call_read_tool_json", provider)
    monkeypatch.setattr(sync_connector.message_access, "known_uids", AsyncMock(return_value={}))
    options = {"folders": ["INBOX"], "limit": 10, "max_body_chars": 100}
    changes, pending = await sync._connector_changes(account("gmail", "connector"), "15", ["mixed"], options)
    assert reads == 2
    assert [message["provider_uid"] for message in changes.upserts] == ["visible"]
    assert pending == ["mixed"]


@pytest.mark.anyio
async def test_imap_deleted_source_no_longer_belongs_to_inbox(monkeypatch):
    server = FakeIMAP([12, 13], flags={12: r"\Deleted"})
    page = await imap_run(monkeypatch, server)
    assert [row["provider_uid"] for row in page.upserts] == ["13"]
    flags = await imap_run(monkeypatch, server, cursor="7:13", known={"12": "old", "13": "other"})
    assert flags.moved_out == ["12"]
