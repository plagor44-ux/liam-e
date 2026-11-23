"""Central mail grants across devices, request entry points and proposal claims."""
import asyncio
import json
import os
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from api.mail import agent, proposals, service
from api.routers.api.v1.chat import route, stream
from api.routers.api.v1.settings import chat as settings_route
from db.accessors import chat_context, chat_message, chat_settings, mail_account
from db.accessors.prompt_template import StaleVersion
from db.engine import get_session_maker
from db.models.mail_message import MailMessage

pytestmark = pytest.mark.skipif(not os.getenv('RUN_PG_CONTRACT'), reason='RUN_PG_CONTRACT is not set')


async def connect(name):
    return await service.upsert_account(provider='imap', auth_type='imap_password', email_address=f'{name}@example.test')


async def resolve(**kwargs):
    options = dict(conversation_id=None, title='Example', model=None, revision_id=None, account_id=None, app_prompt_version='test')
    return await chat_context.resolve(**(options | kwargs))


def test_revision_reconnect_delete_and_regrant(pg_db):
    async def run():
        a, b = await connect('one'), await connect('two')
        initial = await chat_settings.get()
        assert (initial.version, initial.account_ids) == (1, [])
        _, empty, _ = await resolve()
        assert json.loads(empty.account_scope_json)['account_ids'] == []
        edits = await asyncio.gather(chat_settings.update(1, [a.id]), chat_settings.update(1, [b.id]), return_exceptions=True)
        assert sum(isinstance(edit, StaleVersion) for edit in edits) == 1
        both = await chat_settings.update(2, [b.id, a.id])
        assert (both.version, both.account_ids) == (3, sorted([a.id, b.id]))
        assert (await chat_settings.update(3, [a.id, b.id, a.id])).version == 3
        assert (await connect('one')).id == a.id
        conversation, before, _ = await resolve(expected_mail_scope_version=3)
        await chat_message.add_message(conversation_id=conversation.id, role='assistant', context_id=before.id, content='Stored answer')
        await connect('three')
        assert await chat_context.is_live(before.id)
        assert (await chat_settings.get()).account_ids == both.account_ids
        assert await mail_account.delete_account(a.id)
        assert (await chat_settings.get()).account_ids == [b.id]
        assert not await chat_context.is_live(before.id)
        replacement = await connect('one')
        assert replacement.id != a.id
        with pytest.raises(chat_settings.AccountNotGranted):
            await resolve(account_id=replacement.id)
        with pytest.raises(StaleVersion):
            await chat_settings.update(3, [])
        with pytest.raises(chat_settings.AccountNotGranted):
            await chat_settings.update(4, [a.id])
        await chat_settings.update(4, [b.id, replacement.id])
        with pytest.raises(chat_context.ScopeChange):
            await resolve(conversation_id=conversation.id)
        assert [row.content for row in await chat_message.list_messages(conversation.id)] == ['Stored answer']
    asyncio.run(run())


def test_browser_and_compatibility_chat_reject_excluded_accounts_before_model(pg_db, monkeypatch):
    async def run():
        a, b = await connect('permitted'), await connect('excluded')
        mail = MailMessage(account_id=b.id, provider_uid='synthetic', body_text='private synthetic marker')
        granted_mail = MailMessage(account_id=a.id, provider_uid='synthetic-granted')
        async with get_session_maker()() as session:
            session.add_all([mail, granted_mail])
            await session.commit()
        model = Mock(side_effect=AssertionError('unexpected model request'))
        monkeypatch.setattr(agent, 'mistral_client', model)
        app = FastAPI()
        app.include_router(settings_route.router, prefix='/settings/chat')
        app.include_router(route.router, prefix='/chat')
        app.include_router(stream.router, prefix='/chat/stream')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/settings/chat')).json() == {'version': 1, 'account_ids': []}
            async def save(version, ids):
                return await client.patch('/settings/chat', json={'expected_version': version, 'account_ids': ids})
            assert (await save(1, [a.id])).json() == {'version': 2, 'account_ids': [a.id]}
            assert (await save(1, [])).status_code == 409
            assert (await save(2, ['missing'])).status_code == 422
            for path, field in [('/chat', 'message'), ('/chat/stream', 'text')]:
                for extra, status, code in [
                    ({'account_id': b.id}, 403, 'chat_account_not_granted'),
                    ({'context_message_id': mail.id}, 403, 'chat_account_not_granted'),
                    ({'account_id': a.id, 'context_message_id': mail.id}, 403, 'chat_account_not_granted'),
                    ({'context_message_id': 'missing'}, 403, 'chat_account_not_granted'),
                    ({'expected_mail_scope_version': 1}, 409, 'chat_settings_changed'),
                    ({'task_source': 'account', 'task_source_account_id': b.id}, 422, 'context_invalid'),
                ]:
                    response = await client.post(path, json={field: 'Question', **extra})
                    assert (response.status_code, response.json()['detail']['code']) == (status, code)
                    assert mail.body_text not in response.text
        model.assert_not_called()
        _, snapshot, _ = await resolve(context_message_id=granted_mail.id)
        assert json.loads(snapshot.account_scope_json) == {'account_ids': [a.id], 'selection': a.id}
    asyncio.run(run())


def test_confirmation_checks_grant_and_actual_sender_in_claim_transaction(pg_db, monkeypatch):
    async def run():
        a, b = await connect('allowed'), await connect('other')
        await chat_settings.update(1, [a.id])
        conversation, context, _ = await resolve()
        send = AsyncMock()
        monkeypatch.setattr(proposals.send, 'send_message', send)
        for sender in [b, a]:
            draft = proposals.SendDraft(account_id=sender.id, mode='new', to=['recipient@example.test'], cc=[],
                subject='Example', body='Example', quote='', reply_to_message_id=None)
            card = await proposals.create_proposal(conversation.id,
                proposals.Proposal(kind='send', reason='Example', items=[], draft=draft), context_id=context.id)
            if sender == a:
                await chat_settings.update(2, [])
                await chat_settings.update(3, [a.id])
            with pytest.raises(proposals.ProposalContextRevokedError):
                await proposals.confirm(card.id, selected_ids=[])
            assert (await chat_message.get_message(card.id)).proposal_status == 'pending'
            assert (await proposals.cancel(card.id)).proposal_status == 'cancelled'
        send.assert_not_awaited()
    asyncio.run(run())


def test_live_preview_only_exposes_authorized_short_references(pg_db):
    from api.routers.api.v1.chat import references
    async def run():
        first, second = await connect('first'), await connect('second')
        await chat_settings.update(1, [first.id, second.id])
        _, focus, _ = await resolve(account_id=first.id)
        rows = [MailMessage(account_id=a.id, provider_uid='one', subject='Synthetic', snippet='Short', body_text='Full private text') for a in (first, second)]
        async with get_session_maker()() as session:
            session.add_all(rows)
            await session.commit()
        app = FastAPI()
        app.include_router(references.router, prefix='/refs')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            async def preview(id, version=2, context=None):
                params = {'expected_version': version}
                if context:
                    params['context_id'] = context
                return await client.get('/refs/' + id, params=params)
            allowed = await preview(rows[0].id, context=focus.id)
            assert allowed.status_code == 200 and allowed.json()['snippet'] == 'Short'
            assert 'body_text' not in allowed.json() and 'Full private text' not in allowed.text
            assert (await preview(rows[1].id)).status_code == 200
            assert (await preview(rows[1].id, context=focus.id)).json() == (await preview('missing')).json()
            await chat_settings.update(2, [second.id])
            for id, version, context in [(rows[0].id, 2, None), (rows[0].id, 3, None), (rows[1].id, 3, focus.id)]:
                assert (await preview(id, version, context)).status_code == 404
            assert (await preview(rows[1].id, 3)).status_code == 200
    asyncio.run(run())


def test_connector_indexes_need_positive_verification_and_explicit_regrant(pg_db):
    from db.accessors import mail_sync_state
    from api.mail import connector_mailbox, connectors
    async def run():
        a = await service.upsert_account(provider='gmail', auth_type=connectors.CONNECTOR, email_address='a@example.test')
        async def save(cursor, checked):
            await mail_sync_state.save_state(account_id=a.id, cursor="234", connector_cursor=cursor, connector_verified=checked)
        await save('234', False)
        with pytest.raises(chat_settings.AccountNotGranted):
            await chat_settings.update(1, [a.id])
        legacy = MailMessage(account_id=a.id, provider_uid='legacy', folder='ARCHIVE', unverified_connector=True)
        async with get_session_maker()() as session:
            session.add(legacy)
            await session.commit()
        cursor = connector_mailbox.encode(('named', 'thread', 'message'), '244')
        await save(cursor, True)
        await chat_settings.update(1, [a.id])
        _, original, _ = await resolve(account_id=a.id)
        assert await chat_context.is_live(original.id)
        with pytest.raises(chat_settings.AccountNotGranted):
            await resolve(context_message_id=legacy.id)
        assert await chat_settings.mail_preview(legacy.id, 2, original.id) is None
        await chat_settings.invalidate_connector(a.id)
        assert not await chat_context.is_live(original.id)
        with pytest.raises(chat_settings.AccountNotGranted):
            await chat_settings.update(3, [a.id])
        await save(cursor, True)
        assert (await chat_settings.get()).account_ids == []
        assert (await chat_settings.update(3, [a.id])).version == 4
        assert not await chat_context.is_live(original.id)
    asyncio.run(run())


def test_connector_upgrade_verifies_pages_in_place_and_keeps_ordinary_inbox_only_accounts(pg_db, monkeypatch):
    from db.accessors import mail_job, mail_message, mail_sync_state
    from api.mail import connector_mailbox, connectors, sync
    from api.mail.sync_types import BODY_VERSION
    from api.routers.api.v1.mail.accounts import current_account_out

    async def run():
        a = await service.upsert_account(provider='gmail', auth_type=connectors.CONNECTOR, email_address='a@example.test')
        placeholder = await service.upsert_account(provider='gmail', auth_type=connectors.CONNECTOR, email_address='gmail@connector.mistral')
        legacy = [await mail_message.upsert_message(account_id=a.id, folder='INBOX', provider_uid=uid,
            subject='Previous', unverified_connector=True, body_version=BODY_VERSION) for uid in ('one', 'two', 'absent')]
        from db.models.mail_insight import MailInsight
        async with get_session_maker()() as session:
            session.add(MailInsight(message_id=legacy[0].id, needs_reply=True, model="fixture"))
            await session.commit()
        await mail_sync_state.save_state(account_id=a.id, cursor='234')
        listing = AsyncMock(side_effect=[{'credentials': None}, {'credentials': [{'name': 'identity', 'authentication_type': 'oauth2'}]}])
        monkeypatch.setattr(connectors, '_get', listing)
        monkeypatch.setattr(sync.mail_env, 'mail_sync_page_size', 1)
        pages = []
        async def provider(name, tool, args):
            anchor = {'id': 'sent', 'messages': [{'id': 'sent-proof', 'labelIds': ['SENT'], 'sender': a.email_address}]}
            if args.get('query') == 'in:sent from:me':
                return {'threads': [anchor]}
            if args.get('threadId') == 'sent':
                return anchor
            if tool == 'search_threads':
                if 'after:' in args['query']:
                    return {'threads': [{'id': 'new-arrival'}]}
                if 'newer_than:' in args['query']:
                    return {'threads': []}
                assert args['query'] == 'in:inbox'
                pages.append(args.get('pageToken'))
                if args.get('pageToken'):
                    if pages.count('opaque-next') == 1:
                        raise ValueError("Incomplete historical provider response")
                    return {'threads': [{'id': 'two'}]}
                return {'threads': [{'id': 'one'}], 'nextPageToken': 'opaque-next'}
            uid = args['threadId']
            return {'id': uid, 'messages': [{'id': uid, 'labelIds': ['INBOX', 'UNREAD'], 'date': '2026-01-01T10:00:00+00:00', 'subject': 'Checked'}]}
        monkeypatch.setattr(connectors, 'call_read_tool_json', provider)
        assert (await current_account_out(a)).chat_eligibility == "awaiting_verification"
        first = await sync.sync_account(a.id)
        assert first.status == 'ok' and first.new_message_ids == []
        refresh = await mail_job.latest_for_account(a.id, kind=mail_job.VERIFIED_INSIGHTS, open_only=True)
        assert json.loads(refresh.payload_json)['message_ids'] == [legacy[0].id]
        assert listing.await_args_list[1].args[0].endswith('/user/credentials')
        assert connector_mailbox.decode(first.cursor)[1].startswith('initial:')
        assert not (await mail_message.get_message(legacy[0].id)).unverified_connector
        assert (await mail_message.get_message(legacy[1].id)).unverified_connector
        assert (await sync.sync_account(a.id)).status == 'ok' and pages == [None, 'opaque-next']
        arrivals = await mail_message.list_page(account_ids=[a.id], chat_only=True)
        assert 'new-arrival' in {m.provider_uid for m in arrivals}
        assert (await mail_message.get_message(legacy[1].id)).unverified_connector
        assert (await sync.sync_account(a.id)).status == 'ok' and pages[-2:] == ['opaque-next', 'opaque-next']
        assert not (await mail_message.get_message(legacy[1].id)).unverified_connector
        assert (await mail_message.get_message(legacy[2].id)).folder == 'INBOX'
        await chat_settings.update(1, [a.id])
        assert await chat_settings.mail_preview(legacy[2].id, 2, None) is None
        assert (await current_account_out(a)).chat_eligibility == "eligible"
        assert (await sync.sync_account(placeholder.id)).status == 'ok'
        assert (await current_account_out(placeholder)).chat_eligibility == "inbox_only"
        rows = await mail_message.list_page(account_ids=[placeholder.id])
        assert len(rows) == 1 and rows[0].unverified_connector
        assert not (await mail_sync_state.get_state(placeholder.id)).connector_verified
        with pytest.raises(chat_settings.AccountNotGranted):
            await chat_settings.update(2, [placeholder.id])
    asyncio.run(run())


def test_unverified_task_mail_is_refused_at_proposal_and_confirmation(pg_db):
    from types import SimpleNamespace
    from api.tasks import chat, proposals as task_proposals
    from db.accessors import mail_message
    async def run():
        a = await connect('source')
        row = await mail_message.upsert_message(account_id=a.id, folder='ARCHIVE', provider_uid='old', unverified_connector=True)
        ctx = SimpleNamespace(task_scope={'source': 'all'}, account_ids=[a.id], account_id=None, source_message_id=None)
        request = dict(action='create', title='Example', start='2027-01-01', timezone='UTC', source_message_id=row.id)
        with pytest.raises(ValueError, match='task_scope_invalid'):
            await chat.prepare(request, ctx)
        command = task_proposals.TaskCreateCommand(**request, source_account_id=a.id)
        async with get_session_maker()() as session:
            with pytest.raises(task_proposals.TaskSourceGone):
                await task_proposals.execute(session, command, proposal_id='unverified')
    asyncio.run(run())


def test_legacy_writes_and_obsolete_batches_cannot_publish_verified_mail(pg_db):
    from db.accessors import mail_message, mail_sync_state
    from api.mail import connector_mailbox, connectors
    async def run():
        a = await service.upsert_account(provider='gmail', auth_type=connectors.CONNECTOR, email_address='rollout@example.test')
        initial = await mail_sync_state.save_state(account_id=a.id, cursor='12')
        async with mail_sync_state.sync_transaction(a.id, initial, verified=True) as (session, state):
            verified = await mail_message.upsert_message(session=session, account_id=a.id, folder='INBOX', provider_uid='one', subject='Verified')
            state.connector_cursor = connector_mailbox.encode(('named', 'thread', 'proof'), '12')
            state.connector_verified = True
        await chat_settings.update(1, [a.id])
        assert await chat_settings.mail_preview(verified.id, 2, None) is not None
        # Both a new insert and an update by a transaction without a verified provider read are quarantined.
        old = await mail_message.upsert_message(account_id=a.id, folder='INBOX', provider_uid='legacy', subject='Legacy')
        await mail_message.upsert_message(account_id=a.id, folder='INBOX', provider_uid='one', subject='Legacy overwrite')
        old_state = await mail_sync_state.save_state(account_id=a.id, cursor='17')
        assert old_state.connector_cursor and old_state.cursor == '17'
        for row in (old, verified):
            assert await chat_settings.mail_preview(row.id, 2, None) is None
        await chat_settings.invalidate_connector(a.id, old_state.connector_generation)
        with pytest.raises(mail_sync_state.StaleMailSync):
            async with mail_sync_state.sync_transaction(a.id, old_state, verified=True):
                pytest.fail('stale publication was admitted')
        with pytest.raises(chat_settings.AccountNotGranted):
            await chat_settings.update(3, [a.id])
        current = await mail_sync_state.get_state(a.id)
        with pytest.raises(RuntimeError, match='incomplete page'):
            async with mail_sync_state.sync_transaction(a.id, current, verified=True) as (session, state):
                await mail_message.upsert_message(session=session, account_id=a.id, folder='INBOX', provider_uid='partial')
                state.connector_verified = True
                raise RuntimeError('incomplete page')
        assert 'partial' not in await mail_message.known_uids(a.id, 'INBOX')
        assert not (await mail_sync_state.get_state(a.id)).connector_verified
    asyncio.run(run())


@pytest.mark.parametrize("rebind", [False, True])
def test_late_connector_failure_preserves_the_newer_success(pg_db, monkeypatch, rebind):
    from api.mail import connector_mailbox, connectors, sync, sync_connector
    from api.mail.sync_types import SyncChanges
    from db.accessors import mail_sync_state

    async def run():
        account = await service.upsert_account(provider="gmail", auth_type=connectors.CONNECTOR, email_address="sync@example.test")
        binding = ("named", "sent", "proof")
        cursor = connector_mailbox.encode(binding, "12")
        await mail_sync_state.save_state(account_id=account.id, cursor="12", connector_cursor=cursor, connector_verified=True)
        started, release = asyncio.Event(), asyncio.Event()
        anchor = {"id": "sent", "messages": [{"id": "proof", "sender": account.email_address, "labelIds": ["SENT"]}]}

        async def read(provider, tool, args):
            if asyncio.current_task() is old:
                started.set()
                await release.wait()
                return {"id": "other", "messages": []}
            return {"threads": [anchor]} if tool == "search_threads" else anchor

        monkeypatch.setattr(connectors, "call_read_tool_json", read)
        monkeypatch.setattr(connector_mailbox, "_credential", AsyncMock(return_value="rebound"))
        monkeypatch.setattr(sync_connector, "fetch_changes", AsyncMock(return_value=SyncChanges(cursor="12")))
        old = asyncio.create_task(sync.sync_account(account.id))
        try:
            await asyncio.wait_for(started.wait(), 5)
            if rebind:
                await chat_settings.invalidate_connector(account.id)
            assert (await sync.sync_account(account.id)).status == "ok"
            grant = await chat_settings.update(1, [account.id])
        finally:
            release.set()
        assert (await old).error_code == "auth_expired"
        state = await mail_sync_state.get_state(account.id)
        assert (state.status, state.last_error, state.connector_verified) == ("ok", "", True)
        assert connector_mailbox.decode(state.connector_cursor) == (("rebound" if rebind else "named", "sent", "proof"), "12")
        assert (await chat_settings.get()).version == grant.version
        assert (await chat_settings.get()).account_ids == [account.id]
    asyncio.run(run())


def test_verification_restores_every_eligible_insight_in_bounded_jobs(pg_db, monkeypatch):
    from datetime import UTC, datetime
    from sqlalchemy import select
    from api.mail import connector_mailbox, connectors, insights, jobs, sync_connector
    from api.mail.sync_types import SyncChanges
    from db.accessors import mail_insight, mail_job
    from db.models.mail_insight import MailInsight
    from db.models.mail_job import MailJob

    async def run():
        account = await service.upsert_account(provider="gmail", auth_type=connectors.CONNECTOR, email_address="batch@example.test")
        now = datetime.now(UTC)
        rows = [MailMessage(account_id=account.id, folder="INBOX", provider_uid=str(i), unverified_connector=True,
                            internal_date=now, is_read=False) for i in range(60)]
        async with get_session_maker().begin() as session:
            session.add_all(rows)
            session.add_all([MailInsight(message_id=row.id, points_json='["Old"]', model="synthetic") for row in rows])
        anchor = {"id": "sent", "messages": [{"id": "proof", "sender": account.email_address, "labelIds": ["SENT"]}]}
        monkeypatch.setattr(connector_mailbox, "_credential", AsyncMock(return_value="named"))
        monkeypatch.setattr(connectors, "call_read_tool_json", AsyncMock(side_effect=[{"threads": [anchor]}, anchor, anchor]))
        changes = SyncChanges(cursor="25", upserts=[{"folder": "INBOX", "provider_uid": row.provider_uid,
                              "internal_date": now, "body_text": "Synthetic text", "is_read": False} for row in rows])
        monkeypatch.setattr(sync_connector, "fetch_changes", AsyncMock(return_value=changes))
        monkeypatch.setattr(jobs.mail_env, "mail_insight_batch_limit", 50)
        await jobs._sync({"account_id": account.id})
        ids = [row.id for row in rows]
        assert not await mail_insight.get_for_messages(ids)
        async with get_session_maker()() as session:
            payloads = [json.loads(job.payload_json) for job in (await session.scalars(select(MailJob).where(MailJob.kind == mail_job.VERIFIED_INSIGHTS))).all()]
        assert sorted(len(payload["message_ids"]) for payload in payloads) == [10, 50]
        assert {id for payload in payloads for id in payload["message_ids"]} == set(ids)

        async def summarize(message, **kwargs):
            return await mail_insight.store_if_missing(message.id, points=["Updated"], deadline=now.date(),
                needs_reply=True, followups=[], model="synthetic", summarized=message.body_text)
        monkeypatch.setattr(insights, "compute_insight", summarize)
        for _ in payloads:
            assert await jobs.run_once()
        restored = await mail_insight.get_for_messages(ids)
        assert set(restored) == set(ids)
        assert all(row.needs_reply and row.deadline == now.date() for row in restored.values())
        # A job may previously have skipped quarantined mail while its binding was invalid.
        # Proving the mailbox again must restore an unread message even if its cache is gone.
        await chat_settings.invalidate_connector(account.id)
        await mail_insight.delete_for_account(account.id)
        monkeypatch.setattr(connectors, "call_read_tool_json", AsyncMock(side_effect=[{"threads": [anchor]}, anchor, anchor]))
        await jobs._sync({"account_id": account.id})
        for _ in payloads:
            assert await jobs.run_once()
        assert set(await mail_insight.get_for_messages(ids)) == set(ids)
    asyncio.run(run())


def test_confirmed_mismatch_revokes_grant_while_newer_poll_has_no_proof(pg_db, monkeypatch):
    from api.mail import connector_mailbox, connectors, sync
    from db.accessors import mail_sync_state

    async def run():
        account = await service.upsert_account(provider="outlook", auth_type=connectors.CONNECTOR, email_address="proof@example.test")
        cursor = connector_mailbox.encode(("named", "anchor", "proof"), "15")
        await mail_sync_state.save_state(account_id=account.id, connector_cursor=cursor, connector_verified=True)
        await chat_settings.update(1, [account.id])
        first_read, poll_read, mismatch, timeout = (asyncio.Event() for _ in range(4))

        async def read(*args):
            if asyncio.current_task() is label:
                first_read.set()
                await mismatch.wait()
                return {"id": "anchor", "messages": []}
            poll_read.set()
            await timeout.wait()
            raise httpx.ReadTimeout("synthetic timeout")

        monkeypatch.setattr(connectors, "call_read_tool_json", read)
        write = AsyncMock()
        monkeypatch.setattr(connectors, "_call_tool", write)
        label = asyncio.create_task(connectors.call_write_tool(account, "label_message", {}))
        await asyncio.wait_for(first_read.wait(), 5)
        poll = asyncio.create_task(sync.sync_account(account.id))
        try:
            await asyncio.wait_for(poll_read.wait(), 5)
            mismatch.set()
            with pytest.raises(connector_mailbox.MailboxUnavailable):
                await label
            assert (await chat_settings.get()).account_ids == []
            assert not (await mail_sync_state.get_state(account.id)).connector_verified
        finally:
            mismatch.set()
            timeout.set()
        assert (await poll).status == "error"
        assert (await mail_sync_state.get_state(account.id)).last_error == "auth_expired"
        write.assert_not_awaited()
    asyncio.run(run())


def test_first_verified_page_commits_valid_mail_and_retries_only_the_malformed_thread(pg_db, monkeypatch):
    from api.mail import connector_mailbox, connectors, sync
    from db.accessors import mail_message, mail_sync_state

    async def run():
        account = await service.upsert_account(provider="gmail", auth_type=connectors.CONNECTOR, email_address="upgrade@example.test")
        await mail_sync_state.save_state(account_id=account.id, cursor="15")
        anchor = {"id": "anchor", "messages": [{"id": "proof", "sender": account.email_address, "labelIds": ["SENT"]}]}
        fixed = False
        stamp = "2026-10-01T10:00:00+00:00"
        def mail(uid, date):
            return {"id": uid, "date": date, "labelIds": ["INBOX"]}

        async def read(_, tool, args):
            if args.get("query") == "in:sent from:me":
                return {"threads": [anchor]}
            if args.get("threadId") == "anchor":
                return anchor
            if tool == "search_threads":
                if args["query"] != "in:inbox":
                    return {"threads": []}
                if args.get("pageToken") == "next":
                    return {"threads": [{"id": "last"}]}
                return {"threads": [{"id": "mixed"}], "nextPageToken": "next"}
            if args["threadId"] == "mixed":
                return {"id": "mixed", "messages": [mail("bad", stamp if fixed else None), mail("good", stamp)]}
            return {"id": "last", "messages": [mail("tail", stamp)]}

        monkeypatch.setattr(connector_mailbox, "_credential", AsyncMock(return_value="named"))
        monkeypatch.setattr(connectors, "call_read_tool_json", read)
        monkeypatch.setattr(sync.mail_env, "mail_sync_page_size", 1)
        assert (await sync.sync_account(account.id)).status == "ok"
        state = await mail_sync_state.get_state(account.id)
        assert state.connector_verified and state.connector_deferred_threads == ["mixed"]
        assert set(await mail_message.known_uids(account.id, "INBOX", verified_only=True)) == {"good"}
        await chat_settings.update(1, [account.id])
        assert (await sync.sync_account(account.id)).status == "ok"
        assert (await mail_sync_state.get_state(account.id)).connector_deferred_threads == ["mixed"]
        fixed = True
        assert (await sync.sync_account(account.id)).status == "ok"
        assert (await mail_sync_state.get_state(account.id)).connector_deferred_threads == []
        assert set(await mail_message.known_uids(account.id, "INBOX", verified_only=True)) == {"bad", "good", "tail"}
    asyncio.run(run())


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("refused_thread", ["current", "deferred"])
def test_full_content_refusal_after_successful_listing_marks_sync_expired(pg_db, monkeypatch, status, refused_thread):
    from api.mail import connector_mailbox, connectors, sync
    from db.accessors import mail_message, mail_sync_state
    from db.models.mail_sync_state import MailSyncState

    async def run():
        account = await service.upsert_account(provider="gmail", auth_type=connectors.CONNECTOR, email_address="reader@example.test")
        bound = connector_mailbox.encode(("named", "anchor", "proof"), "15")
        state = await mail_sync_state.save_state(account_id=account.id, cursor="15", connector_cursor=bound, connector_verified=True)
        if refused_thread == "deferred":
            async with get_session_maker().begin() as session:
                stored = await session.get(MailSyncState, state.id)
                stored.connector_deferred_threads = ["deferred"]
        reads = []

        async def provider(_, tool, args):
            if tool == "search_threads":
                return {"threads": [{"id": "current"}]}
            if args["messageFormat"] == "MINIMAL":
                return {"id": "anchor", "messages": [{"id": "proof", "sender": account.email_address, "labelIds": ["SENT"]}]}
            reads.append(args["threadId"])
            if args["threadId"] == refused_thread:
                request = httpx.Request("GET", "https://connector.example.test/thread")
                httpx.Response(status, request=request).raise_for_status()
            return {"id": "current", "messages": [{"id": "fresh", "date": "2026-06-01T11:00:00Z", "labelIds": ["INBOX"]}]}

        monkeypatch.setattr(connectors, "call_read_tool_json", provider)
        result = await sync.sync_account(account.id)
        assert reads == (["current"] if refused_thread == "current" else ["current", "deferred"])
        assert (result.status, result.error_code) == ("error", "auth_expired")
        after = await mail_sync_state.get_state(account.id)
        assert (after.status, after.last_error, after.cursor, after.connector_cursor) == ("error", "auth_expired", "15", bound)
        assert await mail_message.known_uids(account.id, "INBOX") == {}
    asyncio.run(run())


@pytest.mark.parametrize("deferred", [False, True])
def test_listing_labels_survive_reverification_and_schedule_unread_insights(pg_db, monkeypatch, deferred):
    from sqlalchemy import select
    from api.mail import connector_mailbox, connectors, sync
    from db.accessors import mail_job, mail_message, mail_sync_state
    from db.models.mail_job import MailJob
    from db.models.mail_sync_state import MailSyncState

    async def run():
        account = await service.upsert_account(provider="gmail", auth_type=connectors.CONNECTOR, email_address="flags@example.test")
        message = MailMessage(account_id=account.id, folder="INBOX", provider_uid="target", unverified_connector=True)
        async with get_session_maker().begin() as session:
            session.add(message)
        labels = ["INBOX", "UNREAD", "STARRED"]
        anchor = {"id": "sent", "messages": [{"id": "proof", "sender": account.email_address, "labelIds": ["SENT"]}]}
        if deferred:
            bound = connector_mailbox.encode(("named", "sent", "proof"), "15")
            state = await mail_sync_state.save_state(account_id=account.id, cursor="15", connector_cursor=bound, connector_verified=True)
            async with get_session_maker().begin() as session:
                stored = await session.get(MailSyncState, state.id)
                stored.connector_deferred_threads = ["held"]

        async def provider(_, tool, args):
            if args.get("query") == "in:sent from:me":
                return {"threads": [anchor]}
            if args.get("threadId") == "sent":
                return anchor
            if tool == "search_threads":
                matched = args["query"] == "in:inbox" or "before:" in args["query"]
                return {"threads": [{"id": "held", "messages": [{"id": "target", "labelIds": labels}]}] if matched else []}
            return {"id": "held", "messages": [{"id": "target", "date": "2026-10-01T10:00:00Z"}]}

        monkeypatch.setattr(connector_mailbox, "_credential", AsyncMock(return_value="named"))
        monkeypatch.setattr(connectors, "call_read_tool_json", provider)
        assert (await sync.sync_account(account.id)).status == "ok"
        indexed = await mail_message.get_message(message.id)
        assert not indexed.unverified_connector and not indexed.is_read and indexed.is_flagged
        assert json.loads(indexed.labels_json) == labels
        async with get_session_maker()() as session:
            job = (await session.scalars(select(MailJob).where(MailJob.kind == mail_job.VERIFIED_INSIGHTS))).one()
            assert json.loads(job.payload_json)["message_ids"] == [message.id]
    asyncio.run(run())


def test_base_worker_cannot_claim_targeted_refreshes_even_after_expiry_or_retry(pg_db):
    from datetime import UTC, datetime, timedelta
    from sqlalchemy import and_, or_, select
    from db.accessors import mail_job
    from db.models.mail_job import MailJob

    async def legacy_candidates(now):
        async with get_session_maker()() as session:
            return list(await session.scalars(select(MailJob.id).where(or_(
                and_(MailJob.status == "queued", MailJob.run_after <= now),
                and_(MailJob.status == "running", MailJob.locked_at < now - timedelta(minutes=10))))))
    async def run():
        start = datetime.now(UTC) + timedelta(seconds=1)
        async with get_session_maker().begin() as session:
            mail_job.queue_verified_insights(session, "synthetic", ["one"], 50)
        assert await legacy_candidates(start) == []
        job = await mail_job.claim_next(start, stale_after=timedelta(minutes=10))
        assert job.kind == mail_job.VERIFIED_INSIGHTS and job.attempts == 1
        later = start + timedelta(minutes=12)
        assert await legacy_candidates(later) == []
        assert await mail_job.fail(job.id, 1, "temporary", retry_at=later)
        assert await legacy_candidates(later) == []
        retry = await mail_job.claim_next(later, stale_after=timedelta(minutes=10))
        assert retry.id == job.id and retry.attempts == 2
        assert await mail_job.finish(retry.id, retry.attempts)
    asyncio.run(run())


def test_long_sync_renews_its_claim_before_a_competing_poll(pg_db, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from api.mail import jobs
    from db.accessors import mail_job

    async def run():
        now = datetime.now(UTC)
        start = now - timedelta(minutes=12)
        await mail_job.enqueue("sync", {"account_id": "synthetic"}, dedupe_key="sync:synthetic", run_after=start)
        job = await mail_job.claim_next(start, stale_after=timedelta(minutes=10))
        renewed, finish = asyncio.Event(), asyncio.Event()
        original = mail_job.renew
        async def observe_renew(id, attempt):
            held = await original(id, attempt)
            if held:
                renewed.set()
            return held
        async def work(payload):
            await finish.wait()
        # Make the heartbeat observable without waiting a real ten-minute lease.
        monkeypatch.setattr(jobs.mail_env, "mail_sync_stale_minutes", 0.001)
        monkeypatch.setattr(jobs, "_sync", work)
        monkeypatch.setattr(mail_job, "renew", observe_renew)
        active = asyncio.create_task(jobs._sync_with_lease({"account_id": "synthetic"}, job.id, job.attempts))
        try:
            await asyncio.wait_for(renewed.wait(), 5)
            assert await mail_job.claim_next(now, stale_after=timedelta(minutes=10)) is None
            assert not await original(job.id, job.attempts + 1)
        finally:
            finish.set()
            await active
        assert await mail_job.finish(job.id, job.attempts)
        assert not await original(job.id, job.attempts)
    asyncio.run(run())


def test_legacy_gmail_studio_proposal_is_refused_inside_claim(pg_db, monkeypatch):
    from api.mail import action_backends, connectors, connector_mailbox
    from api.routers.api.v1.chat.proposals import router
    from db.accessors import mail_sync_state

    async def run():
        account = await service.upsert_account(provider="gmail", auth_type=connectors.CONNECTOR,
                                               email_address="studio@example.test")
        await mail_sync_state.save_state(account_id=account.id, connector_verified=True,
            connector_cursor=connector_mailbox.encode(("synthetic", "thread", "message"), ""))
        await chat_settings.update(1, [account.id])
        conversation, context, _ = await resolve(account_id=account.id)
        mail = MailMessage(account_id=account.id, provider_uid="message", subject="Synthetic title")
        async with get_session_maker().begin() as session:
            await session.execute(text(
                "SELECT set_config('liam.verified_connector', :account, true)"), {"account": account.id})
            session.add(mail)
        card = await proposals.create_proposal(conversation.id, proposals.Proposal(kind="trash", reason="Test", items=[
            proposals.ProposalItem(id=mail.id, kind="mail", title="Synthetic title", subtitle="")]), context_id=context.id)
        backend = AsyncMock()
        backend.apply.return_value = {}
        monkeypatch.setattr(action_backends, "backend_for", lambda _: backend)
        app = FastAPI()
        app.include_router(router, prefix="/proposals")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
            response = await browser.post(f"/proposals/{card.id}/confirm", json={"selected_ids": [mail.id]})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "provider_action_unsupported"
        assert "Synthetic title" not in response.text and mail.id not in response.text
        assert (await chat_message.get_message(card.id)).proposal_status == "pending"
        backend.apply.assert_not_awaited()

    asyncio.run(run())
