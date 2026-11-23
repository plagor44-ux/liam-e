"""Provider wire contracts use invented message identifiers only."""

import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from db.models.mail_account import MailAccount
from db.models.mail_message import MailMessage

from api.mail import action_backends as backends, connectors, service, transport
from api.mail.actions import ActionUnsupportedError


def account(provider="imap", auth_type="imap_password"):
    return MailAccount(id="a", provider=provider, auth_type=auth_type, email_address="ada@example.test")


def message(**kwargs):
    return MailMessage(id="m", account_id="a", provider_uid="12", imap_uidvalidity="7", rfc_message_id="<invented@example.test>", **kwargs)


@pytest.fixture
def imap(monkeypatch):
    connection = Mock()
    connection.capabilities = (b"IMAP4rev1", b"MOVE", b"UIDPLUS")
    connection.login.return_value = ("OK", [])
    connection.select.return_value = ("OK", [])
    connection.list.return_value = ("OK", [b'(\\HasNoChildren \\Archive) "/" "Archiv"', b'(\\Trash) "/" "Bin"'])
    connection.uid.side_effect = lambda command, *args: ("OK", [b"91"] if command.upper() == "SEARCH" else [])
    connection.response.side_effect = lambda name: (name, [b"1 12 91"] if name == "COPYUID" else [b"7"])
    monkeypatch.setattr(transport, "open_imap", Mock(return_value=connection))
    monkeypatch.setattr(transport, "check_mail_host", AsyncMock())
    monkeypatch.setattr(service, "load_credentials", AsyncMock(return_value={"imap_host": "imap.example.test"}))
    monkeypatch.setattr(service, "imap_password", AsyncMock(return_value=""))
    return connection


@pytest.mark.anyio
async def test_imap_restore_keeps_inbox_uid_policy(imap, fake_store):
    from api.mail import actions

    fake_store.accounts["a"] = account()
    fake_store.messages["m"] = message()
    await actions.apply_action("m", "archive")
    assert fake_store.messages["m"].provider_uid == "12"
    await actions.apply_action("m", "restore")
    assert fake_store.messages["m"].provider_uid == "91"
    assert fake_store.messages["m"].folder == "INBOX"


@pytest.mark.anyio
@pytest.mark.parametrize("failed_command", ["STORE", "MOVE"])
async def test_imap_revert_retry_uses_source_message_id(imap, failed_command):
    imap.response.side_effect = lambda name: (name, [b"1 91 91"] if name == "COPYUID" else [b"7"])
    calls = []
    failed = False
    def execute(command, *args):
        nonlocal failed
        calls.append(command)
        if command == failed_command and not failed:
            failed = True
            return "NO", []
        return "OK", [b"91"] if command == "SEARCH" else []
    imap.uid.side_effect = execute
    backend = backends.backend_for(account())
    row = message(folder="ARCHIVE")
    previous = {"folder": "INBOX", "is_read": False}
    with pytest.raises(RuntimeError):
        await backend.revert(row, previous)
    calls.clear()
    assert (await backend.revert(row, previous))["provider_uid"] == "91"
    assert calls == ["SEARCH", "STORE", "MOVE"]


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["microsoft", "google"])
async def test_http_retry_after_partial_failure(monkeypatch, provider):
    calls = []
    failed = False
    def handle(request):
        nonlocal failed
        calls.append((request.method, request.url.path))
        fail_step = request.method == "PATCH" if provider == "microsoft" else request.url.path.endswith("/modify")
        if fail_step and not failed:
            failed = True
            return httpx.Response(503)
        return httpx.Response(200, json={"id": "new-id"})
    client = httpx.AsyncClient
    monkeypatch.setattr(backends.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    monkeypatch.setattr(backends, "_headers", AsyncMock(return_value={}))
    backend = backends.backend_for(account(provider, "oauth2"))
    row = message(folder="TRASH", labels_json='["TRASH"]')
    previous = {"folder": "INBOX", "is_read": False, "labels_json": "[]"}
    with pytest.raises(httpx.HTTPStatusError):
        await backend.revert(row, previous)
    assert not any(path.endswith("/move") for _, path in calls)
    calls.clear()
    await backend.revert(row, previous)
    if provider == "microsoft":
        assert calls[0][0] == "PATCH" and calls[0][1].endswith("/12")
        assert calls[1][1].endswith("/12/move")
    else:
        assert [path.rsplit("/", 1)[1] for _, path in calls] == ["untrash", "modify"]


@pytest.mark.anyio
async def test_gmail_trash_has_no_followup_modify(http):
    await backends.backend_for(account("google", "oauth2")).apply(message(), "trash")
    assert len(http) == 1 and http[0][1].endswith("/trash")


@pytest.mark.anyio
async def test_imap_backend_prefers_special_use_folder(imap):
    result = await backends.backend_for(account()).apply(message(), "archive")
    imap.list.assert_called_once_with('""', '"*"')
    assert any(call.args == ("MOVE", "12", '"Archiv"') for call in imap.uid.call_args_list)
    assert result["provider_uid"] == "12"


@pytest.mark.anyio
async def test_imap_backend_falls_back_to_copy_and_delete_without_move(imap):
    imap.capabilities = (b"IMAP4rev1", b"UIDPLUS")
    await backends.backend_for(account()).apply(message(), "archive")
    assert [call.args for call in imap.uid.call_args_list] == [
        ("COPY", "12", '"Archiv"'), ("STORE", "12", "+FLAGS", "(\\Deleted)"), ("EXPUNGE", "12")]


@pytest.mark.anyio
async def test_imap_undo_finds_message_by_message_id(imap):
    previous = {"folder": "INBOX", "is_read": False, "provider_uid": "12", "labels_json": "[]"}
    result = await backends.backend_for(account()).revert(message(folder="ARCHIVE"), previous)
    imap.uid.assert_any_call("SEARCH", None, "HEADER", "Message-ID", '"<invented@example.test>"')
    imap.uid.assert_any_call("MOVE", "91", '"INBOX"')
    assert result["provider_uid"] == "91"


@pytest.mark.anyio
async def test_imap_undo_read_state_in_inbox_uses_stored_uid(imap):
    backend = backends.backend_for(account())
    # The index row is still in INBOX, so its stored UID is the real INBOX UID.
    row = message(folder="INBOX")
    row.rfc_message_id = ""
    await backend.apply(row, "mark_read")
    imap.uid.reset_mock()
    await backend.revert(row, {"folder": "INBOX", "is_read": False})
    assert not any(call.args[0] == "SEARCH" for call in imap.uid.call_args_list)
    imap.uid.assert_called_once_with("STORE", "12", "-FLAGS", "(\\Seen)")


@pytest.mark.anyio
async def test_imap_restore_searches_inbox_when_copyuid_missing(imap):
    imap.response.side_effect = lambda name: (name, [None] if name == "COPYUID" else [b"7"])
    result = await backends.backend_for(account()).apply(message(folder="ARCHIVE"), "restore")
    imap.select.assert_any_call('"INBOX"')
    assert result["provider_uid"] == "91"


@pytest.mark.anyio
async def test_imap_outside_inbox_never_uses_stored_uid(imap):
    row = message(folder="ARCHIVE")
    row.rfc_message_id = ""
    with pytest.raises(ActionUnsupportedError):
        await backends.backend_for(account()).apply(row, "mark_read")
    imap.uid.assert_not_called()


@pytest.mark.anyio
async def test_imap_read_and_folder_fallback(imap):
    imap.list.return_value = ("OK", [b'() "/" "Archive"'])
    backend = backends.backend_for(account())
    await backend.apply(message(), "mark_read")
    imap.uid.assert_any_call("STORE", "12", "+FLAGS", "(\\Seen)")
    await backend.apply(message(), "archive")
    imap.uid.assert_any_call("MOVE", "12", '"Archive"')


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["folder", "uidplus", "message_id"])
async def test_imap_unsupported_move_does_not_mutate(imap, reason):
    row = message()
    if reason == "folder":
        imap.list.return_value = ("OK", [])
    elif reason == "uidplus":
        imap.capabilities = (b"IMAP4rev1",)
    else:
        row.rfc_message_id = ""
    with pytest.raises(ActionUnsupportedError):
        await backends.backend_for(account()).apply(row, "archive")
    imap.uid.assert_not_called()
    imap.logout.assert_called_once()


@pytest.mark.anyio
async def test_imap_rejects_failed_command_and_checks_host_before_connect(imap, monkeypatch):
    imap.uid.side_effect = None
    imap.uid.return_value = ("NO", [b"invented diagnostic"])
    with pytest.raises(RuntimeError):
        await backends.backend_for(account()).apply(message(), "mark_read")
    transport.check_mail_host.assert_awaited_once_with("imap.example.test")
    imap.logout.assert_called_once()
    transport.open_imap.reset_mock()
    monkeypatch.setattr(transport, "check_mail_host", AsyncMock(side_effect=RuntimeError()))
    with pytest.raises(RuntimeError):
        await backends.backend_for(account()).apply(message(), "archive")
    transport.open_imap.assert_not_called()


@pytest.fixture
def http(monkeypatch):
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"id": "new-id"})
    client = httpx.AsyncClient
    monkeypatch.setattr(backends.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    monkeypatch.setattr(service, "load_credentials", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "valid_access_token", AsyncMock(return_value=""))
    return calls


@pytest.mark.anyio
async def test_graph_move_updates_provider_uid(http):
    result = await backends.backend_for(account("microsoft", "oauth2")).apply(message(), "archive")
    assert result == {"provider_uid": "new-id"}
    assert http[-1][0] == "POST" and http[-1][1].endswith("/me/messages/12/move")
    assert http[-1][2] == {"destinationId": "archive"}


@pytest.mark.anyio
async def test_gmail_archive_and_restore_labels(http):
    backend = backends.backend_for(account("google", "oauth2"))
    result = await backend.apply(message(labels_json='["INBOX", "UNREAD"]'), "archive")
    assert json.loads(result["labels_json"]) == ["UNREAD"]
    assert http[-1][2]["removeLabelIds"] == ["INBOX"]
    await backend.apply(message(folder="TRASH", labels_json='["TRASH"]'), "restore")
    assert any(path.endswith("/untrash") for _, path, _ in http)
    assert http[-1][2]["addLabelIds"] == ["INBOX", "UNREAD"]


@pytest.mark.anyio
async def test_graph_revert_uses_current_id_and_preserves_it_for_read_only_undo(http):
    backend = backends.backend_for(account("microsoft", "oauth2"))
    previous = {"folder": "INBOX", "is_read": False, "provider_uid": "obsolete", "labels_json": "[]"}
    result = await backend.revert(message(), previous)
    assert result["provider_uid"] == "12"
    assert http[-1][0] == "PATCH" and http[-1][1].endswith("/12")
    assert http[-1][2] == {"isRead": False}
    result = await backend.revert(message(folder="TRASH"), previous)
    assert result["provider_uid"] == "new-id"
    assert http[-2][0] == "PATCH" and http[-2][1].endswith("/12")
    assert http[-1][2] == {"destinationId": "inbox"}


@pytest.mark.anyio
async def test_connector_retry_repeats_idempotent_labels(monkeypatch):
    from contextlib import nullcontext
    from api.mail import connector_mailbox
    monkeypatch.setattr(connector_mailbox, "bound_mailbox", lambda _: nullcontext())
    call = AsyncMock(side_effect=["{}", RuntimeError(), "{}", "{}"])
    monkeypatch.setattr(connectors, "_call_tool", call)
    monkeypatch.setattr(connectors, "thread_labels", AsyncMock(return_value={"12": ["TRASH"]}))
    backend = backends.backend_for(account("outlook", connectors.CONNECTOR))
    row = message(thread_id="thread", labels_json='["INBOX"]')
    with pytest.raises(RuntimeError):
        await backend.apply(row, "trash")
    await backend.apply(row, "trash")
    assert call.await_args_list[:2] == call.await_args_list[2:]


@pytest.mark.anyio
async def test_spam_gmail_and_connector_revert(http, monkeypatch):
    call = AsyncMock(return_value="{}")
    monkeypatch.setattr(connectors, "call_write_tool", call)
    previous = {"folder": "INBOX", "is_read": True, "labels_json": '["SPAM", "custom"]'}
    for connector in (False, True):
        backend = backends.LabelBackend(account("gmail" if connector else "google", "oauth2"), connector=connector)
        result = await backend.apply(message(is_read=True, labels_json='["custom"]'), "spam")
        assert json.loads(result["labels_json"]) == ["SPAM", "custom"]
        await backend.revert(message(folder="SPAM", is_read=True, labels_json='["custom"]'), previous)
        if connector:
            assert [c.args[1:] for c in call.await_args_list] == [
                ("label_message", {"messageId": "12", "labelIds": ["SPAM"]}),
                ("unlabel_message", {"messageId": "12", "labelIds": ["INBOX"]}),
                ("label_message", {"messageId": "12", "labelIds": ["INBOX"]}),
                ("unlabel_message", {"messageId": "12", "labelIds": ["SPAM"]}),
            ]
        else:
            assert [c[2] for c in http] == [
                {"addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]},
                {"addLabelIds": ["INBOX"], "removeLabelIds": ["SPAM"]},
            ]


@pytest.mark.anyio
async def test_spam_gmail_from_archive_keeps_unread_and_never_adds_inbox(http):
    backend = backends.LabelBackend(account("google", "oauth2"))
    labels = '["UNREAD", "custom"]'
    result = await backend.apply(message(folder="ARCHIVE", is_read=False, labels_json=labels), "spam")
    assert json.loads(result["labels_json"]) == ["SPAM", "UNREAD", "custom"]
    previous = {"folder": "ARCHIVE", "is_read": False, "labels_json": labels}
    await backend.revert(message(folder="SPAM", is_read=False, labels_json=result["labels_json"]), previous)
    assert [c[2] for c in http] == [
        {"addLabelIds": ["SPAM"], "removeLabelIds": []},
        {"addLabelIds": [], "removeLabelIds": ["SPAM"]},
    ]


@pytest.mark.anyio
async def test_graph_spam_and_revert(http):
    backend = backends.backend_for(account("microsoft", "oauth2"))
    assert await backend.apply(message(), "spam") == {"provider_uid": "new-id"}
    row = message(folder="SPAM")
    row.provider_uid = "new-id"
    assert await backend.revert(row, {"folder": "INBOX", "is_read": False}) == {"provider_uid": "new-id"}
    assert http[0][2] == {"destinationId": "junkemail"}
    assert http[-1][1].endswith("/new-id/move") and http[-1][2] == {"destinationId": "inbox"}


@pytest.mark.anyio
@pytest.mark.parametrize("listing,capability,target", [
    ([b'(\\Junk) "/" "Special"'], b"MOVE", "Special"),
    ([b'() "/" "Junk E-Mail"'], b"UIDPLUS", "Junk E-Mail"),
])
async def test_imap_spam_and_revert(imap, listing, capability, target):
    imap.list.return_value = ("OK", listing)
    imap.capabilities = (capability,)
    backend = backends.backend_for(account())
    await backend.apply(message(), "spam")
    command = "MOVE" if capability == b"MOVE" else "COPY"
    imap.uid.assert_any_call(command, "12", f'"{target}"')
    if command == "COPY":
        imap.uid.assert_any_call("EXPUNGE", "12")
    imap.uid.reset_mock()
    result = await backend.revert(message(folder="SPAM"), {"folder": "INBOX", "is_read": False})
    imap.uid.assert_any_call(command, "91", '"INBOX"')
    assert result["provider_uid"] == "91"


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["folder", "capability", "message_id"])
async def test_imap_spam_unsupported(imap, reason):
    imap.list.return_value = ("OK", [] if reason == "folder" else [b'(\\Junk) "/" "Junk"'])
    if reason == "capability":
        imap.capabilities = ()
    row = message()
    if reason == "message_id":
        row.rfc_message_id = ""
    with pytest.raises(ActionUnsupportedError):
        await backends.backend_for(account()).apply(row, "spam")
    imap.uid.assert_not_called()


@pytest.mark.anyio
async def test_connector_trash_uses_label_or_reports_unsupported(monkeypatch):
    from contextlib import nullcontext
    from api.mail import connector_mailbox
    monkeypatch.setattr(connector_mailbox, "bound_mailbox", lambda _: nullcontext())
    call = AsyncMock(return_value="{}")
    monkeypatch.setattr(connectors, "_call_tool", call)
    monkeypatch.setattr(connectors, "thread_labels", AsyncMock(return_value={"12": ["TRASH"]}))
    sender = account("outlook", connectors.CONNECTOR)
    result = await backends.backend_for(sender).apply(message(thread_id="thread", labels_json='["INBOX", "TRASH"]'), "trash")
    call.assert_any_await(sender.provider, "label_message", {"messageId": "12", "labelIds": ["TRASH"]})
    call.assert_any_await(sender.provider, "unlabel_message", {"messageId": "12", "labelIds": ["INBOX"]})
    assert json.loads(result["labels_json"]) == ["TRASH"]


@pytest.mark.anyio
async def test_imap_trash_keeps_destination_uid_and_reverts_exact_copy(imap):
    imap.list.return_value = ("OK", [b'(\\Trash) "/" "Deleted"'])
    imap.response.side_effect = lambda name: (name, [b"7 12 91"] if name == "COPYUID" else [b"7"])
    def command(name, *args):
        if name == "SEARCH":
            return ("OK", [args[2].encode()] if args[1] == "UID" else [b"90 91"])
        return "OK", []
    imap.uid.side_effect = command
    backend = backends.backend_for(account())
    moved = await backend.apply(message(), "trash")
    assert moved["provider_uid"] == "7:91"
    imap.response.side_effect = lambda name: (name, [b"9 91 50"] if name == "COPYUID" else [b"7"])
    row = message(folder="TRASH")
    row.provider_uid = moved["provider_uid"]
    assert (await backend.revert(row, {"folder": "INBOX", "is_read": False}))["provider_uid"] == "50"
    imap.uid.assert_any_call("MOVE", "91", '"INBOX"')


@pytest.mark.anyio
async def test_imap_missing_source_uid_cannot_report_a_trash_move(imap):
    imap.list.return_value = ("OK", [b'(\\Trash) "/" "Deleted"'])
    imap.uid.side_effect = lambda *_: ("OK", [b""])
    imap.response.side_effect = lambda name: (name, [None] if name == "COPYUID" else [b"7"])
    with pytest.raises(RuntimeError):
        await backends.backend_for(account()).apply(message(), "trash")
    assert not any(call.args[0] in {"MOVE", "COPY", "STORE", "EXPUNGE"} for call in imap.uid.call_args_list)


@pytest.mark.anyio
async def test_imap_undo_refuses_a_reused_qualified_trash_uid(imap):
    original = message(folder="TRASH")
    original.provider_uid = "6:91"
    with pytest.raises(RuntimeError, match="UIDVALIDITY changed"):
        await backends.backend_for(account()).revert(original, {"folder": "INBOX", "is_read": False})
    imap.uid.assert_not_called()
