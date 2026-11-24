"""Calendar proposal scope, confirmation and private serialization contracts."""

import json
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api.calendar import chat, proposals as commands, writes
from api.mail import proposals
from api.mail.agent_tools import TurnContext
from db.models.chat_message import ChatMessage


@pytest.fixture
def context(monkeypatch):
    from db.accessors import calendar_settings, calendar_source, calendar_occurrence
    source = SimpleNamespace(id="synthetic-source", provider="google", is_active=True,
                             capabilities_json='["read","write"]', connection_ref="synthetic-private-ref")
    async def default(): return SimpleNamespace(default_source_id=source.id)
    async def catalog(*, ids):
        current = await calendar_source.get(source.id)
        return [(current, None)] if current and current.id in ids else []
    monkeypatch.setattr(calendar_settings, "get", default)
    monkeypatch.setattr(calendar_source, "catalog", catalog)
    async def get(_): return source
    async def occurrence(_): return SimpleNamespace(source_id=source.id)
    async def live(_): return writes.LiveEvent(occurrence_id="synthetic-occurrence", source_id=source.id, title="Before",
        start_date=date(2026, 12, 5), end_date=date(2026, 12, 6), revision="old", editable=True)
    monkeypatch.setattr(calendar_source, "get", get)
    monkeypatch.setattr(calendar_occurrence, "get_visible", occurrence)
    monkeypatch.setattr(writes, "live", live)
    return TurnContext(conversation_id="synthetic-conversation", account_id=None, today=date(2026, 12, 5),
                       context_id="synthetic-context", calendar_source_ids=[source.id], calendar_timezone="Europe/Berlin")


async def test_propose_out_of_scope_source_refused(context):
    with pytest.raises(writes.WriteError, match="calendar_out_of_scope"):
        await chat.prepare({"action": "create", "source_id": "other", "title": "Example"}, context)


async def test_propose_update_binds_live_revision_and_before(context):
    command = await chat.prepare({"action": "update", "occurrence_id": "synthetic-occurrence", "title": "After"}, context)
    assert command.expected_revision == "old" and command.before.title == "Before"
    assert command.after.title == "After" and command.after.start_date == date(2026, 12, 5)
    assert command.connection_ref == "synthetic-private-ref"


@pytest.fixture
def card(context, monkeypatch):
    from db.accessors import chat_context, chat_message
    command = commands.CalendarCommand(action="create", source_id="synthetic-source", connection_ref="synthetic-private-ref",
        timezone="Europe/Berlin", after=commands.CalendarValues(title="Example", start_date="2026-12-05", end_date="2026-12-06"))
    proposal = proposals.Proposal(kind="calendar", reason="Example", items=[], calendar=command)
    message = ChatMessage(id=str(uuid4()), conversation_id=context.conversation_id, role="proposal", context_id=context.context_id,
        proposal_json=proposal.model_dump_json(context={"storage": True}), proposal_status="pending")
    state = SimpleNamespace(message=message, revoked=False, scope=["synthetic-source"], calls=[], outcome="done")
    async def get(_): return message
    async def live(_): return not state.revoked
    async def ctx(_): return SimpleNamespace(revoked=state.revoked, conversation_id=context.conversation_id, calendar_scope_json=json.dumps(state.scope))
    async def claim(_):
        if message.proposal_status != "pending" or state.revoked: return None
        message.proposal_status = "running"
        return message
    async def set_proposal(_, *, expected_statuses=None, **values):
        if expected_statuses is not None and message.proposal_status not in expected_statuses:
            return None
        for key, value in values.items(): setattr(message, key, value)
        return message
    async def execute(request, *, write_id):
        state.calls.append((request, write_id))
        return writes.WriteOutcome(write_id=write_id, status=state.outcome)
    async def check(identifier): return writes.WriteOutcome(write_id=identifier, status="done")
    async def record(_): return SimpleNamespace(id=message.id)
    from db.accessors import calendar_write
    monkeypatch.setattr(calendar_write, "get", record)
    monkeypatch.setattr(writes, "get", check)
    monkeypatch.setattr(chat_message, "get_message", get)
    monkeypatch.setattr(chat_message, "claim_proposal", claim)
    monkeypatch.setattr(chat_message, "set_proposal", set_proposal)
    monkeypatch.setattr(chat_context, "is_live", live)
    monkeypatch.setattr(chat_context, "get_context", ctx)
    monkeypatch.setattr(writes, "execute", execute)
    monkeypatch.setattr(writes, "check", check)
    return state


async def test_confirm_runs_one_write_with_proposal_id(card):
    result = await proposals.confirm(card.message.id, selected_ids=[])
    assert result.proposal_status == "done" and card.calls[0][1] == card.message.id


async def test_confirm_after_default_changes_keeps_original_target(card, monkeypatch):
    from unittest.mock import AsyncMock
    from db.accessors import calendar_settings
    new_default = AsyncMock(return_value=SimpleNamespace(default_source_id="new-target"))
    monkeypatch.setattr(calendar_settings, "get", new_default)
    assert card.calls == []
    result = await proposals.confirm(card.message.id, selected_ids=[])
    assert result.proposal_status == "done"
    assert [request.source_id for request, _ in card.calls] == ["synthetic-source"]
    new_default.assert_not_awaited()


async def test_second_confirm_proposal_not_pending(card):
    await proposals.confirm(card.message.id, selected_ids=[])
    with pytest.raises(proposals.ProposalStateError): await proposals.confirm(card.message.id, selected_ids=[])
    assert len(card.calls) == 1


async def test_confirm_after_scope_change_refused_without_write(card):
    card.scope = []
    result = await proposals.confirm(card.message.id, selected_ids=[])
    assert result.proposal_status == "failed" and not card.calls


async def test_confirm_revoked_context_refused(card):
    card.revoked = True
    with pytest.raises(proposals.ProposalContextRevokedError): await proposals.confirm(card.message.id, selected_ids=[])
    assert not card.calls


async def test_unknown_card_cannot_be_confirmed_and_check_resolves_to_done(card):
    card.outcome = "unknown"
    await proposals.confirm(card.message.id, selected_ids=[])
    with pytest.raises(proposals.ProposalStateError): await proposals.confirm(card.message.id, selected_ids=[])
    assert (await proposals.recheck_calendar_card(card.message.id)).proposal_status == "done"


def test_card_output_has_no_connection_ref(card):
    from api.mail.chat_schemas import to_message_out
    output = to_message_out(card.message).model_dump_json()
    assert "connection_ref" not in output and "synthetic-private-ref" not in output
    assert "synthetic-private-ref" in card.message.proposal_json


def test_prompt_version_and_calendar_rules_present():
    from api.mail.agent_prompt import APP_PROMPT_VERSION, system_prompt
    text = system_prompt(today=date(2026, 12, 5), account_scope=None)
    assert APP_PROMPT_VERSION == "25" and "propose_calendar_change" in text
    assert "ICS bleibt nur lesbar" in text


async def test_propose_nonexistent_local_time_refused(context):
    # 2026-03-29 02:30 does not exist in Europe/Berlin; the card must not open.
    with pytest.raises(writes.WriteError, match="calendar_time_invalid"):
        await chat.prepare({"action": "create", "source_id": "synthetic-source", "title": "Example",
                            "start_local": "2026-03-29T02:30", "end_local": "2026-03-29T03:30"}, context)


async def test_card_does_not_keep_the_provider_link(context, monkeypatch):
    # Google's htmlLink encodes event and calendar IDs; cards reach chat history and SSE.
    async def live(_): return writes.LiveEvent(occurrence_id="synthetic-occurrence", source_id="synthetic-source",
        title="Before", start_date=date(2026, 12, 5), end_date=date(2026, 12, 6), revision="old", editable=True,
        html_link="https://calendar.example.org/event?eid=synthetic-encoded-id")
    monkeypatch.setattr(writes, "live", live)
    command = await chat.prepare({"action": "update", "occurrence_id": "synthetic-occurrence", "title": "After"}, context)
    assert command.before.html_link is None
    assert "synthetic-encoded-id" not in command.model_dump_json(context={"storage": True})


async def test_running_card_with_write_record_resolves_on_check(card, monkeypatch):
    from db.accessors import calendar_write
    card.message.proposal_status = "running"  # The process died after claiming the card.
    async def record(_): return SimpleNamespace(id=card.message.id)
    async def get(identifier): return writes.WriteOutcome(write_id=identifier, status="done")
    monkeypatch.setattr(calendar_write, "get", record)
    monkeypatch.setattr(writes, "get", get)
    assert (await proposals.recheck_calendar_card(card.message.id)).proposal_status == "done"


async def test_running_card_without_write_record_settles_on_check(card, monkeypatch):
    # The confirm died between claiming the card and claiming the write: nothing was sent.
    # Settling takes the card's write ID, so a confirm that is still alive cannot send later.
    from db.accessors import calendar_write
    card.message.proposal_status = "running"
    taken = {}
    async def record(identifier): return None
    async def abandon(identifier, *, source_id, operation):
        taken[identifier] = (source_id, operation)
        return "abandoned"
    monkeypatch.setattr(calendar_write, "get", record)
    monkeypatch.setattr(calendar_write, "abandon", abandon)
    result = await proposals.recheck_calendar_card(card.message.id)
    assert result.proposal_status == "failed"
    assert proposals.proposal_view(result).result.failed[0].code == "calendar_write_not_applied"
    assert taken == {card.message.id: ("synthetic-source", "create")}


async def test_running_card_check_loses_to_a_live_confirm(card, monkeypatch):
    # The confirm claimed the write first: the check reads that write instead of settling.
    from db.accessors import calendar_write
    card.message.proposal_status = "running"
    async def record(identifier): return None
    async def abandon(identifier, **_): return "taken"
    async def get(identifier): return writes.WriteOutcome(write_id=identifier, status="unknown", error_code="calendar_write_unknown")
    monkeypatch.setattr(calendar_write, "get", record)
    monkeypatch.setattr(calendar_write, "abandon", abandon)
    monkeypatch.setattr(writes, "get", get)
    assert (await proposals.recheck_calendar_card(card.message.id)).proposal_status == "unknown"


async def test_running_card_of_a_removed_calendar_is_never_called_not_applied(card, monkeypatch):
    # Removal deleted the finished record, so the write may have been applied: the card says
    # the calendar is unavailable, not that nothing happened.
    from db.accessors import calendar_write
    card.message.proposal_status = "running"
    async def record(identifier): return None
    async def abandon(identifier, **_): return "source_gone"
    monkeypatch.setattr(calendar_write, "get", record)
    monkeypatch.setattr(calendar_write, "abandon", abandon)
    result = await proposals.recheck_calendar_card(card.message.id)
    assert result.proposal_status == "failed"
    assert proposals.proposal_view(result).result.failed[0].code == "calendar_unavailable"


@pytest.mark.parametrize(
    ("source_id", "expected"),
    [("synthetic-source", "calendar_write_not_applied"), ("unrelated-source", "calendar_write_conflict")],
)
async def test_late_confirm_recognizes_only_its_own_settled_record(card, monkeypatch, source_id, expected):
    from db.accessors import calendar_write

    async def taken(request, *, write_id):
        raise writes.WriteError("calendar_write_conflict")

    async def stored(identifier):
        return SimpleNamespace(
            request_hash=calendar_write.ABANDONED_REQUEST_HASH, source_id=source_id,
            operation="create", status="not_applied", error_code="calendar_write_not_applied",
        )

    monkeypatch.setattr(writes, "execute", taken)
    monkeypatch.setattr(calendar_write, "get", stored)
    result = await proposals.confirm(card.message.id, selected_ids=[])
    assert result.proposal_status == "failed"
    assert proposals.proposal_view(result).result.failed[0].code == expected


async def test_error_before_the_write_claim_fails_the_card(card, monkeypatch):
    from db.accessors import calendar_write
    async def broken(request, *, write_id): raise RuntimeError("test-only")
    async def record(_): return None
    monkeypatch.setattr(writes, "execute", broken)
    monkeypatch.setattr(calendar_write, "get", record)
    result = await proposals.confirm(card.message.id, selected_ids=[])
    assert result.proposal_status == "failed"


async def test_error_after_the_write_claim_keeps_the_card_unknown(card, monkeypatch):
    from db.accessors import calendar_write
    async def broken(request, *, write_id): raise RuntimeError("test-only")
    async def record(_): return SimpleNamespace(id=card.message.id)
    monkeypatch.setattr(writes, "execute", broken)
    monkeypatch.setattr(calendar_write, "get", record)
    result = await proposals.confirm(card.message.id, selected_ids=[])
    assert result.proposal_status == "unknown"


async def test_unknown_card_without_write_record_settles_failed(card, monkeypatch):
    from db.accessors import calendar_write
    card.message.proposal_status = "unknown"
    async def record(_): return None
    monkeypatch.setattr(calendar_write, "get", record)
    assert (await proposals.recheck_calendar_card(card.message.id)).proposal_status == "failed"


@pytest.mark.parametrize("change", [{"is_active": False}, {"capabilities_json": '["read"]'}])
async def test_confirm_after_source_paused_or_read_only_writes_nothing(card, monkeypatch, change):
    from db.accessors import calendar_source
    source = SimpleNamespace(id="synthetic-source", provider="google", is_active=True,
                             capabilities_json='["read","write"]', connection_ref="synthetic-private-ref")
    for key, value in change.items(): setattr(source, key, value)
    async def get(_): return source
    monkeypatch.setattr(calendar_source, "get", get)
    result = await proposals.confirm(card.message.id, selected_ids=[])
    assert result.proposal_status == "failed" and not card.calls
    assert proposals.proposal_view(result).result.failed[0].code == "calendar_not_writable"


async def test_unknown_card_of_a_removed_source_stays_unknown(card, monkeypatch):
    from db.accessors import calendar_write
    card.message.proposal_status = "unknown"
    async def record(_): return SimpleNamespace(id=card.message.id, status="unknown", request_json=None)
    async def kept(identifier): return writes.WriteOutcome(write_id=identifier, status="unknown", error_code="calendar_write_unknown")
    monkeypatch.setattr(calendar_write, "get", record)
    monkeypatch.setattr(writes, "get", kept)
    assert (await proposals.recheck_calendar_card(card.message.id)).proposal_status == "unknown"


async def test_title_text_is_not_a_calendar_destination(monkeypatch):
    from dataclasses import replace
    from db.accessors import calendar_settings, calendar_source
    from db.models.calendar_source import CalendarSource
    from unittest.mock import AsyncMock

    calendars = [CalendarSource(id=key, name=key.title(), provider='google', connection_ref='test', external_id=key,
                                capabilities_json='["read","write"]', timezone='UTC') for key in ('work', 'personal')]
    async def catalog(*, ids):
        return [(row, None) for row in calendars if row.id in ids]
    monkeypatch.setattr(calendar_source, 'catalog', catalog)
    monkeypatch.setattr(calendar_settings, 'get', AsyncMock(return_value=SimpleNamespace(default_source_id='personal')))
    ctx = TurnContext('synthetic', None, date(2026, 11, 20), calendar_source_ids=['work', 'personal'])
    args = {'action': 'create', 'title': 'Look in the Work calendar', 'start_date': '2026-11-21', 'end_date': '2026-11-22'}
    for title in ('‘Look in the Work calendar’', '"Look in the Work calendar"', '„Look in the Work calendar“', 'Look in the Work calendar'):
        turn = replace(ctx, user_text=f'Create a reminder titled {title} for tomorrow at ten')
        assert (await chat.prepare(args, turn)).source_id == 'personal'
        with pytest.raises(writes.WriteError, match='calendar_name_required'):
            await chat.prepare({**args, 'calendar_name': 'Work'}, turn)
    explicit = replace(ctx, user_text='Create “Review” tomorrow in the “Work” calendar')
    assert (await chat.prepare({**args, 'title': 'Review'}, explicit)).source_id == 'work'


async def test_duplicate_calendar_needs_an_identifier_in_the_owner_request(monkeypatch):
    from dataclasses import replace
    from db.accessors import calendar_source
    from db.models.calendar_source import CalendarSource

    calendars = [CalendarSource(id=key, name='Team', provider='google', connection_ref='test', external_id=key,
                                capabilities_json='["read","write"]', timezone='UTC') for key in ('first-team', 'second-team')]
    async def catalog(*, ids):
        return [(row, None) for row in calendars if row.id in ids]
    monkeypatch.setattr(calendar_source, 'catalog', catalog)
    ctx = TurnContext('test', None, date(2026, 11, 20), calendar_source_ids=[row.id for row in calendars])
    args = {'action': 'create', 'calendar_name': 'Team', 'title': 'Review', 'start_date': '2026-11-21', 'end_date': '2026-11-22'}
    first = replace(ctx, user_text='Create a review in the Team calendar')
    for tool_args in (args, {**args, 'source_id': 'second-team'}):
        with pytest.raises(writes.WriteError, match='calendar_name_ambiguous'):
            await chat.prepare(tool_args, first)
    for text in ('Team · second-team', 'second-team'):
        reply = replace(ctx, calendar_choice=first.requested_calendar, user_text=text)
        assert (await chat.prepare({**args, 'source_id': 'second-team'}, reply)).source_id == 'second-team'
        implicit = replace(ctx, calendar_choice=first.requested_calendar, user_text=text)
        assert (await chat.prepare({k: v for k, v in args.items() if k != 'calendar_name'}, implicit)).source_id == 'second-team'
    calendars[1].capabilities_json = '["read"]'
    reply = replace(ctx, user_text='Team · second-team', calendar_choice=first.requested_calendar)
    with pytest.raises(writes.WriteError, match='calendar_not_writable'):
        await chat.prepare({**args, 'source_id': 'second-team'}, reply)


def test_calendar_appointment_excerpts_cannot_reassign_an_explicit_title_or_trim_an_alternative():
    from api.calendar.chat_target import appointment_part, appointment_parts

    first = 'Put the review in the Work calendar or the Home calendar'
    second = 'Separately, schedule a dentist appointment'
    parts = appointment_parts(first + '. ' + second + '.')
    assert appointment_part(parts, 'Dentist', first) is None
    assert appointment_part(parts, 'Review', 'Put the review in the Work calendar') is None
    assert appointment_part(parts, 'Dentist', second) == parts[1]
    assert appointment_part(parts, 'Different wording', None) is None
    one = 'Schedule a review. Put it in the Work calendar.'
    assert appointment_parts(one) == [one]
    assert appointment_part([one], 'Review', 'a request the owner never made') is None
    assert appointment_part([first], 'Review', 'Put the review in the Work calendar') is None
    shared = 'Schedule a review in the Work calendar and a dentist visit at 15:00'
    assert appointment_parts(shared) == ['Schedule a review in the Work calendar', 'a dentist visit at 15:00']
    with_guests = 'Schedule a meeting with Jane and a colleague at 15:00 in the Work calendar'
    assert appointment_parts(with_guests) == [with_guests]


@pytest.mark.parametrize('named', [None, 'Work'])
@pytest.mark.parametrize('details', ['in the Work calendar tomorrow at 10', 'tomorrow at 10 in the Work calendar',
                                    'tomorrow at 10. Use the Work calendar'])
async def test_non_calendar_alternative_does_not_change_explicit_destination(monkeypatch, named, details):
    from db.accessors import calendar_source
    from db.models.calendar_source import CalendarSource
    from unittest.mock import AsyncMock

    sources = [CalendarSource(id=name.lower(), name=name, provider='google', connection_ref='synthetic',
        external_id=name, capabilities_json='["read","write"]', timezone='UTC') for name in ('Work', 'Home')]
    async def catalog(*, ids):
        return [(source, None) for source in sources if source.id in ids]
    monkeypatch.setattr(calendar_source, 'catalog', catalog)
    monkeypatch.setattr(calendar_source, 'configured_names', AsyncMock(return_value={s.name.casefold() for s in sources}))
    ctx = TurnContext('synthetic', None, date(2026, 11, 20), calendar_source_ids=[s.id for s in sources],
        user_text=f'Schedule a home inspection {details}, rain or shine')
    fields = {'action': 'create', 'title': 'Home inspection', 'start_local': '2026-11-21T10:00', 'end_local': '2026-11-21T11:00'}
    if named:
        fields['calendar_name'] = named
    assert (await chat.prepare(fields, ctx)).source_id == 'work'


@pytest.mark.parametrize('default', ['personal', 'work'])
async def test_location_correction_does_not_choose_or_exclude_a_calendar(monkeypatch, default):
    from dataclasses import replace
    from db.accessors import calendar_settings, calendar_source
    from db.models.calendar_source import CalendarSource
    from unittest.mock import AsyncMock

    sources = [CalendarSource(id=name.lower(), name=name, provider='google', connection_ref='synthetic',
        external_id=name, capabilities_json='["read","write"]', timezone='UTC') for name in ('Home', 'Work', 'Personal')]
    async def catalog(*, ids):
        return [(s, None) for s in sources if s.id in ids]
    monkeypatch.setattr(calendar_source, 'catalog', catalog)
    monkeypatch.setattr(calendar_settings, 'get', AsyncMock(return_value=SimpleNamespace(default_source_id=default)))
    text = 'Schedule lunch tomorrow at noon with the location set to Home instead of Work'
    ctx = TurnContext('synthetic', None, date(2026, 11, 20), calendar_source_ids=[s.id for s in sources], user_text=text)
    values = {'action': 'create', 'title': 'Lunch', 'location': 'Home', 'start_local': '2026-11-21T12:00', 'end_local': '2026-11-21T13:00'}
    command = await chat.prepare(values, ctx)
    assert (command.source_id, command.after.location) == (default, 'Home')
    explicit = replace(ctx, user_text=text + ', put it in the Home calendar.')
    assert (await chat.prepare(values, explicit)).source_id == 'home'
    for ending in ('but not in the Work calendar', 'and do not put it in the Work calendar',
                   'but do not use the Work calendar', 'but please don’t choose the Work calendar'):
        after_location = replace(ctx, user_text=f'Schedule lunch tomorrow at noon at Café Central {ending}.')
        args = {**values, 'location': 'Café Central'}
        if default == 'work':
            with pytest.raises(writes.WriteError, match='calendar_name_required'):
                await chat.prepare(args, after_location)
            reply = replace(ctx, calendar_choice=after_location.requested_calendar, user_text='Personal, please.')
            assert (await chat.prepare(args, reply)).source_id == 'personal'
        else:
            card = await chat.prepare(args, after_location)
            assert (card.source_id, card.after.location) == ('personal', 'Café Central')


@pytest.mark.parametrize('named', [False, True])
async def test_calendar_name_is_distinct_from_the_appointment_time_and_title(monkeypatch, named):
    from dataclasses import replace
    from db.accessors import calendar_settings, calendar_source
    from db.models.calendar_source import CalendarSource
    from unittest.mock import AsyncMock

    calendars = [CalendarSource(id=key, name=name, provider='google', connection_ref='synthetic',
        external_id=key, capabilities_json='["read","write"]', timezone='UTC')
        for key, name in [('short-name', 'On'), ('on-call', 'On Call'), ('work', 'Arbeit'), ('personal', 'Personal')]]
    async def catalog(*, ids):
        return [(row, None) for row in calendars if row.id in ids]
    monkeypatch.setattr(calendar_source, 'catalog', catalog)
    monkeypatch.setattr(calendar_settings, 'get', AsyncMock(return_value=SimpleNamespace(default_source_id='personal')))
    ctx = TurnContext('synthetic', None, date(2026, 11, 20), calendar_source_ids=[s.id for s in calendars],
                      user_text='Trage die Übergabe morgen um neun im Kalender On Call ein')
    args = {'action': 'create', 'title': 'Übergabe', 'start_local': '2026-11-21T09:00', 'end_local': '2026-11-21T10:00'}
    if named:
        args['calendar_name'] = 'On Call'
    assert (await chat.prepare(args, ctx)).source_id == 'on-call'
    work_request = replace(ctx, user_text='Kannst du im Kalender Arbeit eine Projektbesprechung für morgen von neun bis zehn eintragen?')
    meeting = {**args, 'title': 'Projektbesprechung'}
    if named:
        meeting['calendar_name'] = 'Arbeit'
    card = await chat.prepare(meeting, work_request)
    assert (card.source_id, card.after.title) == ('work', 'Projektbesprechung')
    lunch = {'action': 'create', 'title': 'Lunch', 'start_local': '2026-11-23T12:00', 'end_local': '2026-11-23T13:00'}
    for when in ('next Monday', 'on Thursday'):
        generic = replace(ctx, user_text=f'Put lunch in my calendar {when} at noon')
        assert (await chat.prepare(lunch, generic)).source_id == 'personal'
        with pytest.raises(writes.WriteError, match='calendar_name_required'):
            await chat.prepare({**lunch, 'calendar_name': 'On'}, generic)


@pytest.mark.parametrize('default, request_text, expected', [
    ('personal', 'Schedule lunch tomorrow at noon, but do not put it in the Work calendar', 'personal'),
    ('personal', 'Plane ein Mittagessen, aber bitte nicht im Kalender „Work“', 'personal'),
    ('work', 'Schedule lunch tomorrow at noon, but do not put it in the Work calendar', None),
    ('work', 'Put lunch in the Personal calendar, not in the Work calendar', 'personal'),
    ('work', 'Schedule lunch, but not in my default calendar', None),
    ('work', 'Schedule lunch, use Personal instead of Work', 'personal'),
    ('personal', 'Schedule lunch, not before noon in the Work calendar', 'work'),
    ('personal', 'Schedule a meeting titled “Not Work” in the Work calendar', 'work'),
    ('personal', 'Add tomorrow’s 10:00 review to Home instead of Work.', 'home'),
    ('personal', 'Schedule home inspection tomorrow, but do not use the Work calendar.', 'personal'),
    ('personal', 'Put lunch in my calendar next Monday at noon', 'personal'),
    ('personal', 'Put lunch in my calendar on Thursday', 'personal'),
    ('personal', 'Put lunch in my calendar after the morning meeting', 'personal'),
    ('personal', 'Trage Mittagessen in meinem Kalender nächsten Montag um zwölf ein', 'personal'),
])
async def test_calendar_create_keeps_an_excluded_destination_out(monkeypatch, default, request_text, expected):
    from dataclasses import replace
    from db.accessors import calendar_settings, calendar_source
    from db.models.calendar_source import CalendarSource
    from unittest.mock import AsyncMock

    sources = [CalendarSource(id=key, name=key.title(), provider='google', connection_ref='synthetic',
        external_id=key, capabilities_json='["read","write"]', timezone='UTC') for key in ('work', 'personal', 'home')]
    async def catalog(*, ids):
        return [(source, None) for source in sources if source.id in ids]
    monkeypatch.setattr(calendar_source, 'catalog', catalog)
    settings = SimpleNamespace(default_source_id=default)
    monkeypatch.setattr(calendar_settings, 'get', AsyncMock(return_value=settings))
    ctx = TurnContext('synthetic', None, date(2026, 11, 20), calendar_source_ids=[s.id for s in sources], user_text=request_text)
    args = {'action': 'create', 'title': 'Lunch', 'start_local': '2026-11-21T12:00', 'end_local': '2026-11-21T13:00'}
    if expected:
        assert (await chat.prepare(args, ctx)).source_id == expected
        if expected == 'home':
            assert (await chat.prepare({**args, 'calendar_name': 'Home'}, ctx)).source_id == 'home'
        if 'home inspection' in request_text:
            with pytest.raises(writes.WriteError, match='calendar_name_required'):
                await chat.prepare({**args, 'calendar_name': 'Home'}, ctx)
        if ctx.requested_calendar and ctx.requested_calendar.excluded_source_ids and ctx.requested_calendar.source_id is None:
            settings.default_source_id = 'work'
            reply = replace(ctx, calendar_choice=ctx.requested_calendar, user_text='Tomorrow at noon')
            with pytest.raises(writes.WriteError, match='calendar_name_required'):
                await chat.prepare(args, reply)
    else:
        with pytest.raises(writes.WriteError, match='calendar_name_required'):
            await chat.prepare(args, ctx)
        reply = replace(ctx, calendar_choice=ctx.requested_calendar, user_text='Personal, please.')
        assert (await chat.prepare(args, reply)).source_id == 'personal'
