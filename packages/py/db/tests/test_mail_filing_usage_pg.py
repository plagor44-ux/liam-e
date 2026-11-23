"""Installation-scoped quick-action history stores no message data."""

import asyncio
import os
from datetime import timedelta

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("RUN_PG_CONTRACT"), reason="RUN_PG_CONTRACT is not set")


def test_successful_filing_records_destinations_and_deletion_removes_history(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_filing_usage, mail_message
        folder = await mail_filing.create_destination('Projects', 'folder')
        label = await mail_filing.create_destination('Important', 'label')
        message = await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid='one')
        await mail_filing.assign([message.id], folder_id=folder.id, add_labels=[label.id])
        await mail_filing.assign([message.id], folder_id=folder.id)
        actions = {item.destination.id: item for item in await mail_filing_usage.list_quick_actions()}
        assert actions[folder.id].use_count == 2 and actions[label.id].use_count == 1
        with pytest.raises(ValueError):
            await mail_filing.assign([message.id], folder_id=label.id)
        assert {item.destination.id: item.use_count for item in await mail_filing_usage.list_quick_actions()} == {folder.id: 2, label.id: 1}
        await mail_filing.delete_destination(folder.id)
        assert {item.destination.id for item in await mail_filing_usage.list_quick_actions()} == {label.id}

    asyncio.run(run())


def test_quick_action_endpoint_returns_only_live_destinations(pg_db):
    async def run():
        import httpx
        from fastapi import FastAPI
        from api.routers.api.v1.mail import folders
        from db.accessors import mail_filing, mail_message

        destination = await mail_filing.create_destination('Projects', 'folder')
        message = await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid='one')
        await mail_filing.assign([message.id], folder_id=destination.id)
        app = FastAPI(); app.include_router(folders.router, prefix='/folders')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/folders/quick-actions')
            assert response.status_code == 200
            assert [(row['destination']['id'], row['use_count']) for row in response.json()] == [(destination.id, 1)]
            await mail_filing.delete_destination(destination.id)
            assert (await client.get('/folders/quick-actions')).json() == []

    asyncio.run(run())


def test_expiry_and_lru_cap_are_enforced(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_filing_usage, mail_message
        from db.engine import get_session_maker
        from db.models._common import utcnow
        from db.models.mail_folder import MailFolder
        from db.models.mail_filing_usage import MailFilingUsage

        now = utcnow()
        destinations = [MailFolder(id=f'destination-{n:03}', name=f'Folder {n:03}', kind='folder') for n in range(201)]
        usages = [MailFilingUsage(destination_id=row.id, use_count=1, last_used_at=now - timedelta(minutes=201 - n)) for n, row in enumerate(destinations)]
        usages[0].last_used_at = now - timedelta(days=91)
        async with get_session_maker().begin() as session:
            session.add_all([*destinations, *usages])
        message = await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid='one')
        newest = await mail_filing.create_destination('Newest', 'folder')
        await mail_filing.assign([message.id], folder_id=newest.id)
        actions = await mail_filing_usage.list_quick_actions()
        ids = {item.destination.id for item in actions}
        assert len(ids) == 200
        assert destinations[0].id not in ids  # Expired.
        assert destinations[1].id not in ids  # Oldest remaining entry was evicted.
        assert newest.id in ids

    asyncio.run(run())


def test_recent_use_increases_the_frequency_score(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_filing_usage
        from db.engine import get_session_maker
        from db.models._common import utcnow
        from db.models.mail_filing_usage import MailFilingUsage

        now = utcnow()
        recent = await mail_filing.create_destination('Recent', 'label')
        older = await mail_filing.create_destination('Older', 'label')
        async with get_session_maker().begin() as session:
            session.add_all([MailFilingUsage(destination_id=recent.id, use_count=4, last_used_at=now),
                             MailFilingUsage(destination_id=older.id, use_count=4, last_used_at=now - timedelta(days=45))])
        actions = {item.destination.id: item for item in await mail_filing_usage.list_quick_actions(now=now)}
        assert actions[recent.id].score > actions[older.id].score

    asyncio.run(run())


def test_history_expires_at_ninety_days_and_contains_only_usage_fields(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_filing_usage
        from db.engine import get_session_maker
        from db.models._common import utcnow
        from db.models.mail_filing_usage import MailFilingUsage

        assert set(MailFilingUsage.model_fields) == {'destination_id', 'use_count', 'last_used_at'}
        now = utcnow()
        boundary = await mail_filing.create_destination('Boundary', 'label')
        current = await mail_filing.create_destination('Current', 'label')
        async with get_session_maker().begin() as session:
            session.add_all([MailFilingUsage(destination_id=boundary.id, last_used_at=now - timedelta(days=90)),
                             MailFilingUsage(destination_id=current.id, last_used_at=now - timedelta(days=89))])
        assert {item.destination.id for item in await mail_filing_usage.list_quick_actions(now=now)} == {current.id}

    asyncio.run(run())


def test_concurrent_filings_keep_history_within_the_installation_cap(pg_db):
    async def run():
        from db.accessors import mail_filing, mail_filing_usage, mail_message
        from db.engine import get_session_maker
        from db.models.mail_folder import MailFolder
        from db.models.mail_filing_usage import MailFilingUsage

        destinations = [MailFolder(id=f'destination-{n:03}', name=f'Folder {n:03}', kind='folder') for n in range(205)]
        async with get_session_maker().begin() as session:
            session.add_all(destinations)
            session.add_all([MailFilingUsage(destination_id=row.id) for row in destinations[:200]])
        messages = [await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid=f'{n}') for n in range(5)]
        await asyncio.gather(*(mail_filing.assign([message.id], folder_id=destination.id)
                               for message, destination in zip(messages, destinations[200:], strict=True)))
        actions = await mail_filing_usage.list_quick_actions()
        assert len(actions) == 200
        assert {destination.id for destination in destinations[200:]}.issubset({item.destination.id for item in actions})

    asyncio.run(run())


def test_seed_history_is_repeatable_and_preserves_other_history(pg_db):
    async def run():
        from api.mail import dev_seed
        from api.mail.seed_ids import seed_id
        from db.accessors import mail_filing, mail_filing_usage, mail_message

        other = await mail_filing.create_destination('Other', 'folder')
        message = await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid='one')
        await mail_filing.assign([message.id], folder_id=other.id)
        await dev_seed.seed_filing()
        await dev_seed.seed_filing()
        actions = {item.destination.id: item.use_count for item in await mail_filing_usage.list_quick_actions()}
        assert actions[other.id] == 1
        assert actions[seed_id('folder', 'projects')] > 0
        assert actions[seed_id('label', 'important')] > 0

    asyncio.run(run())
