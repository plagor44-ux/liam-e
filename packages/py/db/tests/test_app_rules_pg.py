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
        lower = await mail_rule.create(name='Lower', position=2, folder_id=second.id, label_id=label.id, mark_read=True)
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
        await mail_rule.create(name="First", position=1, folder_id=folder.id, label_id=first.id, archive=True)
        await mail_rule.create(name="Second", position=2, label_id=second.id)
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
        assert set(filing.label_ids) == {first.id, second.id}

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
