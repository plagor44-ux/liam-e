"""Live Postgres contracts for the owner default chat model setting."""
import asyncio
import json
import os
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi import FastAPI

from api.mail import agent, models_catalog as catalog
from api.routers.api.v1.chat import models as models_route, route, stream
from api.routers.api.v1.settings.chat import model as model_settings_route, route as chat_settings_route
from db.accessors import chat_context, chat_conversation, chat_message, chat_model_settings
from db.accessors.prompt_template import StaleVersion
from db.engine import get_session_maker
from db.models.chat_conversation import ChatConversation
from db.models.chat_model_settings import ChatModelSettings

pytestmark = pytest.mark.skipif(not os.getenv('RUN_PG_CONTRACT'), reason='RUN_PG_CONTRACT is not set')


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(chunk, str):
            return NS(data=NS(choices=[NS(delta=NS(content=chunk, tool_calls=None))]))
        return chunk


class _FakeMistral:
    def __init__(self, *, streams=(), models=()):
        self._streams = list(streams)
        self._models = list(models)
        self.requests = []
        self.chat = self
        self.models = self

    async def stream_async(self, **kwargs):
        self.requests.append(kwargs)
        result = self._streams.pop(0)
        if isinstance(result, Exception):
            raise result
        return _FakeStream(result)

    async def list_async(self, **kwargs):
        self.requests.append(kwargs)
        result = self._models.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _card(model_id: str, *aliases: str):
    return NS(id=model_id, aliases=list(aliases), capabilities=NS(completion_chat=True, function_calling=True))


def test_parallel_updates_and_revision_checks(pg_db):
    async def run():
        initial = await chat_model_settings.get()
        assert (initial.version, initial.default_model) == (1, None)

        edits = await asyncio.gather(
            chat_model_settings.update(1, 'mistral-small-latest'),
            chat_model_settings.update(1, 'mistral-large-latest'),
            return_exceptions=True,
        )
        assert sum(isinstance(edit, StaleVersion) for edit in edits) == 1
        winner = next(edit for edit in edits if not isinstance(edit, StaleVersion))
        assert winner.version == 2

        unchanged = await chat_model_settings.update(2, winner.default_model)
        assert unchanged.version == 2

        with pytest.raises(StaleVersion):
            await chat_model_settings.update(1, None)

        reset = await chat_model_settings.update(2, None)
        assert (reset.version, reset.default_model) == (3, None)

    asyncio.run(run())


def test_upgraded_installation_new_chats_and_legacy_semantics_e2e(pg_db, monkeypatch):
    async def run():
        # Legacy pre-feature conversation stored with empty model ('').
        async with get_session_maker().begin() as session:
            legacy = ChatConversation(title='Altbestand', model='')
            session.add(legacy)
            await session.flush()
            legacy_id = legacy.id

        monkeypatch.setattr(catalog.mail_env, 'mail_chat_model', 'mistral-medium-latest')
        monkeypatch.setattr(catalog, 'mistral_client', lambda: _FakeMistral(models=[NS(data=[
            _card('mistral-medium-3-5', 'mistral-medium-latest'),
            _card('mistral-small-2603', 'mistral-small-latest'),
            _card('mistral-large-4', 'mistral-large-latest'),
        ])]))
        catalog.reset_cache()

        app = FastAPI()
        app.include_router(chat_settings_route.router, prefix='/settings/chat')
        app.include_router(model_settings_route.router, prefix='/settings/chat/model')
        app.include_router(models_route.router, prefix='/chat/models')
        app.include_router(route.router, prefix='/chat')
        app.include_router(stream.router, prefix='/chat/stream')

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            # Fresh/upgraded installation defaults to app_default without touching chat_settings.
            assert (await client.get('/settings/chat/model')).json() == {
                'version': 1,
                'default_model': None,
                'effective_default_model': 'mistral-medium-latest',
                'app_default_model': 'mistral-medium-latest',
                'status': 'app_default',
            }
            assert (await client.get('/settings/chat')).json() == {'version': 1, 'account_ids': []}

            # Configure default model to mistral-small-latest.
            saved = await client.patch('/settings/chat/model', json={'expected_version': 1, 'default_model': 'mistral-small-latest'})
            assert saved.status_code == 200
            assert saved.json()['version'] == 2
            assert (await client.get('/settings/chat')).json() == {'version': 1, 'account_ids': []}

            # Stale conflict does not overwrite.
            conflict = await client.patch('/settings/chat/model', json={'expected_version': 1, 'default_model': None})
            assert conflict.status_code == 409
            assert (await client.get('/settings/chat/model')).json()['default_model'] == 'mistral-small-latest'

            # JSON turn without explicit model starts a new conversation with mistral-small-latest.
            fake_json = _FakeMistral(streams=[['Antwort JSON']])
            monkeypatch.setattr(agent, 'mistral_client', lambda: fake_json)
            json_res = await client.post('/chat', json={'message': 'Erster JSON Turn'})
            assert json_res.status_code == 200
            json_conv_id = json_res.json()['conversation_id']
            assert fake_json.requests[0]['model'] == 'mistral-small-latest'
            assert (await chat_conversation.get_conversation(json_conv_id)).model == 'mistral-small-latest'

            # SSE turn without explicit model starts a new conversation with mistral-small-latest.
            fake_sse = _FakeMistral(streams=[['Antwort SSE']])
            monkeypatch.setattr(agent, 'mistral_client', lambda: fake_sse)
            async with client.stream('POST', '/chat/stream', json={'text': 'Erster SSE Turn'}) as sse_res:
                assert sse_res.status_code == 200
                events = [json.loads(line[5:]) async for line in sse_res.aiter_lines() if line.startswith('data:')]
            start = next(e for e in events if e['event'] == 'start')
            assert start['model'] == 'mistral-small-latest'
            assert fake_sse.requests[0]['model'] == 'mistral-small-latest'
            sse_conv_id = start['conversation_id']
            assert (await chat_conversation.get_conversation(sse_conv_id)).model == 'mistral-small-latest'

            # Explicit model on new conversation overrides the setting.
            fake_override = _FakeMistral(streams=[['Override Antwort']])
            monkeypatch.setattr(agent, 'mistral_client', lambda: fake_override)
            async with client.stream('POST', '/chat/stream', json={'text': 'Explizite Wahl', 'model': 'mistral-large-latest'}) as sse_res:
                events = [json.loads(line[5:]) async for line in sse_res.aiter_lines() if line.startswith('data:')]
            override_conv_id = next(e for e in events if e['event'] == 'start')['conversation_id']
            assert fake_override.requests[0]['model'] == 'mistral-large-latest'
            assert (await chat_conversation.get_conversation(override_conv_id)).model == 'mistral-large-latest'

            # Change setting to mistral-large-latest: existing conversations & Altbestand keep their models!
            assert (await client.patch('/settings/chat/model', json={'expected_version': 2, 'default_model': 'mistral-large-latest'})).status_code == 200

            fake_existing = _FakeMistral(streams=[['Weiter']])
            monkeypatch.setattr(agent, 'mistral_client', lambda: fake_existing)
            assert (await client.post('/chat', json={'conversation_id': sse_conv_id, 'message': 'Zweite Frage'})).status_code == 200
            assert fake_existing.requests[0]['model'] == 'mistral-small-latest'

            fake_legacy = _FakeMistral(streams=[['Altbestand Antwort']])
            monkeypatch.setattr(agent, 'mistral_client', lambda: fake_legacy)
            assert (await client.post('/chat', json={'conversation_id': legacy_id, 'message': 'Altbestand Frage'})).status_code == 200
            assert fake_legacy.requests[0]['model'] == 'mistral-medium-latest'
            assert (await chat_conversation.get_conversation(legacy_id)).model == ''

            # Legacy conversation context & chat_models output keep app default identifiable.
            models = {m['id']: m for m in (await client.get('/chat/models')).json()}
            assert models['mistral-large-latest']['is_default'] is True
            assert models['mistral-medium-latest']['is_app_default'] is True

            # Removed stored model falls back for new turns and /chat/models.
            monkeypatch.setattr(catalog, '_cached_at', float('-inf'))
            monkeypatch.setattr(catalog, 'mistral_client', lambda: _FakeMistral(models=[NS(data=[_card('mistral-medium-3-5', 'mistral-medium-latest')])]))
            assert (await client.get('/settings/chat/model')).json()['status'] == 'unlisted'
            fallback_models = {m['id']: m for m in (await client.get('/chat/models')).json()}
            assert fallback_models['mistral-medium-latest']['is_default'] is True

            fake_fallback = _FakeMistral(streams=[['Fallback']])
            monkeypatch.setattr(agent, 'mistral_client', lambda: fake_fallback)
            fallback_res = await client.post('/chat', json={'message': 'Nach Wegfall'})
            assert fallback_res.status_code == 200
            assert fake_fallback.requests[0]['model'] == 'mistral-medium-latest'
            assert (await chat_conversation.get_conversation(fallback_res.json()['conversation_id'])).model == 'mistral-medium-latest'

    asyncio.run(run())


def test_alembic_upgrade_from_previous_head_preserves_existing_installation(monkeypatch):
    import uuid
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import MetaData, Table, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    import env.db
    from db import engine as db_engine

    project = Path(__file__).resolve().parents[1]
    name = f"upgrade_{uuid.uuid4().hex[:12]}"

    async def admin(stmt: str):
        engine = create_async_engine(env.db.env.async_database_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                await conn.execute(text(stmt))
        finally:
            await engine.dispose()

    asyncio.run(admin(f'CREATE DATABASE "{name}"'))
    url = make_url(env.db.env.async_database_url).set(database=name).render_as_string(hide_password=False)
    config = Config(str(project / "alembic.ini"))
    config.set_main_option("script_location", str(project / "src" / "db" / "migrations"))
    config.set_main_option("prepend_sys_path", str(project / "src"))
    monkeypatch.setattr(env.db.env, "database_url", url)

    try:
        command.upgrade(config, "ea71730d38eb")
        legacy = ChatConversation(title="Altbestand vor Upgrade", model="")
        pinned = ChatConversation(title="Fixiertes Modell vor Upgrade", model="mistral-large-latest")

        async def seed_previous():
            engine = create_async_engine(url)
            try:
                async with engine.begin() as conn:
                    def insert(sync_conn):
                        table = Table("chat_conversation", MetaData(), autoload_with=sync_conn)
                        for row in (legacy, pinned):
                            values = {col.name: getattr(row, col.name) for col in table.c if col.computed is None}
                            sync_conn.execute(table.insert().values(**values))
                    await conn.run_sync(insert)
            finally:
                await engine.dispose()

        asyncio.run(seed_previous())
        command.upgrade(config, "84c042b6122f")
        # The current API expects all later, unrelated schema changes too.
        command.upgrade(config, "heads")

        monkeypatch.setattr(catalog.mail_env, "mail_chat_model", "mistral-medium-latest")
        monkeypatch.setattr(catalog, "mistral_client", lambda: _FakeMistral(models=[NS(data=[
            _card("mistral-medium-3-5", "mistral-medium-latest"),
            _card("mistral-small-2603", "mistral-small-latest"),
            _card("mistral-large-4", "mistral-large-latest"),
        ])]))
        catalog.reset_cache()

        app = FastAPI()
        app.include_router(model_settings_route.router, prefix="/settings/chat/model")
        app.include_router(route.router, prefix="/chat")

        async def verify():
            await db_engine.dispose_engine()
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    assert (await client.get("/settings/chat/model")).json() == {
                        "version": 1,
                        "default_model": None,
                        "effective_default_model": "mistral-medium-latest",
                        "app_default_model": "mistral-medium-latest",
                        "status": "app_default",
                    }
                    assert (await client.patch("/settings/chat/model", json={"expected_version": 1, "default_model": "mistral-small-latest"})).status_code == 200

                    fake_legacy = _FakeMistral(streams=[["Antwort Altbestand"]])
                    monkeypatch.setattr(agent, "mistral_client", lambda: fake_legacy)
                    assert (await client.post("/chat", json={"conversation_id": legacy.id, "message": "Weiter"})).status_code == 200
                    assert fake_legacy.requests[0]["model"] == "mistral-medium-latest"

                    fake_pinned = _FakeMistral(streams=[["Antwort Fixiert"]])
                    monkeypatch.setattr(agent, "mistral_client", lambda: fake_pinned)
                    assert (await client.post("/chat", json={"conversation_id": pinned.id, "message": "Weiter"})).status_code == 200
                    assert fake_pinned.requests[0]["model"] == "mistral-large-latest"

                    fake_new = _FakeMistral(streams=[["Antwort Neu"]])
                    monkeypatch.setattr(agent, "mistral_client", lambda: fake_new)
                    new_res = await client.post("/chat", json={"message": "Neuer Chat"})
                    assert new_res.status_code == 200
                    assert fake_new.requests[0]["model"] == "mistral-small-latest"
            finally:
                await db_engine.dispose_engine()

        asyncio.run(verify())
    finally:
        monkeypatch.undo()
        asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
