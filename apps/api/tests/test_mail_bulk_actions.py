"""Bulk actions preserve ordering, individual outcomes and undo identifiers."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from db.models.mail_account import MailAccount
from db.models.mail_message import MailMessage

from api.mail import action_backends
from api.mail.seed_ids import seed_id
from api.routers.api.v1.mail import messages


@pytest.fixture
def bulk_store(fake_store, monkeypatch):
    fake_store.accounts['a'] = MailAccount(id='a', provider='google', auth_type='oauth2', email_address='me@example.test')
    for mid in ('one', 'two', 'unsupported'):
        fake_store.messages[mid] = MailMessage(id=mid, account_id='a', provider_uid=mid, folder='ARCHIVE' if mid == 'unsupported' else 'INBOX')
    backend = AsyncMock()
    backend.apply.return_value = {}
    backend.revert.return_value = {}
    monkeypatch.setattr(action_backends, 'backend_for', lambda _: backend)
    monkeypatch.setattr(messages.insight_access, 'get_for_messages', AsyncMock(return_value={}))
    return fake_store, backend


@pytest.mark.parametrize('body', [
    {'message_ids': [], 'kind': 'archive'},
    {'message_ids': [str(i) for i in range(101)], 'kind': 'archive'},
    {'message_ids': ['one', 'one'], 'kind': 'archive'},
    {'message_ids': ['one'], 'kind': 'restore'},
    {'message_ids': ['one'], 'kind': 'archive', 'extra': True},
])
def test_invalid_request_is_atomic(client, bulk_store, body):
    store, backend = bulk_store
    assert client.post('/api/v1/mail/messages/actions', json=body).status_code == 422
    assert not store.actions
    assert store.messages['one'].folder == 'INBOX'
    backend.apply.assert_not_awaited()


def test_partial_order_and_group_undo(client, bulk_store):
    store, backend = bulk_store
    response = client.post('/api/v1/mail/messages/actions', json={'message_ids': ['two', 'missing', 'unsupported', 'one'], 'kind': 'archive'})
    assert response.status_code == 200
    rows = response.json()['results']
    assert [r['message_id'] for r in rows] == ['two', 'missing', 'unsupported', 'one']
    assert [r['status'] for r in rows] == ['done', 'not_found', 'unsupported', 'done']
    ids = [r['action_id'] for r in rows if r['action_id']]
    assert len(ids) == 2 and rows[0]['message']['id'] == 'two'
    undo = client.post('/api/v1/mail/actions/undo', json={'action_ids': ids}).json()
    assert set(undo['restored']) == set(ids)
    assert store.messages['one'].folder == store.messages['two'].folder == 'INBOX'
    assert backend.revert.await_count == 2


@pytest.mark.parametrize("kind", ["archive", "mark_read"])
def test_alias_and_current_id_dispatch_one_action_and_one_undo(client, bulk_store, kind):
    store, backend = bulk_store
    store.messages["one"].connector_alias_ids = ["former"]
    body = {"message_ids": ["former", "one"], "kind": kind}
    rows = client.post("/api/v1/mail/messages/actions", json=body).json()["results"]
    assert [(row["message_id"], row["message"]["id"], row["status"]) for row in rows] == [("former", "one", "done")]
    assert list(store.actions) == [rows[0]["action_id"]]
    backend.apply.assert_awaited_once()
    assert client.post("/api/v1/mail/actions/undo", json={"action_ids": list(store.actions)}).json()["restored"] == list(store.actions)
    backend.revert.assert_awaited_once()


def test_provider_failure_does_not_stop_other_items(client, bulk_store):
    store, backend = bulk_store

    async def apply(message, kind):
        if message.id == 'one':
            raise RuntimeError('private provider diagnostic')
        return {}

    backend.apply.side_effect = apply
    response = client.post('/api/v1/mail/messages/actions', json={'message_ids': ['one', 'two'], 'kind': 'archive'})
    assert [row['status'] for row in response.json()['results']] == ['failed', 'done']
    assert store.messages['one'].folder == 'INBOX'
    assert store.messages['two'].folder == 'ARCHIVE'
    assert 'private provider diagnostic' not in response.text


def test_applied_action_id_survives_display_failure(client, bulk_store, monkeypatch):
    store, _ = bulk_store
    monkeypatch.setattr(messages.insight_access, 'get_for_messages', AsyncMock(side_effect=RuntimeError('private')))
    row = client.post('/api/v1/mail/messages/actions', json={'message_ids': ['one'], 'kind': 'archive'}).json()['results'][0]
    assert row['status'] == 'done' and row['action_id'] in store.actions and row['message'] is None
    assert client.post('/api/v1/mail/actions/undo', json={'action_ids': [row['action_id']]}).json()['restored'] == [row['action_id']]


def test_seed_account_does_not_contact_provider(client, fake_store, monkeypatch):
    account_id = seed_id('account', 'privat')
    fake_store.accounts[account_id] = MailAccount(id=account_id, provider='google', auth_type='oauth2', email_address='me@example.test')
    fake_store.messages['seed'] = MailMessage(id='seed', account_id=account_id, provider_uid='seed')
    monkeypatch.setattr(messages.insight_access, 'get_for_messages', AsyncMock(return_value={}))
    backend = action_backends.backend_for(fake_store.accounts[account_id])
    assert type(backend).__name__ == 'IndexBackend'
    response = client.post('/api/v1/mail/messages/actions', json={'message_ids': ['seed'], 'kind': 'archive'})
    row = response.json()['results'][0]
    assert row['status'] == 'done' and fake_store.messages['seed'].folder == 'ARCHIVE'
    assert client.post('/api/v1/mail/actions/undo', json={'action_ids': [row['action_id']]}).json()['restored'] == [row['action_id']]
    assert fake_store.messages['seed'].folder == 'INBOX'


@pytest.mark.anyio
async def test_concurrency_is_bounded(bulk_store, monkeypatch):
    active = peak = 0
    original = messages.actions.apply_action

    async def slow(mid, kind):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        try:
            return await original(mid, kind)
        finally:
            active -= 1

    monkeypatch.setattr(messages.actions, 'apply_action', slow)
    from api.routers.api.v1.mail.schemas import BulkMessageActionIn
    result = await messages.bulk_message_action(BulkMessageActionIn(message_ids=[str(i) for i in range(12)], kind='archive'))
    assert peak == 4
    assert all(row.status == 'not_found' for row in result.results)


@pytest.mark.parametrize('kind', ['trash', 'restore', 'archive', 'spam', 'mark_read', 'mark_unread'])
def test_gmail_studio_rejects_action_before_provider_or_index_change(client, bulk_store, kind):
    store, backend = bulk_store
    account = store.accounts['a']
    account.provider = 'gmail'
    account.auth_type = 'mistral_connector'
    account.display_name = 'Arbitrary display name'
    message = store.messages['one']
    message.folder = 'TRASH' if kind == 'restore' else 'INBOX'
    old = (message.folder, message.is_read, message.labels_json)
    response = client.post('/api/v1/mail/messages/one/actions', json={'kind': kind})
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'provider_action_unsupported'
    assert set(response.json()['detail']) == {'code', 'message'}
    assert (message.folder, message.is_read, message.labels_json) == old
    assert not store.actions
    backend.apply.assert_not_awaited()


def test_gmail_studio_capabilities_block_mixed_bulk_and_legacy_undo(client, bulk_store):
    from db.models.mail_action import MailAction

    store, backend = bulk_store
    account = MailAccount(id='studio', provider='google_mail', auth_type='mistral_connector', email_address='mail@example.test')
    store.accounts[account.id] = account
    target = MailMessage(id='studio-mail', account_id=account.id, provider_uid='indexed')
    store.messages[target.id] = target
    action = MailAction(id='old-undo', account_id=account.id, message_id=target.id, kind='trash', previous_json='{}')
    store.actions[action.id] = action
    capabilities = {a['id']: a['action_capabilities'] for a in client.get('/api/v1/mail/accounts').json()}
    assert capabilities[account.id] == dict.fromkeys(('archive','trash','restore','spam','mark_read','mark_unread','move','label'), False)
    assert capabilities['a']['trash']
    for path, body in [
        ('/api/v1/mail/messages/actions', {'message_ids': ['one', target.id], 'kind': 'trash'}),
        ('/api/v1/mail/actions/undo', {'action_ids': [action.id]}),
    ]:
        response = client.post(path, json=body)
        assert response.status_code == 409 and response.json()['detail']['code'] == 'provider_action_unsupported'
        assert target.id not in response.text and action.id not in response.text
    assert action.undone_at is None
    assert store.messages['one'].folder == target.folder == 'INBOX'
    backend.apply.assert_not_awaited()
    backend.revert.assert_not_awaited()
