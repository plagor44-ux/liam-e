"""Destination colours are local, validated and independent of account colours."""

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(not os.getenv('RUN_PG_CONTRACT'), reason='RUN_PG_CONTRACT is not set')


def test_destination_color_defaults_persists_and_rejects_invalid_values(pg_db):
    async def run():
        import httpx
        from fastapi import FastAPI
        from api.routers.api.v1.mail import folders

        app = FastAPI(); app.include_router(folders.router, prefix='/folders')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            neutral = await client.post('/folders', json={'name': 'Neutral', 'kind': 'folder'})
            assert neutral.status_code == 200 and neutral.json()['color'] == 'none'
            created = await client.post('/folders', json={'name': 'Important', 'kind': 'label', 'color': 'moss'})
            assert created.status_code == 200 and created.json()['color'] == 'moss'
            destination_id = created.json()['id']
            changed = await client.patch(f'/folders/{destination_id}', json={'name': 'Important', 'color': 'teal'})
            assert changed.status_code == 200 and changed.json()['color'] == 'teal'
            assert (await client.patch(f'/folders/{destination_id}', json={'name': 'Important', 'color': '#ffffff'})).status_code == 422
            assert (await client.patch(f'/folders/{destination_id}', json={'name': 'Important', 'color': None})).status_code == 422
            assert (await client.post('/folders', json={'name': 'Bad', 'kind': 'folder', 'color': 'invalid'})).status_code == 422
            renamed = await client.patch(f'/folders/{destination_id}', json={'name': 'Renamed'})
            assert renamed.status_code == 200 and renamed.json()['color'] == 'teal'
            rows = (await client.get('/folders')).json()
            assert next(row for row in rows if row['id'] == destination_id)['color'] == 'teal'

    asyncio.run(run())


def test_rule_assigned_label_keeps_its_destination_color(pg_db):
    async def run():
        from api.mail import rules
        from db.accessors import mail_filing, mail_message, mail_rule

        label = await mail_filing.create_destination('Important', 'label', 'teal')
        rule = await mail_rule.create(name='Example', sender_domain='example.org', label_ids=[label.id])
        message = await mail_message.upsert_message(account_id='synthetic', folder='INBOX', provider_uid='one', sender_address='person@example.org')
        run = await rules.execute_rule_run(rule.id, rule.version, message.id)
        assert run.status == 'done'
        assert (await mail_filing.for_message(message.id)).label_ids == [label.id]
        assert next(row for row in await mail_filing.list_destinations() if row.id == label.id).color == 'teal'

    asyncio.run(run())
