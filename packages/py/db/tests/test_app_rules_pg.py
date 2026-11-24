"""Version conflicts cannot overwrite a newer mail rule."""

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("RUN_PG_CONTRACT"), reason="RUN_PG_CONTRACT is not set")


def test_create_rule_from_message_checks_source_and_verified_alias_in_same_transaction(pg_db):
    async def run():
        import httpx
        from fastapi import FastAPI
        from api.routers.api.v1.mail import rules
        from db.accessors import mail_account, mail_filing, mail_message, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount
        from db.models.mail_message import MailMessage

        account = MailAccount(id='synthetic', provider='imap', auth_type='imap_password', email_address='owner@example.org')
        async with get_session_maker().begin() as session:
            session.add(account)
        folder = await mail_filing.create_destination('Projects', 'folder')
        message = await mail_message.upsert_message(account_id=account.id, folder='INBOX', provider_uid='one')
        async with get_session_maker().begin() as session:
            row = await session.get(MailMessage, message.id)
            row.connector_alias_ids = ['old-local-id']; row.unverified_connector = False
        app = FastAPI(); app.include_router(rules.router, prefix='/rules')
        body = {'source_message_id': 'old-local-id', 'source_account_id': account.id, 'name': 'Example',
                'account_id': account.id, 'sender_domain': 'example.org', 'folder_id': folder.id}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.post('/rules/from-message', json=body | {'source_account_id': 'wrong'})).status_code == 404
            assert (await client.post('/rules/from-message', json=body | {'source_message_id': 'missing'})).status_code == 404
            assert await mail_rule.list_rules() == []
            created = await client.post('/rules/from-message', json=body)
            assert created.status_code == 200
            assert created.json()['account_id'] == account.id
            assert len(await mail_rule.list_rules()) == 1
            await mail_account.delete_account(account.id)
            assert (await client.post('/rules/from-message', json=body | {'account_id': None})).status_code == 404
            assert await mail_rule.list_rules() == []

    asyncio.run(run())


def test_first_import_skips_rules_but_later_delivery_runs_and_reconnect_notifies(pg_db, monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        from sqlalchemy import select
        from api.mail import actions, jobs, sync, sync_imap
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_message, mail_rule, mail_rule_run, mail_sync_state
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount
        from db.models.mail_job import MailJob

        account = MailAccount(id='account-a', provider='imap', auth_type='imap_password', email_address='a@example.test')
        async with get_session_maker().begin() as session:
            session.add(account)
        folder = await mail_filing.create_destination('Projects', 'folder')
        rule = await mail_rule.create(name='File', folder_id=folder.id, mark_read=True)
        batches = [
            SyncChanges(full_rebuild=True, cursor='7:1', upserts=[{'folder': 'INBOX', 'provider_uid': '1', 'imap_uidvalidity': '7'}]),
            SyncChanges(cursor='7:2', upserts=[{'folder': 'INBOX', 'provider_uid': '2', 'imap_uidvalidity': '7'}]),
            SyncChanges(full_rebuild=True, cursor='8:1', upserts=[{'folder': 'INBOX', 'provider_uid': '1', 'imap_uidvalidity': '8'}]),
        ]
        fetch = AsyncMock(side_effect=batches)
        monkeypatch.setattr(sync_imap, 'fetch_changes', fetch)
        monkeypatch.setattr(sync, '_check_native_returns', AsyncMock(return_value={}))
        monkeypatch.setattr(sync, 'refresh_bodies', AsyncMock(return_value=[]))
        monkeypatch.setattr(sync, 'refresh_html_bodies', AsyncMock())
        write = AsyncMock()
        monkeypatch.setattr(actions, 'apply_action', write)
        first = await sync.sync_account(account.id)
        assert first.status == 'ok' and len(first.new_message_ids) == 1  # Insight scheduling remains unchanged.
        async with get_session_maker()() as session:
            assert not (await session.scalars(select(MailJob).where(MailJob.kind == 'mail_rule_apply'))).all()
        assert not await jobs.run_once()
        write.assert_not_awaited()
        runs = await mail_rule_run.list_for_rule(rule.id)
        assert len(runs) == 1 and runs[0].status == 'skipped_initial_import' and runs[0].account_id == account.id
        second = await sync.sync_account(account.id)
        assert second.status == 'ok' and len(second.new_message_ids) == 1
        assert await jobs.run_once()
        write.assert_awaited_once_with(second.new_message_ids[0], 'mark_read')
        assert (await mail_filing.for_message(first.new_message_ids[0])).folder_id is None
        await mail_sync_state.save_state(account_id=account.id, cursor='')
        third = await sync.sync_account(account.id)
        assert third.status == 'ok' and len(third.new_message_ids) == 1
        assert not await jobs.run_once()
        assert write.await_count == 1
        runs = await mail_rule_run.list_for_rule(rule.id)
        notices = [row for row in runs if row.status == 'skipped_initial_import']
        assert len(notices) == 2 and len({row.message_id for row in notices}) == 2
    asyncio.run(run())


def test_graph_initial_pages_share_one_notice_and_skip_rules(pg_db, monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        from sqlalchemy import select
        from api.mail import actions, jobs, sync, sync_graph
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount
        from db.models.mail_job import MailJob
        account = MailAccount(id='account-a', provider='microsoft', auth_type='oauth2', email_address='a@example.test')
        async with get_session_maker().begin() as session:
            session.add(account)
        folder = await mail_filing.create_destination('Projects', 'folder')
        rule = await mail_rule.create(name='File', folder_id=folder.id, mark_read=True)
        monkeypatch.setattr(sync_graph, 'fetch_changes', AsyncMock(side_effect=[
            SyncChanges(full_rebuild=True, initial_pending=True, cursor='next-page', upserts=[{'folder': 'INBOX', 'provider_uid': 'old-1'}]),
            SyncChanges(cursor='delta', upserts=[{'folder': 'INBOX', 'provider_uid': 'old-2'}]),
            SyncChanges(cursor='delta-2', upserts=[{'folder': 'INBOX', 'provider_uid': 'new'}]),
        ]))
        monkeypatch.setattr(sync, '_check_native_returns', AsyncMock(return_value={}))
        monkeypatch.setattr(sync, 'refresh_bodies', AsyncMock(return_value=[]))
        monkeypatch.setattr(sync, 'refresh_html_bodies', AsyncMock())
        write = AsyncMock()
        monkeypatch.setattr(actions, 'apply_action', write)
        first = await sync.sync_account(account.id)
        rule = await mail_rule.update(rule.id, rule.version, name='File updated')
        second = await sync.sync_account(account.id)
        assert first.status == second.status == 'ok'
        assert len(first.new_message_ids) == len(second.new_message_ids) == 1
        async with get_session_maker()() as session:
            assert not (await session.scalars(select(MailJob).where(MailJob.kind == 'mail_rule_apply'))).all()
        assert not await jobs.run_once()
        write.assert_not_awaited()
        notices = await mail_rule_run.list_for_rule(rule.id)
        assert len(notices) == 1 and notices[0].status == 'skipped_initial_import'
        third = await sync.sync_account(account.id)
        assert third.status == 'ok' and len(third.new_message_ids) == 1
        assert await jobs.run_once()
        write.assert_awaited_once_with(third.new_message_ids[0], 'mark_read')
        assert len([row for row in await mail_rule_run.list_for_rule(rule.id) if row.status == 'skipped_initial_import']) == 1
    asyncio.run(run())


def test_graph_partial_import_resumes_after_failure_without_running_history(pg_db, monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        from api.mail import actions, jobs, sync, sync_graph
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_rule, mail_rule_run, mail_sync_state
        from db.engine import dispose_engine, get_session_maker
        from db.models.mail_account import MailAccount
        account = MailAccount(id='account-a', provider='microsoft', auth_type='oauth2', email_address='a@example.test')
        async with get_session_maker().begin() as session:
            session.add(account)
        folder = await mail_filing.create_destination('Projects', 'folder')
        rule = await mail_rule.create(name='File', folder_id=folder.id, mark_read=True)
        monkeypatch.setattr(sync_graph, 'fetch_changes', AsyncMock(side_effect=[
            SyncChanges(full_rebuild=True, initial_pending=True, cursor='next-page', upserts=[{'folder': 'INBOX', 'provider_uid': 'old-1'}]),
            OSError('synthetic interruption'),
            SyncChanges(cursor='delta', upserts=[{'folder': 'INBOX', 'provider_uid': 'old-2'}]),
            SyncChanges(cursor='delta-2', upserts=[{'folder': 'INBOX', 'provider_uid': 'new'}]),
        ]))
        monkeypatch.setattr(sync, '_check_native_returns', AsyncMock(return_value={}))
        monkeypatch.setattr(sync, 'refresh_bodies', AsyncMock(return_value=[]))
        monkeypatch.setattr(sync, 'refresh_html_bodies', AsyncMock())
        write = AsyncMock()
        monkeypatch.setattr(actions, 'apply_action', write)
        assert (await sync.sync_account(account.id)).status == 'ok'
        before = await mail_sync_state.get_state(account.id)
        assert before.cursor == 'next-page' and before.rules_initial_import
        assert (await sync.sync_account(account.id)).status == 'error'
        await dispose_engine()  # Reopen the pool to prove the import state is persisted.
        after = await mail_sync_state.get_state(account.id)
        assert (after.cursor, after.rules_initial_import) == (before.cursor, before.rules_initial_import)
        resumed = await sync.sync_account(account.id)
        assert resumed.status == 'ok' and len(resumed.new_message_ids) == 1
        assert not await jobs.run_once()
        write.assert_not_awaited()
        runs = await mail_rule_run.list_for_rule(rule.id)
        assert len(runs) == 1 and runs[0].status == 'skipped_initial_import'
        delivery = await sync.sync_account(account.id)
        assert await jobs.run_once()
        write.assert_awaited_once_with(delivery.new_message_ids[0], 'mark_read')
    asyncio.run(run())


def test_connector_deferred_initial_history_is_skipped_but_new_mail_runs(pg_db, monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        from api.mail import actions, jobs, sync, sync_connector
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_rule, mail_rule_run
        from db.engine import dispose_engine, get_session_maker
        from db.models.mail_account import MailAccount
        account = MailAccount(id='account-a', provider='gmail', auth_type='mistral_connector', email_address='example@connector.mistral')
        async with get_session_maker().begin() as session:
            session.add(account)
        folder = await mail_filing.create_destination('Projects', 'folder')
        rule = await mail_rule.create(name='File', folder_id=folder.id, mark_read=True)
        monkeypatch.setattr(sync_connector, 'fetch_changes', AsyncMock(side_effect=[
            SyncChanges(cursor='initial:[100,"next"]', deferred_threads=['old-thread'], upserts=[{'folder': 'INBOX', 'provider_uid': 'old-1', 'thread_id': 'old-1-thread'}]),
            SyncChanges(cursor='101:90:100', initial_history_uids={'old-3'}, upserts=[
                {'folder': 'INBOX', 'provider_uid': 'new-1', 'thread_id': 'new-thread'},
                {'folder': 'INBOX', 'provider_uid': 'old-3', 'thread_id': 'old-3-thread'},
                {'folder': 'INBOX', 'provider_uid': 'old-2', 'thread_id': 'old-thread', 'labels_json': None},
            ]),
            SyncChanges(cursor='102', upserts=[{'folder': 'INBOX', 'provider_uid': 'mixed-gap', 'thread_id': 'mixed-gap-thread'}]),
            SyncChanges(cursor='103', upserts=[{'folder': 'INBOX', 'provider_uid': 'new-2', 'thread_id': 'new-2-thread'}]),
        ]))
        monkeypatch.setattr(sync_connector, 'retry_threads', AsyncMock(return_value=SyncChanges(upserts=[
            {'folder': 'INBOX', 'provider_uid': 'old-2', 'thread_id': 'old-thread', 'labels_json': None},
        ])))
        monkeypatch.setattr(sync, 'refresh_bodies', AsyncMock(return_value=[]))
        monkeypatch.setattr(sync, 'refresh_html_bodies', AsyncMock())
        write = AsyncMock()
        monkeypatch.setattr(actions, 'apply_action', write)
        first = await sync.sync_account(account.id)
        assert first.status == 'ok' and len(first.new_message_ids) == 1
        assert not await jobs.run_once()
        await dispose_engine()  # Resume historical pagination with the persisted import marker.
        second = await sync.sync_account(account.id)
        assert second.status == 'ok' and len(second.new_message_ids) == 3
        assert not await jobs.run_once()
        third = await sync.sync_account(account.id)
        assert third.status == 'ok' and len(third.new_message_ids) == 1
        assert not await jobs.run_once()
        fourth = await sync.sync_account(account.id)
        assert fourth.status == 'ok' and len(fourth.new_message_ids) == 1
        assert await jobs.run_once()
        assert not await jobs.run_once()
        write.assert_not_awaited()  # Studio Gmail has no provider write capability.
        runs = await mail_rule_run.list_for_rule(rule.id)
        notices = [row for row in runs if row.status == 'skipped_initial_import']
        assert len(notices) == 1 and notices[0].account_id == account.id
        applied_ids = {row.message_id for row in runs if row.status == 'done'}
        assert applied_ids == {fourth.new_message_ids[0]}
        for message_id in set(first.new_message_ids + second.new_message_ids + third.new_message_ids) - applied_ids:
            assert (await mail_filing.for_message(message_id)).folder_id is None
    asyncio.run(run())


def test_connector_mixed_import_does_not_file_a_delta_item_without_delivery_proof(pg_db, monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        from api.mail import actions, jobs, sync, sync_connector
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount
        account = MailAccount(id='account-a', provider='gmail', auth_type='mistral_connector', email_address='example@connector.mistral')
        async with get_session_maker().begin() as session:
            session.add(account)
        folder = await mail_filing.create_destination('Projects', 'folder')
        rule = await mail_rule.create(name='File', folder_id=folder.id, mark_read=True)
        monkeypatch.setattr(sync_connector, 'fetch_changes', AsyncMock(side_effect=[
            SyncChanges(cursor='initial:[100,"next"]', upserts=[{'folder': 'INBOX', 'provider_uid': 'old-1', 'thread_id': 'old-1'}]),
            # Delta-only here means absent from this historical page, not necessarily newly delivered.
            SyncChanges(cursor='101', upserts=[{'folder': 'INBOX', 'provider_uid': 'ambiguous', 'thread_id': 'not-on-this-history-page'}]),
        ]))
        monkeypatch.setattr(sync, 'refresh_bodies', AsyncMock(return_value=[]))
        monkeypatch.setattr(sync, 'refresh_html_bodies', AsyncMock())
        write = AsyncMock()
        monkeypatch.setattr(actions, 'apply_action', write)
        assert (await sync.sync_account(account.id)).status == 'ok'
        resumed = await sync.sync_account(account.id)
        assert resumed.status == 'ok' and len(resumed.new_message_ids) == 1
        assert not await jobs.run_once()
        write.assert_not_awaited()
        assert (await mail_filing.for_message(resumed.new_message_ids[0])).folder_id is None
        runs = await mail_rule_run.list_for_rule(rule.id)
        assert len(runs) == 1 and runs[0].status == 'skipped_initial_import'
    asyncio.run(run())


def test_connector_steady_state_gap_is_not_suppressed_by_deferred_history(pg_db, monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        from api.mail import jobs, sync, sync_connector
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount
        account = MailAccount(id='account-a', provider='gmail', auth_type='mistral_connector', email_address='example@connector.mistral')
        async with get_session_maker().begin() as session:
            session.add(account)
        folder = await mail_filing.create_destination('Projects', 'folder')
        rule = await mail_rule.create(name='File', folder_id=folder.id)
        monkeypatch.setattr(sync_connector, 'fetch_changes', AsyncMock(side_effect=[
            SyncChanges(cursor='100', deferred_threads=['old-thread'], upserts=[{'folder': 'INBOX', 'provider_uid': 'old-1', 'thread_id': 'old-1'}]),
            SyncChanges(cursor='101:90:100', upserts=[{'folder': 'INBOX', 'provider_uid': 'new-1', 'thread_id': 'new-1'}]),
            SyncChanges(cursor='102', upserts=[{'folder': 'INBOX', 'provider_uid': 'new-2', 'thread_id': 'new-2'}]),
        ]))
        monkeypatch.setattr(sync_connector, 'retry_threads', AsyncMock(return_value=SyncChanges(deferred_threads=['old-thread'])))
        monkeypatch.setattr(sync, 'refresh_bodies', AsyncMock(return_value=[]))
        monkeypatch.setattr(sync, 'refresh_html_bodies', AsyncMock())
        assert (await sync.sync_account(account.id)).status == 'ok'
        assert not await jobs.run_once()
        second = await sync.sync_account(account.id)
        assert second.status == 'ok' and await jobs.run_once()
        third = await sync.sync_account(account.id)
        assert third.status == 'ok' and await jobs.run_once()
        runs = await mail_rule_run.list_for_rule(rule.id)
        assert {row.message_id for row in runs if row.status == 'done'} == {second.new_message_ids[0], third.new_message_ids[0]}
        assert len([row for row in runs if row.status == 'skipped_initial_import']) == 1
    asyncio.run(run())


def test_stale_rule_update_is_rejected(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_rule

        folder = await mail_filing.create_destination("Projects", "folder")
        rule = await mail_rule.create(name="File projects", folder_id=folder.id)
        updated = await mail_rule.update(rule.id, 1, name="File work")
        assert updated.version == 2
        with pytest.raises(mail_rule.VersionConflict):
            await mail_rule.update(rule.id, 1, name="Stale change")
        current = await mail_rule.get(rule.id)
        assert current.name == "File work"
        assert current.version == 2

    asyncio.run(run())


def test_manual_apply_recovers_interrupted_provider_action_as_unknown(pg_db, monkeypatch):
    async def run():
        import json
        from datetime import timedelta
        from unittest.mock import AsyncMock
        from api.mail import actions, jobs, rules
        from api.routers.api.v1.mail.rules import apply_rule, ApplyIn
        from db.accessors import mail_filing, mail_job, mail_message, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models._common import utcnow
        from db.models.mail_account import MailAccount
        account = MailAccount(id='account-a', provider='google', auth_type='oauth2', email_address='a@example.test')
        async with get_session_maker().begin() as session:
            session.add(account)
        folder = await mail_filing.create_destination('Projects', 'folder')
        message = await mail_message.upsert_message(account_id=account.id, folder='INBOX', provider_uid='uid', sender_address='a@example.test')
        rule = await mail_rule.create(name='File', sender_domain='example.test', folder_id=folder.id, mark_read=True)
        assert (await apply_rule(rule.id, ApplyIn(expected_version=rule.version, confirm_apply=True)))['matched'] == 1
        job = await mail_job.claim_next(utcnow(), stale_after=timedelta(minutes=5))
        assert job and job.kind == 'mail_rule_apply'
        run = await rules.prepare_rule_run(rule.id, rule.version, message.id, snapshot={rule.id: rule.version})
        run, claimed = await mail_rule_run.record_action(run.id, 'mark_read', expected='pending', result='dispatching')
        assert claimed
        write = AsyncMock()
        monkeypatch.setattr(actions, 'apply_action', write)
        assert await jobs.run_once(utcnow() + timedelta(hours=1))
        recovered = (await mail_rule_run.list_for_rule(rule.id))[0]
        assert recovered.status == 'unknown'
        assert json.loads(recovered.provider_results_json)['mark_read'] == 'unknown'
        write.assert_not_awaited()
    asyncio.run(run())


def test_studio_skipped_seed_run_is_repeatable(pg_db):
    async def run():
        import json
        from api.mail import dev_seed
        from db.accessors import mail_rule, mail_rule_run
        await dev_seed.seed_filing()
        await dev_seed.seed_filing()
        rule = await mail_rule.get(dev_seed.seed_id('rule', 'studio-skip'))
        runs = await mail_rule_run.list_for_rule(rule.id)
        assert rule.account_id == dev_seed.seed_id('account', 'privat') and rule.mark_read
        assert len(runs) == 1 and json.loads(runs[0].provider_results_json) == {'mark_read': 'skipped_unsupported'}
    asyncio.run(run())


def test_lower_priority_folder_reports_override_but_applies_label(pg_db):
    async def run():
        import json
        from api.mail import rules
        from db.accessors import mail_filing, mail_message, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount
        account = MailAccount(id='account-a', provider='gmail', auth_type='mistral_connector', email_address='a@example.test')
        async with get_session_maker().begin() as session:
            session.add(account)
        message = await mail_message.upsert_message(account_id=account.id, folder='INBOX', provider_uid='uid')
        first = await mail_filing.create_destination('First', 'folder')
        second = await mail_filing.create_destination('Second', 'folder')
        label = await mail_filing.create_destination('Work', 'label')
        await mail_rule.create(name='Higher', position=1, folder_id=first.id)
        lower = await mail_rule.create(name='Lower', position=2, folder_id=second.id, label_ids=[label.id], mark_read=True)
        result = await rules.execute_rule_run(lower.id, lower.version, message.id)
        assert result.status == 'done'
        assert json.loads(result.provider_results_json) == {'_folder': 'overridden', 'mark_read': 'skipped_unsupported'}
        filing = await mail_filing.for_message(message.id)
        assert filing.folder_id is None and filing.label_ids == [label.id]
    asyncio.run(run())


def test_rule_run_is_deduplicated_for_each_version_and_message(pg_db):
    async def run():
        from db.accessors import mail_rule_run
        first, created = await mail_rule_run.create_once("rule-a", 1, "mail-a", "account-a")
        again, duplicate = await mail_rule_run.create_once("rule-a", 1, "mail-a", "account-a")
        newer, new_version = await mail_rule_run.create_once("rule-a", 2, "mail-a", "account-a")
        assert created and not duplicate and new_version
        assert first.id == again.id
        assert newer.id != first.id

    asyncio.run(run())


def test_unsupported_provider_action_is_skipped_without_losing_filing(pg_db, monkeypatch):
    async def run():
        import json
        from unittest.mock import AsyncMock
        from api.mail import actions, rules
        from db.accessors import mail_filing, mail_message, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(id="account-a", provider="gmail", auth_type="mistral_connector", email_address="owner@example.test")
        async with get_session_maker().begin() as session:
            session.add(account)
        message = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="uid")
        folder = await mail_filing.create_destination("Projects", "folder")
        rule = await mail_rule.create(name="Project", folder_id=folder.id, mark_read=True)
        send = AsyncMock()
        monkeypatch.setattr(actions, "apply_action", send)
        result = await rules.execute_rule_run(rule.id, rule.version, message.id)
        assert (await mail_filing.for_message(message.id)).folder_id == folder.id
        assert result.status == "done"
        assert json.loads(result.provider_results_json)["mark_read"] == "skipped_unsupported"
        send.assert_not_awaited()
        again = await rules.execute_rule_run(rule.id, rule.version, message.id)
        assert again.id == result.id
        send.assert_not_awaited()

    asyncio.run(run())


def test_concurrent_rule_delivery_does_not_repeat_provider_write(pg_db, monkeypatch):
    async def run():
        from api.mail import actions, rules
        from db.accessors import mail_message, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(id="account-a", provider="google", auth_type="oauth2", email_address="owner@example.test")
        async with get_session_maker().begin() as session:
            session.add(account)
        message = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="uid")
        rule = await mail_rule.create(name="Read", mark_read=True)
        entered, resume = asyncio.Event(), asyncio.Event()
        calls = []
        async def write(message_id, kind):
            calls.append((message_id, kind))
            entered.set()
            await resume.wait()
        monkeypatch.setattr(actions, "apply_action", write)
        first = asyncio.create_task(rules.execute_rule_run(rule.id, rule.version, message.id))
        await entered.wait()
        await rules.execute_rule_run(rule.id, rule.version, message.id)
        resume.set()
        result = await first
        assert calls == [(message.id, "mark_read")]
        assert result.status == "done"

    asyncio.run(run())


def test_uncertain_provider_write_is_not_retried(pg_db, monkeypatch):
    async def run():
        import json
        from unittest.mock import AsyncMock
        from api.mail import actions, rules
        from db.accessors import mail_message, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(id="account-a", provider="google", auth_type="oauth2", email_address="owner@example.test")
        async with get_session_maker().begin() as session:
            session.add(account)
        message = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="uid")
        rule = await mail_rule.create(name="Read", mark_read=True)
        write = AsyncMock(side_effect=TimeoutError())
        monkeypatch.setattr(actions, "apply_action", write)
        first = await rules.execute_rule_run(rule.id, rule.version, message.id)
        second = await rules.execute_rule_run(rule.id, rule.version, message.id, recover=True)
        assert first.status == second.status == "unknown"
        assert json.loads(second.provider_results_json)["mark_read"] == "unknown"
        write.assert_awaited_once()

    asyncio.run(run())


def test_sync_enqueues_durable_versioned_rule_job(pg_db):
    async def run():
        from sqlalchemy import select
        from api.mail import jobs, sync
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_message, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount
        from db.models.mail_job import MailJob

        account = MailAccount(id="account-a", provider="google", auth_type="oauth2", email_address="owner@example.test")
        async with get_session_maker().begin() as session:
            session.add(account)
        folder = await mail_filing.create_destination("Projects", "folder")
        await mail_rule.create(name="File", sender_domain="example.test", folder_id=folder.id)
        changes = SyncChanges(cursor="next", upserts=[{"folder": "INBOX", "provider_uid": "uid", "sender_address": "ada@example.test"}])
        async with get_session_maker().begin() as session:
            ids, _ = await sync._apply_changes(account, changes, session, False, {})
        async with get_session_maker()() as session:
            queued = (await session.scalars(select(MailJob).where(MailJob.kind == "mail_rule_apply"))).all()
        assert len(queued) == 1
        assert await jobs.run_once()
        assert (await mail_filing.for_message(ids[0])).folder_id == folder.id
        assert (await mail_message.get_message(ids[0])).folder == "INBOX"

    asyncio.run(run())


def test_rule_labels_accumulate_before_provider_archive(pg_db, monkeypatch):
    async def run():
        from api.mail import actions, jobs, sync
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_message, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(id="account-a", provider="google", auth_type="oauth2", email_address="owner@example.test")
        async with get_session_maker().begin() as session:
            session.add(account)
        folder = await mail_filing.create_destination("Projects", "folder")
        first = await mail_filing.create_destination("One", "label")
        second = await mail_filing.create_destination("Two", "label")
        third = await mail_filing.create_destination("Three", "label")
        await mail_rule.create(name="First", position=1, folder_id=folder.id, label_ids=[first.id, second.id], archive=True)
        await mail_rule.create(name="Second", position=2, label_ids=[second.id, third.id])
        async def archive(message_id, kind):
            assert kind == "archive"
            await mail_message.set_state(message_id, folder="ARCHIVE")
        monkeypatch.setattr(actions, "apply_action", archive)
        changes = SyncChanges(cursor="next", upserts=[{"folder": "INBOX", "provider_uid": "uid"}])
        async with get_session_maker().begin() as session:
            ids, _ = await sync._apply_changes(account, changes, session, False, {})
        assert await jobs.run_once()
        filing = await mail_filing.for_message(ids[0])
        assert filing.folder_id == folder.id
        assert set(filing.label_ids) == {first.id, second.id, third.id}

    asyncio.run(run())


def test_stale_rule_job_is_skipped_before_local_or_provider_changes(pg_db, monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        from api.mail import actions, rules
        from db.accessors import mail_filing, mail_message, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(id="account-a", provider="google", auth_type="oauth2", email_address="owner@example.test")
        async with get_session_maker().begin() as session:
            session.add(account)
        message = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="uid")
        folder = await mail_filing.create_destination("Projects", "folder")
        rule = await mail_rule.create(name="File", folder_id=folder.id, mark_read=True)
        await mail_rule.update(rule.id, rule.version, name="Updated")
        write = AsyncMock()
        monkeypatch.setattr(actions, "apply_action", write)
        result = await rules.execute_rule_run(rule.id, 1, message.id)
        assert result.status == "skipped_stale"
        assert (await mail_filing.for_message(message.id)).folder_id is None
        write.assert_not_awaited()

    asyncio.run(run())


def test_mark_unread_is_skipped_for_unsupported_connector(pg_db, monkeypatch):
    async def run():
        import json
        from unittest.mock import AsyncMock
        from api.mail import actions, rules
        from db.accessors import mail_message, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(id="account-a", provider="gmail", auth_type="mistral_connector", email_address="owner@example.test")
        async with get_session_maker().begin() as session:
            session.add(account)
        message = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="uid", is_read=True)
        rule = await mail_rule.create(name="Unread", mark_unread=True)
        write = AsyncMock()
        monkeypatch.setattr(actions, "apply_action", write)
        result = await rules.execute_rule_run(rule.id, rule.version, message.id)
        assert json.loads(result.provider_results_json)["mark_unread"] == "skipped_unsupported"
        write.assert_not_awaited()

    asyncio.run(run())


def test_rule_api_rejects_stale_edit_and_unconfirmed_bulk_apply(pg_db):
    async def run():
        import httpx
        from fastapi import FastAPI
        from api.routers.api.v1.mail import rules as routes
        from db.accessors import mail_filing

        app = FastAPI()
        app.include_router(routes.router, prefix="/rules")
        folder = await mail_filing.create_destination("Projects", "folder")
        body = {"name": "File", "sender_domain": "example.test", "folder_id": folder.id}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=httpx.URL(scheme="http", host="test")) as client:
            created = await client.post("/rules", json=body)
            assert created.status_code == 200
            rule = created.json()
            edited = await client.patch(f"/rules/{rule['id']}", json=body | {"expected_version": 1, "name": "Updated"})
            assert edited.status_code == 200 and edited.json()["version"] == 2
            stale = await client.patch(f"/rules/{rule['id']}", json=body | {"expected_version": 1})
            assert stale.status_code == 409
            apply = await client.post(f"/rules/{rule['id']}/apply", json={"expected_version": 2, "confirm_apply": False})
            assert apply.status_code == 422

    asyncio.run(run())


def test_rule_edit_before_dispatch_skips_provider_action(pg_db, monkeypatch):
    async def run():
        import json
        from unittest.mock import AsyncMock
        from api.mail import actions, rules
        from db.accessors import mail_message, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(id="account-a", provider="google", auth_type="oauth2", email_address="owner@example.test")
        async with get_session_maker().begin() as session:
            session.add(account)
        message = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="uid")
        rule = await mail_rule.create(name="Read", mark_read=True)
        run = await rules.prepare_rule_run(rule.id, rule.version, message.id)
        await mail_rule.update(rule.id, rule.version, name="Changed")
        write = AsyncMock()
        monkeypatch.setattr(actions, "apply_action", write)
        result = await rules.dispatch_rule_run(run)
        assert json.loads(result.provider_results_json)["mark_read"] == "skipped_stale"
        write.assert_not_awaited()

    asyncio.run(run())


def test_new_rule_cannot_steal_a_queued_versioned_folder_assignment(pg_db):
    async def run():
        from api.mail import jobs, sync
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(id='account-a', provider='google', auth_type='oauth2', email_address='owner@example.test')
        async with get_session_maker().begin() as session:
            session.add(account)
        first = await mail_filing.create_destination('First', 'folder')
        later = await mail_filing.create_destination('Later', 'folder')
        await mail_rule.create(name='Original', position=10, folder_id=first.id)
        async with get_session_maker().begin() as session:
            ids, _ = await sync._apply_changes(account, SyncChanges(cursor='next', upserts=[{'folder': 'INBOX', 'provider_uid': 'uid'}]), session, False, {})
        await mail_rule.create(name='New high priority', position=0, folder_id=later.id)
        assert await jobs.run_once()
        assert (await mail_filing.for_message(ids[0])).folder_id == first.id

    asyncio.run(run())


def test_rule_skips_paused_verified_gmail_binding_without_retrying_provider(pg_db, monkeypatch):
    async def run():
        import json
        from unittest.mock import AsyncMock
        from api.mail import actions, connector_mailbox, rules, service
        from db.accessors import mail_message, mail_rule, mail_sync_state
        from db.engine import get_session_maker
        from db.models.mail_message import MailMessage
        from sqlalchemy import text

        account = await service.upsert_account(provider="gmail", auth_type="mistral_connector",
                                               email_address="rule@example.test")
        state = await mail_sync_state.save_state(account_id=account.id, connector_verified=True,
            connector_cursor=connector_mailbox.encode(("rule-pin", "thread", "proof"), ""))
        message = MailMessage(account_id=account.id, provider_uid="uid", folder="INBOX")
        async with get_session_maker().begin() as session:
            await session.execute(text("SELECT set_config('liam.verified_connector', :account, true)"),
                                  {"account": account.id})
            session.add(message)
        rule = await mail_rule.create(name="Read", mark_read=True)
        write = AsyncMock()
        monkeypatch.setattr(actions, "apply_action", write)
        await mail_sync_state.record_connector_write(account.id, state.write_evidence()[:2], outcome="blocked")
        first = await rules.execute_rule_run(rule.id, rule.version, message.id)
        assert first.status == "done"
        assert json.loads(first.provider_results_json) == {"mark_read": "skipped_permission"}
        assert not (await mail_message.get_message(message.id)).is_read
        assert await mail_sync_state.retry_connector_writes(account.id)
        assert (await rules.execute_rule_run(rule.id, rule.version, message.id)).id == first.id
        write.assert_not_awaited()
    asyncio.run(run())


def test_multiple_rule_labels_are_applied_and_deletion_updates_versions(pg_db):
    async def run():
        from api.mail import rules
        from db.accessors import mail_filing, mail_message, mail_rule
        first = await mail_filing.create_destination('First', 'label')
        second = await mail_filing.create_destination('Second', 'label')
        folder = await mail_filing.create_destination('Folder', 'folder')
        message = await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid='one')
        rule = await mail_rule.create(name='Labels', folder_id=folder.id, label_ids=[first.id, second.id, first.id])
        assert rule.label_ids == [first.id, second.id]
        result = await rules.execute_rule_run(rule.id, rule.version, message.id)
        assert result.status == 'done'
        assert set((await mail_filing.for_message(message.id)).label_ids) == {first.id, second.id}
        await mail_filing.delete_destination(first.id)
        changed = await mail_rule.get(rule.id)
        assert changed.label_ids == [second.id] and changed.version == rule.version + 1
        with pytest.raises(mail_rule.VersionConflict):
            await mail_rule.update(rule.id, rule.version, label_ids=[second.id])
        await mail_filing.delete_destination(second.id)
        changed = await mail_rule.get(rule.id)
        assert changed.label_ids == [] and changed.is_active
        only = await mail_filing.create_destination('Only', 'label')
        last = await mail_rule.create(name='Only label', label_ids=[only.id])
        await mail_filing.delete_destination(only.id)
        changed = await mail_rule.get(last.id)
        assert changed.label_ids == [] and not changed.is_active
        with pytest.raises(ValueError):
            await mail_filing.delete_destination(folder.id)
    asyncio.run(run())


def test_rule_label_migration_carries_existing_values(pg_db):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from db.accessors import mail_rule
    from db.engine import dispose_engine, get_session_maker

    async def seed():
        from db.accessors import mail_filing
        label = await mail_filing.create_destination('Legacy', 'label')
        return (await mail_rule.create(name='Legacy', label_ids=[label.id])).id, label.id

    async def clear_legacy(rule_id):
        async with get_session_maker().begin() as session:
            assert (await session.execute(text('SELECT label_id FROM mail_rule WHERE id = :id'), {'id': rule_id})).scalar_one()
            await session.execute(text('UPDATE mail_rule SET label_id = NULL WHERE id = :id'), {'id': rule_id})

    async def verify(rule_id, expected):
        rule = await mail_rule.get(rule_id)
        assert rule.label_ids == expected

    async def replace_labels(rule_id, labels):
        async with get_session_maker().begin() as session:
            from db.models.mail_rule import MailRule
            rule = await session.get(MailRule, rule_id)
            rule.label_ids = labels

    async def verify_and_replace(rule_id, expected, labels):
        await verify(rule_id, expected)
        await replace_labels(rule_id, labels)

    project = Path(__file__).resolve().parents[1]
    config = Config(str(project / 'alembic.ini'))
    config.set_main_option('script_location', str(project / 'src/db/migrations'))
    rule_id, label_id = asyncio.run(seed())
    asyncio.run(dispose_engine())
    command.downgrade(config, '6f7221132711')
    asyncio.run(dispose_engine())
    command.upgrade(config, 'heads')
    asyncio.run(verify_and_replace(rule_id, [label_id], [label_id, 'another-synthetic-label']))
    asyncio.run(dispose_engine())
    with pytest.raises(Exception, match='Cannot downgrade rules with multiple labels'):
        command.downgrade(config, '6f7221132711')
    asyncio.run(verify_and_replace(rule_id, [label_id, 'another-synthetic-label'], [label_id]))
    asyncio.run(dispose_engine())
    command.downgrade(config, '6f7221132711')
    asyncio.run(clear_legacy(rule_id))
    asyncio.run(dispose_engine())
    command.upgrade(config, 'heads')
    asyncio.run(verify(rule_id, []))


def test_rule_api_validates_multiple_destinations_and_versions(pg_db):
    async def run():
        import httpx
        from fastapi import FastAPI
        from api.routers.api.v1.mail import rules
        from db.accessors import mail_filing
        first = await mail_filing.create_destination('First', 'label')
        second = await mail_filing.create_destination('Second', 'label')
        folder = await mail_filing.create_destination('Folder', 'folder')
        app = FastAPI(); app.include_router(rules.router, prefix='/rules')
        body = {'name': 'Example', 'sender_domain': 'example.org', 'label_ids': [first.id, second.id, first.id]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.post('/rules', json=body | {'label_ids': [folder.id]})).status_code == 422
            assert (await client.post('/rules', json=body | {'label_ids': ['missing']})).status_code == 422
            created = await client.post('/rules', json=body)
            assert created.status_code == 200
            row = created.json()
            assert row['label_ids'] == [first.id, second.id]
            changed = await client.patch('/rules/' + row['id'], json=body | {'label_ids': [second.id], 'expected_version': row['version']})
            assert changed.status_code == 200 and changed.json()['label_ids'] == [second.id]
            assert (await client.patch('/rules/' + row['id'], json=body | {'expected_version': row['version']})).status_code == 409
    asyncio.run(run())


def test_concurrent_rule_update_and_label_deletion_do_not_deadlock(pg_db, monkeypatch):
    async def run():
        from sqlalchemy.ext.asyncio import AsyncSession
        from db.accessors import mail_filing, mail_rule
        from db.models.mail_folder import MailFolder

        label = await mail_filing.create_destination('Concurrent', 'label')
        folder = await mail_filing.create_destination('Folder', 'folder')
        rule = await mail_rule.create(name='Original', folder_id=folder.id, label_ids=[label.id])
        in_update, release_update, deletion_has_label = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_check = mail_rule._check_targets
        original_scalars = AsyncSession.scalars

        async def paused_check(session, draft, *args):
            if asyncio.current_task().get_name() == 'rule-update':
                in_update.set()
                await release_update.wait()
            return await original_check(session, draft, *args)

        async def observe_scalars(session, statement, *args, **kwargs):
            result = await original_scalars(session, statement, *args, **kwargs)
            if (asyncio.current_task().get_name() == 'label-delete'
                    and statement.column_descriptions[0].get('entity') is MailFolder):
                deletion_has_label.set()
            return result

        monkeypatch.setattr(mail_rule, '_check_targets', paused_check)
        monkeypatch.setattr(AsyncSession, 'scalars', observe_scalars)
        updating = asyncio.create_task(mail_rule.update(rule.id, rule.version, name='Updated', label_ids=[label.id]), name='rule-update')
        await asyncio.wait_for(in_update.wait(), 5)
        deleting = asyncio.create_task(mail_filing.delete_destination(label.id), name='label-delete')
        try:
            await asyncio.wait_for(deletion_has_label.wait(), 0.5)
        except TimeoutError:
            pass  # The update holds the destination lock first.
        finally:
            release_update.set()
        await asyncio.wait_for(asyncio.gather(updating, deleting), 5)
        updated = await mail_rule.get(rule.id)
        assert updated.name == 'Updated'
        assert updated.label_ids == [] and updated.version == rule.version + 2
        assert updated.is_active  # The folder is still present.

    asyncio.run(run())


def test_label_deletion_racing_new_rule_target_never_leaves_a_dangling_id(pg_db, monkeypatch):
    async def run():
        from sqlalchemy.ext.asyncio import AsyncSession
        from db.accessors import mail_filing, mail_rule
        from db.models.mail_folder import MailFolder

        deleted = await mail_filing.create_destination('Delete', 'label')
        retained = await mail_filing.create_destination('Retain', 'label')
        rule = await mail_rule.create(name='Example', label_ids=[retained.id])
        deletion_has_label, release_delete, update_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_scalars, original_locks = AsyncSession.scalars, mail_rule._lock_targets

        async def pause_delete(session, statement, *args, **kwargs):
            result = await original_scalars(session, statement, *args, **kwargs)
            if (asyncio.current_task().get_name() == 'label-delete'
                    and statement.column_descriptions[0].get('entity') is MailFolder):
                deletion_has_label.set()
                await release_delete.wait()
            return result

        async def observe_update(session, draft):
            if asyncio.current_task().get_name() == 'rule-update':
                update_started.set()
            return await original_locks(session, draft)

        monkeypatch.setattr(AsyncSession, 'scalars', pause_delete)
        monkeypatch.setattr(mail_rule, '_lock_targets', observe_update)
        deleting = asyncio.create_task(mail_filing.delete_destination(deleted.id), name='label-delete')
        await asyncio.wait_for(deletion_has_label.wait(), 5)
        updating = asyncio.create_task(mail_rule.update(rule.id, rule.version, label_ids=[retained.id, deleted.id]), name='rule-update')
        await asyncio.wait_for(update_started.wait(), 5)
        release_delete.set()
        await asyncio.wait_for(deleting, 5)
        with pytest.raises(ValueError, match='Invalid destination'):
            await asyncio.wait_for(updating, 5)
        saved = await mail_rule.get(rule.id)
        assert saved.label_ids == [retained.id] and saved.version == rule.version

    asyncio.run(run())


def test_rule_update_rechecks_version_after_waiting_for_destination(pg_db, monkeypatch):
    async def run():
        from sqlalchemy import select
        from db.accessors import mail_filing, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_folder import MailFolder

        existing = await mail_filing.create_destination('Existing', 'label')
        blocked = await mail_filing.create_destination('Blocked', 'label')
        rule = await mail_rule.create(name='Original', label_ids=[existing.id])
        waiting = asyncio.Event()
        original_locks = mail_rule._lock_targets

        async def observe_locks(session, draft):
            if asyncio.current_task().get_name() == 'waiting-update':
                waiting.set()
            return await original_locks(session, draft)

        monkeypatch.setattr(mail_rule, '_lock_targets', observe_locks)
        async with get_session_maker().begin() as session:
            await session.scalars(select(MailFolder).where(MailFolder.id == blocked.id).with_for_update())
            stale = asyncio.create_task(mail_rule.update(rule.id, rule.version, name='Stale', label_ids=[blocked.id]), name='waiting-update')
            await asyncio.wait_for(waiting.wait(), 5)
            fresh = await asyncio.wait_for(mail_rule.update(rule.id, rule.version, name='Fresh'), 5)
            assert fresh.version == rule.version + 1
        with pytest.raises(mail_rule.VersionConflict):
            await asyncio.wait_for(stale, 5)
        saved = await mail_rule.get(rule.id)
        assert saved.name == 'Fresh' and saved.label_ids == [existing.id]

    asyncio.run(run())


def test_manual_apply_includes_filed_provider_inbox_without_moving_it(pg_db, monkeypatch):
    async def run():
        import json
        from unittest.mock import AsyncMock
        from api.mail import action_capabilities, actions, jobs, rules
        from api.mail.rule_schemas import RuleIn
        from api.routers.api.v1.mail.rules import ApplyIn, apply_rule, preview
        from db.accessors import mail_filing, mail_message, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(id='synthetic', provider='gmail', auth_type='mistral_connector', email_address='owner@example.org')
        async with get_session_maker().begin() as session:
            session.add(account)
        higher_folder = await mail_filing.create_destination('Higher', 'folder')
        owner_folder = await mail_filing.create_destination('Owner', 'folder')
        lower_folder = await mail_filing.create_destination('Lower', 'folder')
        label = await mail_filing.create_destination('Work', 'label')
        existing = await mail_filing.create_destination('Existing', 'label')
        first = await mail_message.upsert_message(account_id=account.id, folder='INBOX', provider_uid='first', sender_address='person@example.org')
        owner = await mail_message.upsert_message(account_id=account.id, folder='INBOX', provider_uid='owner', sender_address='person@example.org')
        unfiled = await mail_message.upsert_message(account_id=account.id, folder='INBOX', provider_uid='unfiled', sender_address='person@example.org')
        archived = await mail_message.upsert_message(account_id=account.id, folder='ARCHIVE', provider_uid='archive', sender_address='person@example.org')
        trashed = await mail_message.upsert_message(account_id=account.id, folder='TRASH', provider_uid='trash', sender_address='person@example.org')
        higher = await mail_rule.create(name='Higher', position=1, sender_domain='example.org', folder_id=higher_folder.id)
        lower = await mail_rule.create(name='Lower', position=2, sender_domain='example.org', folder_id=lower_folder.id, label_ids=[label.id], mark_read=True)
        await rules.execute_rule_run(higher.id, higher.version, first.id)
        await mail_filing.assign([owner.id], folder_id=owner_folder.id, add_labels=[existing.id, label.id])
        body = RuleIn(name=lower.name, sender_domain=lower.sender_domain, folder_id=lower.folder_id, label_ids=lower.label_ids, mark_read=True, position=2)
        previewed = await preview(body)
        assert previewed['matched'] == 3 and previewed['already_filed'] == 2
        assert set(previewed['message_ids']) == {first.id, owner.id, unfiled.id}
        gate = AsyncMock(side_effect=action_capabilities.unsupported())
        writer = AsyncMock()
        monkeypatch.setattr(action_capabilities, 'require_action', gate)
        monkeypatch.setattr(actions, 'apply_action', writer)
        result = await apply_rule(lower.id, ApplyIn(expected_version=lower.version, confirm_apply=True))
        assert result['matched'] == 3
        for _ in range(3):
            assert await jobs.run_once()
        assert not await jobs.run_once()
        filings = await mail_filing.for_messages([first.id, owner.id, unfiled.id, archived.id, trashed.id])
        assert filings[first.id].folder_id == higher_folder.id and filings[first.id].label_ids == [label.id]
        assert filings[owner.id].folder_id == owner_folder.id and set(filings[owner.id].label_ids) == {existing.id, label.id}
        assert filings[unfiled.id].folder_id is None and filings[unfiled.id].label_ids == [label.id]
        assert filings[archived.id].label_ids == [] and filings[trashed.id].label_ids == []
        assert gate.await_count == 3
        writer.assert_not_awaited()
        runs = await mail_rule_run.list_for_rule(lower.id)
        assert len(runs) == 3 and all(json.loads(run.provider_results_json)['mark_read'] == 'skipped_unsupported' for run in runs)
    asyncio.run(run())


def test_manual_reapply_restores_filed_labels_without_repeating_provider_action(pg_db, monkeypatch):
    async def run():
        import json
        from unittest.mock import AsyncMock
        from api.mail import action_capabilities, actions, jobs, rules
        from api.routers.api.v1.mail.rules import ApplyIn, apply_rule
        from db.accessors import mail_filing, mail_message, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(id='synthetic', provider='gmail', auth_type='mistral_connector', email_address='owner@example.org')
        async with get_session_maker().begin() as session:
            session.add(account)
        folder = await mail_filing.create_destination('Owner', 'folder')
        label = await mail_filing.create_destination('Work', 'label')
        message = await mail_message.upsert_message(account_id=account.id, folder='INBOX', provider_uid='test', sender_address='person@example.org')
        rule = await mail_rule.create(name='Example', sender_domain='example.org', label_ids=[label.id], mark_read=True)
        gate, writer = AsyncMock(), AsyncMock()
        monkeypatch.setattr(action_capabilities, 'require_action', gate)
        monkeypatch.setattr(actions, 'apply_action', writer)
        first = await rules.execute_rule_run(rule.id, rule.version, message.id)
        assert first.status == 'done' and json.loads(first.provider_results_json)['mark_read'] == 'done'
        writer.assert_awaited_once_with(message.id, 'mark_read')
        await mail_filing.assign([message.id], folder_id=folder.id, remove_labels=[label.id])
        await rules.execute_rule_run(rule.id, rule.version, message.id)
        assert (await mail_filing.for_message(message.id)).label_ids == []  # Automatic replay stays deduplicated.
        for _ in range(2):
            outcome = await apply_rule(rule.id, ApplyIn(expected_version=rule.version, confirm_apply=True))
            assert outcome == {'matched': 1, 'app_changed': 1, 'provider_skipped': 1, 'provider_queued': 0, 'conflicts': 0}
            assert not await jobs.run_once()
            filing = await mail_filing.for_message(message.id)
            assert filing.folder_id == folder.id and filing.label_ids == [label.id]
            await mail_filing.assign([message.id], remove_labels=[label.id])
        writer.assert_awaited_once_with(message.id, 'mark_read')
        gate.assert_awaited_once()
        runs = await mail_rule_run.list_for_rule(rule.id)
        assert len(runs) == 1 and runs[0].status == 'done'
        assert json.loads(runs[0].provider_results_json)['mark_read'] == 'done'
    asyncio.run(run())


def test_manual_reapply_does_not_retry_unknown_provider_action(pg_db, monkeypatch):
    async def run():
        import json
        from unittest.mock import AsyncMock
        from api.mail import action_capabilities, actions, jobs, rules
        from api.routers.api.v1.mail.rules import ApplyIn, apply_rule
        from db.accessors import mail_filing, mail_message, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(id='synthetic', provider='gmail', auth_type='mistral_connector', email_address='owner@example.org')
        async with get_session_maker().begin() as session:
            session.add(account)
        folder = await mail_filing.create_destination('Owner', 'folder')
        label = await mail_filing.create_destination('Work', 'label')
        message = await mail_message.upsert_message(account_id=account.id, folder='INBOX', provider_uid='test', sender_address='person@example.org')
        rule = await mail_rule.create(name='Example', sender_domain='example.org', label_ids=[label.id], mark_read=True)
        gate, writer = AsyncMock(), AsyncMock(side_effect=RuntimeError('synthetic uncertain result'))
        monkeypatch.setattr(action_capabilities, 'require_action', gate)
        monkeypatch.setattr(actions, 'apply_action', writer)
        first = await rules.execute_rule_run(rule.id, rule.version, message.id)
        assert first.status == 'unknown' and json.loads(first.provider_results_json)['mark_read'] == 'unknown'
        await mail_filing.assign([message.id], folder_id=folder.id, remove_labels=[label.id])
        outcome = await apply_rule(rule.id, ApplyIn(expected_version=rule.version, confirm_apply=True))
        assert outcome == {'matched': 1, 'app_changed': 1, 'provider_skipped': 1, 'provider_queued': 0, 'conflicts': 0}
        assert not await jobs.run_once()
        filing = await mail_filing.for_message(message.id)
        assert filing.folder_id == folder.id and filing.label_ids == [label.id]
        writer.assert_awaited_once_with(message.id, 'mark_read')
        gate.assert_awaited_once()
        runs = await mail_rule_run.list_for_rule(rule.id)
        assert len(runs) == 1 and runs[0].status == 'unknown'
        assert json.loads(runs[0].provider_results_json)['mark_read'] == 'unknown'
    asyncio.run(run())


def test_saved_preview_counts_only_changes_and_stale_rule_rejects_apply(pg_db):
    async def run():
        from fastapi import HTTPException
        from api.mail import rules
        from api.routers.api.v1.mail.rules import ApplyIn, RulePreviewIn, apply_rule, preview
        from db.accessors import mail_filing, mail_message, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        async with get_session_maker().begin() as session:
            session.add(MailAccount(id='synthetic', provider='imap', auth_type='imap_password', email_address='owner@example.org'))
        label = await mail_filing.create_destination('Work', 'label')
        message = await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid='test', sender_address='person@example.org')
        rule = await mail_rule.create(name='Example', sender_domain='example.org', label_ids=[label.id])
        body = RulePreviewIn(name=rule.name, sender_domain=rule.sender_domain, label_ids=[label.id], saved_rule_id=rule.id, expected_version=rule.version)
        await mail_filing.assign([message.id], add_labels=[label.id])
        assert (await preview(body))['matched'] == 0
        assert (await apply_rule(rule.id, ApplyIn(expected_version=rule.version, confirm_apply=True)))['matched'] == 0
        from db.accessors import mail_rule_run
        assert await mail_rule_run.list_for_rule(rule.id) == []
        await rules.execute_rule_run(rule.id, rule.version, message.id)
        assert (await preview(body))['matched'] == 0
        assert (await apply_rule(rule.id, ApplyIn(expected_version=rule.version, confirm_apply=True)))['app_changed'] == 0
        await mail_filing.assign([message.id], remove_labels=[label.id])
        assert (await preview(body))['matched'] == 1
        result = await apply_rule(rule.id, ApplyIn(expected_version=rule.version, confirm_apply=True))
        assert result['matched'] == result['app_changed'] == 1
        await mail_filing.assign([message.id], remove_labels=[label.id])
        await mail_rule.update(rule.id, rule.version, name='New name')
        with pytest.raises(HTTPException) as exc:
            await apply_rule(rule.id, ApplyIn(expected_version=rule.version, confirm_apply=True))
        assert exc.value.status_code == 409
        assert (await mail_filing.for_message(message.id)).label_ids == []
    asyncio.run(run())


def test_manual_apply_reports_mail_changed_after_preview(pg_db):
    async def run():
        from api.routers.api.v1.mail.rules import ApplyIn, RulePreviewIn, apply_rule, preview
        from db.accessors import mail_filing, mail_message, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount
        from db.models.mail_message import MailMessage

        async with get_session_maker().begin() as session:
            session.add(MailAccount(id='synthetic', provider='imap', auth_type='imap_password', email_address='owner@example.org'))
        label = await mail_filing.create_destination('Work', 'label')
        message = await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid='test', sender_address='person@example.org')
        rule = await mail_rule.create(name='Example', sender_domain='example.org', label_ids=[label.id])
        body = RulePreviewIn(name=rule.name, sender_domain=rule.sender_domain, label_ids=[label.id], saved_rule_id=rule.id, expected_version=rule.version)
        estimate = await preview(body)
        assert estimate['message_ids'] == [message.id]
        async with get_session_maker().begin() as session:
            row = await session.get(MailMessage, message.id)
            row.folder = 'ARCHIVE'
        result = await apply_rule(rule.id, ApplyIn(expected_version=rule.version, confirm_apply=True, preview_message_ids=estimate['message_ids']))
        assert result == {'matched': 0, 'app_changed': 0, 'provider_skipped': 0, 'provider_queued': 0, 'conflicts': 1}
        assert (await mail_filing.for_message(message.id)).label_ids == []
    asyncio.run(run())


def test_manual_apply_ignores_paused_higher_priority_folder_rule(pg_db):
    async def run():
        import json
        from api.routers.api.v1.mail.rules import ApplyIn, RulePreviewIn, apply_rule, preview
        from db.accessors import mail_filing, mail_message, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        async with get_session_maker().begin() as session:
            session.add(MailAccount(id='synthetic', provider='imap', auth_type='imap_password', email_address='owner@example.org'))
        paused_folder = await mail_filing.create_destination('Paused', 'folder')
        active_folder = await mail_filing.create_destination('Active', 'folder')
        message = await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid='test', sender_address='person@example.org')
        await mail_rule.create(name='Paused', sender_domain='example.org', folder_id=paused_folder.id, position=1, is_active=False)
        rule = await mail_rule.create(name='Active', sender_domain='example.org', folder_id=active_folder.id, position=2)
        body = RulePreviewIn(name=rule.name, sender_domain=rule.sender_domain, folder_id=rule.folder_id,
                             position=rule.position, saved_rule_id=rule.id, expected_version=rule.version)
        estimate = await preview(body)
        assert estimate['matched'] == 1 and estimate['message_ids'] == [message.id]
        result = await apply_rule(rule.id, ApplyIn(expected_version=rule.version, confirm_apply=True,
                                                   preview_message_ids=estimate['message_ids']))
        assert result == {'matched': 1, 'app_changed': 1, 'provider_skipped': 0, 'provider_queued': 0, 'conflicts': 0}
        assert (await mail_filing.for_message(message.id)).folder_id == active_folder.id
        runs = await mail_rule_run.list_for_rule(rule.id)
        assert len(runs) == 1 and runs[0].status == 'done'
        assert '_folder' not in json.loads(runs[0].provider_results_json)
    asyncio.run(run())


def test_manual_batch_rechecks_rule_version_inside_transaction(pg_db, monkeypatch):
    async def run():
        from fastapi import HTTPException
        from api.routers.api.v1.mail import rules as router
        from db.accessors import mail_filing, mail_message, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        async with get_session_maker().begin() as session:
            session.add(MailAccount(id='synthetic', provider='imap', auth_type='imap_password', email_address='owner@example.org'))
        label = await mail_filing.create_destination('Work', 'label')
        message = await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid='test', sender_address='person@example.org')
        rule = await mail_rule.create(name='Example', sender_domain='example.org', label_ids=[label.id])
        original = router.matching_messages
        async def change_after_scan(current):
            async for item in original(current):
                yield item
            await mail_rule.update(rule.id, rule.version, name='Changed during confirmation')
        monkeypatch.setattr(router, 'matching_messages', change_after_scan)
        with pytest.raises(HTTPException) as exc:
            await router.apply_rule(rule.id, router.ApplyIn(expected_version=rule.version, confirm_apply=True))
        assert exc.value.status_code == 409
        assert (await mail_filing.for_message(message.id)).label_ids == []
        assert await mail_rule_run.list_for_rule(rule.id) == []
    asyncio.run(run())


def test_concurrent_manual_apply_deduplicates_provider_work(pg_db, monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        from api.mail import action_capabilities, actions, jobs
        from api.routers.api.v1.mail.rules import ApplyIn, apply_rule
        from db.accessors import mail_filing, mail_message, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        async with get_session_maker().begin() as session:
            session.add(MailAccount(id='synthetic', provider='gmail', auth_type='mistral_connector', email_address='owner@example.org'))
        label = await mail_filing.create_destination('Work', 'label')
        message = await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid='test', sender_address='person@example.org')
        rule = await mail_rule.create(name='Example', sender_domain='example.org', label_ids=[label.id], mark_read=True)
        gate, writer = AsyncMock(), AsyncMock()
        monkeypatch.setattr(action_capabilities, 'require_action', gate)
        monkeypatch.setattr(actions, 'apply_action', writer)
        body = ApplyIn(expected_version=rule.version, confirm_apply=True)
        results = await asyncio.gather(apply_rule(rule.id, body), apply_rule(rule.id, body))
        assert sum(item['app_changed'] for item in results) == 1
        assert sum(item['provider_queued'] for item in results) == 2  # The second request observes the same queued action.
        assert sum(item['provider_skipped'] for item in results) == 0
        assert await jobs.run_once()
        assert not await jobs.run_once()
        writer.assert_awaited_once_with(message.id, 'mark_read')
        assert len(await mail_rule_run.list_for_rule(rule.id)) == 1
    asyncio.run(run())


def test_manual_apply_does_not_recover_an_inflight_provider_write(pg_db):
    async def run():
        import json
        from api.mail import jobs, rules
        from api.routers.api.v1.mail.rules import ApplyIn, apply_rule
        from db.accessors import mail_filing, mail_message, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        async with get_session_maker().begin() as session:
            session.add(MailAccount(id='synthetic', provider='gmail', auth_type='mistral_connector', email_address='owner@example.org'))
        label = await mail_filing.create_destination('Work', 'label')
        message = await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid='test', sender_address='person@example.org')
        rule = await mail_rule.create(name='Example', sender_domain='example.org', label_ids=[label.id], mark_read=True)
        run = await rules.prepare_rule_run(rule.id, rule.version, message.id)
        run, claimed = await mail_rule_run.record_action(run.id, 'mark_read', expected='pending', result='dispatching')
        assert claimed
        await mail_filing.assign([message.id], remove_labels=[label.id])
        result = await apply_rule(rule.id, ApplyIn(expected_version=rule.version, confirm_apply=True))
        assert result == {'matched': 1, 'app_changed': 1, 'provider_skipped': 0, 'provider_queued': 1, 'conflicts': 0}
        assert not await jobs.run_once()
        current = (await mail_rule_run.list_for_rule(rule.id))[0]
        assert current.status == 'local_done' and json.loads(current.provider_results_json)['mark_read'] == 'dispatching'
    asyncio.run(run())


def test_manual_batch_rolls_back_if_local_assignment_fails(pg_db, monkeypatch):
    async def run():
        from api.routers.api.v1.mail.rules import ApplyIn, apply_rule
        from db.accessors import mail_filing, mail_message, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        async with get_session_maker().begin() as session:
            session.add(MailAccount(id='synthetic', provider='imap', auth_type='imap_password', email_address='owner@example.org'))
        label = await mail_filing.create_destination('Work', 'label')
        messages = [await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid=uid, sender_address='person@example.org') for uid in ('one', 'two')]
        rule = await mail_rule.create(name='Example', sender_domain='example.org', label_ids=[label.id], mark_read=True)
        original = mail_rule_run._apply_assignments
        calls = 0
        async def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('synthetic transaction failure')
            return await original(*args)
        monkeypatch.setattr(mail_rule_run, '_apply_assignments', fail_second)
        with pytest.raises(RuntimeError, match='synthetic transaction failure'):
            await apply_rule(rule.id, ApplyIn(expected_version=rule.version, confirm_apply=True))
        for message in messages:
            assert not (await mail_filing.for_message(message.id)).label_ids
        assert await mail_rule_run.list_for_rule(rule.id) == []
    asyncio.run(run())
