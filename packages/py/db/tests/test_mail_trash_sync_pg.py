"""Trash actions and inbox observations through the ordinary mail routes, with a local provider."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from api.mail import connector_mailbox, connectors, service, sync
from api.routers.api.v1.mail import accounts, actions, messages
from db.accessors import mail_action, mail_message, mail_sync_state
from db.engine import get_session_maker
from db.models.mail_message import MailMessage


pytestmark = pytest.mark.skipif(not os.getenv("RUN_PG_CONTRACT"), reason="RUN_PG_CONTRACT is not set")
MAIL = "/api/v1/mail"


def client():
    app = FastAPI()
    app.include_router(messages.router, prefix=MAIL + "/messages")
    app.include_router(actions.router, prefix=MAIL + "/actions")
    app.include_router(accounts.router, prefix=MAIL + "/accounts")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def listed(browser, folder, *, query=""):
    response = await browser.get(MAIL + "/messages", params={"folder": folder, "q": query})
    assert response.status_code == 200
    return {item["id"] for item in response.json()["items"]}


class StudioMailbox:
    """Simulate a connector with working label writes and a lagging search replica."""

    def __init__(self):
        self.date = datetime.now(UTC).isoformat()
        self.labels = {"target": {"INBOX"}, "sibling": {"INBOX"}}
        self.replay = False
        self.replayed_target_labels = ["INBOX"]
        self.reject = None
        self.reject_tool = None
        self.writes = []

    async def connect(self, monkeypatch, *, verified=True, provider="outlook"):
        email = "owner@example.test" if verified else "outlook@connector.mistral"
        self.account = await service.upsert_account(provider=provider, auth_type=connectors.CONNECTOR, email_address=email)
        progress = connector_mailbox.encode(("named", "anchor", "proof"), "") if verified else ""
        await mail_sync_state.save_state(account_id=self.account.id, connector_verified=verified, connector_cursor=progress)
        async def credentials(*args):
            return {"credentials": [{"name": "named", "authentication_type": "oauth2"}]}
        monkeypatch.setattr(connectors, "_get", credentials)
        monkeypatch.setattr(connectors, "_post", self.dispatch)
        assert (await sync.sync_account(self.account.id)).status == "ok"
        return await mail_message.known_uids(self.account.id, "INBOX")

    async def dispatch(self, path, payload):
        args = payload["arguments"]
        tool = urlsplit(path).path.split("/")[-2]
        if tool in {"label_message", "unlabel_message"}:
            self.writes.append((tool, args["messageId"], tuple(args["labelIds"])))
            if self.reject is not None and self.reject_tool in (None, tool):
                return self.reject
            labels = self.labels[args["messageId"]]
            if tool == "label_message":
                labels.update(args["labelIds"])
            else:
                labels.difference_update(args["labelIds"])
            data = {"id": args["messageId"], "labelIds": sorted(labels)}
        elif args.get("threadId") == "anchor":
            data = {"id": "anchor", "messages": [{"id": "proof", "sender": self.account.email_address, "labelIds": ["SENT"]}]}
        else:
            use_replica = self.replay and args.get("messageFormat") != "MINIMAL"
            rows = [{"id": uid, "labelIds": self.replayed_target_labels if use_replica and uid == "target" else sorted(labels),
                     "date": self.date, "subject": uid, "sender": "sender@example.test"}
                    for uid, labels in self.labels.items()]
            thread = {"id": "mixed", "messages": rows}
            if tool == "get_thread":
                assert args["threadId"] == thread["id"]
                data = thread
            else:
                assert tool == "search_threads"
                data = {"threads": [thread]}
        return {"content": [{"type": "text", "text": json.dumps(data)}]}


@pytest.mark.parametrize("verified", [True, False], ids=["bound", "inbox-only"])
def test_trash_survives_replayed_inbox_and_restores_same_message(pg_db, monkeypatch, verified):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch, verified=verified)
        target, sibling = ids["target"], ids["sibling"]
        async with client() as browser:
            result = await browser.post(f"{MAIL}/messages/{target}/actions", json={"kind": "trash"})
            assert result.status_code == 200
            assert provider.labels == {"target": {"TRASH"}, "sibling": {"INBOX"}}
            assert await listed(browser, "INBOX") == {sibling}
            assert await listed(browser, "TRASH") == {target}
            provider.replay = True
            for _ in range(2):
                assert (await sync.sync_account(provider.account.id)).status == "ok"
                assert await listed(browser, "INBOX") == {sibling}
                assert await listed(browser, "TRASH") == {target}
            provider.replay = False
            provider.labels["target"] = {"INBOX"}  # The owner moves it back at the provider.
            assert (await sync.sync_account(provider.account.id)).status == "ok"
            assert await listed(browser, "INBOX") == {target, sibling}
            assert await listed(browser, "TRASH") == set()
            assert len(provider.writes) == 2
    asyncio.run(run())


@pytest.mark.parametrize("verified", [True, False], ids=["bound", "inbox-only"])
@pytest.mark.parametrize("omit_labels", [False, True], ids=["stale-labels", "missing-labels"])
def test_move_and_undo_recovers_from_stale_or_missing_minimal_labels(pg_db, monkeypatch, verified, omit_labels):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch, verified=verified)
        stale = None
        delays = []

        async def wait_for_replication(seconds):
            delays.append(seconds)

        monkeypatch.setattr(connectors, "sleep", wait_for_replication, raising=False)

        async def lagged_readback(path, payload):
            nonlocal stale
            tool = urlsplit(path).path.split("/")[-2]
            args = payload["arguments"]
            if tool in {"label_message", "unlabel_message"}:
                uid = args["messageId"]
                stale = (uid, sorted(provider.labels[uid]), len(delays))
            result = await provider.dispatch(path, payload)
            if tool == "get_thread" and args == {"threadId": "mixed", "messageFormat": "MINIMAL"} and stale:
                uid, labels, prior_delays = stale
                thread = json.loads(result["content"][0]["text"])
                member = next(row for row in thread["messages"] if row["id"] == uid)
                if omit_labels:
                    member.pop("labelIds")
                    stale = None
                elif delays[prior_delays:] != [0.5, 1.0]:
                    member["labelIds"] = labels
                else:
                    stale = None
                result["content"][0]["text"] = json.dumps(thread)
            return result

        monkeypatch.setattr(connectors, "_post", lagged_readback)
        target, sibling = ids["target"], ids["sibling"]
        async with client() as browser:
            moved = await browser.post(f"{MAIL}/messages/{target}/actions", json={"kind": "trash"})
            assert moved.status_code == 200
            assert await listed(browser, "INBOX") == {sibling}
            assert await listed(browser, "TRASH") == {target}
            provider.replay = True
            assert (await sync.sync_account(provider.account.id)).status == "ok"
            assert await listed(browser, "INBOX") == {sibling}
            assert await listed(browser, "TRASH") == {target}

            provider.replay = False
            action = moved.json()["action_id"]
            undone = await browser.post(MAIL + "/actions/undo", json={"action_ids": [action]})
            assert undone.json()["restored"] == [action]
            provider.replay = True
            provider.replayed_target_labels = ["TRASH"]
            assert (await sync.sync_account(provider.account.id)).status == "ok"
            assert await listed(browser, "INBOX") == {target, sibling}
            assert await listed(browser, "TRASH") == set()
            assert provider.writes == [
                ("label_message", "target", ("TRASH",)),
                ("unlabel_message", "target", ("INBOX",)),
                ("label_message", "target", ("INBOX",)),
                ("unlabel_message", "target", ("TRASH",)),
            ]
            assert delays == ([] if omit_labels else [0.5, 1.0] * 4)

    asyncio.run(run())


def test_trash_during_inflight_inbox_snapshot_then_undo(pg_db, monkeypatch):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch)
        target, sibling = ids["target"], ids["sibling"]
        # Resume an initial history page. It can contain a UID already in the local index.
        state = await mail_sync_state.get_state(provider.account.id)
        progress = "initial:" + json.dumps([str(int(datetime.fromisoformat(provider.date).timestamp())), "page2"])
        await mail_sync_state.save_state(account_id=provider.account.id, connector_cursor=connector_mailbox.encode(
            connector_mailbox.decode(state.connector_cursor)[0], progress))
        fetched, publish = asyncio.Event(), asyncio.Event()
        fetch = sync._connector_changes
        async def delayed(*args, **kwargs):
            batch = await fetch(*args, **kwargs)
            assert any(row["provider_uid"] == "target" for row in batch[0].upserts)
            fetched.set()
            await publish.wait()
            return batch
        monkeypatch.setattr(sync, "_connector_changes", delayed)
        async with client() as browser:
            pending = asyncio.create_task(sync.sync_account(provider.account.id))
            await asyncio.wait_for(fetched.wait(), 5)
            try:
                result = await browser.post(f"{MAIL}/messages/{target}/actions", json={"kind": "trash"})
                assert result.status_code == 200
                assert await listed(browser, "INBOX") == {sibling}
            finally:
                publish.set()
                await pending
            assert await listed(browser, "INBOX") == {sibling}
            assert await listed(browser, "TRASH") == {target}
            undone = await browser.post(MAIL + "/actions/undo", json={"action_ids": [result.json()["action_id"]]})
            assert undone.json()["restored"] == [result.json()["action_id"]]
            assert provider.labels["target"] == {"INBOX"}
            assert await listed(browser, "INBOX") == {target, sibling}
    asyncio.run(run())


@pytest.mark.parametrize("rejection", [
    {"isError": True, "content": [{"text": "private diagnostic"}]},
    {"content": [{"text": "Provider rejected this label"}]},
    {"content": [{"text": "{}"}]},
], ids=["tool-error", "text-error", "unconfirmed-noop"])
def test_refused_trash_never_hides_mail_or_records_success(pg_db, monkeypatch, rejection):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch)
        provider.reject = rejection
        async with client() as browser:
            result = await browser.post(f"{MAIL}/messages/{ids['target']}/actions", json={"kind": "trash"})
            assert result.status_code == 502
            assert result.json()["detail"]["code"] == "provider_failed"
            assert len(provider.writes) == 1  # A failed add of TRASH must not remove INBOX.
            assert await mail_action.latest_open(ids["target"]) is None
            assert await listed(browser, "TRASH") == set()
            assert await listed(browser, "INBOX") == set(ids.values())
    asyncio.run(run())


@pytest.mark.parametrize("verified", [True, False], ids=["bound", "inbox-only"])
@pytest.mark.parametrize("undo", [False, True], ids=["trash", "undo"])
@pytest.mark.parametrize("omit_on_retry", [False, True], ids=["contradiction", "missing-labels-on-retry"])
def test_stale_full_read_cannot_confirm_a_noop_studio_move(pg_db, monkeypatch, caplog, verified, undo, omit_on_retry):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch, verified=verified)
        target, sibling = ids["target"], ids["sibling"]
        endpoint = f"{MAIL}/messages/{target}/actions"
        async with client() as browser:
            action = None
            if undo:
                moved = await browser.post(endpoint, json={"kind": "trash"})
                assert moved.status_code == 200
                action = moved.json()["action_id"]

            writes_before = len(provider.writes)
            readbacks = []
            delays = []

            async def wait_for_replication(seconds):
                delays.append(seconds)

            monkeypatch.setattr(connectors, "sleep", wait_for_replication, raising=False)
            provider.reject = {"content": [{"text": "{}"}]}
            provider.replay = True
            provider.replayed_target_labels = ["INBOX"] if undo else ["TRASH"]

            async def observe_readback(path, payload):
                result = await provider.dispatch(path, payload)
                args = payload["arguments"]
                if args.get("threadId") == "mixed" and len(provider.writes) > writes_before:
                    readbacks.append(args["messageFormat"])
                    if omit_on_retry and readbacks == ["MINIMAL", "MINIMAL"]:
                        thread = json.loads(result["content"][0]["text"])
                        member = next(row for row in thread["messages"] if row["id"] == "target")
                        member.pop("labelIds")
                        result["content"][0]["text"] = json.dumps(thread)
                return result

            monkeypatch.setattr(connectors, "_post", observe_readback)
            if undo:
                response = await browser.post(MAIL + "/actions/undo", json={"action_ids": [action]})
                assert response.status_code == 200
                assert response.json()["restored"] == []
                assert response.json()["failed"] == [action]
                assert (await mail_action.latest_open(target)).id == action
            else:
                response = await browser.post(endpoint, json={"kind": "trash"})
                assert response.status_code == 502
                assert response.json()["detail"]["code"] == "provider_failed"
                assert await mail_action.latest_open(target) is None

            diagnostics = [record.getMessage() for record in caplog.records if record.name == connectors.__name__]
            assert diagnostics == [
                "Connector label change not confirmed format=MINIMAL attempt=1 "
                "labels_present=True required_met=False forbidden_absent=True",
                f"Connector label change not confirmed format=MINIMAL attempt=2 "
                f"labels_present={not omit_on_retry} required_met=False forbidden_absent={not omit_on_retry}",
                "Connector label change not confirmed format=MINIMAL attempt=3 "
                "labels_present=True required_met=False forbidden_absent=True",
            ]
            assert readbacks == ["MINIMAL"] * 3
            assert delays == [0.5, 1.0]
            assert provider.writes[writes_before:] == [
                ("label_message", "target", ("INBOX" if undo else "TRASH",)),
            ]
            assert provider.labels == {"target": {"TRASH" if undo else "INBOX"}, "sibling": {"INBOX"}}
            assert await listed(browser, "INBOX") == ({sibling} if undo else {target, sibling})
            assert await listed(browser, "TRASH") == ({target} if undo else set())

    asyncio.run(run())


@pytest.mark.parametrize("verified", [True, False], ids=["bound", "inbox-only"])
def test_bulk_trash_needs_target_thread_before_any_connector_write(pg_db, monkeypatch, verified):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch, verified=verified)
        target, sibling = ids["target"], ids["sibling"]
        async with get_session_maker().begin() as session:
            row = await session.get(MailMessage, target)
            row.thread_id = ""
        async with client() as browser:
            response = await browser.post(MAIL + "/messages/actions", json={"message_ids": [target, sibling], "kind": "trash"})
            assert response.status_code == 200
            failed, done = response.json()["results"]
            assert (failed["message_id"], failed["status"], failed["action_id"]) == (target, "failed", None)
            assert (done["message_id"], done["status"]) == (sibling, "done")
            assert [uid for _, uid, _ in provider.writes] == ["sibling", "sibling"]
            assert provider.labels == {"target": {"INBOX"}, "sibling": {"TRASH"}}
            assert await mail_action.latest_open(target) is None
            assert await listed(browser, "INBOX") == {target}
            assert await listed(browser, "TRASH") == {sibling}
    asyncio.run(run())


def test_trash_coalesces_existing_folder_copies_without_losing_links(pg_db, monkeypatch):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch)
        async with get_session_maker().begin() as session:
            await session.execute(text("SELECT set_config('liam.verified_connector', :id, true)"), {"id": provider.account.id})
            copies = [MailMessage(account_id=provider.account.id, provider_uid="target", thread_id="mixed",
                                  folder=folder) for folder in ("ARCHIVE", "TRASH")]
            session.add_all(copies)
        async with client() as browser:
            res = await browser.post(f"{MAIL}/messages/{ids['target']}/actions", json={"kind": "trash"})
            assert res.status_code == 200
            assert provider.labels["target"] == {"TRASH"}
            assert await listed(browser, "INBOX") == {ids["sibling"]}
            assert await listed(browser, "ARCHIVE") == set()
            assert await listed(browser, "TRASH") == {ids["target"]}
            for copy in copies:
                assert (await mail_message.get_message(copy.id)).id == ids["target"]
    asyncio.run(run())


@pytest.mark.parametrize("provider_name", ["google", "microsoft", "imap"])
def test_older_native_poll_cannot_publish_after_move(pg_db, monkeypatch, provider_name):
    from unittest.mock import AsyncMock, MagicMock
    from api.mail import sync_gmail, sync_graph, sync_imap, transport
    from api.mail.sync_types import BODY_VERSION, SyncChanges

    async def run():
        account = await service.upsert_account(provider=provider_name, auth_type="imap_password" if provider_name == "imap" else "oauth2",
                                               email_address="native@example.test")
        row = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="12",
            rfc_message_id="<duplicate@example.test>", body_version=BODY_VERSION)
        sibling = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="13",
            rfc_message_id=row.rfc_message_id, body_version=BODY_VERSION)
        await mail_sync_state.save_state(account_id=account.id, cursor="7:13")
        provider = {"google": sync_gmail, "microsoft": sync_graph, "imap": sync_imap}[provider_name]
        fetched, publish = asyncio.Event(), asyncio.Event()
        async def old_page(*args, **kwargs):
            fetched.set()
            await publish.wait()
            return SyncChanges(upserts=[{"folder": "INBOX", "provider_uid": "12", "body_text": "",
                                         "rfc_message_id": row.rfc_message_id}], cursor="later")
        monkeypatch.setattr(provider, "fetch_changes", old_page)
        monkeypatch.setattr(service, "load_credentials", AsyncMock(return_value={"imap_host": "imap.example.test"}))
        monkeypatch.setattr(service, "valid_access_token", AsyncMock(return_value="test"))
        monkeypatch.setattr(service, "imap_password", AsyncMock(return_value="test"))
        monkeypatch.setattr(transport, "check_mail_host", AsyncMock())
        imap = MagicMock(capabilities=(b"MOVE",))
        imap.login.return_value = imap.select.return_value = ("OK", [])
        imap.list.return_value = ("OK", [b'(\\Trash) "/" "Deleted"'])
        imap.uid.side_effect = lambda command, *args: ("OK", [b"12"] if command == "SEARCH" else [])
        imap.response.side_effect = lambda name: (name, [b"7 12 91"] if name == "COPYUID" else [b"7"])
        monkeypatch.setattr(transport, "open_imap", lambda *_: imap)
        browser = client()
        real_client = httpx.AsyncClient
        def http_provider(req):
            if req.url.host == "graph.microsoft.com":
                assert req.url.path.endswith("/me/messages/12/move") and json.loads(req.content) == {"destinationId": "deleteditems"}
                return httpx.Response(201, json={"id": "moved"})
            if req.method == "GET":
                return httpx.Response(200, json={"id": "12", "labelIds": ["TRASH"]})
            assert req.url.path.endswith("/messages/12/trash")
            return httpx.Response(200, json={"id": "12", "labelIds": ["TRASH"]})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(http_provider), **kw))
        async with browser:
            pending = asyncio.create_task(sync.sync_account(account.id))
            await asyncio.wait_for(fetched.wait(), 5)
            try:
                result = await browser.post(f"{MAIL}/messages/{row.id}/actions", json={"kind": "trash"})
                assert result.status_code == 200
                assert await listed(browser, "INBOX") == {sibling.id}
                if provider_name == "imap":
                    imap.uid.assert_any_call("MOVE", "12", '"Deleted"')
            finally:
                publish.set()
                await pending
            assert await listed(browser, "INBOX") == {sibling.id}
            assert await listed(browser, "TRASH") == {row.id}
            assert (await mail_sync_state.get_state(account.id)).cursor == "7:13"
    asyncio.run(run())


def test_graph_delta_replay_after_id_changing_trash_and_undo(pg_db, monkeypatch):
    from unittest.mock import AsyncMock
    from api.mail.sync_types import BODY_VERSION

    async def run():
        account = await service.upsert_account(provider="microsoft", auth_type="oauth2", email_address="outlook@example.test")
        target = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="old", body_version=BODY_VERSION)
        delta = "https://graph.microsoft.com/v1.0/delta"
        await mail_sync_state.save_state(account_id=account.id, cursor=delta)
        browser = client()
        real_client = httpx.AsyncClient
        current = "old"
        def respond(req):
            nonlocal current
            path = req.url.path
            if path.endswith("/move"):
                destination = json.loads(req.content)["destinationId"]
                assert path.endswith(f"/messages/{current}/move")
                current = {"deleteditems": "in-trash", "inbox": "back"}[destination]
                return httpx.Response(201, json={"id": current})
            if req.method == "PATCH":  # restore the old read flag
                assert path.endswith("/messages/in-trash")
                return httpx.Response(200, json={})
            if path.endswith("/delta"):
                return httpx.Response(200, json={"@odata.deltaLink": delta, "value": [{"id": "old"}, {"id": "other"}]})
            assert req.method == "GET" and path.endswith("/mailFolders/inbox/messages/old")
            return httpx.Response(404)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
        monkeypatch.setattr(service, "load_credentials", AsyncMock(return_value={}))
        monkeypatch.setattr(service, "valid_access_token", AsyncMock(return_value="test"))
        async with browser:
            result = await browser.post(f"{MAIL}/messages/{target.id}/actions", json={"kind": "trash"})
            assert result.status_code == 200 and current == "in-trash"
            assert await listed(browser, "INBOX") == set()
            assert (await sync.sync_account(account.id)).status == "ok"
            assert set(await mail_message.known_uids(account.id, "INBOX")) == {"other"}
            undone = await browser.post(MAIL + "/actions/undo", json={"action_ids": [result.json()["action_id"]]})
            assert undone.json()["restored"] == [result.json()["action_id"]]
            assert (await mail_message.known_uids(account.id, "INBOX"))["back"] == target.id and current == "back"
            assert (await sync.sync_account(account.id)).status == "ok"
            assert set(await mail_message.known_uids(account.id, "INBOX")) == {"other", "back"}
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["trash", "undo"])
def test_native_provider_move_overlapping_rebuild_keeps_action_id(pg_db, monkeypatch, kind):
    """The other ordering: the provider moved the mail, then sync commits before the action ack."""
    from unittest.mock import AsyncMock
    from api.mail import sync_graph
    from api.mail.sync_types import BODY_VERSION, SyncChanges

    async def run():
        account = await service.upsert_account(provider="microsoft", auth_type="oauth2", email_address="overlap@example.test")
        target = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="first",
            rfc_message_id="<item@example.test>", body_text="Kieselstein", body_version=BODY_VERSION)
        browser = client()
        real_client = httpx.AsyncClient
        async def dispatch(req):
            if req.method == "PATCH":
                return httpx.Response(200, json={})
            destination = json.loads(req.content)["destinationId"]
            is_undo = destination == "inbox"
            uid = "back" if is_undo else "in-trash"
            if (kind == "undo") == is_undo:
                page = SyncChanges(cursor="next", full_rebuild=True)
                if is_undo:
                    page.upserts.append({"folder": "INBOX", "provider_uid": uid,
                                         "rfc_message_id": target.rfc_message_id, "body_text": ""})
                monkeypatch.setattr(sync_graph, "fetch_changes", AsyncMock(return_value=page))
                assert (await sync.sync_account(account.id)).status == "ok"
            return httpx.Response(201, json={"id": uid})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(dispatch), **kw))
        monkeypatch.setattr(service, "load_credentials", AsyncMock(return_value={}))
        monkeypatch.setattr(service, "valid_access_token", AsyncMock(return_value="test"))
        async with browser:
            result = await browser.post(f"{MAIL}/messages/{target.id}/actions", json={"kind": "trash"})
            assert result.status_code == 200 and await listed(browser, "TRASH") == {target.id}
            assert await listed(browser, "TRASH", query="Kieselstein") == {target.id}
            action_id = result.json()["action_id"]
            undone = await browser.post(MAIL + "/actions/undo", json={"action_ids": [action_id]})
            assert undone.json()["restored"] == [action_id]
            assert (await mail_message.known_uids(account.id, "INBOX")) == {"back": target.id}
            assert await listed(browser, "INBOX", query="Kieselstein") == {target.id}
            assert await listed(browser, "TRASH") == set()
    asyncio.run(run())


def test_rebuilt_native_move_requeues_unloaded_html(pg_db):
    from sqlalchemy import delete, inspect

    async def run():
        account = await service.upsert_account(provider="google", auth_type="oauth2", email_address="synthetic@example.test")
        created = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="synthetic",
            body_html="<p>Synthetic</p>", html_status="available", html_version=1)
        source = await mail_message.get_message(created.id)
        assert "body_html" in inspect(source).unloaded and source.html_status == "available"
        async with get_session_maker().begin() as session:
            await session.execute(delete(MailMessage).where(MailMessage.id == source.id))
        await mail_message.commit_trash_move(source, {"folder": "TRASH"})
        rebuilt = await mail_message.get_message(source.id, include_html=True)
        assert (rebuilt.body_html, rebuilt.html_status, rebuilt.html_version) == (None, "pending", 0)
        await mail_message.commit_trash_move(rebuilt, {"folder": "INBOX"})
        assert source.id in {row.id for row in await mail_message.stale_html_bodies(account.id, 1, limit=10)}
    asyncio.run(run())


def test_incremental_flags_heal_a_legacy_inbox_copy_of_a_trashed_mail(pg_db, monkeypatch):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch)
        async with client() as browser:
            res = await browser.post(f"{MAIL}/messages/{ids['target']}/actions", json={"kind": "trash"})
            assert res.status_code == 200
            # A legacy worker inserted an ordinary, unverified INBOX copy after the trash action.
            old = await mail_message.upsert_message(account_id=provider.account.id, provider_uid="target",
                thread_id="mixed", folder="INBOX", subject="Legacy source", internal_date=datetime.now(UTC))
            provider.replay = True
            assert (await sync.sync_account(provider.account.id)).status == "ok"
            assert await listed(browser, "INBOX") == {ids["sibling"]}
            assert await listed(browser, "TRASH") == {ids["target"]}
            assert await mail_message.get_message(old.id) is None
            legacy = await browser.get(f"{MAIL}/messages/{old.id}")
            assert legacy.json()["local_reference_only"] and legacy.json()["subject"] == "Legacy source"
    asyncio.run(run())


def test_gmail_history_replay_and_real_external_restore(pg_db, monkeypatch):
    from unittest.mock import AsyncMock
    from api.mail.sync_types import BODY_VERSION

    async def run():
        account = await service.upsert_account(provider="google", auth_type="oauth2", email_address="gmail@example.test")
        original = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="target",
                                                    labels_json='["INBOX"]', body_version=BODY_VERSION)
        await mail_sync_state.save_state(account_id=account.id, cursor="19")
        browser = client()
        real_client = httpx.AsyncClient
        labels = ["INBOX"]
        replay = ["INBOX"]
        def respond(req):
            if req.url.path.endswith("/trash"):
                labels[:] = ["TRASH"]
                return httpx.Response(200, json={"id": "target", "labelIds": labels})
            if req.url.path.endswith("/history"):
                return httpx.Response(200, json={"historyId": "20", "history": [
                    {"labelsAdded": [{"message": {"id": "target"}}]}]})
            assert req.url.path.endswith("/messages/target")
            return httpx.Response(200, json={"id": "target", "labelIds": replay if req.url.params["format"] == "full" else labels})
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
        monkeypatch.setattr(service, "load_credentials", AsyncMock(return_value={}))
        monkeypatch.setattr(service, "valid_access_token", AsyncMock(return_value="test"))
        async with browser:
            result = await browser.post(f"{MAIL}/messages/{original.id}/actions", json={"kind": "trash"})
            assert result.status_code == 200
            for observed in (["INBOX", "TRASH"], ["INBOX"]):
                replay[:] = observed
                assert (await sync.sync_account(account.id)).status == "ok"
                assert await listed(browser, "INBOX") == set()
                assert await listed(browser, "TRASH") == {original.id}
            labels[:] = ["INBOX"]
            assert (await sync.sync_account(account.id)).status == "ok"
            assert await listed(browser, "INBOX") == {original.id} and await listed(browser, "TRASH") == set()
            replay[:] = ["TRASH"]
            assert (await sync.sync_account(account.id)).status == "ok"
            assert await listed(browser, "INBOX") == {original.id}
            labels[:] = ["TRASH"]
            assert (await sync.sync_account(account.id)).status == "ok"
            assert await listed(browser, "TRASH") == {original.id}
    asyncio.run(run())


def test_imap_late_source_uid_replay_and_uidvalidity_reuse(pg_db, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from api.mail import sync_imap, transport
    from api.mail.sync_types import BODY_VERSION, SyncChanges
    from db.accessors import mail_insight

    async def run():
        account = await service.upsert_account(provider="imap", auth_type="imap_password", email_address="imap@example.test")
        target = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="12",
                                                  rfc_message_id="<first@example.test>", body_version=BODY_VERSION)
        existing = await mail_message.upsert_message(account_id=account.id, folder="TRASH", provider_uid="12",
                                                    rfc_message_id="<independent@example.test>", body_version=BODY_VERSION)
        await mail_sync_state.save_state(account_id=account.id, cursor="7:12")
        validity = "7"
        removed = False
        imap = MagicMock(capabilities=(b"MOVE",))
        imap.select.return_value = imap.login.return_value = ("OK", [])
        imap.list.return_value = ("OK", [b'(\\Trash) "/" "Deleted"'])
        imap.response.side_effect = lambda name: (name, [b"7 12 91"] if name == "COPYUID" else [validity.encode()])
        def command(name, *args):
            if name.upper() == "SEARCH":
                return "OK", [b"12"]
            if name.upper() == "FETCH" and validity == "8" and not removed:
                return "OK", [b"1 (UID 12 FLAGS ())"]
            return "OK", []
        imap.uid.side_effect = command
        monkeypatch.setattr(transport, "open_imap", lambda *_: imap)
        monkeypatch.setattr(transport, "check_mail_host", AsyncMock())
        monkeypatch.setattr(service, "load_credentials", AsyncMock(return_value={"imap_host": "imap.example.test"}))
        monkeypatch.setattr(service, "imap_password", AsyncMock(return_value="test"))
        async def page(*args, **kwargs):
            if removed:
                return SyncChanges(cursor="8:12", moved_out=["12"])
            return SyncChanges(cursor=f"{validity}:12", full_rebuild=validity == "8",
                upserts=[{"folder": "INBOX", "provider_uid": "12", "body_text": "", "rfc_message_id":
                         target.rfc_message_id if validity == "7" else "<new@example.test>"}])
        monkeypatch.setattr(sync_imap, "fetch_changes", page)
        async with client() as browser:
            result = await browser.post(f"{MAIL}/messages/{target.id}/actions", json={"kind": "trash"})
            assert result.status_code == 200
            assert (await sync.sync_account(account.id)).status == "ok"
            assert await listed(browser, "INBOX") == set()
            assert await listed(browser, "TRASH") == {existing.id, target.id}
            validity = "8"
            assert (await sync.sync_account(account.id)).status == "ok"
            returned = await mail_message.list_page(folder="INBOX")
            assert [row.rfc_message_id for row in returned] == ["<new@example.test>"]
            assert returned[0].id not in {existing.id, target.id}
            assert await listed(browser, "TRASH") == {existing.id, target.id}
            replacement = returned[0].id
            await mail_insight.store_if_missing(replacement, points=["Synthetic"], deadline=None,
                                                needs_reply=False, followups=[], model="test")
            removed = True
            assert (await sync.sync_account(account.id)).status == "ok"
            assert await listed(browser, "ARCHIVE") == {replacement}
            assert await mail_insight.get(replacement) is not None
            assert await listed(browser, "TRASH") == {existing.id, target.id}
    asyncio.run(run())


def test_imap_old_inbox_rebuild_commits_before_trash_ack(pg_db, monkeypatch):
    from unittest.mock import AsyncMock
    from bulk_mailboxes import Mailboxes
    from api.mail import sync_imap

    async def run():
        provider = Mailboxes(["Yahoo"], False, "move")
        await provider.connect(monkeypatch)
        account = provider.accounts["Yahoo"]
        ids = await mail_message.known_uids(account.id, "INBOX")
        before_move = await sync_imap.fetch_changes(account, cursor="", folders=["INBOX"], limit=20, max_body_chars=100)
        assert before_move.full_rebuild
        commit = mail_message.commit_trash_move
        async def rebuild_first(*args, **kwargs):
            with monkeypatch.context() as patch:
                patch.setattr(sync_imap, "fetch_changes", AsyncMock(return_value=before_move))
                assert (await sync.sync_account(account.id)).status == "ok"
            # The matching UIDVALIDITY now preserves the local identity and its app filing.
            assert (await mail_message.known_uids(account.id, "INBOX"))["11"] == ids["11"]
            await commit(*args, **kwargs)
        monkeypatch.setattr(mail_message, "commit_trash_move", rebuild_first)
        async with client() as browser:
            result = await browser.post(MAIL + "/messages/actions", json={"message_ids": [ids["11"]], "kind": "trash"})
            assert result.json()["results"][0]["status"] == "done"
            assert provider.labels["Yahoo"]["11"] == {"TRASH"}
            rows = (await browser.get(MAIL + "/messages")).json()["items"]
            assert {row["subject"] for row in rows} == {"Yahoo 12", "Yahoo 13", "Yahoo 14"}
            assert await listed(browser, "TRASH") == {ids["11"]}
    asyncio.run(run())


def test_imap_refuses_a_reused_uid_even_with_the_same_message_id(pg_db, monkeypatch):
    from bulk_mailboxes import Mailboxes, YahooConnection

    async def run():
        provider = Mailboxes(["Yahoo"], False, "move")
        await provider.connect(monkeypatch)
        ids = await mail_message.known_uids(provider.accounts["Yahoo"].id, "INBOX")
        before = YahooConnection.response
        def changed_generation(connection, name):
            if name == "UIDVALIDITY" and connection.folder == "INBOX":
                return name, [b"9"]
            return before(connection, name)
        monkeypatch.setattr(YahooConnection, "response", changed_generation)
        # The fresh delivery has the same numeric UID and RFC Message-ID as the selected row.
        writes = []
        submit = YahooConnection.uid
        def record(connection, cmd, *args):
            if cmd.upper() in {"MOVE", "COPY", "STORE", "EXPUNGE"}:
                writes.append(cmd)
            return submit(connection, cmd, *args)
        monkeypatch.setattr(YahooConnection, "uid", record)
        async with client() as browser:
            for cursor in ("7:14", "9:14"):
                # Even a newer account cursor cannot rebind the selected generation-7 row.
                await mail_sync_state.save_state(account_id=provider.accounts["Yahoo"].id, cursor=cursor)
                response = await browser.post(MAIL + "/messages/actions", json={"message_ids": [ids["11"]], "kind": "trash"})
                result, = response.json()["results"]
                assert result["status"] == "failed" and result["action_id"] is None
            assert writes == [] and all(labels == {"INBOX"} for labels in provider.labels["Yahoo"].values())
            assert await listed(browser, "INBOX") == set(ids.values())
            assert await listed(browser, "TRASH") == set()
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["text", "unauthorized"])
def test_unreadable_studio_trash_thread_and_new_inbox_arrival(pg_db, monkeypatch, fault):
    async def run():
        provider = StudioMailbox()
        provider.date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        provider.labels = {"target": {"INBOX"}}
        ids = await provider.connect(monkeypatch)
        async with client() as browser:
            assert (await browser.post(f"{MAIL}/messages/{ids['target']}/actions", json={"kind": "trash"})).is_success
            before = (await mail_sync_state.get_state(provider.account.id)).cursor
            now = datetime.now(UTC)
            thread = {"id": "new-thread", "messages": [{"id": "arrival", "labelIds": ["INBOX"], "date": now.isoformat(),
                                                       "subject": "New arrival", "sender": "new@example.test"}]}
            async def observe(path, payload):
                args = payload["arguments"]
                if urlsplit(path).path.endswith("/search_threads/call"):
                    body = {"threads": [thread]}
                elif args.get("threadId") == "new-thread":
                    body = thread
                elif args.get("threadId") == "mixed":
                    if fault == "unauthorized":
                        req = httpx.Request("POST", "https://example.test")
                        raise httpx.HTTPStatusError("test", request=req, response=httpx.Response(401, request=req))
                    return {"isError": True, "content": [{"type": "text", "text": "Thread unavailable"}]}
                else:
                    return await provider.dispatch(path, payload)
                return {"content": [{"type": "text", "text": json.dumps(body)}]}
            monkeypatch.setattr(connectors, "_post", observe)
            result = await sync.sync_account(provider.account.id)
            current = (await mail_sync_state.get_state(provider.account.id)).cursor
            if fault == "unauthorized":
                assert (result.status, result.error_code) == ("error", "auth_expired")
                assert current == before and await listed(browser, "INBOX") == set()
            else:
                assert result.status == "ok" and current != before
                assert (await sync.sync_account(provider.account.id)).status == "ok"
                rows = (await browser.get(MAIL + "/messages")).json()["items"]
                assert [row["subject"] for row in rows] == ["New arrival"]
            assert await listed(browser, "TRASH") == {ids["target"]}
    asyncio.run(run())


@pytest.mark.parametrize("initially_indexed", [True, False])
def test_imap_deleted_flag_cleared_below_cursor(pg_db, monkeypatch, initially_indexed):
    from bulk_mailboxes import Mailboxes, YahooConnection

    async def run():
        provider = Mailboxes(["Yahoo"], False, "move")
        deleted = not initially_indexed
        submit = YahooConnection.uid
        def observe(self, cmd, *args):
            status, data = submit(self, cmd, *args)
            if not deleted or cmd.upper() != "FETCH":
                return status, data
            def flag(header):
                return header.replace(b"FLAGS ()", b"FLAGS (\\Deleted)") if b"UID 11 " in header else header
            return status, [(flag(item[0]), item[1]) if isinstance(item, tuple) else flag(item) for item in data]
        monkeypatch.setattr(YahooConnection, "uid", observe)
        await provider.connect(monkeypatch)
        account = provider.accounts["Yahoo"]
        ids = await mail_message.known_uids(account.id, "INBOX")
        assert ("11" in ids) == initially_indexed
        older = await mail_message.upsert_message(account_id=account.id, folder="ARCHIVE", provider_uid="11",
            rfc_message_id="<older-generation@example.test>", subject="Older archive")
        if initially_indexed:
            await mail_sync_state.save_state(account_id=account.id, cursor="7:14")
        provider.yahoo_folders["INBOX"]["15"] = "15"
        provider.labels["Yahoo"]["15"] = {"INBOX"}
        deleted = True
        async with client() as browser:
            assert (await sync.sync_account(account.id)).status == "ok"
            assert len(await listed(browser, "INBOX")) == 4
            assert "15" in await mail_message.known_uids(account.id, "INBOX")
            deleted = False
            assert (await sync.sync_account(account.id)).status == "ok"
            restored = await mail_message.known_uids(account.id, "INBOX")
            assert set(restored) == {"11", "12", "13", "14", "15"}
            if initially_indexed:
                assert restored["11"] == ids["11"]
            assert await listed(browser, "ARCHIVE") == {older.id}
    asyncio.run(run())


def test_studio_owner_restores_quiet_old_trash_without_duplicate(pg_db, monkeypatch):
    async def run():
        provider = StudioMailbox()
        provider.date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        ids = await provider.connect(monkeypatch)
        async with client() as browser:
            move = await browser.post(f"{MAIL}/messages/{ids['target']}/actions", json={"kind": "trash"})
            assert move.status_code == 200 and await listed(browser, "TRASH") == {ids["target"]}
            async def quiet(path, payload):
                if urlsplit(path).path.endswith("/search_threads/call"):
                    return {"content": [{"type": "text", "text": '{"threads":[]}'}]}
                return await provider.dispatch(path, payload)
            monkeypatch.setattr(connectors, "_post", quiet)
            assert (await sync.sync_account(provider.account.id)).status == "ok"
            assert await listed(browser, "TRASH") == {ids["target"]}
            provider.labels["target"] = {"INBOX", "UNREAD", "STARRED"}
            assert (await sync.sync_account(provider.account.id)).status == "ok"
            assert ids["target"] in await listed(browser, "INBOX")
            filters = await browser.get(MAIL + "/messages", params={"unread": True, "important": True})
            assert [row["id"] for row in filters.json()["items"]] == [ids["target"]]
            assert await listed(browser, "TRASH") == set()
            assert len(provider.writes) == 2
    asyncio.run(run())


def test_graph_undo_restore_rejects_intermediate_inbox_id(pg_db, monkeypatch):
    from unittest.mock import AsyncMock
    from api.mail.sync_types import BODY_VERSION

    async def run():
        account = await service.upsert_account(provider="microsoft", auth_type="oauth2", email_address="graph@example.test")
        row = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="initial", body_version=BODY_VERSION)
        delta = "https://graph.microsoft.com/v1.0/delta"
        await mail_sync_state.save_state(account_id=account.id, cursor=delta)
        browser = client()
        real_client = httpx.AsyncClient
        moves = iter([("initial", "deleteditems", "trash-one"), ("trash-one", "inbox", "returned"),
                      ("returned", "deleteditems", "trash-two")])
        def dispatch(request):
            path = request.url.path
            if path.endswith("/move"):
                previous, destination, next_id = next(moves)
                assert path.endswith(f"/messages/{previous}/move") and json.loads(request.content)["destinationId"] == destination
                return httpx.Response(201, json={"id": next_id})
            if request.method == "PATCH":
                return httpx.Response(200, json={})
            if path.endswith("/delta"):
                return httpx.Response(200, json={"value": [{"id": "returned"}, {"id": "control"}], "@odata.deltaLink": delta})
            assert path.endswith("/mailFolders/inbox/messages/returned")
            return httpx.Response(404)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(dispatch), **kw))
        monkeypatch.setattr(service, "load_credentials", AsyncMock(return_value={}))
        monkeypatch.setattr(service, "valid_access_token", AsyncMock(return_value="test"))
        async with browser:
            endpoint = f"{MAIL}/messages/{row.id}/actions"
            assert (await browser.post(endpoint, json={"kind": "trash"})).status_code == 200
            restored = await browser.post(endpoint, json={"kind": "restore"})
            assert restored.status_code == 200 and await listed(browser, "INBOX") == {row.id}
            token = restored.json()["action_id"]
            undo = await browser.post(MAIL + "/actions/undo", json={"action_ids": [token]})
            assert undo.json()["restored"] == [token] and await listed(browser, "INBOX") == set()
            assert (await sync.sync_account(account.id)).status == "ok"
            assert set(await mail_message.known_uids(account.id, "INBOX")) == {"control"}
            assert await listed(browser, "TRASH") == {row.id}
    asyncio.run(run())


@pytest.mark.parametrize("verified", [True, False], ids=["bound", "inbox-only"])
@pytest.mark.parametrize("reject", [
    {"isError": True, "content": [{"text": "Refused"}]},
    {"content": [{"text": "{}"}]},
], ids=["refused", "unconfirmed-noop"])
def test_partial_label_failure_publishes_observed_trash(pg_db, monkeypatch, verified, reject):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch, verified=verified)

        async def retry_without_delay(_):
            pass

        monkeypatch.setattr(connectors, "sleep", retry_without_delay)
        provider.reject = reject
        provider.reject_tool = "unlabel_message"
        async with client() as browser:
            result = await browser.post(f"{MAIL}/messages/{ids['target']}/actions", json={"kind": "trash"})
            assert provider.labels["target"] == {"INBOX", "TRASH"}
            assert await mail_action.latest_open(ids["target"]) is None
            assert result.status_code == 409
            assert result.json()["detail"] == {
                "code": "provider_partial",
                "message": "Der Anbieter hat nur einen Teil der Änderung ausgeführt.",
                "folder": "TRASH",
            }
            assert await listed(browser, "INBOX") == {ids["sibling"]}
            assert await listed(browser, "TRASH") == {ids["target"]}
            provider.replay = True
            assert (await sync.sync_account(provider.account.id)).status == "ok"
            assert await listed(browser, "TRASH") == {ids["target"]}
            assert await listed(browser, "INBOX") == {ids["sibling"]}
    asyncio.run(run())


def test_bulk_trash_reports_partial_and_preserves_successful_undo(pg_db, monkeypatch):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch)
        async def refuse_target_inbox_removal(path, payload):
            args = payload["arguments"]
            if "/unlabel_message/" in path and args["messageId"] == "target":
                return {"isError": True, "content": [{"text": "Refused"}]}
            return await provider.dispatch(path, payload)
        monkeypatch.setattr(connectors, "_post", refuse_target_inbox_removal)
        target, sibling = ids["target"], ids["sibling"]
        async with client() as browser:
            response = await browser.post(MAIL + "/messages/actions", json={"kind": "trash", "message_ids": [target, sibling]})
            assert response.status_code == 200
            partial, done = response.json()["results"]
            assert (partial["message_id"], partial["status"], partial.get("folder"), partial["action_id"]) == (target, "partial", "TRASH", None)
            assert done["message_id"] == sibling and done["status"] == "done"
            assert await listed(browser, "TRASH") == {target, sibling}
            undone = await browser.post(MAIL + "/actions/undo", json={"action_ids": [done["action_id"]]})
            assert undone.json()["restored"] == [done["action_id"]]
            assert await listed(browser, "INBOX") == {sibling}
            assert await listed(browser, "TRASH") == {target}

    asyncio.run(run())


def test_partial_connector_undo_keeps_real_labels_and_releases_the_token(pg_db, monkeypatch):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch)
        target, sibling = ids["target"], ids["sibling"]
        async with client() as browser:
            moved = await browser.post(f"{MAIL}/messages/{target}/actions", json={"kind": "trash"})
            assert moved.status_code == 200
            action = moved.json()["action_id"]
            provider.reject = {"isError": True, "content": [{"text": "Refused"}]}
            provider.reject_tool = "unlabel_message"
            refused = await browser.post(MAIL + "/actions/undo", json={"action_ids": [action]})
            assert refused.status_code == 200 and refused.json()["failed"] == [action]
            assert refused.json()["restored"] == []
            assert (await mail_action.latest_open(target)).id == action
            assert await listed(browser, "INBOX") == {sibling}
            assert await listed(browser, "TRASH") == {target}
            actual = await mail_message.get_message(target)
            assert set(json.loads(actual.labels_json)) == {"TRASH", "INBOX"}
            provider.reject = None
            retried = await browser.post(MAIL + "/actions/undo", json={"action_ids": [action]})
            assert retried.json()["restored"] == [action]
            assert await listed(browser, "INBOX") == {target, sibling}
            assert await listed(browser, "TRASH") == set()

    asyncio.run(run())


def test_undo_survives_a_delayed_trash_observation(pg_db, monkeypatch):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch)
        async with client() as browser:
            moved = await browser.post(f"{MAIL}/messages/{ids['target']}/actions", json={"kind": "trash"})
            action = moved.json()["action_id"]
            undone = await browser.post(MAIL + "/actions/undo", json={"action_ids": [action]})
            assert undone.json()["restored"] == [action]
            provider.replay = True
            provider.replayed_target_labels = ["TRASH"]
            assert (await sync.sync_account(provider.account.id)).status == "ok"
            assert await listed(browser, "INBOX") == set(ids.values())
            assert await listed(browser, "TRASH") == set()
            provider.labels["target"] = {"TRASH"}  # The owner actually trashes it again.
            assert (await sync.sync_account(provider.account.id)).status == "ok"
            assert await listed(browser, "INBOX") == {ids["sibling"]}
            assert await listed(browser, "TRASH") == {ids["target"]}
    asyncio.run(run())


@pytest.mark.parametrize(("provider", "live_folder"), [
    ("studio", "TRASH"), ("studio", "INBOX"), ("studio", None), ("google", "TRASH"),
    ("microsoft", "TRASH"), ("imap", "TRASH")])
def test_quiet_legacy_duplicate_heals_on_normal_sync(pg_db, monkeypatch, provider, live_folder):
    from unittest.mock import AsyncMock, MagicMock
    from api.mail import transport
    from api.mail.sync_types import BODY_VERSION

    async def run():
        account = await service.upsert_account(provider="gmail" if provider == "studio" else provider,
            auth_type={"studio": connectors.CONNECTOR, "imap": "imap_password"}.get(provider, "oauth2"),
            email_address="owner@example.test")
        old = datetime.now(UTC) - timedelta(days=60)
        progress = {"studio": str(int(old.timestamp()) + 10), "google": "23", "imap": "7:13",
                    "microsoft": "https://graph.microsoft.com/v1.0/delta"}[provider]
        await mail_sync_state.save_state(account_id=account.id, cursor=progress, connector_verified=provider == "studio",
            connector_cursor=connector_mailbox.encode(("named", "anchor", "proof"), progress) if provider == "studio" else "")
        def row(folder, uid):
            return MailMessage(account_id=account.id, folder=folder, provider_uid=uid, thread_id="mixed",
                internal_date=old, body_version=BODY_VERSION, rfc_message_id="<same@example.test>")
        original = row("TRASH", "moved" if provider == "microsoft" else "12")
        ghost, sibling = row("INBOX", "12"), row("INBOX", "13")
        archive = row("ARCHIVE", "12")
        archive.rfc_message_id = "<independent@example.test>"
        pending = {folder: [] for folder in ("INBOX", "TRASH")}
        async with get_session_maker().begin() as session:
            await session.execute(text("SELECT set_config('liam.verified_connector', :id, true)"), {"id": account.id})
            session.add_all([original, ghost, sibling])
            if live_folder is None:
                # A page of inconclusive older pairs must not starve the next pair.
                for folder in pending:
                    pending[folder] = [row(folder, f"00-{i:02d}") for i in range(25)]
                    session.add_all(pending[folder])
            if provider == "imap":
                # An independent archived mail can have the same numeric UID.
                session.add(archive)
        await mail_action.record(message_id=original.id, account_id=account.id, kind="trash",
            previous_json=json.dumps({"provider_uid": "12", "folder": "INBOX", "is_read": False, "labels_json": '["INBOX"]'}))
        browser = client()
        evidence = live_folder
        if provider == "studio":
            async def studio_read(name, tool, args):
                if tool == "search_threads":
                    return {"threads": []}
                assert tool == "get_thread"
                if args["threadId"] == "anchor":
                    return {"id": "anchor", "messages": [{"id": "proof", "sender": account.email_address, "labelIds": ["SENT"]}]}
                assert args["threadId"] == "mixed"
                labels = {"labelIds": [evidence]} if evidence else {}
                return {"id": "mixed", "messages": [{"id": "12", **labels}]}
            monkeypatch.setattr(connectors, "call_read_tool_json", studio_read)
        elif provider == "imap":
            imap = MagicMock()
            imap.login.return_value = imap.select.return_value = ("OK", [])
            imap.response.return_value = ("UIDVALIDITY", [b"7"])
            def command(name, uids, *args):
                if name.lower() == "search":
                    return "OK", [b""]
                assert name.lower() == "fetch"
                return "OK", [b"1 (UID 13 FLAGS ())"] if uids in ("12:13", "13:13") else []
            imap.uid.side_effect = command
            monkeypatch.setattr(transport, "open_imap", lambda *_: imap)
            monkeypatch.setattr(transport, "check_mail_host", AsyncMock())
            monkeypatch.setattr(service, "imap_password", AsyncMock(return_value="test"))
            monkeypatch.setattr(service, "load_credentials", AsyncMock(return_value={"imap_host": "imap.example.test"}))
        else:
            real_client = httpx.AsyncClient
            def read(req):
                assert req.method == "GET"
                if req.url.path.endswith("/delta"):
                    return httpx.Response(200, json={"value": [], "@odata.deltaLink": progress})
                if req.url.path.endswith("/history"):
                    return httpx.Response(200, json={"history": [], "historyId": progress})
                assert req.url.path.endswith("/messages/12")
                return httpx.Response(404) if provider == "microsoft" else httpx.Response(200, json={"id": "12", "labelIds": ["TRASH"]})
            monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(read), **kw))
            monkeypatch.setattr(service, "load_credentials", AsyncMock(return_value={}))
            monkeypatch.setattr(service, "valid_access_token", AsyncMock(return_value="test"))
        async with browser:
            async def visible(folder):
                return await listed(browser, folder) - {item.id for item in pending.get(folder, [])}
            assert await visible("INBOX") == {ghost.id, sibling.id}
            if evidence is None:
                for _ in range(2):
                    assert (await sync.sync_account(account.id)).status == "ok"
                    assert await visible("INBOX") == {ghost.id, sibling.id}
                    assert await visible("TRASH") == {original.id}
                evidence = "TRASH"
                assert (await sync.sync_account(account.id)).status == "ok"
                assert ghost.id in await visible("INBOX")  # First repair page, still inconclusive.
            for _ in range(2):
                assert (await sync.sync_account(account.id)).status == "ok"
                assert await visible("INBOX") == ({sibling.id, original.id} if evidence == "INBOX" else {sibling.id})
                assert await visible("TRASH") == (set() if evidence == "INBOX" else {original.id})
                if provider == "imap":
                    # The old action has no UIDVALIDITY. Preserve the unlinked row and its identity.
                    assert await listed(browser, "ARCHIVE") == {archive.id, ghost.id}
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["Yahoo", "Graph"])
def test_open_trash_token_keeps_previous_api_snapshot(pg_db, monkeypatch, provider):
    from unittest.mock import AsyncMock
    from bulk_mailboxes import Mailboxes

    async def run():
        browser = client()
        if provider == "Yahoo":
            mailbox = Mailboxes(["Yahoo"], False, "move")
            await mailbox.connect(monkeypatch)
            account = mailbox.accounts["Yahoo"]
            target = (await mail_message.known_uids(account.id, "INBOX"))["11"]
        else:
            account = await service.upsert_account(provider="microsoft", auth_type="oauth2", email_address="graph@example.test")
            target = (await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="11")).id
            real_client = httpx.AsyncClient
            def graph(request):
                if request.method == "PATCH" and request.url.path.endswith("/messages/trashed"):
                    return httpx.Response(200, json={})
                destination = json.loads(request.content)["destinationId"]
                uid, moved_uid = {"deleteditems": ("11", "trashed"), "inbox": ("trashed", "returned")}[destination]
                assert request.url.path.endswith(f"/messages/{uid}/move")
                return httpx.Response(201, json={"id": moved_uid})
            monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(graph), **kw))
            monkeypatch.setattr(service, "load_credentials", AsyncMock(return_value={}))
            monkeypatch.setattr(service, "valid_access_token", AsyncMock(return_value="test"))
        async with browser:
            result = await browser.post(MAIL + "/messages/actions", json={"kind": "trash", "message_ids": [target]})
            token = result.json()["results"][0]["action_id"]
            record, = await mail_action.get_many([token])
            assert await listed(browser, "TRASH") == {target}
            undo = await browser.post(MAIL + "/actions/undo", json={"action_ids": [token]})
            assert undo.status_code == 200 and undo.json()["restored"] == [token]
            assert target in await listed(browser, "INBOX") and await listed(browser, "TRASH") == set()
            assert (await browser.post(MAIL + "/actions/undo", json={"action_ids": [token]})).json()["skipped"] == [token]
            # The previous API passes the entire snapshot as keyword arguments to set_state.
            assert set(json.loads(record.previous_json)) == {"folder", "is_read", "provider_uid", "labels_json"}
    asyncio.run(run())


@pytest.mark.parametrize('path', ['single', 'bulk', 'partial', 'undo'])
def test_studio_permission_denial_never_confirms_a_write(pg_db, monkeypatch, path):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch)
        target = ids['target']
        endpoint = f'{MAIL}/messages/{target}/actions'
        refusal = {'content': [{'type': 'text', 'text': 'Insufficient authentication scopes. Private synthetic diagnostic.'}]}
        async with client() as browser:
            if path == 'undo':
                moved = await browser.post(endpoint, json={'kind': 'trash'})
                assert moved.status_code == 200
                token = moved.json()['action_id']
                provider.reject = refusal
                refused = await browser.post(MAIL + '/actions/undo', json={'action_ids': [token]})
                assert refused.status_code == 200
                assert refused.json()['failed'] == [token]
                assert refused.json()['restored'] == []
                assert refused.json()['error_code'] == 'provider_permission_missing'
                assert (await mail_action.latest_open(target)).id == token
            else:
                provider.reject = refusal
                if path == 'partial':
                    provider.reject_tool = 'unlabel_message'
                if path == 'bulk':
                    refused = await browser.post(MAIL + '/messages/actions', json={'kind': 'trash', 'message_ids': [target]})
                    assert refused.status_code == 200
                    result, = refused.json()['results']
                    assert (result['status'], result['error_code'], result['action_id']) == ('failed', 'provider_permission_missing', None)
                else:
                    refused = await browser.post(endpoint, json={'kind': 'trash'})
                    assert refused.status_code == (409 if path == 'partial' else 403)
                    assert refused.json()['detail']['code'] == 'provider_permission_missing'
                    if path == 'partial':
                        assert refused.json()['detail']['folder'] == 'TRASH'
                assert await mail_action.latest_open(target) is None
            assert 'Private synthetic diagnostic' not in refused.text
            assert await listed(browser, 'TRASH') == ({target} if path in ('partial', 'undo') else set())
            assert await listed(browser, 'INBOX') == ({ids['sibling']} if path in ('partial', 'undo') else set(ids.values()))
    asyncio.run(run())


@pytest.mark.parametrize("denial", ["scope diagnostic", "unchanged point read"])
def test_pinned_gmail_confirms_action_pauses_and_allows_explicit_retry(pg_db, monkeypatch, denial):
    async def run():
        provider = StudioMailbox()
        provider.labels["target"].add("UNREAD")
        ids = await provider.connect(monkeypatch, provider="gmail")
        target = ids["target"]
        path = f"{MAIL}/messages/{target}/actions"
        account_path = f"{MAIL}/accounts/{provider.account.id}"
        dispatch = provider.dispatch

        async def empty_receipt(path, payload):
            response = await dispatch(path, payload)
            if urlsplit(path).path.split("/")[-2] in {"label_message", "unlabel_message"} and provider.reject is None:
                return {"content": [{"type": "text", "text": "{}"}]}
            return response

        monkeypatch.setattr(connectors, "_post", empty_receipt)
        async with client() as browser:
            accounts_response = await browser.get(f"{MAIL}/accounts")
            row, = accounts_response.json()
            assert row["action_capabilities"]["mark_read"]
            assert row["action_capabilities"]["archive"]
            assert row["action_capabilities"]["trash"]
            assert not any(row["action_capabilities"][action] for action in ("spam", "move", "label"))

            result = await browser.post(path, json={"kind": "mark_read"})
            assert result.status_code == 200
            assert result.json()["message"]["is_read"]
            assert provider.labels["target"] == {"INBOX"}
            token = result.json()["action_id"]
            undone = await browser.post(f"{MAIL}/actions/undo", json={"action_ids": [token]})
            assert undone.json()["restored"] == [token]
            assert provider.labels["target"] == {"INBOX", "UNREAD"}

            provider.reject = {"content": [{"type": "text", "text":
                "Insufficient authentication scopes." if denial == "scope diagnostic" else "{}"}]}
            # A stale full response that appears to confirm the change must not override MINIMAL.
            provider.replay = denial == "unchanged point read"
            provider.replayed_target_labels = ["UNREAD"]
            refused = await browser.post(path, json={"kind": "archive"})
            if denial == "unchanged point read":
                assert refused.status_code == 502
                assert refused.json()["detail"]["code"] == "provider_failed"
                row, = (await browser.get(f"{MAIL}/accounts")).json()
                assert row["action_capabilities"]["archive"] and not row["provider_actions_blocked"]
                refused = await browser.post(path, json={"kind": "archive"})
            assert refused.status_code == 403
            assert refused.json()["detail"]["code"] == "provider_permission_missing"
            assert await mail_action.latest_open(target) is None
            assert target in await listed(browser, "INBOX")
            row, = (await browser.get(f"{MAIL}/accounts")).json()
            assert row["provider_actions_blocked"]
            assert not any(row["action_capabilities"].values())
            writes = len(provider.writes)
            assert (await browser.post(path, json={"kind": "mark_read"})).status_code == 403
            assert len(provider.writes) == writes

            blocked = await mail_sync_state.get_state(provider.account.id)
            proof = blocked.write_evidence()
            assert proof[2] == "blocked"
            provider.reject = None
            provider.replay = False
            retry = await browser.post(f"{account_path}/retry-provider-actions")
            assert retry.status_code == 200
            assert not retry.json()["provider_actions_blocked"]
            assert len(provider.writes) == writes
            await mail_sync_state.record_connector_write(provider.account.id, proof[:2], outcome="blocked")
            archived = await browser.post(path, json={"kind": "archive"})
            assert archived.status_code == 200
            assert provider.labels["target"] == {"UNREAD"}
            archive_token = archived.json()["action_id"]
            assert (await browser.post(f"{MAIL}/actions/undo", json={"action_ids": [archive_token]})).json()["restored"] == [archive_token]
            assert provider.labels["target"] == {"INBOX", "UNREAD"}

            trashed = await browser.post(path, json={"kind": "trash"})
            assert trashed.status_code == 200
            assert provider.labels["target"] == {"TRASH", "UNREAD"}
            assert target in await listed(browser, "TRASH")
            restored = await browser.post(path, json={"kind": "restore"})
            assert restored.status_code == 200
            assert provider.labels["target"] == {"INBOX", "UNREAD"}
            assert target in await listed(browser, "INBOX")
            assert provider.labels["sibling"] == {"INBOX"}
    asyncio.run(run())

@pytest.mark.parametrize("consumer", ["bulk", "chat", "rule", "rule with folder reconciliation"])
def test_gmail_action_that_pauses_reports_permission_to_its_caller(pg_db, monkeypatch, consumer):
    from api.mail import proposals, rules
    from db.accessors import chat_context, chat_settings, mail_rule

    async def run():
        provider = StudioMailbox()
        provider.labels["target"].add("UNREAD")
        ids = await provider.connect(monkeypatch, provider="gmail")
        target = ids["target"]
        provider.reject = {"content": [{"type": "text", "text": "{}"}]}

        async def no_delay(_seconds):
            pass

        monkeypatch.setattr(connectors, "sleep", no_delay)
        async with client() as browser:
            first = await browser.post(f"{MAIL}/messages/{target}/actions", json={"kind": "mark_read"})
            assert first.status_code == 502
            if consumer == "bulk":
                result = await browser.post(f"{MAIL}/messages/actions", json={"kind": "mark_read", "message_ids": [target]})
                assert result.status_code == 200
                row, = result.json()["results"]
                assert (row["status"], row["error_code"], row["action_id"]) == ("failed", "provider_permission_missing", None)
            elif consumer == "chat":
                await chat_settings.update(1, [provider.account.id])
                conversation, context, _ = await chat_context.resolve(
                    conversation_id=None, title="Synthetic", model=None, revision_id=None,
                    account_id=provider.account.id, app_prompt_version="test")
                card = await proposals.create_proposal(conversation.id, proposals.Proposal(
                    kind="mark_read", reason="Synthetic", items=[
                        proposals.ProposalItem(id=target, kind="mail", title="Synthetic", subtitle=""),
                    ]), context_id=context.id)
                result = proposals.proposal_view(await proposals.confirm(card.id, selected_ids=[target])).result
                assert result.done == 0
                assert [item.code for item in result.failed] == ["provider_permission_missing"]
                assert result.undo_action_ids == []
            else:
                if consumer == "rule with folder reconciliation":
                    provider.labels["target"].remove("INBOX")
                rule = await mail_rule.create(name="Read", mark_read=True)
                run = await rules.execute_rule_run(rule.id, rule.version, target)
                assert json.loads(run.provider_results_json) == {"mark_read": "skipped_permission"}
                if consumer == "rule with folder reconciliation":
                    assert (await mail_message.get_message(target)).folder == "ARCHIVE"
            state = await mail_sync_state.get_state(provider.account.id)
            assert state.write_evidence()[2] == "blocked"
            assert await mail_action.latest_open(target) is None
            assert provider.writes == [("unlabel_message", "target", ("UNREAD",))] * 2

    asyncio.run(run())


def test_confirmed_gmail_write_resets_unchanged_readback_streak(pg_db, monkeypatch):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch, provider="gmail")
        target = f"{MAIL}/messages/{ids['target']}/actions"
        sibling = f"{MAIL}/messages/{ids['sibling']}/actions"
        async with client() as browser:
            provider.reject = {"content": [{"type": "text", "text": "{}"}]}
            unchanged = await browser.post(target, json={"kind": "archive"})
            assert unchanged.status_code == 502
            assert unchanged.json()["detail"]["code"] == "provider_failed"
            assert await mail_action.latest_open(ids["target"]) is None

            provider.reject = None
            assert (await browser.post(sibling, json={"kind": "mark_unread"})).status_code == 200
            provider.reject = {"content": [{"type": "text", "text": "{}"}]}
            unchanged = await browser.post(target, json={"kind": "archive"})
            assert unchanged.status_code == 502
            account, = (await browser.get(f"{MAIL}/accounts")).json()
            assert account["action_capabilities"]["archive"] and not account["provider_actions_blocked"]
            assert await listed(browser, "INBOX") == set(ids.values())

    asyncio.run(run())


@pytest.mark.parametrize("intervening", ["already read", "already inbox restore step", "positive receipt"])
def test_gmail_unchanged_streak_needs_receipt_or_requested_label_transition(pg_db, monkeypatch, intervening):
    async def run():
        provider = StudioMailbox()
        provider.labels["target"].add("UNREAD")
        ids = await provider.connect(monkeypatch, provider="gmail")

        async def no_delay(_seconds):
            pass

        monkeypatch.setattr(connectors, "sleep", no_delay)
        empty_receipt = {"content": [{"type": "text", "text": "{}"}]}
        target = f"{MAIL}/messages/{ids['target']}/actions"
        sibling = f"{MAIL}/messages/{ids['sibling']}/actions"
        provider.reject = empty_receipt
        async with client() as browser:
            unchanged = await browser.post(target, json={"kind": "mark_read"})
            assert unchanged.status_code == 502
            assert unchanged.json()["detail"]["code"] == "provider_failed"
            state = await mail_sync_state.get_state(provider.account.id)
            assert state.write_evidence()[2] == "unchanged"

            if intervening == "already inbox restore step":
                provider.labels["sibling"].add("TRASH")
                await mail_message.set_state(ids["sibling"], folder="TRASH", labels_json='["INBOX", "TRASH"]')
                move = await browser.post(sibling, json={"kind": "restore"})
                assert move.status_code == 403
                assert move.json()["detail"]["code"] == "provider_permission_missing"
                assert provider.writes[-2:] == [
                    ("label_message", "sibling", ("INBOX",)), ("unlabel_message", "sibling", ("TRASH",))]
            else:
                if intervening == "positive receipt":
                    provider.reject = None
                satisfied = await browser.post(sibling, json={"kind": "mark_read"})
                assert satisfied.status_code == 200
                assert provider.labels["sibling"] == {"INBOX"}
                provider.reject = empty_receipt
                unchanged = await browser.post(target, json={"kind": "mark_read"})
                assert unchanged.status_code == (502 if intervening == "positive receipt" else 403)

            state = await mail_sync_state.get_state(provider.account.id)
            assert state.write_evidence()[2] == ("unchanged" if intervening == "positive receipt" else "blocked")
            assert provider.labels["target"] == {"INBOX", "UNREAD"}
            assert await mail_action.latest_open(ids["target"]) is None

    asyncio.run(run())


def test_gmail_commits_confirmed_action_after_pin_is_replaced(pg_db, monkeypatch):
    async def run():
        provider = StudioMailbox()
        provider.labels["target"].add("UNREAD")
        ids = await provider.connect(monkeypatch, provider="gmail")
        initial = await mail_sync_state.get_state(provider.account.id)
        replacement_cursor = connector_mailbox.encode(("replacement-pin", "anchor", "proof"), "")
        original = provider.dispatch

        async def dispatch(path, payload):
            result = await original(path, payload)
            if payload["arguments"].get("threadId") == "mixed" and "UNREAD" not in provider.labels["target"]:
                await mail_sync_state.save_state(account_id=provider.account.id, connector_cursor=replacement_cursor)
            return result

        monkeypatch.setattr(connectors, "_post", dispatch)
        async with client() as browser:
            result = await browser.post(f"{MAIL}/messages/{ids['target']}/actions", json={"kind": "mark_read"})
            assert result.status_code == 200
            assert result.json()["message"]["is_read"]
            assert result.json()["action_id"]
            assert (await mail_message.get_message(ids["target"])).is_read
            assert provider.writes == [("unlabel_message", "target", ("UNREAD",))]
            latest = await mail_sync_state.get_state(provider.account.id)
            assert latest.connector_cursor == replacement_cursor
            assert latest.connector_write_evidence == initial.connector_write_evidence
            assert latest.write_evidence()[2] == "ready"

    asyncio.run(run())


def test_gmail_point_readback_can_confirm_after_three_seconds(pg_db, monkeypatch):
    async def run():
        provider = StudioMailbox()
        provider.labels["target"].add("UNREAD")
        ids = await provider.connect(monkeypatch, provider="gmail")
        sleeps = []

        async def delay(seconds):
            sleeps.append(seconds)

        original = provider.dispatch

        async def dispatch(path, payload):
            if payload["arguments"].get("threadId") == "mixed" and sum(sleeps) >= 3.0:
                provider.labels["target"].discard("UNREAD")
            return await original(path, payload)

        provider.reject = {"content": [{"type": "text", "text": "{}"}]}
        monkeypatch.setattr(connectors, "_post", dispatch)
        monkeypatch.setattr(connectors, "sleep", delay)
        async with client() as browser:
            result = await browser.post(f"{MAIL}/messages/{ids['target']}/actions", json={"kind": "mark_read"})
            assert result.status_code == 200
            assert result.json()["message"]["is_read"]
            assert sleeps == [1.0, 2.0]
            assert len(provider.writes) == 1
            state = await mail_sync_state.get_state(provider.account.id)
            assert state.write_evidence()[2] == "confirmed"

    asyncio.run(run())


@pytest.mark.parametrize(("kind", "reads", "writes"), [("mark_read", 3, 1), ("archive", 3, 1), ("trash", 5, 2)])
def test_gmail_normal_action_checks_anchor_once_per_write(pg_db, monkeypatch, kind, reads, writes):
    async def run():
        provider = StudioMailbox()
        provider.labels["target"].add("UNREAD")
        ids = await provider.connect(monkeypatch, provider="gmail")
        calls = []
        original = provider.dispatch

        async def dispatch(path, payload):
            calls.append((urlsplit(path).path.split("/")[-2], payload["arguments"].get("threadId")))
            return await original(path, payload)

        monkeypatch.setattr(connectors, "_post", dispatch)
        async with client() as browser:
            result = await browser.post(f"{MAIL}/messages/{ids['target']}/actions", json={"kind": kind})
            assert result.status_code == 200
            assert sum(tool == "get_thread" for tool, _ in calls) == reads
            assert calls.count(("get_thread", "anchor")) == writes
            assert len(calls) == reads + writes
            assert len(provider.writes) == writes

    asyncio.run(run())


def test_gmail_denial_in_mixed_bulk_keeps_other_accounts_and_blocks_only_its_binding(pg_db, monkeypatch):
    from api.mail.seed_ids import seed_id
    from db.models.mail_account import MailAccount

    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch, provider="gmail")
        other = seed_id("account", "privat")
        async with get_session_maker().begin() as session:
            session.add(MailAccount(id=other, provider="google", auth_type="oauth2", email_address="other@example.test"))
        control = await mail_message.upsert_message(account_id=other, folder="INBOX", provider_uid="control")
        provider.reject = {"content": [{"type": "text", "text": "Permission denied."}]}
        async with client() as browser:
            result = await browser.post(f"{MAIL}/messages/actions", json={"kind": "trash",
                                         "message_ids": [ids["target"], control.id]})
            assert result.status_code == 200
            denied, done = result.json()["results"]
            assert (denied["status"], denied["error_code"], denied["action_id"]) == ("failed", "provider_permission_missing", None)
            assert done["status"] == "done" and done["action_id"]
            assert provider.labels["target"] == {"INBOX"}
            inbox = await listed(browser, "INBOX")
            assert ids["target"] in inbox and control.id not in inbox
            assert (await browser.post(f"{MAIL}/actions/undo", json={"action_ids": [done["action_id"]]})).json()["restored"] == [done["action_id"]]
            assert control.id in await listed(browser, "INBOX")
            assert (await browser.post(f"{MAIL}/messages/{ids['target']}/actions", json={"kind": "trash"})).status_code == 403
            assert len(provider.writes) == 1

            # Another pin never inherits a denial or a late result from this one.
            before = await mail_sync_state.get_state(provider.account.id)
            await mail_sync_state.save_state(account_id=provider.account.id, connector_verified=True,
                connector_cursor=connector_mailbox.encode(("another-synthetic-pin", "anchor", "proof"), ""))
            await mail_sync_state.record_connector_write(provider.account.id, before.write_evidence()[:2], outcome="blocked")
            status = await browser.get(f"{MAIL}/accounts")
            gmail = next(row for row in status.json() if row["provider"] == "gmail")
            assert gmail["action_capabilities"]["archive"] and not gmail["provider_actions_blocked"]

    asyncio.run(run())


@pytest.mark.parametrize("blocked_reason", ["paused", "unverified"])
def test_mixed_bulk_undo_restores_other_account_when_gmail_binding_is_unavailable(pg_db, monkeypatch, blocked_reason):
    from api.mail.seed_ids import seed_id
    from db.accessors import chat_settings
    from db.models.mail_account import MailAccount

    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch, provider="gmail")
        target = ids["target"]
        other = seed_id("account", "privat")
        async with get_session_maker().begin() as session:
            session.add(MailAccount(id=other, provider="google", auth_type="oauth2", email_address="other@example.test"))
        control = await mail_message.upsert_message(account_id=other, folder="INBOX", provider_uid="control")

        async with client() as browser:
            bulk = await browser.post(f"{MAIL}/messages/actions", json={"kind": "trash", "message_ids": [target, control.id]})
            assert bulk.status_code == 200
            gmail, other_result = bulk.json()["results"]
            assert gmail["status"] == other_result["status"] == "done"
            gmail_token, other_token = gmail["action_id"], other_result["action_id"]
            assert gmail_token and other_token
            assert await listed(browser, "TRASH") == {target, control.id}

            if blocked_reason == "paused":
                provider.reject = {"content": [{"type": "text", "text": "Insufficient authentication scopes."}]}
                denied = await browser.post(f"{MAIL}/messages/{ids['sibling']}/actions", json={"kind": "trash"})
                assert denied.status_code == 403
                assert denied.json()["detail"]["code"] == "provider_permission_missing"
            else:
                await chat_settings.invalidate_connector(provider.account.id)
                assert not (await mail_sync_state.get_state(provider.account.id)).connector_verified
            writes = list(provider.writes)

            undo = await browser.post(f"{MAIL}/actions/undo", json={"action_ids": [gmail_token, other_token]})
            assert undo.status_code == 200
            assert undo.json()["restored"] == [other_token]
            assert undo.json()["failed"] == [gmail_token]
            assert undo.json().get("error_code") == ("provider_permission_missing" if blocked_reason == "paused" else None)
            assert provider.writes == writes
            assert provider.labels["target"] == {"TRASH"}
            assert (await mail_message.get_message(control.id)).folder == "INBOX"
            assert (await mail_message.get_message(target)).folder == "TRASH"
            assert (await mail_action.latest_open(target)).id == gmail_token
            if blocked_reason == "unverified":
                return

            provider.reject = None
            retry = await browser.post(f"{MAIL}/accounts/{provider.account.id}/retry-provider-actions")
            assert retry.status_code == 200
            undo = await browser.post(f"{MAIL}/actions/undo", json={"action_ids": [gmail_token, other_token]})
            assert undo.status_code == 200
            assert undo.json()["restored"] == [gmail_token]
            assert undo.json()["skipped"] == [other_token]
            assert provider.labels["target"] == {"INBOX"}
            assert await listed(browser, "TRASH") == set()

    asyncio.run(run())


@pytest.mark.parametrize("saved_is_read", [False, True], ids=["restore-unread", "restore-read"])
def test_gmail_undo_uses_live_read_flag_and_releases_an_unconfirmed_token(pg_db, monkeypatch, saved_is_read):
    async def run():
        provider = StudioMailbox()
        if not saved_is_read:
            provider.labels["target"].add("UNREAD")
        ids = await provider.connect(monkeypatch, provider="gmail")
        target = ids["target"]

        async def no_delay(_seconds):
            pass

        monkeypatch.setattr(connectors, "sleep", no_delay)
        async with client() as browser:
            action = "mark_unread" if saved_is_read else "mark_read"
            changed = await browser.post(f"{MAIL}/messages/{target}/actions", json={"kind": action})
            assert changed.status_code == 200
            token = changed.json()["action_id"]
            changed_labels = set(provider.labels["target"])
            stale_labels = ["INBOX"] if saved_is_read else ["INBOX", "UNREAD"]
            await mail_message.set_state(target, is_read=saved_is_read, labels_json=json.dumps(stale_labels))

            provider.reject = {"content": [{"type": "text", "text": "{}"}]}
            undo = await browser.post(f"{MAIL}/actions/undo", json={"action_ids": [token]})
            assert undo.status_code == 200
            assert undo.json()["restored"] == []
            assert undo.json()["failed"] == [token]
            assert (await mail_action.latest_open(target)).id == token
            assert provider.labels["target"] == changed_labels

            provider.reject = None
            undo = await browser.post(f"{MAIL}/actions/undo", json={"action_ids": [token]})
            assert undo.json()["restored"] == [token]
            tool = "unlabel_message" if saved_is_read else "label_message"
            assert provider.writes[-2:] == [(tool, "target", ("UNREAD",))] * 2
            assert provider.labels["target"] == set(stale_labels)
            assert (await mail_message.get_message(target)).is_read == saved_is_read

    asyncio.run(run())


@pytest.mark.parametrize("live_folder", ["SPAM", "TRASH"])
def test_gmail_trash_reconciles_a_disallowed_live_source_without_writing(pg_db, monkeypatch, live_folder):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch, provider="gmail")
        target = ids["target"]
        labels = {live_folder, "UNREAD"}
        provider.labels["target"] = set(labels)
        async with client() as browser:
            result = await browser.post(f"{MAIL}/messages/{target}/actions", json={"kind": "trash"})
            assert result.status_code == 409
            assert result.json()["detail"]["code"] == "provider_partial"
            assert result.json()["detail"]["folder"] == live_folder
            assert not provider.writes
            assert provider.labels["target"] == labels
            actual = await mail_message.get_message(target)
            assert actual.folder == live_folder
            assert set(json.loads(actual.labels_json)) == labels
            assert await listed(browser, "INBOX") == {ids["sibling"]}
            assert await mail_action.latest_open(target) is None

    asyncio.run(run())


def test_gmail_archive_reconciles_a_conflicting_trash_destination_without_undo(pg_db, monkeypatch):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch, provider="gmail")
        provider.labels["target"] = {"TRASH", "UNREAD"}
        provider.reject = {"content": [{"type": "text", "text": "{}"}]}
        async with client() as browser:
            target = ids["target"]
            result = await browser.post(f"{MAIL}/messages/{target}/actions", json={"kind": "archive"})
            assert result.status_code == 409
            assert result.json()["detail"]["code"] == "provider_partial"
            assert result.json()["detail"]["folder"] == "TRASH"
            assert await listed(browser, "TRASH") == {target}
            assert await mail_action.latest_open(target) is None
            state = await mail_sync_state.get_state(provider.account.id)
            assert state.write_evidence()[2] == "ready"
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "http-500"])
def test_gmail_trash_reads_current_state_after_the_second_write_fails(pg_db, monkeypatch, failure):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch, provider="gmail")
        original = provider.dispatch

        async def dispatch(path, payload):
            tool = urlsplit(path).path.split("/")[-2]
            if tool == "unlabel_message":
                provider.writes.append((tool, "target", ("INBOX",)))
                provider.labels["target"].add("STARRED")
                request = httpx.Request("POST", "https://connector.example.test")
                if failure == "timeout":
                    raise httpx.ReadTimeout("synthetic timeout", request=request)
                raise httpx.HTTPStatusError("synthetic error", request=request,
                    response=httpx.Response(500, request=request))
            result = await original(path, payload)
            if tool == "label_message":
                provider.labels["target"].discard("INBOX")
            return result

        monkeypatch.setattr(connectors, "_post", dispatch)
        async with client() as browser:
            target = ids["target"]
            result = await browser.post(f"{MAIL}/messages/{target}/actions", json={"kind": "trash"})
            assert result.status_code == 409
            assert result.json()["detail"]["folder"] == "TRASH"
            assert result.json()["detail"]["code"] == "provider_partial"
            assert await listed(browser, "TRASH") == {target}
            assert (await mail_message.get_message(target)).is_flagged
            assert await mail_action.latest_open(target) is None
            assert [(tool, labels) for tool, _, labels in provider.writes] == [
                ("label_message", ("TRASH",)), ("unlabel_message", ("INBOX",))]
    asyncio.run(run())


def test_gmail_rechecks_the_live_binding_before_the_second_trash_write(pg_db, monkeypatch):
    async def run():
        provider = StudioMailbox()
        ids = await provider.connect(monkeypatch, provider="gmail")
        original = provider.dispatch
        replacement = False

        async def dispatch(path, payload):
            nonlocal replacement
            tool = urlsplit(path).path.split("/")[-2]
            if tool == "get_thread" and payload["arguments"].get("threadId") == "anchor" and replacement:
                return {"content": [{"type": "text", "text": json.dumps({"id": "anchor", "messages": []})}]}
            result = await original(path, payload)
            if tool == "get_thread" and payload["arguments"].get("threadId") == "mixed" and "TRASH" in provider.labels["target"]:
                replacement = True
            return result

        monkeypatch.setattr(connectors, "_post", dispatch)
        async with client() as browser:
            target = ids["target"]
            result = await browser.post(f"{MAIL}/messages/{target}/actions", json={"kind": "trash"})
            assert replacement
            assert provider.writes == [("label_message", "target", ("TRASH",))]
            assert result.status_code == 502
            assert not (await mail_sync_state.get_state(provider.account.id)).connector_verified
            assert await mail_action.latest_open(target) is None
    asyncio.run(run())


def test_gmail_trash_undo_restores_the_observed_archive_source(pg_db, monkeypatch):
    async def run():
        provider = StudioMailbox()
        provider.labels["target"].add("UNREAD")
        ids = await provider.connect(monkeypatch, provider="gmail")
        provider.labels["target"].discard("INBOX")
        async with client() as browser:
            target = ids["target"]
            result = await browser.post(f"{MAIL}/messages/{target}/actions", json={"kind": "trash"})
            assert result.status_code == 200
            token = result.json()["action_id"]
            record, = await mail_action.get_many([token])
            prior = json.loads(record.previous_json)
            assert prior["folder"] == "ARCHIVE"
            assert json.loads(prior["labels_json"]) == ["UNREAD"]
            assert not prior["is_read"]
            undo = await browser.post(f"{MAIL}/actions/undo", json={"action_ids": [token]})
            assert undo.json()["restored"] == [token]
            assert provider.labels["target"] == {"UNREAD"}
            assert await listed(browser, "ARCHIVE") == {target}
            assert await listed(browser, "INBOX") == {ids["sibling"]}
    asyncio.run(run())
