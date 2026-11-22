"""Actual resolver and agent payloads, with controlled model streams and live revocation."""
import asyncio
import json
import os
from types import SimpleNamespace as NS
import pytest
from db.accessors import chat_settings

pytestmark = pytest.mark.skipif(not os.getenv('RUN_PG_CONTRACT'), reason='RUN_PG_CONTRACT is not set')


def delta(content=None, calls=None):
    return NS(data=NS(choices=[NS(delta=NS(content=content, tool_calls=calls))]))


class Stream:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def __aiter__(self): return self.generate()
    async def generate(self):
        yield delta('Antwort')


def test_real_model_payload_scope_and_history(pg_db, monkeypatch, caplog):
    async def run():
        from api.mail import agent
        from db.accessors import user_preference as p, mail_account as a, prompt_template as t, chat_context as c
        from api.routers.api.v1.preferences.route import get_preference_context
        one = await a.create_account(provider='imap', auth_type='imap_password', email_address='one@example.test')
        two = await a.create_account(provider='imap', auth_type='imap_password', email_address='two@example.test')
        await chat_settings.update(1, [one.id, two.id])
        global_pref = await p.create(key='answer_length', value='test-only kurz')
        await p.create(key='signature', scope=one.id, value='test-only one')
        await p.create(key='signature', scope=two.id, value='test-only two')
        template = await t.create_template('Test', 'test-only ausführlich')
        requests = []
        async def stream_async(**kw):
            requests.append(kw)
            return Stream()
        monkeypatch.setattr(agent, 'mistral_client', lambda: NS(chat=NS(stream_async=stream_async)))
        async def turn(account=None, skip=False):
            resolved = await c.resolve(conversation_id=None, title='', model='', revision_id=template.revision_id,
                                       account_id=account, app_prompt_version='test', without_preferences=skip)
            events = [e async for e in agent.run_turn_events(conversation_id=None, text='Bitte lang antworten',
                account_id=account, resolved_context=resolved)]
            assert events[-1].event == 'done'
            return resolved[1]
        await turn(one.id)
        assert 'test-only kurz' not in json.dumps(requests[-1])
        await p.update_settings(1, use_preferences=True)
        for account, expected, absent in [(one.id, 'test-only one', 'test-only two'), (two.id, 'test-only two', 'test-only one'), (None, 'test-only kurz', 'test-only one')]:
            snapshot = await turn(account)
            sent = json.dumps(requests[-1], ensure_ascii=False)
            assert expected in sent and absent not in sent and 'test-only kurz' in sent
            assert 'test-only ausführlich' in sent
            messages = requests[-1]['messages']
            assert all(m['role'] == 'user' for m in messages if 'test-only' in (m.get('content') or ''))
            from api.mail.agent_prompt import reply_language_instruction
            assert messages[-2] == {'role': 'user', 'content': 'Bitte lang antworten'}
            assert messages[-1] == {'role': 'system', 'content': reply_language_instruction('en')}
            contents = [m.get('content') or '' for m in messages]
            assert 'Rangfolge' in contents[0]
            preference = next(i for i, text in enumerate(contents) if 'Stilpräferenzen' in text)
            chosen = next(i for i, text in enumerate(contents) if 'Nutzervorlage für diesen Turn' in text)
            assert 0 < preference < chosen < len(messages) - 2
        await p.change(global_pref.id, 1, value='test-only changed')
        historical = await get_preference_context(snapshot.id)
        assert historical.revisions[0].value == 'test-only kurz'
        await p.change(global_pref.id, 2, delete=True)
        assert not await c.is_live(snapshot.id)
        assert (await get_preference_context(snapshot.id)).revisions[0].value is None
        assert (await get_preference_context(snapshot.id)).revisions[0].removed
        await turn(one.id, skip=True)
        assert 'test-only one' not in json.dumps(requests[-1])
        assert 'test-only' not in caplog.text
    asyncio.run(run())


def test_revocation_reaches_only_turns_that_used_the_preference(pg_db):
    async def run():
        from db.accessors import user_preference as p, mail_account as a, chat_context as c
        one = await a.create_account(provider='imap', auth_type='imap_password', email_address='one@example.test')
        two = await a.create_account(provider='imap', auth_type='imap_password', email_address='two@example.test')
        await chat_settings.update(1, [one.id, two.id])
        await p.update_settings(1, use_preferences=True)
        signature = await p.create(key='signature', scope=one.id, value='test-only one')
        await p.create(key='answer_length', value='test-only paused', status='paused')
        async def turn(account):
            return (await c.resolve(conversation_id=None, title='', model='', revision_id=None,
                                    account_id=account, app_prompt_version='test'))[1]
        used, unused = await turn(one.id), await turn(two.id)
        # The paused global value is never resolved, for either account.
        assert json.loads(used.preference_revision_ids_json) == [signature.revision_id]
        assert unused.preferences_enabled and json.loads(unused.preference_revision_ids_json) == []
        await p.change(signature.id, 1, scope=two.id)
        assert not await c.is_live(used.id) and await c.is_live(unused.id)
        later = await turn(one.id)
        assert json.loads(later.preference_revision_ids_json) == []
        await p.update_settings(2, use_preferences=False)
        assert await c.is_live(unused.id) and await c.is_live(later.id)
    asyncio.run(run())


@pytest.mark.parametrize('action', ['pause', 'delete'])
def test_pause_and_delete_revoke_turns_of_every_revision(pg_db, action):
    async def run():
        from db.accessors import user_preference as p, mail_account as a, chat_context as c
        one = await a.create_account(provider='imap', auth_type='imap_password', email_address='one@example.test')
        await chat_settings.update(1, [one.id])
        await p.update_settings(1, use_preferences=True)
        await p.create(key='signature', scope=one.id, value='test-only one')
        async def turn(account):
            return (await c.resolve(conversation_id=None, title='', model='', revision_id=None,
                                    account_id=account, app_prompt_version='test'))[1]
        unrelated = await turn(one.id)
        row = await p.create(key='answer_length', value='test-only first')
        old = await turn(None)
        await p.change(row.id, 1, value='test-only second')
        # A value edit applies from the next turn; it does not revoke the turn that used the first revision.
        assert await c.is_live(old.id)
        current = await turn(None)
        assert json.loads(old.preference_revision_ids_json) != json.loads(current.preference_revision_ids_json)
        if action == 'pause': await p.change(row.id, 2, status='paused')
        else: await p.change(row.id, 2, delete=True)
        assert not await c.is_live(old.id) and not await c.is_live(current.id)
        assert await c.is_live(unrelated.id)
    asyncio.run(run())


def test_account_value_overrides_global_per_key(pg_db):
    async def run():
        from db.accessors import user_preference as p, mail_account as a, chat_context as c
        one = await a.create_account(provider='imap', auth_type='imap_password', email_address='one@example.test')
        two = await a.create_account(provider='imap', auth_type='imap_password', email_address='two@example.test')
        await chat_settings.update(1, [one.id, two.id])
        await p.update_settings(1, use_preferences=True)
        general = await p.create(key='answer_length', value='test-only global')
        specific = await p.create(key='answer_length', scope=one.id, value='test-only one')
        async def used(account):
            snapshot = (await c.resolve(conversation_id=None, title='', model='', revision_id=None,
                                        account_id=account, app_prompt_version='test'))[1]
            return json.loads(snapshot.preference_revision_ids_json)
        assert await used(one.id) == [specific.revision_id]
        assert await used(two.id) == [general.revision_id]
        assert await used(None) == [general.revision_id]
    asyncio.run(run())


def test_resolver_failure_requires_explicit_skip(pg_db, monkeypatch):
    async def run():
        from db.accessors import user_preference as p, chat_context as c
        from sqlalchemy.exc import SQLAlchemyError
        async def broken(*args):
            raise SQLAlchemyError('test-only')
        monkeypatch.setattr(p, 'resolve_in_session', broken)
        args = dict(conversation_id=None, title='', model='', revision_id=None, account_id=None, app_prompt_version='test')
        with pytest.raises(c.PreferencesUnavailable):
            await c.resolve(**args)
        _, snapshot, _ = await c.resolve(**args, without_preferences=True)
        assert not snapshot.preferences_enabled and snapshot.preference_revision_ids_json == '[]'
    asyncio.run(run())


@pytest.mark.parametrize('action', ['disable', 'delete', 'pause'])
def test_paused_stream_actual_revocation(pg_db, monkeypatch, action):
    async def run():
        from api.mail import agent
        from db.accessors import user_preference as p
        row = await p.create(key='answer_length', value='test-only')
        await p.update_settings(1, use_preferences=True)
        entered, release = asyncio.Event(), asyncio.Event()
        class Paused(Stream):
            async def generate(self):
                yield delta('Teil')
                entered.set()
                await release.wait()
                yield delta(calls=[NS(index=0, id='call', function=NS(name='suggest_followups', arguments='{"texts":["Weiter"]}'))])
        requests = []
        async def stream_async(**kw):
            requests.append(kw)
            return Paused()
        monkeypatch.setattr(agent, 'mistral_client', lambda: NS(chat=NS(stream_async=stream_async)))
        async def forbidden(*args):
            pytest.fail('revoked context reached a tool')
        monkeypatch.setattr(agent, 'execute_tool', forbidden)
        async def collect():
            return [e async for e in agent.run_turn_events(conversation_id=None, text='Hallo')]
        task = asyncio.create_task(collect())
        await asyncio.wait_for(entered.wait(), 5)
        if action == 'disable': await p.update_settings(2, use_preferences=False)
        elif action == 'delete': await p.change(row.id, 1, delete=True)
        else: await p.change(row.id, 1, status='paused')
        release.set()
        events = await asyncio.wait_for(task, 5)
        assert events[-1].code == 'context_revoked' and len(requests) == 1
    asyncio.run(run())
