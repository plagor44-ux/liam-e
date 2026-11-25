"""Live calendar-scope provenance and model-history isolation contracts."""
import asyncio
import copy
import json
import os
from types import SimpleNamespace as NS

import pytest

pytestmark = pytest.mark.skipif(not os.getenv('RUN_PG_CONTRACT'), reason='RUN_PG_CONTRACT is not set')


@pytest.fixture
def appointment_stream(pg_db, monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(Path(__file__).resolve().parents[4] / 'apps/api/tests')
    from support.fake_mistral import FakeMistral
    from api.calendar import writes
    from api.mail import agent

    fake = FakeMistral()
    dispatched = []
    async def execute(request, *, write_id):
        dispatched.append((request.title, request.source_id, write_id))
        return writes.WriteOutcome(write_id=write_id, status='done')
    monkeypatch.setattr(agent, 'mistral_client', lambda: fake)
    monkeypatch.setattr(writes, 'execute', execute)
    return fake.streams, dispatched


async def _seed_appointment_calendars(first='work', *, default='personal'):
    from db.accessors import calendar_settings
    from db.engine import get_session_maker
    from db.models.calendar_source import CalendarSource
    async with get_session_maker().begin() as session:
        for key in (first, default):
            session.add(CalendarSource(id=key, name=key.title(), provider='google', connection_ref='synthetic-' + key,
                external_id=key, capabilities_json='["read","write"]', timezone='UTC'))
    await calendar_settings.update(1, default)


def _create_args(title, start=None, end=None, **fields):
    args = {'action': 'create', 'title': title, 'timezone': 'UTC', **fields}
    if start and end:
        args.update(start_local='2026-11-21T' + start, end_local='2026-11-21T' + end)
    return args


def _calendar_calls(*arguments):
    def chunk(args, index):
        tool = NS(id=f'call{index}', index=index, type='function',
                  function=NS(name='propose_calendar_change', arguments=json.dumps(args)))
        return NS(data=NS(choices=[NS(delta=NS(content=None, tool_calls=[tool]))]))
    return [chunk(args, i) for i, args in enumerate(arguments)]


async def _visible_cards(conversation):
    from api.mail import agent
    from api.mail.chat_schemas import to_message_out
    from db.accessors import chat_message
    return [to_message_out(row) for row in agent.visible_messages(await chat_message.list_messages(conversation)) if row.role == 'proposal']


async def _appointment_turn(streams, conversation, text, answer, *calls):
    from api.mail import agent
    if calls:
        streams.append(_calendar_calls(*calls))
    streams.append([answer])
    events = [event async for event in agent.run_turn_events(conversation_id=conversation, text=text)]
    assert events[-1].event == 'done'
    return events[-1].conversation_id, [event.message for event in events if event.event == 'proposal']


@pytest.mark.parametrize('access,opening', [
    ('paused', 'Schedule a review; use Work'), ('disconnected', 'Schedule a review; use Work'),
    ('paused', 'Schedule a review in Work'), ('paused', 'Schedule a review'),
])
def test_unavailable_configured_calendar_after_plain_time_question(appointment_stream, access, opening):
    from db.accessors import calendar_source, chat_message
    from db.engine import get_session_maker
    from db.models.calendar_sync_state import CalendarSyncState
    from api.mail import proposals
    streams, dispatched = appointment_stream

    async def run():
        await _seed_appointment_calendars(default='home')
        if access == 'paused':
            await calendar_source.set_active('work', False)
        else:
            async with get_session_maker().begin() as session:
                session.add(CalendarSyncState(source_id='work', last_error='reconnect_required'))
        assert await calendar_source.readable_ids(None) == ['home']
        conversation, cards = await _appointment_turn(streams, None, opening, 'What time should the review start?')
        assert not cards
        history = await chat_message.list_messages(conversation)
        choice = json.loads(history[-1].extras_json)['calendar_clarification']
        named = 'Work' in opening
        if named:
            assert choice['name'].casefold() == 'work' and choice['source_id'] is None
        else:
            assert choice['source_id'] == 'home'
        args = _create_args('Review', '10:00', '11:00')
        _, cards = await _appointment_turn(streams, conversation, 'Tomorrow at ten for one hour.',
            'Which calendar?' if named else 'Ready.', args)
        if named:
            assert not cards and not dispatched
            history = await chat_message.list_messages(conversation)
            assert json.loads([row for row in history if row.role == 'tool'][-1].content)['error'] == 'calendar_name_unknown'
            _, cards = await _appointment_turn(streams, conversation, 'Use my default calendar instead.', 'Ready.', args)
        card, = cards
        assert card.proposal.calendar.source_id == 'home'
        assert (await proposals.confirm(card.id, selected_ids=[])).proposal_status == 'done'
        assert dispatched == [('Review', 'home', card.id)]
    asyncio.run(run())


@pytest.mark.parametrize('tool_names_calendar', [False, True])
def test_calendar_name_answer_is_saved_before_time_question(appointment_stream, tool_names_calendar):
    from api.mail import proposals
    from db.accessors import chat_message
    streams, dispatched = appointment_stream

    async def run():
        await _seed_appointment_calendars(default='home')
        conversation, cards = await _appointment_turn(streams, None,
            'Schedule the review in the Work or Home calendar.', 'Which calendar should I use?')
        assert not cards
        opening = (await chat_message.list_messages(conversation))[-1]
        assert json.loads(opening.extras_json)['calendar_clarification']['error'] == 'calendar_name_ambiguous'
        _, cards = await _appointment_turn(streams, conversation, 'Work', 'What time should the review start?')
        assert not cards and not dispatched
        question = (await chat_message.list_messages(conversation))[-1]
        target = json.loads(question.extras_json)['calendar_clarification']
        assert (target['source_id'], target['connection_ref'], target['error']) == ('work', 'synthetic-work', None)
        args = _create_args('Review', '09:00', '10:00')
        if tool_names_calendar:
            args['calendar_name'] = 'Work'
        _, (card,) = await _appointment_turn(streams, conversation, 'Tomorrow at 09:00 for one hour.', 'Ready.', args)
        assert card.proposal.calendar.source_id == 'work' and not dispatched
        assert (await proposals.confirm(card.id, selected_ids=[])).proposal_status == 'done'
        assert dispatched == [('Review', 'work', card.id)]
    asyncio.run(run())


@pytest.mark.parametrize('raw,confirm', [
    ('{', True), ('[]', False), ('{"calendar_followups":null}', True),
])
def test_corrupt_queue_cannot_append_or_block_settling_an_existing_card(appointment_stream, raw, confirm):
    from datetime import date
    from api.mail import agent_tools, proposals
    from db.engine import get_session_maker
    from db.models.chat_message import ChatMessage
    streams, dispatched = appointment_stream

    async def run():
        await _seed_appointment_calendars()
        conversation, (first,) = await _appointment_turn(streams, None, 'Schedule breakfast. Schedule lunch in Work.',
            'Breakfast is ready.', _create_args('Breakfast', '08:00', '09:00'))
        async with get_session_maker().begin() as session:
            row = await session.get(ChatMessage, first.id)
            row.extras_json = raw
        ctx = agent_tools.TurnContext(conversation, None, date(2026, 11, 21), context_id=first.context.id,
            calendar_source_ids=['personal', 'work'], user_text='Schedule lunch in Work.')
        ctx.calendar_sequence_id = first.id
        result = await agent_tools.execute_tool('propose_calendar_change', _create_args('Lunch', '12:00', '13:00'), ctx)
        assert json.loads(result.content) == {'error': 'proposal_not_pending'} and not dispatched
        pending, = await _visible_cards(conversation)
        assert pending.id == first.id and pending.proposal_status == 'pending'
        if confirm:
            assert (await proposals.confirm(first.id, selected_ids=[])).proposal_status == 'done'
            assert dispatched == [('Breakfast', 'personal', first.id)]
        else:
            assert (await proposals.cancel(first.id)).proposal_status == 'cancelled'
            assert not dispatched
        assert [card.id for card in await _visible_cards(conversation)] == [first.id]
    asyncio.run(run())


def test_malformed_followup_does_not_block_a_later_valid_card(appointment_stream):
    from api.mail import proposals
    from db.engine import get_session_maker
    from db.models.chat_message import ChatMessage
    streams, dispatched = appointment_stream

    async def run():
        await _seed_appointment_calendars()
        conversation, (first,) = await _appointment_turn(streams, None,
            'Schedule breakfast in Personal. Schedule lunch in Work.', 'Breakfast is ready.',
            _create_args('Breakfast', '08:00', '09:00'), _create_args('Lunch', '12:00', '13:00'))
        async with get_session_maker().begin() as session:
            row = await session.get(ChatMessage, first.id)
            extras = json.loads(row.extras_json)
            extras['calendar_followups'][:0] = [None, {'proposal': {}}]
            row.extras_json = json.dumps(extras)
        assert (await proposals.cancel(first.id)).proposal_status == 'cancelled'
        lunch, = [card for card in await _visible_cards(conversation) if card.proposal_status == 'pending']
        assert lunch.proposal.calendar.source_id == 'work' and not dispatched
        assert (await proposals.confirm(lunch.id, selected_ids=[])).proposal_status == 'done'
        assert dispatched == [('Lunch', 'work', lunch.id)]
    asyncio.run(run())


async def _change_work_access(change):
    from db.accessors import calendar_source
    from db.engine import get_session_maker
    from db.models.calendar_source import CalendarSource
    if change == 'paused':
        await calendar_source.set_active('work', False)
    elif change == 'removed':
        await calendar_source.remove('work')
    else:
        async with get_session_maker().begin() as session:
            source = await session.get(CalendarSource, 'work')
            if change == 'rebound':
                source.connection_ref = 'synthetic-replacement'
            else:
                assert change == 'read_only'
                source.capabilities_json = '["read"]'


@pytest.mark.parametrize('queued,change', [
    (False, 'paused'), (False, 'removed'), (False, 'rebound'), (True, 'read_only'), (True, 'rebound'),
])
def test_prepared_calendar_target_changes_before_storage(appointment_stream, monkeypatch, queued, change):
    from api.calendar import chat
    from api.mail import agent_tools
    from db.accessors import chat_message
    from datetime import date

    async def run():
        await _seed_appointment_calendars()
        first_request = 'Schedule breakfast in the Personal calendar.'
        second_request = 'Schedule lunch in the Work calendar.'
        streams, dispatched = appointment_stream
        text = first_request + ' ' + second_request if queued else first_request
        conversation, (first,) = await _appointment_turn(streams, None, text, 'Breakfast is ready.',
            _create_args('Breakfast', '09:00', '10:00'))
        ctx = agent_tools.TurnContext(conversation, None, date(2026, 11, 21), context_id=first.context.id,
            calendar_source_ids=['personal', 'work'], user_text=second_request)
        if queued:
            ctx.calendar_sequence_id = first.id
        prepare = chat.prepare
        async def revoke_after_prepare(*args):
            command = await prepare(*args)
            assert command.source_id == 'work' and command.connection_ref == 'synthetic-work'
            await _change_work_access(change)
            return command
        monkeypatch.setattr(chat, 'prepare', revoke_after_prepare)
        result = await agent_tools.execute_tool('propose_calendar_change', _create_args('Lunch', '12:00', '13:00'), ctx)
        assert json.loads(result.content) == {'error': 'calendar_not_writable'}
        saved = await chat_message.get_message(first.id)
        assert saved.proposal_status == 'pending'
        assert not json.loads(saved.extras_json or '{}').get('calendar_followups')
        assert [card.id for card in await _visible_cards(conversation)] == [first.id]
        assert not dispatched
    asyncio.run(run())


@pytest.mark.parametrize('change', ['read_only', 'rebound', None])
def test_concurrent_calendar_queue_entries_rechecked_at_publication(appointment_stream, change):
    from datetime import date
    from api.mail import agent_tools, proposals
    streams, dispatched = appointment_stream

    async def run():
        await _seed_appointment_calendars()
        opening = 'Schedule breakfast in Personal. Schedule lunch in Work. Schedule dinner in Personal.'
        conversation, (first,) = await _appointment_turn(streams, None, opening, 'Breakfast is ready.',
            _create_args('Breakfast', '09:00', '10:00'))
        async def enqueue(title, calendar, start, end):
            ctx = agent_tools.TurnContext(conversation, None, date(2026, 11, 21), context_id=first.context.id,
                calendar_source_ids=['personal', 'work'], user_text=f'Schedule {title} in the {calendar} calendar.')
            ctx.calendar_sequence_id = first.id
            result = await agent_tools.execute_tool('propose_calendar_change', _create_args(title, start, end), ctx)
            assert json.loads(result.content)['status'] == 'queued'
        await asyncio.gather(enqueue('Lunch', 'Work', '12:00', '13:00'), enqueue('Dinner', 'Personal', '18:00', '19:00'))
        if change:
            await _change_work_access(change)
        await proposals.cancel(first.id)
        for _ in range(1 if change else 2):
            card, = [card for card in await _visible_cards(conversation) if card.proposal_status == 'pending']
            assert (await proposals.confirm(card.id, selected_ids=[])).proposal_status == 'done'
        expected = [('Dinner', 'personal')] if change else [('Dinner', 'personal'), ('Lunch', 'work')]
        assert sorted((title, source) for title, source, _ in dispatched) == expected
        assert not [card for card in await _visible_cards(conversation) if card.proposal_status == 'pending']
    asyncio.run(run())


@pytest.mark.parametrize('confirm_first,superseded', [(False, False), (True, False), (True, True)])
def test_unfinished_appointment_survives_discarded_queue(appointment_stream, confirm_first, superseded):
    from datetime import date
    from api.mail import proposals, agent_tools
    from db.accessors import chat_message
    streams, dispatched = appointment_stream

    async def run():
        await _seed_appointment_calendars()
        opening = 'Schedule breakfast at 08:00. Schedule lunch in the Work calendar at 12:00. Schedule dinner in Personal.'
        conversation, (first,) = await _appointment_turn(streams, None, opening, 'What time for dinner?',
            _create_args('Breakfast', '08:00', '09:00'), _create_args('Lunch', '12:00', '13:00'), _create_args('Dinner'))
        await _change_work_access('read_only')
        if confirm_first:
            assert (await proposals.confirm(first.id, selected_ids=[])).proposal_status == 'done'
        else:
            assert (await proposals.cancel(first.id)).proposal_status == 'cancelled'
        assert [card.id for card in await _visible_cards(conversation)] == [first.id]
        if superseded:
            other_turn = agent_tools.TurnContext(conversation, None, date(2026, 11, 21), context_id=first.context.id,
                calendar_source_ids=['personal', 'work'], user_text='Schedule an independent appointment in Personal.')
            independent = await agent_tools.execute_tool('propose_calendar_change', _create_args('Independent', '10:00', '11:00'), other_turn)
            assert independent.proposal_message.proposal_status == 'pending'
        _, cards = await _appointment_turn(streams, conversation, 'Tomorrow at 18:00 for one hour.', 'Ready.',
            _create_args('Dinner', '18:00', '19:00'))
        if superseded:
            assert not cards
            history = await chat_message.list_messages(conversation)
            assert json.loads([row for row in history if row.role == 'tool'][-1].content)['error'] == 'proposal_not_pending'
            pending, = [card for card in await _visible_cards(conversation) if card.proposal_status == 'pending']
            assert pending.id == independent.proposal_message.id
            assert (await proposals.confirm(pending.id, selected_ids=[])).proposal_status == 'done'
            assert dispatched == [('Breakfast', 'personal', first.id), ('Independent', 'personal', pending.id)]
            return
        dinner, = cards
        assert dinner.proposal.calendar.source_id == 'personal'
        assert (await proposals.confirm(dinner.id, selected_ids=[])).proposal_status == 'done'
        expected = [('Breakfast', 'personal', first.id)] if confirm_first else []
        assert dispatched == [*expected, ('Dinner', 'personal', dinner.id)]
    asyncio.run(run())


@pytest.mark.parametrize('revoke_first', [False, True])
@pytest.mark.parametrize('resource', ['context', 'source'])
def test_queue_publication_serializes_with_context_revocation(appointment_stream, monkeypatch, revoke_first, resource):
    from sqlalchemy.exc import DBAPIError
    from api.mail import proposals
    from db.accessors import calendar_proposal_queue as queue
    from db.engine import get_session_maker
    from db.models.chat_context import ChatContext
    from db.models.calendar_source import CalendarSource
    streams, dispatched = appointment_stream

    async def run():
        await _seed_appointment_calendars()
        conversation, (first,) = await _appointment_turn(streams, None,
            'Schedule breakfast in Personal. Schedule lunch in Work.', 'Ready.',
            _create_args('Breakfast', '08:00', '09:00'), _create_args('Lunch', '12:00', '13:00'))
        async def revoke():
            try:
                async with get_session_maker().begin() as session:
                    if resource == 'context':
                        context = await session.get(ChatContext, first.context.id, with_for_update={'nowait': True})
                        context.revoked = True
                    else:
                        source = await session.get(CalendarSource, 'work', with_for_update={'nowait': True})
                        source.is_active = False
            except DBAPIError as error:
                assert error.orig.sqlstate == '55P03'  # locked by the publishing transaction
                return False
            return True
        revocations = []
        if revoke_first:
            assert await revoke()
        else:
            original = queue.can_offer
            async def checked(*args):
                allowed = await original(*args)
                if allowed:
                    revocations.append(await revoke())
                return allowed
            monkeypatch.setattr(queue, 'can_offer', checked)
        await proposals.cancel(first.id)
        pending = [c for c in await _visible_cards(conversation) if c.proposal_status == 'pending']
        if revoke_first:
            assert not pending
        else:
            assert revocations == [False]
            card, = pending
            assert await revoke()
            with pytest.raises(proposals.ProposalContextRevokedError):
                await proposals.confirm(card.id, selected_ids=[])
        assert not dispatched
    asyncio.run(run())


@pytest.mark.parametrize('representation', ['reordered', 'changed', 'malformed'])
def test_feed_binding_representation_during_a_real_chat_turn(appointment_stream, representation):
    from fastapi import HTTPException
    from sqlalchemy import select
    from api.mail import agent
    from db.accessors import chat_context
    from db.engine import get_session_maker
    from db.models.calendar_feed_config import CalendarFeedConfig
    from db.models.calendar_source import CalendarSource
    from db.models.chat_context import ChatContext
    streams, _ = appointment_stream

    async def run():
        async with get_session_maker().begin() as session:
            for key in ('a', 'b'):
                session.add(CalendarSource(id=key, name=key, provider='ics', connection_ref='synthetic-' + key,
                    external_id=key, timezone='UTC', capabilities_json='["read"]'))
                session.add(CalendarFeedConfig(source_id=key, encrypted_payload='synthetic-config-' + key))
        conversation, _ = await _appointment_turn(streams, None, 'What is on the calendar?', 'Synthetic agenda.')
        async with get_session_maker().begin() as session:
            prior = (await session.scalars(select(ChatContext).where(ChatContext.conversation_id == conversation))).one()
            bindings = json.loads(prior.calendar_feed_bindings_json)
            assert set(bindings) == {'a', 'b'}
            if representation == 'changed':
                bindings['a'] = 'a different fingerprint'
            prior.calendar_feed_bindings_json = (json.dumps(dict(reversed(bindings.items())), indent=2)
                if representation != 'malformed' else '{')
        if representation == 'reordered':
            assert await chat_context.is_live(prior.id)
            await _appointment_turn(streams, conversation, 'Continue.', 'The same two calendars.')
        else:
            assert not await chat_context.is_live(prior.id)
            with pytest.raises(HTTPException) as refusal:
                _ = [event async for event in agent.run_turn_events(conversation_id=conversation, text='Continue.')]
            assert refusal.value.detail['code'] == 'conversation_scope_change_requires_new'
            assert not streams
    asyncio.run(run())


@pytest.mark.parametrize('dentist_first', [False, True])
@pytest.mark.parametrize('shared_verb,clarify,early', [(False, False, False), (True, False, False),
                                                    (False, True, False), (False, True, True)])
@pytest.mark.parametrize('cancel_first', [False, True])
def test_two_appointment_requests_bind_each_card_to_its_own_target(appointment_stream, dentist_first, shared_verb, clarify, early, cancel_first):
    from api.mail import agent, proposals
    from db.accessors import calendar_settings
    streams, dispatched = appointment_stream

    async def run():
        await _seed_appointment_calendars()
        calls = [_create_args('Review', '09:00', '10:00', calendar_name='Work'), _create_args('Dentist appointment', '15:00', '15:30')]
        owner = 'Put the 09:00 review in the Work calendar. Separately, schedule a dentist appointment at 15:00.'
        if shared_verb:
            clauses = ['Schedule a 09:00 review in the Work calendar', 'a 15:00 dentist visit']
            owner = ' and '.join(clauses) + '.'
            calls[0]['appointment_request'], calls[1]['appointment_request'] = clauses
            calls[1]['title'] = 'Dentist visit'
        targets = ['work', 'personal']
        if dentist_first:
            calls.reverse()
            targets.reverse()
        initial = calls.copy()
        later = []
        if clarify:
            owner = owner.replace('09:00 ' if dentist_first else ' at 15:00', '')
            initial[1] = {key: value for key, value in calls[1].items() if key not in ('start_local', 'end_local')}
            final = {key: value for key, value in calls[1].items() if key != 'calendar_name'}
            later = [_calendar_calls(final), ['Ready.']]
        streams.extend([_calendar_calls(*initial), ['What time for the other appointment?' if clarify else 'Ready for confirmation.'], *later])
        events = [e async for e in agent.run_turn_events(conversation_id=None, text=owner)]
        assert events[-1].event == 'done'
        conversation = events[-1].conversation_id
        first, = [e.message for e in events if e.event == 'proposal']
        assert (first.proposal.calendar.after.title, first.proposal.calendar.source_id) == (calls[0]['title'], targets[0])
        assert first.proposal_status == 'pending' and dispatched == []
        assert 'synthetic-' not in first.model_dump_json()
        assert [row.id for row in await _visible_cards(conversation)] == [first.id]
        await calendar_settings.update(2, 'work')
        async def settle_first():
            if cancel_first:
                assert (await proposals.cancel(first.id)).proposal_status == 'cancelled'
            else:
                assert (await proposals.confirm(first.id, selected_ids=[])).proposal_status == 'done'
        if early:
            await settle_first()
        if clarify:
            answer = 'Tomorrow at 09:00 for an hour.' if dentist_first else 'Tomorrow at 15:00 for half an hour.'
            followup = [e async for e in agent.run_turn_events(conversation_id=conversation, text=answer)]
            assert followup[-1].event == 'done'
            assert any(event.event == 'proposal' for event in followup) == early
            if not early:
                assert [(row.id, row.proposal_status) for row in await _visible_cards(conversation)] == [(first.id, 'pending')]
        if not early:
            await settle_first()
        second, = [row for row in await _visible_cards(conversation) if row.proposal_status == 'pending']
        assert second.id != first.id and (second.proposal.calendar.after.title, second.proposal.calendar.source_id) == (calls[1]['title'], targets[1])
        assert len(dispatched) == int(not cancel_first)
        with pytest.raises(proposals.ProposalStateError):
            await proposals.confirm(first.id, selected_ids=[])
        assert (await proposals.confirm(second.id, selected_ids=[])).proposal_status == 'done'
        expected = [] if cancel_first else [(calls[0]['title'], targets[0], first.id)]
        assert dispatched == [*expected, (calls[1]['title'], targets[1], second.id)]
        assert all(row.proposal_status != 'pending' for row in await _visible_cards(conversation))
    asyncio.run(run())


@pytest.mark.parametrize('lunch_ready,dinner_calendar', [(True, 'Work'), (False, 'Work'), (False, 'Personal')])
def test_confirm_three_appointments_across_time_clarifications(appointment_stream, lunch_ready, dinner_calendar):
    from api.mail import agent, proposals
    from db.accessors import chat_message
    streams, dispatched = appointment_stream

    async def run():
        await _seed_appointment_calendars()
        conversation = None
        async def turn(text, calls, answer):
            nonlocal conversation
            streams.extend([_calendar_calls(*calls), [answer]])
            events = [event async for event in agent.run_turn_events(conversation_id=conversation, text=text)]
            assert events[-1].event == 'done'
            conversation = events[-1].conversation_id
            return [event.message.id for event in events if event.event == 'proposal']

        async def pending(title, calendar):
            card, = [row for row in await _visible_cards(conversation) if row.proposal_status == 'pending']
            assert (card.proposal.calendar.after.title, card.proposal.calendar.source_id) == (title, calendar)
            return card.id

        async def confirm(card_id):
            assert (await proposals.confirm(card_id, selected_ids=[])).proposal_status == 'done'

        breakfast = _create_args('Breakfast', '08:00', '09:00')
        lunch = _create_args('Lunch', '12:00', '13:00')
        dinner = _create_args('Dinner', '18:00', '19:00')
        lunch_to = 'personal' if lunch_ready else 'work'
        lunch_text = 'Schedule lunch at 12:00.' if lunch_ready else 'Schedule lunch in the Work calendar.'
        owner = f'Schedule breakfast at 08:00. {lunch_text} Schedule dinner in the {dinner_calendar} calendar.'
        first, = await turn(owner, [breakfast, lunch if lunch_ready else _create_args('Lunch'), _create_args('Dinner')],
                            'What time should dinner start?' if lunch_ready else 'What times for lunch and dinner?')
        assert first == await pending('Breakfast', 'personal') and dispatched == []
        if not lunch_ready:
            if dinner_calendar == 'Personal':
                assert await turn('Tomorrow at noon for one hour.', [lunch], 'Which appointment is that for?') == []
                history = await chat_message.list_messages(conversation)
                assert json.loads([row for row in history if row.role == 'tool'][-1].content)['error'] == 'calendar_appointment_ambiguous'
            assert await turn('Lunch at noon for one hour.', [lunch], 'What time for dinner?') == []
            assert await pending('Breakfast', 'personal') == first and dispatched == []
        await confirm(first)
        second = await pending('Lunch', lunch_to)
        assert await turn('Tomorrow at 18:00 for one hour.', [dinner], 'Ready for confirmation.') == []
        assert await pending('Lunch', lunch_to) == second
        assert dispatched == [('Breakfast', 'personal', first)]
        await confirm(second)
        third = await pending('Dinner', dinner_calendar.casefold())
        assert dispatched[-1] == ('Lunch', lunch_to, second)
        await confirm(third)
        assert dispatched == [('Breakfast', 'personal', first), ('Lunch', lunch_to, second), ('Dinner', dinner_calendar.casefold(), third)]
        assert [row.proposal_status for row in await _visible_cards(conversation)] == ['done', 'done', 'done']
    asyncio.run(run())


@pytest.mark.parametrize('dinner_first', [False, True])
@pytest.mark.parametrize('reply,clauses', [
    ('Schedule lunch at noon. Schedule dinner at 18:00.', None),
    ('Lunch at 12:00 for one hour, dinner at 18:00 for one hour',
     ('Lunch at 12:00 for one hour', 'dinner at 18:00 for one hour')),
    ('Lunch at noon for one hour and dinner at 18:00 for one hour.', None),
])
def test_clarify_both_times_in_one_reply(appointment_stream, dinner_first, reply, clauses):
    from api.mail import agent, proposals
    streams, dispatched = appointment_stream

    async def run():
        await _seed_appointment_calendars()
        streams.append(['What times for lunch and dinner?'])
        opening = 'Schedule lunch in the Work calendar. Schedule dinner in the Personal calendar.'
        events = [e async for e in agent.run_turn_events(conversation_id=None, text=opening)]
        conversation = events[-1].conversation_id
        assert not await _visible_cards(conversation)
        calls = [_create_args('Lunch', '12:00', '13:00'), _create_args('Dinner', '18:00', '19:00')]
        if clauses:
            for call, clause in zip(calls, clauses, strict=True):
                call['appointment_request'] = clause
        expected = [('Lunch', 'work', '12:00', '13:00'), ('Dinner', 'personal', '18:00', '19:00')]
        if dinner_first:
            calls.reverse()
            expected.reverse()
        streams.extend([_calendar_calls(*calls), ['Ready for confirmation.']])
        events = [e async for e in agent.run_turn_events(conversation_id=conversation, text=reply)]
        assert [e.event for e in events].count('proposal') == 1 and dispatched == []
        for title, source_id, start, end in expected:
            card, = [row for row in await _visible_cards(conversation) if row.proposal_status == 'pending']
            command = card.proposal.calendar
            assert (command.after.title, command.source_id) == (title, source_id)
            assert (command.after.start_local, command.after.end_local) == ('2026-11-21T' + start, '2026-11-21T' + end)
            assert (await proposals.confirm(card.id, selected_ids=[])).proposal_status == 'done'
            assert dispatched[-1] == (title, source_id, card.id)
        assert len(dispatched) == 2
    asyncio.run(run())


@pytest.mark.parametrize('reply,named_tool_argument,switch_default,ask_duration', [
    ('Schedule the review in the Work calendar at 09:00 for one hour.', False, False, False),
    ('Schedule the review in the Work calendar at 09:00 for one hour.', True, False, False),
    ('Use the Work calendar.', False, False, False),
    ('Use the Work calendar.', True, False, False),
    ('Work.', False, False, False),
    ('Schedule the review in my default calendar at 09:00 for one hour.', False, True, False),
    ('Use my default calendar.', False, True, False),
    ('Use the Work calendar.', False, False, True),
    ('Use my default calendar.', False, True, True),
])
def test_clarify_same_title_by_original_calendar(appointment_stream, monkeypatch, reply, named_tool_argument, switch_default, ask_duration):
    from api.mail import agent, proposals
    from db.accessors import calendar_settings, chat_message
    streams, dispatched = appointment_stream

    async def run():
        await _seed_appointment_calendars()
        streams.append(['What times should the two reviews start?'])
        opening = 'Schedule the review in the Work calendar. Schedule the review in the Personal calendar.'
        events = [e async for e in agent.run_turn_events(conversation_id=None, text=opening)]
        conversation = events[-1].conversation_id

        async def turn(text, call, answer):
            streams.extend([_calendar_calls(call), [answer]])
            events = [e async for e in agent.run_turn_events(conversation_id=conversation, text=text)]
            assert events[-1].event == 'done'
            return [e.message for e in events if e.event == 'proposal']

        morning = _create_args('Review', '09:00', '10:00')
        assert await turn('The review at 09:00 for one hour.', {**morning, 'calendar_name': 'Work'}, 'Which review is that for?') == []
        history = await chat_message.list_messages(conversation)
        assert json.loads([row for row in history if row.role == 'tool'][-1].content)['error'] == 'calendar_appointment_ambiguous'
        if named_tool_argument:
            morning['calendar_name'] = 'Work'
        read_settings = calendar_settings.get
        reads = []
        if switch_default:
            await calendar_settings.update(2, 'work')
            async def change_after_read():
                setting = await read_settings()
                reads.append(setting.default_source_id)
                if len(reads) == 1:
                    await calendar_settings.update(setting.version, 'personal')
                return setting
            monkeypatch.setattr(calendar_settings, 'get', change_after_read)
        if ask_duration:
            streams.append(['Which duration would work for this review?' if switch_default else 'How long should this review last?'])
            answer = [e async for e in agent.run_turn_events(conversation_id=conversation, text=reply)]
            assert answer[-1].event == 'done' and not await _visible_cards(conversation)
            first, = await turn('One hour.', morning, 'What time is the other review?')
        else:
            first, = await turn(reply, morning, 'What time is the other review?')
        assert first.proposal.calendar.source_id == 'work' and first.proposal.calendar.after.start_local == '2026-11-21T09:00'
        stored = json.loads((await chat_message.get_message(first.id)).proposal_json)['calendar']
        assert stored['connection_ref'] == 'synthetic-work'
        if switch_default:
            assert reads == ['work'] and (await read_settings()).default_source_id == 'personal'
        assert dispatched == []
        assert (await proposals.confirm(first.id, selected_ids=[])).proposal_status == 'done'
        await calendar_settings.update((await read_settings()).version, 'work')
        other, = await turn('Tomorrow at 15:00 for one hour.', _create_args('Review', '15:00', '16:00'), 'Ready for confirmation.')
        assert other.proposal.calendar.source_id == 'personal' and other.proposal.calendar.after.start_local == '2026-11-21T15:00'
        stored = json.loads((await chat_message.get_message(other.id)).proposal_json)['calendar']
        assert stored['connection_ref'] == 'synthetic-personal'
        assert (await proposals.confirm(other.id, selected_ids=[])).proposal_status == 'done'
        assert dispatched == [('Review', 'work', first.id), ('Review', 'personal', other.id)]
    asyncio.run(run())


@pytest.mark.parametrize('selection', ['Office.', 'Use the Office calendar.'])
@pytest.mark.parametrize('suggest_office', [False, True])
def test_venue_does_not_select_a_review_calendar(appointment_stream, selection, suggest_office):
    from api.mail import proposals
    streams, dispatched = appointment_stream

    async def run():
        await _seed_appointment_calendars('office')
        opening = 'Schedule the review in the Office calendar. Schedule the review in the Personal calendar.'
        conversation, cards = await _appointment_turn(streams, None, opening, 'What times for the reviews?')
        assert cards == []
        morning = _create_args('Review', '09:00', '10:00', location='office kitchen')
        guess = {**morning, 'calendar_name': 'Office'} if suggest_office else morning
        _, cards = await _appointment_turn(streams, conversation, 'The review is at 09:00 for one hour in the office kitchen.',
                                          'Which review is that for?', guess)
        assert cards == [] and not await _visible_cards(conversation) and dispatched == []
        _, (office,) = await _appointment_turn(streams, conversation, selection, 'What time for the other review?', morning)
        assert (office.proposal.calendar.source_id, office.proposal.calendar.after.location) == ('office', 'office kitchen')
        assert (await proposals.confirm(office.id, selected_ids=[])).proposal_status == 'done'
        _, (personal,) = await _appointment_turn(streams, conversation, '15:00 for one hour.', 'Ready.', _create_args('Review', '15:00', '16:00'))
        assert personal.proposal.calendar.source_id == 'personal'
        assert (await proposals.confirm(personal.id, selected_ids=[])).proposal_status == 'done'
        assert dispatched == [('Review', 'office', office.id), ('Review', 'personal', personal.id)]
    asyncio.run(run())


@pytest.mark.parametrize('question,needs_choice', [
    ('What time should the review in the Personal calendar start?', False),
    ('What time should the review in the Work or Personal calendar start?', True),
    ('What time should the review in the Travel calendar start?', True),
    ('Which review do you mean?', True),
])
def test_time_question_selects_only_a_unique_bound_review(appointment_stream, question, needs_choice):
    from api.mail import proposals
    streams, dispatched = appointment_stream

    async def run():
        await _seed_appointment_calendars()
        opening = 'Schedule the review in the Work calendar. Schedule the review in the Personal calendar.'
        conversation, _ = await _appointment_turn(streams, None, opening, 'What times for the reviews?')
        partial = _create_args('Review', start_local='2026-11-21T09:00')
        _, cards = await _appointment_turn(streams, conversation, 'Schedule the review in the Work calendar at 09:00.', question, partial)
        assert cards == []
        afternoon = _create_args('Review', '15:00', '16:00')
        _, cards = await _appointment_turn(streams, conversation, '15:00 for one hour.',
                                          'Which review?' if needs_choice else 'Ready.', afternoon)
        if needs_choice:
            assert cards == [] and dispatched == []
            _, cards = await _appointment_turn(streams, conversation, 'Use the Personal calendar.', 'Ready.', afternoon)
        personal, = cards
        assert (personal.proposal.calendar.source_id, personal.proposal.calendar.after.start_local) == ('personal', '2026-11-21T15:00')
        assert (await proposals.confirm(personal.id, selected_ids=[])).proposal_status == 'done'
        _, (work,) = await _appointment_turn(streams, conversation, 'One hour starting at 09:00.', 'Ready.', _create_args('Review', '09:00', '10:00'))
        assert (work.proposal.calendar.source_id, work.proposal.calendar.after.start_local) == ('work', '2026-11-21T09:00')
        assert (await proposals.confirm(work.id, selected_ids=[])).proposal_status == 'done'
        assert dispatched == [('Review', 'personal', personal.id), ('Review', 'work', work.id)]
    asyncio.run(run())


def test_calendar_scope_and_model_history_isolation(pg_db, monkeypatch):
    async def run():
        from db.engine import get_session_maker
        from db.models.calendar_source import CalendarSource
        from db.accessors import chat_context, chat_message, chat_conversation, calendar_source
        from api.mail import agent
        from fastapi import HTTPException

        sources = [CalendarSource(provider='synthetic', connection_ref='test-only', external_id=key,
            name=key, timezone='UTC', capabilities_json='["read"]') for key in ('work', 'private')]
        async with get_session_maker()() as session:
            session.add_all(sources)
            await session.commit()
        ids = [s.id for s in sources]
        async def resolve(id=None, scope=None):
            return await chat_context.resolve(conversation_id=id, title='Test', model='', revision_id=None,
                account_id=None, app_prompt_version='test', calendar_source_ids=scope or [])
        conversation, snapshot, _ = await resolve(scope=ids)
        assert json.loads(snapshot.calendar_scope_json) == sorted(ids)
        for role in ('user', 'tool', 'assistant'):
            await chat_message.add_message(conversation_id=conversation.id, role=role,
                content='test-only foreign calendar marker', context_id=snapshot.id)
        requests = []
        class Stream:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def __aiter__(self):
                yield NS(data=NS(choices=[NS(delta=NS(content='Antwort', tool_calls=None))]))
        class Chat:
            async def stream_async(self, **kwargs):
                requests.append(copy.deepcopy(kwargs))
                return Stream()
        monkeypatch.setattr(agent, 'mistral_client', lambda: NS(chat=Chat()))
        for scope in ([], [ids[0]], [ids[1]]):
            with pytest.raises(HTTPException) as error:
                _ = [e async for e in agent.run_turn_events(conversation_id=conversation.id, text='Weiter', calendar_source_ids=scope)]
            assert error.value.status_code == 409
            assert requests == []
        # Even expansion starts fresh; never reinterpret provenance of older user/assistant text.
        single, single_context, _ = await resolve(scope=[ids[0]])
        await chat_message.add_message(conversation_id=single.id, role='user', content='Test', context_id=single_context.id)
        with pytest.raises(chat_context.ScopeChange): await resolve(single.id, ids)
        legacy = await chat_conversation.create_conversation()
        await chat_message.add_message(conversation_id=legacy.id, role='assistant', content='Unbelegter Altverlauf')
        with pytest.raises(chat_context.ScopeChange): await resolve(legacy.id, ids)
        with pytest.raises(chat_context.ScopeChange): await resolve(legacy.id)
        events = [e async for e in agent.run_turn_events(conversation_id=None, text='Neue Frage', calendar_source_ids=[ids[0]])]
        assert events[-1].event == 'done'
        assert len(requests) == 1
        assert 'foreign calendar marker' not in json.dumps(requests)
        assert 'Unbelegter Altverlauf' not in json.dumps(requests)
        assert len(await chat_message.list_messages(conversation.id)) == 3
        await calendar_source.set_active(ids[0], False)
        assert not await chat_context.is_live(snapshot.id)
        with pytest.raises(chat_context.ScopeChange): await resolve(conversation.id, ids)
        await calendar_source.remove(ids[1])
        with pytest.raises(ValueError): await resolve(scope=[ids[1]])
    asyncio.run(run())


def test_calendar_seed_roundtrip_and_repeatability(pg_db, monkeypatch):
    async def run():
        from datetime import UTC, datetime
        from api.mail.dev_seed import seed_calendars
        from db.accessors import calendar_source, calendar_occurrence
        from db.engine import get_session_maker
        from db.models.calendar_source import CalendarSource
        outsider = CalendarSource(provider='synthetic', connection_ref='test-only', external_id='other', name='Other', timezone='UTC')
        async with get_session_maker()() as session:
            session.add(outsider)
            await session.commit()
        for _ in range(2):
            await seed_calendars(datetime(2026, 11, 13, 12, tzinfo=UTC))
        assert len(await calendar_source.list_all()) == 6
        assert await calendar_source.get(outsider.id) is not None
        from api.mail.seed_ids import seed_id
        assert await calendar_occurrence.get_visible(seed_id('calendar-occurrence', 'work')) is not None
        assert await calendar_occurrence.get_visible(seed_id('calendar-occurrence', 'paused')) is None
    asyncio.run(run())


def test_seeded_conversations_bind_readable_calendars_and_accept_a_follow_up_turn(pg_db):
    async def run():
        from datetime import UTC, datetime
        from api.mail import dev_seed
        from api.mail.agent_prompt import APP_PROMPT_VERSION
        from db.accessors import calendar_source, chat_context, chat_message, chat_settings
        from db.engine import get_session_maker
        from db.models.chat_context import ChatContext

        from api.mail.seed_ids import SEED_ACCOUNT_IDS

        now = datetime(2026, 11, 13, 12, tzinfo=UTC)
        await dev_seed.seed(now)
        # The owner grants the eligible seed accounts and reseeds, as for a visual check; contexts follow the grant.
        settings = await chat_settings.get()
        async with get_session_maker()() as session:
            eligible = await chat_settings.active_ids(session, sorted(SEED_ACCOUNT_IDS))
        assert eligible
        await chat_settings.update(settings.version, eligible)
        await dev_seed.seed(now)
        grants = await chat_settings.get()
        assert grants.account_ids == eligible
        keys = ('was-ist-heute-wichtig', 'newsletter-aufraeumen', 'angebot-meyer-gmbh', 'belege-fuer-die-steuer', 'skill-offene-rechnungen')
        async with get_session_maker()() as session:
            readable = await calendar_source.readable_ids(session)
            assert readable
            for key in keys:
                conversation_id = dev_seed.seed_id('conversation', key)
                rows = await chat_message.list_messages(conversation_id)
                assert rows and all(row.context_id for row in rows)
                for context_id in {row.context_id for row in rows}:
                    context = await session.get(ChatContext, context_id)
                    assert context.conversation_id == conversation_id and context.mail_scope_version == grants.version
                    assert json.loads(context.calendar_scope_json) == readable
                    assert await calendar_source.permits_context(session, context)
        for key in keys[:4]:
            await chat_context.resolve(conversation_id=dev_seed.seed_id('conversation', key), title='Test', model='',
                                       revision_id=None, account_id=None, app_prompt_version=APP_PROMPT_VERSION)
        # The former seed left the calendar scope empty; such history still needs a new conversation.
        unbound = dev_seed.seed_id('conversation', 'angebot-meyer-gmbh')
        async with get_session_maker()() as session:
            stale = ChatContext(conversation_id=unbound, app_prompt_version=APP_PROMPT_VERSION, mail_scope_version=grants.version,
                                account_scope_json=json.dumps({'account_ids': await chat_settings.active_ids(session, grants.account_ids), 'selection': None}))
            session.add(stale)
            await session.commit()
        await chat_message.add_message(conversation_id=unbound, role='user', content='Synthetic', context_id=stale.id)
        with pytest.raises(chat_context.ScopeChange):
            await chat_context.resolve(conversation_id=unbound, title='Test', model='', revision_id=None,
                                       account_id=None, app_prompt_version=APP_PROMPT_VERSION)
    asyncio.run(run())


def test_automatic_calendar_scope_and_coverage_beyond_fifty_sources(pg_db):
    async def run():
        from datetime import UTC, date, datetime, timedelta
        from db.accessors import calendar_source, chat_context, chat_message
        from db.engine import get_session_maker
        from db.models.calendar_occurrence import CalendarOccurrence
        from db.models.calendar_source import CalendarSource
        from db.models.calendar_sync_state import CalendarSyncState
        from api.mail.calendar_tools import read_calendar

        async def resolve(conversation_id=None, scope=None):
            return await chat_context.resolve(conversation_id=conversation_id, title='Test', model='',
                revision_id=None, account_id=None, app_prompt_version='test', calendar_source_ids=scope)

        async def search(ids):
            result, _ = await read_calendar('search_calendar', {'from': '2026-11-12', 'to': '2026-11-14'}, ids, 'UTC')
            return result

        def source(key, **options):
            return CalendarSource(id=key, provider='ics', connection_ref='test-only', external_id=key,
                                  name=key, timezone='UTC', **options)

        _, empty, _ = await resolve()
        assert json.loads(empty.calendar_scope_json) == []
        assert not (await search([]))['index_complete_for_window']
        readable = [source(f'test-{n:02}', capabilities_json='["read"]') for n in range(52)]
        paused = source('paused-private', capabilities_json='["read"]', is_active=False)
        denied = source('denied-private', capabilities_json='[]')
        gone = source('reconnect-private', capabilities_json='["read"]')
        maker = get_session_maker()
        async with maker.begin() as session:
            session.add(readable[0])
        conversation, single, _ = await resolve()
        assert json.loads(single.calendar_scope_json) == [readable[0].id]
        await chat_message.add_message(conversation_id=conversation.id, context_id=single.id, role='user', content='Test')
        async with maker.begin() as session:
            session.add_all([*readable[1:], paused, denied, gone])
            await session.flush()
            session.add(CalendarSyncState(source_id=gone.id, last_error='reconnect_required'))
            session.add(CalendarOccurrence(source_id=readable[-1].id, event_id='synthetic-event', external_id='test',
                title='Last source', start_date=date(2026, 11, 13), end_date=date(2026, 11, 14)))
            session.add(CalendarOccurrence(source_id=readable[0].id, event_id='synthetic-multiday', external_id='test-span',
                title='Ongoing', start_date=date(2026, 11, 10), end_date=date(2026, 11, 13)))
        with pytest.raises(chat_context.ScopeChange):
            await resolve(conversation.id)
        _, snapshot, _ = await resolve()
        ids = json.loads(snapshot.calendar_scope_json)
        assert ids == [item.id for item in readable]
        _, narrow, _ = await resolve(scope=[])
        assert json.loads(narrow.calendar_scope_json) == []
        with pytest.raises(ValueError):
            await resolve(scope=[denied.id])
        result = await search(ids)
        assert result['source_count'] == 52
        assert not result['index_complete_for_window']
        assert result['unavailable_source_counts'] == {'paused': 1, 'disconnected': 1, 'unreadable': 1}
        assert not any(item.id in json.dumps(result) for item in (paused, denied, gone))
        assert [event['title'] for event in result['untrusted_calendar_data']['occurrences']] == ['Ongoing', 'Last source']

        now = datetime.now(UTC)
        async with maker.begin() as session:
            for item in readable:
                session.add(CalendarSyncState(source_id=item.id, incomplete=False, last_success_at=now,
                    coverage_start=datetime(2026, 11, 12, tzinfo=UTC), coverage_end=datetime(2026, 11, 14, tzinfo=UTC)))
        assert (await search(ids))['index_complete_for_window']
        async with maker.begin() as session:
            state = await session.get(CalendarSyncState, readable[-1].id)
            state.last_success_at = now - timedelta(days=2)
        assert not (await search(ids))['index_complete_for_window']
        await calendar_source.set_active(readable[-1].id, False)
        assert not await chat_context.is_live(snapshot.id)
        assert (await search(ids))['error'] == 'calendar_scope_revoked'
        await calendar_source.remove(readable[0].id)
        _, remaining, _ = await resolve()
        assert json.loads(remaining.calendar_scope_json) == ids[1:-1]
    asyncio.run(run())


def test_calendar_default_revision_and_proposal_target(pg_db, monkeypatch):
    async def run():
        from dataclasses import replace
        from datetime import date
        from db.accessors import calendar_settings, calendar_source
        from db.accessors.prompt_template import StaleVersion
        from db.engine import get_session_maker
        from db.models.calendar_source import CalendarSource
        from api.calendar import chat, writes
        from api.mail import proposals
        from api.mail.agent_tools import TurnContext
        from db.accessors import chat_context

        def source(key, name, provider='google', write=True):
            return CalendarSource(id=key, provider=provider, connection_ref='test-only', external_id=key, name=name,
                                  timezone='UTC', capabilities_json='["read","write"]' if write else '["read"]')

        first, second = source('primary', 'Team'), source('secondary', 'Privat')
        feed, twin = source('ics', 'Ferien', 'ics', False), source('twin', 'Team')
        paused = source('paused-research', 'Research')
        west = source('paused-west', 'Team West')
        paused.is_active = west.is_active = False
        async with get_session_maker().begin() as session:
            session.add_all([first, second, feed, twin, paused, west])
        conversation, snapshot, _ = await chat_context.resolve(conversation_id=None, title='Test', model='',
            revision_id=None, account_id=None, app_prompt_version='test')
        ctx = TurnContext(conversation.id, None, date(2026, 11, 12), context_id=snapshot.id,
                          calendar_source_ids=json.loads(snapshot.calendar_scope_json))
        values = {'action': 'create', 'title': 'Test', 'start_date': '2026-11-13', 'end_date': '2026-11-14'}
        assert (await calendar_settings.get()).default_source_id is None
        with pytest.raises(writes.WriteError, match='calendar_default_missing'):
            await chat.prepare(values, ctx)
        with pytest.raises(calendar_settings.CalendarNotWritable):
            await calendar_settings.update(1, feed.id)
        race = await asyncio.gather(*(calendar_settings.update(1, s.id) for s in (first, second)), return_exceptions=True)
        assert sum(isinstance(item, StaleVersion) for item in race) == 1
        chosen = await calendar_settings.get()
        assert chosen.version == 2 and chosen.default_source_id in {first.id, second.id}
        card = await chat.prepare(values, ctx)
        assert card.source_id == chosen.default_source_id
        from api.calendar.chat_target import choice_for_clarification
        unfinished = await choice_for_clarification(replace(ctx, user_text='Schedule lunch'), 'What time?')
        assert unfinished.source_id == chosen.default_source_id and unfinished.connection_ref == card.connection_ref
        for unavailable, text in [(paused, 'Schedule a review in the Research calendar'), (west, 'Plane die Besprechung im Kalender Team West')]:
            assert unavailable.id not in ctx.calendar_source_ids
            pending = await choice_for_clarification(replace(ctx, user_text=text), 'What time?')
            assert pending and pending.name.casefold() == unavailable.name.casefold()
            with pytest.raises(writes.WriteError, match='calendar_name_unknown'):
                await chat.prepare(values, replace(ctx, calendar_choice=pending, user_text='Morgen um neun.'))
        for instruction in ('Use the Privat calendar', 'in the Privat calendar'):
            selection = replace(ctx, user_text=f'Schedule lunch tomorrow at ten {instruction}, research or team west.')
            with pytest.raises(writes.WriteError, match='calendar_name_ambiguous'):
                await chat.prepare(values, selection)
            reply = replace(ctx, calendar_choice=selection.requested_calendar, user_text='Privat, bitte.')
            assert (await chat.prepare(values, reply)).source_id == second.id
            condition = replace(ctx, user_text=f'Schedule lunch tomorrow at ten {instruction}, rain or shine.')
            assert (await chat.prepare(values, condition)).source_id == second.id
        dispatched = []
        async def execute(request, *, write_id):
            dispatched.append((request.source_id, write_id))
            return writes.WriteOutcome(write_id=write_id, status='done')
        monkeypatch.setattr(writes, 'execute', execute)
        saved = await proposals.create_proposal(conversation.id,
            proposals.Proposal(kind='calendar', reason='Test', items=[], calendar=card), context_id=snapshot.id)
        other = first if chosen.default_source_id == second.id else second
        await calendar_settings.update(2, other.id)
        followup = replace(ctx, user_text='Tomorrow at noon', calendar_choice=unfinished)
        continued = await chat.prepare(values, followup)
        assert (continued.source_id, continued.connection_ref) == (card.source_id, card.connection_ref)
        async with get_session_maker().begin() as session:
            session.add(source('newly-connected', 'Additional'))
        assert await chat_context.is_live(snapshot.id)
        assert saved.proposal_status == 'pending' and dispatched == []
        assert (await proposals.confirm(saved.id, selected_ids=[])).proposal_status == 'done'
        with pytest.raises(proposals.ProposalStateError):
            await proposals.confirm(saved.id, selected_ids=[])
        assert dispatched == [(chosen.default_source_id, saved.id)]
        assert (await chat.prepare(values, ctx)).source_id == other.id
        named = replace(ctx, user_text='Bitte im Kalender PRIVAT eintragen')
        explicit = await chat.prepare({**values, 'calendar_name': 'Privat'}, named)
        assert explicit.source_id == second.id
        with pytest.raises(writes.WriteError, match='calendar_name_required'):
            await chat.prepare({**values, 'calendar_name': 'Team'}, named)
        for name, code in [('Team', 'calendar_name_ambiguous'), ('Wunsch', 'calendar_name_unknown'), ('Ferien', 'calendar_not_writable')]:
            named = replace(ctx, user_text=f'Bitte in {name} eintragen')
            with pytest.raises(writes.WriteError, match=code):
                await chat.prepare({**values, 'calendar_name': name}, named)
            with pytest.raises(writes.WriteError, match=code):
                await chat.prepare(values, named)
        with pytest.raises(writes.WriteError, match='calendar_name_required'):
            await chat.prepare({**values, 'source_id': chosen.default_source_id}, ctx)
        later = await proposals.create_proposal(conversation.id,
            proposals.Proposal(kind='calendar', reason='Test', items=[], calendar=await chat.prepare(values, ctx)),
            context_id=snapshot.id)
        await calendar_source.set_active(other.id, False)
        with pytest.raises(proposals.ProposalContextRevokedError):
            await proposals.confirm(later.id, selected_ids=[])
        assert dispatched == [(chosen.default_source_id, saved.id)]
        with pytest.raises(writes.WriteError, match='calendar_not_writable'):
            await chat.prepare(values, ctx)
        await calendar_source.remove(other.id)
        assert (await calendar_settings.get()).default_source_id == other.id
        ctx.calendar_source_ids.remove(other.id)
        with pytest.raises(writes.WriteError, match='calendar_default_unavailable'):
            await chat.prepare(values, ctx)
        assert (await calendar_settings.update(3, None)).default_source_id is None
    asyncio.run(run())


@pytest.mark.parametrize('denial', ['paused', 'missing_read', 'disconnected'])
def test_missing_explicit_event_rejected_before_either_chat_endpoint_calls_model(pg_db, monkeypatch, denial):
    async def run():
        from datetime import date
        from fastapi import HTTPException
        from sqlalchemy import func, select
        from db.accessors import calendar_source
        from db.engine import get_session_maker
        from db.models.calendar_source import CalendarSource
        from db.models.calendar_occurrence import CalendarOccurrence
        from db.models.chat_conversation import ChatConversation
        from api.mail import agent
        from api.routers.api.v1.chat.route import ChatTurnRequest, chat_turn
        from api.routers.api.v1.chat.stream import ChatStreamRequest, chat_stream

        make = get_session_maker()
        async with make.begin() as session:
            for key in ('a', 'b'):
                session.add(CalendarSource(id=key, provider='google', name=key, connection_ref='test',
                                           external_id=key, capabilities_json='["read"]', timezone='UTC'))
            session.add(CalendarOccurrence(id='selected', source_id='a', event_id='event', external_id='event',
                                          start_date=date(2026, 11, 13), end_date=date(2026, 11, 14)))
        if denial == 'paused':
            await calendar_source.set_active('a', False)
        else:
            from db.models.calendar_sync_state import CalendarSyncState
            async with make.begin() as session:
                if denial == 'missing_read':
                    source = await session.get(CalendarSource, 'a')
                    source.capabilities_json = '[]'
                else:
                    session.add(CalendarSyncState(source_id='a', last_error='reconnect_required'))
        assert await calendar_source.readable_ids(None) == ['b']
        monkeypatch.setattr(agent, 'mistral_client', lambda: pytest.fail('unexpected model call'))
        for occurrence_id in ('selected', 'absent'):
            args = {'context_occurrence_id': occurrence_id}
            for call in (lambda: chat_turn(ChatTurnRequest(message='This event?', **args)),
                         lambda: chat_stream(ChatStreamRequest(text='This event?', **args), NS())):
                with pytest.raises(HTTPException) as error:
                    await call()
                assert error.value.status_code == 404
                assert error.value.detail['code'] == 'calendar_reference_unavailable'
        async with make() as session:
            assert not await session.scalar(select(func.count()).select_from(ChatConversation))
    asyncio.run(run())
