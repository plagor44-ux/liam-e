"""Global filing is independent of the provider and exclusive only for folders."""

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("RUN_PG_CONTRACT"), reason="RUN_PG_CONTRACT is not set")


def test_cross_account_folders_replace_and_labels_accumulate(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_message

        first = await mail_filing.create_destination("Projects", "folder")
        second = await mail_filing.create_destination("Later", "folder")
        label_a = await mail_filing.create_destination("Important", "label")
        label_b = await mail_filing.create_destination("Work", "label")
        mail_a = await mail_message.upsert_message(account_id="account-a", folder="INBOX", provider_uid="a")
        mail_b = await mail_message.upsert_message(account_id="account-b", folder="INBOX", provider_uid="b")
        await mail_filing.assign([mail_a.id, mail_b.id], folder_id=first.id, add_labels=[label_a.id])
        await mail_filing.assign([mail_a.id], folder_id=second.id, add_labels=[label_b.id])
        a = await mail_filing.for_message(mail_a.id)
        b = await mail_filing.for_message(mail_b.id)
        assert a.folder_id == second.id
        assert set(a.label_ids) == {label_a.id, label_b.id}
        assert b.folder_id == first.id
        assert b.label_ids == [label_a.id]
        batch = await mail_filing.for_messages([mail_a.id, mail_b.id])
        assert batch[mail_a.id] == a and batch[mail_b.id] == b
        assert (await mail_message.get_message(mail_a.id)).folder == "INBOX"
        assert await mail_message.list_page(hide_filed=True) == []
        assert {row.id for row in await mail_message.list_page(app_folder_id=first.id)} == {mail_b.id}
        assert {row.id for row in await mail_message.list_page(app_label_id=label_a.id)} == {mail_a.id, mail_b.id}
        await mail_filing.assign([mail_a.id], folder_id=None)
        assert {row.id for row in await mail_message.list_page(hide_filed=True)} == {mail_a.id}

    asyncio.run(run())


def test_verified_sync_merge_preserves_filing(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_message
        from db.engine import get_session_maker

        folder = await mail_filing.create_destination("Projects", "folder")
        label = await mail_filing.create_destination("Important", "label")
        keep = await mail_message.upsert_message(account_id="account-a", folder="INBOX", provider_uid="one")
        duplicate = await mail_message.upsert_message(account_id="account-a", folder="ARCHIVE", provider_uid="one")
        await mail_filing.assign([duplicate.id], folder_id=folder.id, add_labels=[label.id])
        async with get_session_maker().begin() as session:
            keep_row = await mail_message.get_message(keep.id, session)
            duplicate_row = await mail_message.get_message(duplicate.id, session)
            await mail_message._merge_copy(session, keep_row, duplicate_row, verified=True)
        filing = await mail_filing.for_message(keep.id)
        assert filing.folder_id == folder.id
        assert filing.label_ids == [label.id]

    asyncio.run(run())


def test_removing_account_removes_its_filing_only(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_message
        folder = await mail_filing.create_destination("Projects", "folder")
        label = await mail_filing.create_destination("Important", "label")
        a = await mail_message.upsert_message(account_id="account-a", folder="INBOX", provider_uid="a")
        b = await mail_message.upsert_message(account_id="account-b", folder="INBOX", provider_uid="b")
        await mail_filing.assign([a.id, b.id], folder_id=folder.id, add_labels=[label.id])
        await mail_message.delete_for_account("account-a")
        assert (await mail_filing.for_message(a.id)).folder_id is None
        assert (await mail_filing.for_message(a.id)).label_ids == []
        assert (await mail_filing.for_message(b.id)).folder_id == folder.id
        assert (await mail_filing.for_message(b.id)).label_ids == [label.id]

    asyncio.run(run())


def test_full_rebuild_keeps_verified_gmail_filing(pg_db):
    async def run():
        from api.mail.sync import _apply_changes
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_message
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(id="account-a", provider="google", auth_type="oauth2", email_address="owner@example.test")
        message = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="uid")
        folder = await mail_filing.create_destination("Projects", "folder")
        await mail_filing.assign([message.id], folder_id=folder.id)
        changes = SyncChanges(full_rebuild=True, cursor="new", upserts=[{"folder": "INBOX", "provider_uid": "uid"}])
        async with get_session_maker().begin() as session:
            await _apply_changes(account, changes, session, False, {})
        assert (await mail_message.get_message(message.id)) is not None
        assert (await mail_filing.for_message(message.id)).folder_id == folder.id

    asyncio.run(run())


def test_filing_rejects_wrong_destination_without_partial_changes(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_message
        folder = await mail_filing.create_destination("Projects", "folder")
        label = await mail_filing.create_destination("Important", "label")
        mail = await mail_message.upsert_message(account_id="account-a", folder="INBOX", provider_uid="a")
        with pytest.raises(ValueError):
            await mail_filing.assign([mail.id], folder_id=folder.id, add_labels=[folder.id])
        assert (await mail_filing.for_message(mail.id)).folder_id is None
        await mail_filing.assign([mail.id], add_labels=[label.id])
        await mail_filing.assign([mail.id], remove_labels=[label.id])
        assert (await mail_filing.for_message(mail.id)).label_ids == []

    asyncio.run(run())


def test_deleting_destination_clears_only_its_assignments(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_message
        folder = await mail_filing.create_destination("Projects", "folder")
        label = await mail_filing.create_destination("Important", "label")
        mail = await mail_message.upsert_message(account_id="account-a", folder="INBOX", provider_uid="a")
        await mail_filing.assign([mail.id], folder_id=folder.id, add_labels=[label.id])
        await mail_filing.delete_destination(folder.id)
        filing = await mail_filing.for_message(mail.id)
        assert filing.folder_id is None
        assert filing.label_ids == [label.id]
        assert {row.id for row in await mail_message.list_page(hide_filed=True)} == {mail.id}

    asyncio.run(run())


def test_filing_api_requires_delete_confirmation_and_files_across_accounts(pg_db):
    async def run():
        import httpx
        from fastapi import FastAPI
        from api.routers.api.v1.mail import folders
        from db.accessors import mail_message

        app = FastAPI()
        app.include_router(folders.router, prefix="/folders")
        one = await mail_message.upsert_message(account_id="account-a", folder="INBOX", provider_uid="a")
        two = await mail_message.upsert_message(account_id="account-b", folder="INBOX", provider_uid="b")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=httpx.URL(scheme="http", host="test")) as client:
            created = await client.post("/folders", json={"name": "Projects", "kind": "folder"})
            assert created.status_code == 200
            folder_id = created.json()["id"]
            result = await client.post("/folders/assign", json={"message_ids": [one.id, two.id], "folder_id": folder_id})
            assert result.status_code == 200
            assert (await client.delete(f"/folders/{folder_id}")).status_code == 422
            assert (await client.delete(f"/folders/{folder_id}", params={"confirm_delete": True})).status_code == 200
            assert {row.id for row in await mail_message.list_page(hide_filed=True)} == {one.id, two.id}

    asyncio.run(run())


def test_mail_api_filters_app_folders_and_labels(pg_db):
    async def run():
        import httpx
        from fastapi import FastAPI
        from api.routers.api.v1.mail import messages
        from db.accessors import mail_filing, mail_message

        app = FastAPI()
        app.include_router(messages.router, prefix="/messages")
        folder = await mail_filing.create_destination("Projects", "folder")
        label = await mail_filing.create_destination("Important", "label")
        one = await mail_message.upsert_message(account_id="account-a", folder="INBOX", provider_uid="a")
        two = await mail_message.upsert_message(account_id="account-b", folder="INBOX", provider_uid="b")
        await mail_filing.assign([one.id], folder_id=folder.id, add_labels=[label.id])
        await mail_filing.assign([two.id], add_labels=[label.id])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=httpx.URL(scheme="http", host="test")) as client:
            inbox = await client.get("/messages")
            in_folder = await client.get("/messages", params={"app_folder_id": folder.id})
            labelled = await client.get("/messages", params={"app_label_id": label.id})
            assert {row["id"] for row in inbox.json()["items"]} == {two.id}
            assert {row["id"] for row in in_folder.json()["items"]} == {one.id}
            assert {row["id"] for row in labelled.json()["items"]} == {one.id, two.id}

    asyncio.run(run())


def test_seed_filing_replaces_only_its_synthetic_records(pg_db):
    async def run():
        from api.mail import dev_seed
        from api.mail.seed_ids import seed_id
        from db.accessors import mail_filing

        outsider = await mail_filing.create_destination("Unrelated", "label")
        await dev_seed.seed_filing()
        await dev_seed.seed_filing()
        destinations = await mail_filing.list_destinations()
        assert outsider.id in {row.id for row in destinations}
        assert seed_id('folder', 'projects') in {row.id for row in destinations}
        assert len([row for row in destinations if row.id == seed_id('folder', 'projects')]) == 1

    asyncio.run(run())


def test_app_folder_and_label_can_show_indexed_archived_mail(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_message
        folder = await mail_filing.create_destination("Projects", "folder")
        label = await mail_filing.create_destination("Important", "label")
        message = await mail_message.upsert_message(account_id="account-a", folder="INBOX", provider_uid="a")
        await mail_filing.assign([message.id], folder_id=folder.id, add_labels=[label.id])
        await mail_message.set_state(message.id, folder="ARCHIVE")
        assert {row.id for row in await mail_message.list_page(app_folder_id=folder.id)} == {message.id}
        assert {row.id for row in await mail_message.list_page(app_label_id=label.id)} == {message.id}

    asyncio.run(run())


@pytest.mark.parametrize('provider,auth,validity,prior', [
    ('microsoft', 'oauth2', None, ''),
    ('imap', 'imap_password', '7', '7:10'),
    ('gmail', 'mistral_connector', None, ''),
])
def test_full_rebuild_keeps_same_verified_message_filing(pg_db, provider, auth, validity, prior):
    async def run():
        from sqlalchemy import text
        from api.mail.sync import _apply_changes
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_message, mail_sync_state
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(id='account-a', provider=provider, auth_type=auth, email_address='owner@example.test')
        async with get_session_maker().begin() as session:
            session.add(account)
            if auth == 'mistral_connector':
                await session.execute(text("SELECT set_config('liam.verified_connector', :account, true)"), {'account': account.id})
            message = await mail_message.upsert_message(account_id=account.id, folder='INBOX', provider_uid='1', imap_uidvalidity=validity, session=session)
        if prior:
            await mail_sync_state.save_state(account_id=account.id, cursor=prior)
        folder = await mail_filing.create_destination('Projects', 'folder')
        await mail_filing.assign([message.id], folder_id=folder.id)
        upsert = {'folder': 'INBOX', 'provider_uid': '1'}
        if validity:
            upsert['imap_uidvalidity'] = validity
        changes = SyncChanges(full_rebuild=True, cursor=f'{validity}:10' if validity else 'new', upserts=[upsert])
        async with get_session_maker().begin() as session:
            if auth == 'mistral_connector':
                await session.execute(text("SELECT set_config('liam.verified_connector', :account, true)"), {'account': account.id})
            await _apply_changes(account, changes, session, auth == 'mistral_connector', {})
        assert (await mail_message.get_message(message.id)) is not None
        assert (await mail_filing.for_message(message.id)).folder_id == folder.id

    asyncio.run(run())


def test_imap_generation_change_does_not_transfer_filing(pg_db, monkeypatch):
    async def run():
        import json
        from datetime import timedelta
        from unittest.mock import AsyncMock
        from sqlalchemy import select
        from api.mail import actions, jobs
        from api.mail.sync import _apply_changes
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_message, mail_sync_state, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models._common import utcnow
        from db.models.mail_account import MailAccount
        from db.models.mail_job import MailJob

        account = MailAccount(id='account-a', provider='imap', auth_type='imap_password', email_address='owner@example.test')
        async with get_session_maker().begin() as session:
            session.add(account)
        await mail_sync_state.save_state(account_id=account.id, cursor='7:10')
        original = await mail_message.upsert_message(account_id=account.id, folder='INBOX', provider_uid='1', imap_uidvalidity='7')
        folder = await mail_filing.create_destination('Projects', 'folder')
        await mail_filing.assign([original.id], folder_id=folder.id)
        rule = await mail_rule.create(name='File', folder_id=folder.id, mark_read=True)
        changes = SyncChanges(full_rebuild=True, cursor='8:10', upserts=[{'folder': 'INBOX', 'provider_uid': '1', 'imap_uidvalidity': '8', 'internal_date': utcnow()}])
        async with get_session_maker().begin() as session:
            ids, _ = await _apply_changes(account, changes, session, False, {})
        rows = await mail_message.list_page()
        assert len(rows) == 1 and rows[0].id != original.id
        assert ids == []  # A new UID generation cannot prove that this is a new delivery.
        async with get_session_maker()() as session:
            assert not (await session.scalars(select(MailJob).where(MailJob.kind == 'mail_rule_apply'))).all()
        notices = await mail_rule_run.list_for_rule(rule.id)
        assert len(notices) == 1 and notices[0].status == 'skipped_imap_rebuild'
        assert notices[0].message_id != rows[0].id
        rebuilt_id = rows[0].id
        assert (await mail_filing.for_message(rows[0].id)).folder_id is None
        assert (await mail_filing.for_message(original.id)).folder_id is None
        await mail_sync_state.save_state(account_id=account.id, cursor='8:10')
        later = SyncChanges(full_rebuild=False, cursor='8:11', upserts=[
            {'folder': 'INBOX', 'provider_uid': '1', 'imap_uidvalidity': '8', 'internal_date': utcnow()},
            {'folder': 'INBOX', 'provider_uid': '2', 'imap_uidvalidity': '8', 'internal_date': utcnow()},
        ])
        async with get_session_maker().begin() as session:
            later_ids, _ = await _apply_changes(account, later, session, False, {})
        assert len(later_ids) == 1 and (await mail_message.get_message(later_ids[0])).provider_uid == '2'
        async with get_session_maker()() as session:
            queued = (await session.scalars(select(MailJob).where(MailJob.kind == 'mail_rule_apply'))).all()
            assert len(queued) == 1 and json.loads(queued[0].payload_json)['message_id'] == later_ids[0]
        write = AsyncMock()
        monkeypatch.setattr(actions, 'apply_action', write)
        assert await jobs.run_once()
        write.assert_awaited_once_with(later_ids[0], 'mark_read')
        runs = await mail_rule_run.list_for_rule(rule.id)
        assert {run.message_id for run in runs} == {notices[0].message_id, later_ids[0]}
        assert (await mail_filing.for_message(rebuilt_id)).folder_id is None
        # Normal account-wide insight filling still finds recent unread rebuilt mail.
        candidates = await mail_message.unread_without_insight(since=utcnow() - timedelta(days=14), limit=100, account_id=account.id)
        assert rebuilt_id in {message.id for message in candidates}

    asyncio.run(run())


def test_imap_same_generation_rebuild_still_schedules_new_delivery(pg_db):
    async def run():
        from sqlalchemy import select
        from api.mail.sync import _apply_changes
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_message, mail_rule, mail_sync_state
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount
        from db.models.mail_job import MailJob
        account = MailAccount(id='account-a', provider='imap', auth_type='imap_password', email_address='a@example.test')
        async with get_session_maker().begin() as session:
            session.add(account)
        await mail_sync_state.save_state(account_id=account.id, cursor='7:10')
        await mail_message.upsert_message(account_id=account.id, folder='INBOX', provider_uid='1', imap_uidvalidity='7')
        folder = await mail_filing.create_destination('Projects', 'folder')
        await mail_rule.create(name='File', folder_id=folder.id)
        changes = SyncChanges(full_rebuild=True, cursor='7:15', upserts=[
            {'folder': 'INBOX', 'provider_uid': '1', 'imap_uidvalidity': '7'},
            {'folder': 'INBOX', 'provider_uid': '2', 'imap_uidvalidity': '7'},
        ])
        async with get_session_maker().begin() as session:
            ids, _ = await _apply_changes(account, changes, session, False, {})
        assert len(ids) == 1 and (await mail_message.get_message(ids[0])).provider_uid == '2'
        async with get_session_maker()() as session:
            assert len((await session.scalars(select(MailJob).where(MailJob.kind == 'mail_rule_apply'))).all()) == 1
    asyncio.run(run())


def test_inbox_unread_count_excludes_only_foldered_mail(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_message
        folder = await mail_filing.create_destination('Projects', 'folder')
        label = await mail_filing.create_destination('Important', 'label')
        first = await mail_message.upsert_message(account_id='account-a', folder='INBOX', provider_uid='a', is_read=False)
        second = await mail_message.upsert_message(account_id='account-a', folder='INBOX', provider_uid='b', is_read=False)
        await mail_filing.assign([first.id], folder_id=folder.id)
        await mail_filing.assign([second.id], add_labels=[label.id])
        assert await mail_message.count_unread() == {'account-a': 1}

    asyncio.run(run())


def test_verified_action_duplicate_keeps_app_filing(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_message
        keep = await mail_message.upsert_message(account_id='account-a', folder='INBOX', provider_uid='source', rfc_message_id='<same@example.test>')
        duplicate = await mail_message.upsert_message(account_id='account-a', folder='ARCHIVE', provider_uid='target', rfc_message_id='<same@example.test>')
        label = await mail_filing.create_destination('Important', 'label')
        await mail_filing.assign([duplicate.id], add_labels=[label.id])
        assert await mail_message.delete_duplicate('account-a', 'ARCHIVE', 'target', keep_id=keep.id, rfc_message_id='<same@example.test>')
        assert (await mail_filing.for_message(keep.id)).label_ids == [label.id]
        assert await mail_message.get_message(duplicate.id) is None

    asyncio.run(run())


def test_filing_accepts_verified_alias_and_checks_account_scope(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_message
        from db.engine import get_session_maker
        from db.models.mail_message import MailMessage
        folder = await mail_filing.create_destination('Projects', 'folder')
        row = await mail_message.upsert_message(account_id='account-a', folder='INBOX', provider_uid='uid')
        async with get_session_maker().begin() as session:
            current = await session.get(MailMessage, row.id)
            current.connector_alias_ids = ['old-id']
        with pytest.raises(LookupError):
            await mail_filing.assign(['old-id'], folder_id=folder.id, account_ids=['account-b'])
        await mail_filing.assign(['old-id', row.id], folder_id=folder.id, account_ids=['account-a'])
        assert (await mail_filing.for_message(row.id)).folder_id == folder.id
    asyncio.run(run())


def test_rebuild_invalidates_changed_insight_for_retained_message(pg_db):
    async def run():
        from api.mail.sync import _apply_changes
        from api.mail.sync_types import SyncChanges, BODY_VERSION
        from db.accessors import mail_insight, mail_message
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount
        account = MailAccount(id='account-a', provider='google', auth_type='oauth2', email_address='a@example.test')
        row = await mail_message.upsert_message(account_id=account.id, folder='INBOX', provider_uid='uid', body_text='old', body_version=0)
        await mail_insight.store_if_missing(row.id, points=['old'], deadline=None, needs_reply=False, followups=[], model='seed')
        changes = SyncChanges(full_rebuild=True, cursor='new', upserts=[{'folder': 'INBOX', 'provider_uid': 'uid', 'body_text': 'new'}])
        async with get_session_maker().begin() as session:
            _, invalidated = await _apply_changes(account, changes, session, False, {})
        assert row.id in invalidated
        assert await mail_insight.get(row.id) is None
        assert (await mail_message.get_message(row.id)).body_version == BODY_VERSION
    asyncio.run(run())


def test_deleting_account_removes_only_its_scoped_rules(pg_db):
    async def run():
        from db.accessors import mail_account, mail_filing, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount
        account = MailAccount(id='account-a', provider='google', auth_type='oauth2', email_address='a@example.test')
        async with get_session_maker().begin() as session:
            session.add(account)
        folder = await mail_filing.create_destination('Projects', 'folder')
        scoped = await mail_rule.create(name='Scoped', account_id=account.id, folder_id=folder.id)
        global_rule = await mail_rule.create(name='Global')
        await mail_rule_run.create_once(scoped.id, scoped.version, 'mail', account.id)
        assert await mail_account.delete_account(account.id)
        assert await mail_rule.get(scoped.id) is None
        assert await mail_rule.get(global_rule.id) is not None
        assert await mail_rule_run.list_for_rule(scoped.id) == []
        await mail_filing.delete_destination(folder.id)
    asyncio.run(run())


@pytest.mark.parametrize('collision', [False, True])
def test_verified_merge_preserves_rule_dispatch_deduplication(pg_db, monkeypatch, collision):
    async def run():
        import json
        from unittest.mock import AsyncMock
        from api.mail import actions, rules
        from db.accessors import mail_message, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        keep = await mail_message.upsert_message(account_id='account-a', folder='INBOX', provider_uid='uid')
        duplicate = await mail_message.upsert_message(account_id='account-a', folder='ARCHIVE', provider_uid='uid')
        rule = await mail_rule.create(name='Rule', mark_read=True)
        prior, _ = await mail_rule_run.create_once(rule.id, rule.version, duplicate.id, keep.account_id)
        async with get_session_maker().begin() as session:
            row = await session.get(type(prior), prior.id)
            row.status, row.provider_results_json = 'local_done', json.dumps({'mark_read': 'dispatching'})
        if collision:
            current, _ = await mail_rule_run.create_once(rule.id, rule.version, keep.id, keep.account_id)
            async with get_session_maker().begin() as session:
                row = await session.get(type(current), current.id)
                row.status, row.provider_results_json = 'local_done', json.dumps({'mark_read': 'pending'})
        async with get_session_maker().begin() as session:
            keep_row = await mail_message.get_message(keep.id, session)
            duplicate_row = await mail_message.get_message(duplicate.id, session)
            await mail_message._merge_copy(session, keep_row, duplicate_row, verified=True)
        write = AsyncMock()
        monkeypatch.setattr(actions, 'apply_action', write)
        outcome = await rules.execute_rule_run(rule.id, rule.version, duplicate.id, recover=True)
        assert outcome.status == 'unknown'
        assert json.loads(outcome.provider_results_json)['mark_read'] == 'unknown'
        assert len(await mail_rule_run.list_for_rule(rule.id)) == 1
        write.assert_not_awaited()
    asyncio.run(run())
