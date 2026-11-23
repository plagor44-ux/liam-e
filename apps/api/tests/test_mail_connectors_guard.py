"""Connector capability boundaries."""

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.mail import connectors
from api.routers.api.v1.mail import connectors as routes


def test_call_tool_route_is_gone():
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/mail/connectors")
    assert TestClient(app).post("/api/v1/mail/connectors/gmail/tools/send_email", json={}).status_code in (404, 405)
    assert "mail_connector_call_tool" not in {op["operationId"] for path in app.openapi()["paths"].values() for op in path.values()}


@pytest.mark.anyio
async def test_read_call_refuses_write_tool():
    with pytest.raises(connectors.ToolNotAllowedError):
        await connectors.call_read_tool("gmail", "send_email", {})


@pytest.mark.anyio
async def test_write_call_refuses_unknown_tool():
    with pytest.raises(connectors.ToolNotAllowedError):
        await connectors.call_write_tool("gmail", "delete_label", {})


def test_only_send_and_actions_call_write_tools():
    root = Path(__file__).parents[1] / "src/api"
    allowed = {"mail/connectors.py", "mail/send.py", "mail/actions.py"}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(getattr(node, "attr", None) == "call_write_tool" or getattr(node, "id", None) == "call_write_tool" or getattr(node, "name", None) == "call_write_tool" for node in ast.walk(tree)):
            assert path.relative_to(root).as_posix() in allowed


@pytest.mark.anyio
async def test_named_mailbox_is_pinned_for_reads_and_writes(monkeypatch):
    import json
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit
    from unittest.mock import AsyncMock
    from api.mail import connector_mailbox

    mailbox = SimpleNamespace(id='account-a', provider='gmail', email_address='a@example.test')
    thread = {'id': 'sent-thread', 'messages': [{'id': 'sent-message', 'labelIds': ['SENT'], 'sender': 'A <a@example.test>'}]}
    listing = AsyncMock(return_value={'credentials': [{'name': 'credential A/1', 'authentication_type': 'oauth2'}]})
    monkeypatch.setattr(connectors, '_get', listing)
    sent = []
    visible = thread

    async def dispatch(path, payload):
        parsed = urlsplit(path)
        assert parse_qs(parsed.query) == {'credentials_name': ['credential A/1']}
        tool = parsed.path.split('/')[-2]
        if tool == 'search_threads':
            assert payload['arguments']['query'] == 'in:sent from:me'
            result = {'threads': [visible]}
        elif tool == 'get_thread':
            result = visible
        else:
            sent.append(tool)
            result = {'id': 'sent-copy', 'threadId': 'sent-thread'}
        return {'content': [{'text': json.dumps(result)}]}

    invalidated = AsyncMock()
    monkeypatch.setattr(connector_mailbox.chat_settings, 'invalidate_connector', invalidated)
    monkeypatch.setattr(connectors, '_post', dispatch)
    async with connector_mailbox.bound_mailbox(mailbox, cursor='234', bootstrap=True, check_after=True) as (binding, progress):
        assert progress == ''
    state = SimpleNamespace(connector_verified=True, connector_generation=0, connector_cursor=connector_mailbox.encode(binding, '350'))
    monkeypatch.setattr(connector_mailbox.mail_sync_state, 'get_state', AsyncMock(return_value=state))
    listing.return_value = {'credentials': [{'name': 'new-default', 'authentication_type': 'oauth2'}]}
    await connectors.call_write_tool(mailbox, 'send_email', {})
    assert sent == ['send_email']
    assert listing.await_count == 1
    visible = {'id': 'different-thread', 'messages': thread['messages']}
    with pytest.raises(connector_mailbox.MailboxUnavailable):
        await connectors.call_write_tool(mailbox, 'send_email', {})
    assert sent == ['send_email']
    invalidated.assert_awaited_once_with(mailbox.id, 0, observed=state)
    assert connectors.mailbox_credential.get() is None


@pytest.mark.anyio
async def test_connector_change_during_sync_discards_the_unverified_page(fake_store, monkeypatch):
    from unittest.mock import AsyncMock
    from api.mail import connector_mailbox, sync, sync_connector
    from api.mail.sync_types import SyncChanges
    account = await fake_store.create_account(provider='gmail', auth_type=connectors.CONNECTOR, email_address='a@example.test')
    binding = ('named', 'thread-a', 'message-a')
    await fake_store.save_state(account_id=account.id, cursor='27', connector_cursor=connector_mailbox.encode(binding, '27'), connector_verified=True)
    thread = {'id': 'thread-a', 'messages': [{'id': 'message-a', 'sender': account.email_address, 'labelIds': ['SENT']}]}
    monkeypatch.setattr(connectors, 'call_read_tool_json', AsyncMock(side_effect=[thread, {'id': 'thread-b', 'messages': []}]))
    invalidated = AsyncMock()
    monkeypatch.setattr(connector_mailbox.chat_settings, 'invalidate_connector', invalidated)
    monkeypatch.setattr(sync_connector, 'fetch_changes', AsyncMock(return_value=SyncChanges(cursor='29', full_rebuild=True)))
    monkeypatch.setattr(sync.message_access, 'trash_conflicts', AsyncMock(return_value={}))
    replace = AsyncMock()
    monkeypatch.setattr(sync.message_access, 'delete_folder', replace)
    result = await sync.sync_account(account.id)
    assert (result.status, result.error_code) == ('error', 'auth_expired')
    replace.assert_not_awaited()
    invalidated.assert_awaited_once_with(account.id, 1, observed=await fake_store.get_state(account.id))


@pytest.mark.anyio
@pytest.mark.parametrize('after,status_code', [(False, 429), (True, 503), (True, None)])
async def test_transient_proof_failure_retains_the_mailbox_binding(monkeypatch, after, status_code):
    import httpx
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api.mail import connector_mailbox
    account = SimpleNamespace(id='account', provider='gmail', email_address='a@example.test')
    thread = {'id': 'thread', 'messages': [{'id': 'message', 'sender': account.email_address, 'labelIds': ['SENT']}]}
    request = httpx.Request('GET', 'https://example.test')
    error = httpx.ReadTimeout('timeout', request=request) if status_code is None else httpx.HTTPStatusError(
        'temporary', request=request, response=httpx.Response(status_code, request=request))
    monkeypatch.setattr(connectors, 'call_read_tool_json', AsyncMock(side_effect=[thread, error] if after else [error]))
    invalidate = AsyncMock()
    monkeypatch.setattr(connector_mailbox.chat_settings, 'invalidate_connector', invalidate)
    with pytest.raises(httpx.HTTPError):
        async with connector_mailbox.bound_mailbox(account, cursor=connector_mailbox.encode(('pinned', 'thread', 'message'), '12'), check_after=True):
            assert after
    invalidate.assert_not_awaited()
    assert connectors.mailbox_credential.get() is None


@pytest.mark.anyio
async def test_incoming_only_labels_verify_the_target_message_without_allowing_send(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api.mail import actions, connector_mailbox
    account = SimpleNamespace(id='placeholder', provider='outlook', auth_type=connectors.CONNECTOR, email_address='outlook@connector.mistral')
    mail = SimpleNamespace(account_id=account.id, provider_uid='uid', thread_id='thread', sender='a@example.test', subject='Example')
    row = {'id': mail.provider_uid, 'sender': mail.sender, 'subject': mail.subject}
    get = AsyncMock(side_effect=[{'id': 'thread', 'messages': [row]}, {'id': 'thread', 'messages': []}])
    monkeypatch.setattr(connectors, 'call_read_tool_json', get)
    monkeypatch.setattr(connector_mailbox, '_credential', AsyncMock(return_value='pinned'))
    async def dispatch(*args):
        assert connectors.mailbox_credential.get() == 'pinned'
        return '{}'
    write = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(connectors, '_call_tool', write)
    await actions.write_connector_labels(account, mail, added=[], removed=['UNREAD'])
    with pytest.raises(connector_mailbox.MailboxUnavailable):
        await actions.write_connector_labels(account, mail, added=['TRASH'], removed=[])
    with pytest.raises(connector_mailbox.MailboxUnavailable):
        await connectors.call_write_tool(account, 'send_email', {}, target=mail)
    write.assert_awaited_once_with('outlook', 'unlabel_message', {'messageId': mail.provider_uid, 'labelIds': ['UNREAD']})
    assert connectors.mailbox_credential.get() is None
    monkeypatch.setattr(connector_mailbox, '_credential', AsyncMock(side_effect=connector_mailbox.MailboxUnproven()))
    get.reset_mock()
    with pytest.raises(connector_mailbox.MailboxUnavailable):
        await actions.write_connector_labels(account, mail, added=['TRASH'], removed=[])
    get.assert_not_awaited()
    assert write.await_count == 1


@pytest.mark.anyio
async def test_insights_removed_from_existing_mail_are_rescheduled(monkeypatch):
    from unittest.mock import AsyncMock
    from api.mail import jobs
    from api.mail.sync_types import SyncResult
    queue = AsyncMock()
    monkeypatch.setattr(jobs, 'enqueue_insights', queue)
    monkeypatch.setattr(jobs.sync, 'sync_account', AsyncMock(return_value=SyncResult('ok', 1, '25', '', [], True)))
    await jobs._sync({'account_id': 'synthetic'})
    queue.assert_awaited_once_with('synthetic')


@pytest.mark.anyio
async def test_named_legacy_account_keeps_its_index_when_identity_is_unprovable(fake_store, monkeypatch):
    from unittest.mock import AsyncMock
    from api.mail import connector_mailbox, sync, sync_connector
    account = await fake_store.create_account(provider='gmail', auth_type=connectors.CONNECTOR, email_address='known@example.test')
    await fake_store.save_state(account_id=account.id, cursor='19')
    monkeypatch.setattr(connector_mailbox, '_credential', AsyncMock(return_value='named'))
    monkeypatch.setattr(connectors, 'call_read_tool_json', AsyncMock(return_value={'threads': []}))
    fetch = AsyncMock()
    monkeypatch.setattr(sync_connector, 'fetch_changes', fetch)
    result = await sync.sync_account(account.id)
    assert result.status == 'error'
    assert (await fake_store.get_state(account.id)).cursor == '19'
    fetch.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize('tool,target_key', [
    ('label_message', 'messageId'), ('unlabel_message', 'messageId'),
    ('label_thread', 'threadId'), ('unlabel_thread', 'threadId'),
    ('send_email', 'replyToMessageId'),
])
async def test_connector_write_rejects_unflagged_permission_diagnostic(monkeypatch, caplog, tool, target_key):
    import httpx

    requests = []
    reply = {'content': [{'type': 'text', 'text': 'Request had insufficient authentication scopes. Permission denied. Synthetic private detail.'}]}

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=reply)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(connectors, '_headers', lambda: {})
    monkeypatch.setattr(connectors, 'mistral_api_url', lambda: 'https://studio.example.test')
    monkeypatch.setattr(connectors.httpx, 'AsyncClient', lambda **kwargs: client)
    with pytest.raises(ValueError, match='^provider_permission_missing$'):
        await connectors._call_tool('gmail', tool, {target_key: 'selected', 'labelIds': ['UNREAD']})
    assert [request.url.path for request in requests] == [f'/v1/connectors/gmail/tools/{tool}/call']
    assert 'provider_permission_missing' in caplog.text
    assert 'Synthetic private detail' not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize('tool,args,payload', [
    ('label_message', {'messageId': 'selected', 'labelIds': ['UNREAD']}, {'id': 'selected', 'labelIds': ['UNREAD']}),
    ('unlabel_message', {'messageId': 'selected', 'labelIds': ['UNREAD']}, {'id': 'selected', 'labelIds': []}),
    ('label_thread', {'threadId': 'thread', 'labelIds': ['UNREAD']}, {'id': 'thread', 'messages': [{'id': 'selected', 'labelIds': ['UNREAD']}]}),
    ('unlabel_thread', {'threadId': 'thread', 'labelIds': ['UNREAD']}, {'id': 'thread', 'messages': [{'id': 'selected', 'labelIds': []}]}),
    ('send_email', {'replyToMessageId': 'original'}, {'messageId': 'sent-copy', 'threadId': 'thread'}),
])
async def test_connector_write_accepts_a_structured_receipt(monkeypatch, tool, args, payload):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(connectors, '_post', AsyncMock(return_value={'structuredContent': payload}))
    await connectors._call_tool('gmail', tool, args)


@pytest.mark.anyio
@pytest.mark.parametrize('tool,args,payload', [
    ('label_message', {'messageId': 'selected', 'labelIds': ['UNREAD']}, {'id': 'other', 'labelIds': ['UNREAD']}),
    ('unlabel_thread', {'threadId': 'thread', 'labelIds': ['UNREAD']}, {'id': 'thread', 'messages': [{'id': 'selected', 'labelIds': ['UNREAD']}]}),
    ('send_email', {'replyToMessageId': 'original'}, {'id': 'original', 'threadId': 'thread'}),
    ('send_email', {}, {'success': True}),
])
async def test_connector_write_needs_evidence_for_the_intended_change(monkeypatch, tool, args, payload):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(connectors, '_post', AsyncMock(return_value={'structuredContent': payload}))
    with pytest.raises(ValueError, match='^connector_write_unconfirmed$'):
        await connectors._call_tool('gmail', tool, args)
