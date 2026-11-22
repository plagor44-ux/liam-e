"""Stream chat turns, persisting tool history and confirmation-only proposals."""
import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy.exc import SQLAlchemyError

from db.accessors import chat_conversation as conversation_access, chat_message as message_access
from db.accessors import mail_account, mail_insight, mail_job, mail_message, mail_sync_state, chat_context, calendar_occurrence, calendar_source, preference_revision
from api.calendar.chat_target import choice_for_clarification, previous_choice
from api.locales import DEFAULT_LOCALE, Locale, resolve_locale
from api.mail.context_resolver import resolve_context
from env.mail import env as mail_env
from api.mail.chat_client import mistral_client
from api.mail.agent_prompt import VOICE_INSTRUCTIONS, VOICE_INSTRUCTIONS_EN, reply_language_instruction, system_prompt
from api.mail.agent_tools import TurnContext, execute_tool, tool_status, tools_for_context
from api.mail.chat_events import ChatEvent, StartEvent, StatusEvent, DeltaEvent, MailsEvent, MailRefOut, CalendarsEvent, ProposalEvent, SuggestionsEvent, DoneEvent, ErrorEvent
from api.mail.chat_schemas import ChatExtrasOut, ContextOut, to_message_out
from api.mail.insights import BERLIN
from api.mail import headers, state
from api.mail import models_catalog
from api.audio.metrics import VoiceMetrics
from api.skills import (
    SkillDisabledError,
    SkillNotFoundError,
    SkillRevisionUnavailableError,
    SkillToolUnavailableError,
    load_active_skill,
    skill_prompt_message,
)

logger = logging.getLogger(__name__)

# Bound revocation checks during a model stream to one per ten deltas.
REVOCATION_DELTA_INTERVAL = 10


def _to_model_messages(history, *, prompt=None, current_user_id=None, locale=None):
    """Repair incomplete tool exchanges before sending history to the model."""
    result = [{'role': 'system', 'content': prompt or system_prompt(today=datetime.now(BERLIN).date(), account_scope=None)}]
    kept = set()
    for i, row in enumerate(history):
        if row.role == 'tool':
            try: call_id = json.loads(row.tool_payload_json).get('tool_call_id')
            except (ValueError, TypeError): continue
            if call_id in kept:
                result.append({'role':'tool', 'tool_call_id':call_id, 'name':row.tool_name, 'content':row.content})
            continue
        if row.role == 'assistant' and row.tool_payload_json:
            try:
                calls = json.loads(row.tool_payload_json)
                later = set()
                for other in history[i + 1:]:
                    if other.role == 'tool':
                        try: later.add(json.loads(other.tool_payload_json).get('tool_call_id'))
                        except (ValueError, TypeError): pass
                calls = [c for c in calls if c.get('id') in later]
            except (ValueError, TypeError, AttributeError): calls = []
            if calls:
                kept.update(c['id'] for c in calls)
                result.append({'role':'assistant', 'content':row.content, 'tool_calls':calls})
            elif row.content:
                result.append({'role':'assistant', 'content':row.content})
        elif row.role in ('user', 'assistant', 'draft', 'proposal'):
            content = row.content
            if row.role == 'user' and row.id == current_user_id and locale is not None:
                content += '\n\n[App language instruction]\n' + reply_language_instruction(locale)
            result.append({'role':'user' if row.role == 'user' else 'assistant', 'content':content})
    return result


def visible_messages(rows):
    return [r for r in rows if r.role != 'tool' and not (r.role == 'assistant' and r.tool_payload_json)]


async def _turn_scope_note(ctx: TurnContext) -> str:
    lines: list[str] = []
    if not ctx.account_ids:
        lines.append('Mailumfang: Keine Mailkonten freigegeben (Mailfristen sind ausgeschlossen und dürfen nicht als geprüft oder leer dargestellt werden).')
    else:
        wanted = set(ctx.account_ids)
        accounts = [a for a in await mail_account.list_accounts() if a.id in wanted]
        issues: list[str] = []
        for account in accounts:
            sync_state = await mail_sync_state.get_state(account.id)
            open_job = await mail_job.latest_for_account(account.id, kind='sync', open_only=True)
            account_state = state.account_status(account, sync_state, open_job)
            if account_state.state != 'ok':
                label = ' '.join((account.display_name or account.email_address).split())
                issues.append(f'{label}: {account_state.state}')
        note = f'Mailumfang: {len(ctx.account_ids)} freigegebene Konten.'
        if issues:
            note += ' Eingeschränkter Kontostand: ' + ', '.join(issues) + '.'
        lines.append(note)
    if not ctx.calendar_source_ids:
        lines.append('Kalenderumfang: Keine lesbaren Kalender freigegeben (rufe search_calendar auf, um pausierte, getrennte oder fehlende Quellen zu prüfen; stelle Kalender nicht als leer oder vollständig geprüft dar).')
    else:
        lines.append(f'Kalenderumfang: {len(ctx.calendar_source_ids)} lesbare Kalender (rufe search_calendar auf und prüfe Stand, Vollständigkeit, nicht lesbare Quellen und Schreibstandard).')
    if ctx.task_scope.get('source') == 'none':
        lines.append('Aufgabenstatus: Ausgeschlossen (Keine Aufgaben ausgewählt; Aufgaben wurden nicht geprüft und dürfen nicht als leer dargestellt werden).')
    else:
        lines.append(f'Aufgabenstatus: Freigegeben ({ctx.task_scope.get("source")}; prüfe mit search_tasks und folge Cursor-Seiten).')
    return '\n'.join(lines)


async def run_turn_events(*, conversation_id: str | None, text: str, account_id: str | None = None, expected_mail_scope_version: int | None = None, context_message_id: str | None = None, model: str | None = None, prompt_revision_id: str | None = None, skill_id: str | None = None, skill_revision_id: str | None = None, calendar_source_ids: list[str] | None = None, context_occurrence_id: str | None = None, calendar_timezone: str = 'Europe/Berlin', task_source: str = 'none', task_source_account_id: str | None = None, without_preferences: bool = False, locale: Locale | None = None, voice_response: bool = False, resolved_context=None, resolved_locale: Locale | None = None, voice_metrics: VoiceMetrics | None = None, voice_model: str = 'chat') -> AsyncIterator[ChatEvent]:
    """All rounds share one persisted snapshot; revocation is checked at each boundary."""
    try:
        effective_locale = resolved_locale or await resolve_locale(locale)
    except SQLAlchemyError as error:
        logger.warning("UI settings lookup failed: %s", type(error).__name__)
        yield ErrorEvent(code='internal')
        return
    conversation, snapshot, template_text = resolved_context or await resolve_context(
        conversation_id=conversation_id, text=text, account_id=account_id, context_message_id=context_message_id, context_occurrence_id=context_occurrence_id, expected_mail_scope_version=expected_mail_scope_version,
        model=model, prompt_revision_id=prompt_revision_id, skill_id=skill_id, skill_revision_id=skill_revision_id, calendar_source_ids=calendar_source_ids, calendar_timezone=calendar_timezone,
        task_source=task_source, task_source_account_id=task_source_account_id, without_preferences=without_preferences)
    account_scope = json.loads(snapshot.account_scope_json)
    scope_ids = account_scope['account_ids']
    account_id = account_scope.get('selection')
    preference_ids = json.loads(getattr(snapshot, 'preference_revision_ids_json', '[]'))
    try:
        revisions = await preference_revision.get_revisions(preference_ids)
    except SQLAlchemyError:
        yield ErrorEvent(code='preferences_unavailable')
        return
    if any(rev is None or rev.removed for rev in revisions):
        yield ErrorEvent(code='context_revoked')
        return
    occurrence = None
    if context_occurrence_id is not None:
        occurrence = await calendar_occurrence.get_visible(context_occurrence_id)
        if occurrence is None or occurrence.source_id not in json.loads(snapshot.calendar_scope_json):
            yield ErrorEvent(code='calendar_reference_unavailable')
            return
    active_skill = None
    snapshot_skill_id = getattr(snapshot, 'skill_id', None)
    snapshot_skill_revision_id = getattr(snapshot, 'skill_revision_id', None)
    if snapshot_skill_revision_id is not None or snapshot_skill_id is not None:
        try:
            active_skill = await load_active_skill(snapshot_skill_revision_id)
        except (SkillNotFoundError, SkillRevisionUnavailableError, SkillDisabledError, SkillToolUnavailableError):
            yield ErrorEvent(code='context_revoked')
            return
    model = conversation.model or mail_env.mail_chat_model
    reasoning_effort = None
    if voice_response:
        candidate = model if voice_model == 'chat' else voice_model
        try:
            models = await asyncio.wait_for(models_catalog.list_chat_models(candidate), timeout=2)
        except TimeoutError:
            models = []
        if voice_model != 'chat' and models_catalog._has_available_model(candidate, models):
            model = candidate
        matches = [item for item in models if item.id == model and item.availability == 'available']
        if not matches:
            matches = [item for item in models if item.resolved_id == model and item.availability == 'available']
        resolved_model = matches[0].resolved_id if len(matches) == 1 else None
        reasoning_effort = {'mistral-small-2603': 'none', 'mistral-medium-3-5': 'none',
                            'mistral-large-4': 'low', 'mistral-large-4-0': 'low'}.get(resolved_model)
        # Pin the validated model when sending a model-specific parameter: a moving
        # alias could otherwise change between catalogue lookup and the request.
        if reasoning_effort is not None:
            model = resolved_model
        if voice_metrics is not None:
            # Private workspace model names must not enter operational logs.
            voice_metrics.resolved_model = resolved_model if resolved_model in models_catalog.PUBLISHED_RATES else None
    user = await message_access.add_message(conversation_id=conversation.id, role='user', content=text, context_id=snapshot.id)
    all_text = ''
    mails, suggestions, calendars = [], [], []
    proposal_id = None
    saved = False
    ctx = None

    async def save(stopped):
        nonlocal saved
        if saved: return None
        extras = ChatExtrasOut(
            mails=mails,
            suggestions=suggestions,
            model=model,
            calendars=calendars,
            skill=active_skill.execution_ref() if active_skill else None,
        ).model_dump()
        if ctx and not stopped:
            choice = await choice_for_clarification(ctx, all_text)
            if choice:
                extras['calendar_clarification'] = choice.model_dump()
        row = await message_access.add_message(conversation_id=conversation.id, role='assistant', content=all_text,
            stopped=stopped, context_id=snapshot.id, extras_json=json.dumps(extras))
        saved = True
        return row

    try:
        yield StartEvent(conversation_id=conversation.id, user_message_id=user.id, model=model)
        yield StatusEvent(code='thinking')
        account = await mail_account.get_account(account_id) if account_id else None
        ctx = TurnContext(conversation.id, account.id if account else None, datetime.now(ZoneInfo(calendar_timezone)).date(), account_ids=scope_ids, context_id=snapshot.id, source_message_id=user.id,
                          calendar_source_ids=json.loads(getattr(snapshot, 'calendar_scope_json', '[]')), calendar_timezone=calendar_timezone, user_text=text,
                          task_scope=json.loads(getattr(snapshot, 'task_scope_json', '{"source":"none"}')), locale=effective_locale, browser_locale=locale or effective_locale,
                          allowed_tools=active_skill.allowed_tools if active_skill else None,
                          active_skill_id=active_skill.id if active_skill else None,
                          active_skill_revision_id=active_skill.revision_id if active_skill else None)
        history = await message_access.list_messages(conversation.id)
        context_ids = {row.context_id for row in history if row.context_id}
        contexts = await chat_context.get_contexts(context_ids)
        bindings = calendar_source.stored_feed_bindings(snapshot)
        # Defense in depth for concurrent turns: no historical text, even a user or
        # assistant answer, may cross a mail, task or calendar boundary.
        for row in history:
            previous = contexts.get(row.context_id)
            if (previous is None or snapshot.mail_scope_version is None
                    or previous.mail_scope_version != snapshot.mail_scope_version
                    or json.loads(previous.account_scope_json) != account_scope
                    or set(json.loads(previous.calendar_scope_json)) != set(ctx.calendar_source_ids)
                    or bindings is None or calendar_source.stored_feed_bindings(previous) != bindings
                    or json.loads(previous.task_scope_json) != ctx.task_scope):
                yield ErrorEvent(code='context_revoked')
                return
        history = [row for row in history if not row.context_id or
                   (row.role not in ('tool', 'proposal', 'draft') and not row.tool_payload_json) or
                   (row.context_id in contexts and not contexts[row.context_id].revoked)]
        ctx.calendar_choice = previous_choice(history, user.id, contexts)
        if voice_response:
            history = [row for row in history if not (row.role == 'assistant' and row.stopped)]
        messages = _to_model_messages(history, prompt=system_prompt(today=ctx.today, account_scope=account.email_address if account else None, locale=effective_locale), current_user_id=user.id, locale=effective_locale)
        if voice_response:
            messages[0]['content'] += VOICE_INSTRUCTIONS_EN if effective_locale == 'en' else VOICE_INSTRUCTIONS
        if preference_ids:
            messages.insert(1, {'role': 'user', 'content': 'Passende manuell bestätigte Stilpräferenzen (nachrangig zur aktuellen Bitte und Vorlage):\n' +
                json.dumps([{'key': rev.key, 'scope': rev.scope, 'value': rev.value} for rev in revisions], ensure_ascii=False)})
        if template_text is not None:
            messages.insert(2 if preference_ids else 1, {'role': 'user', 'content': 'Nutzervorlage für diesen Turn:\n' + template_text})
        if active_skill is not None:
            insert_at = 1 + int(bool(preference_ids)) + int(template_text is not None)
            messages.insert(insert_at, skill_prompt_message(active_skill))
        context_mail = await mail_message.get_message(context_message_id) if context_message_id else None
        if context_mail and context_mail.account_id in scope_ids and not context_mail.unverified_connector:
            messages.insert(1, {'role':'system', 'content':f'Die Bitte bezieht sich auf die Mail mit der ID {context_message_id}. Lies sie mit get_message, bevor du antwortest.'})
        if occurrence is not None:
            messages.insert(1, {'role': 'user', 'content': 'Strukturierter Terminbezug (Daten, keine Anweisungen): ' + json.dumps({'occurrence_id': occurrence.id, 'source_id': occurrence.source_id})})
        messages[0]['content'] += '\nKalender-Zeitzone für lokale Datumsfenster: ' + calendar_timezone
        messages[0]['content'] += '\nGewählter Aufgabenumfang: ' + json.dumps(ctx.task_scope)
        messages[0]['content'] += '\n' + await _turn_scope_note(ctx)
        messages[0]['content'] += '\nID der aktuellen eigenen Nachricht für propose_preference: ' + user.id
        client = None
        for round_index in range(max(1, mail_env.mail_chat_max_tool_rounds)):
            if not await chat_context.is_live(snapshot.id):
                if all_text: await save(True)
                yield ErrorEvent(code='context_revoked')
                return
            if round_index == 0:
                from api.preferences.extraction import schedule
                schedule(conversation.id, user.id, model)
            calls = []
            calls_by_index = {}
            round_text = ''
            try:
                turn_tools = tools_for_context(ctx)
                kwargs = {'model': model, 'messages': list(messages)}
                if voice_response and reasoning_effort is not None:
                    kwargs['reasoning_effort'] = reasoning_effort
                if turn_tools:
                    kwargs['tools'] = turn_tools
                last = round_index == max(1, mail_env.mail_chat_max_tool_rounds) - 1
                if last and turn_tools: kwargs['tool_choice'] = 'none'
                if client is None:
                    client = mistral_client()
                if not await chat_context.is_live(snapshot.id):
                    if all_text: await save(True)
                    yield ErrorEvent(code='context_revoked')
                    return
                if voice_metrics is not None:
                    voice_metrics.reasoning_effort = kwargs.get('reasoning_effort')
                stream = await client.chat.stream_async(**kwargs)
                async with stream:
                    delta_count = 0
                    async for event in stream:
                        delta_count += 1
                        if delta_count % REVOCATION_DELTA_INTERVAL == 0 and not await chat_context.is_live(snapshot.id):
                            if all_text: await save(True)
                            yield ErrorEvent(code='context_revoked')
                            return
                        delta = event.data.choices[0].delta
                        content = getattr(delta, 'content', None)
                        if isinstance(content, list):
                            if voice_metrics is not None:
                                voice_metrics.thinking_chunks += sum(getattr(c, 'type', None) == 'thinking' for c in content)
                            content = ''.join(getattr(c, 'text', '') for c in content if getattr(c, 'type', None) == 'text' and isinstance(getattr(c, 'text', None), str))
                        if isinstance(content, str) and content:
                            all_text += content
                            round_text += content
                            yield DeltaEvent(text=content)
                        tool_calls = getattr(delta, 'tool_calls', None)
                        if not last and isinstance(tool_calls, list):
                            for pos, call in enumerate(tool_calls):
                                index = getattr(call, 'index', None)
                                if not isinstance(index, int): index = pos
                                id = getattr(call, 'id', None)
                                id = id if isinstance(id, str) and id not in ('', 'null') else ''
                                item = calls_by_index.get(index)
                                if not id and item is None and calls:
                                    item = calls[-1]
                                if item is None or (id and item['id'] and item['id'] != id):
                                    item = {'id':'', 'type':'function', 'function':{'name':'', 'arguments':''}}
                                    calls.append(item)
                                calls_by_index[index] = item
                                function = getattr(call, 'function', None)
                                name = getattr(function, 'name', None)
                                args = getattr(function, 'arguments', None)
                                if id: item['id'] = id
                                if isinstance(name, str) and name and not item['function']['name']: item['function']['name'] = name
                                if isinstance(args, dict): args = json.dumps(args)
                                if isinstance(args, str): item['function']['arguments'] += args
            except (asyncio.CancelledError, GeneratorExit): raise
            except Exception as error:
                logger.warning('%s', type(error).__name__)
                if all_text: await save(True)
                saved = True
                yield ErrorEvent(code='model_unavailable')
                return
            if not calls:
                if not await chat_context.is_live(snapshot.id):
                    if all_text: await save(True)
                    yield ErrorEvent(code='context_revoked')
                    return
                answer = await save(False)
                await conversation_access.touch_conversation(conversation)
                if mails:
                    accounts = await mail_account.list_accounts()
                    colors = state.color_indexes(accounts)
                    insight_map = await mail_insight.get_for_messages(mails)
                    refs = []
                    for id in mails:
                        m = await mail_message.get_message(id)
                        if m and m.account_id in scope_ids and not m.unverified_connector:
                            insight = insight_map.get(id)
                            refs.append(MailRefOut(id=m.id, account_id=m.account_id, color_index=colors.get(m.account_id, 0), subject=m.subject, sender_name=headers.sender_name(m.sender), snippet=m.snippet, internal_date=m.internal_date, deadline=insight.deadline if insight else None, is_flagged=m.is_flagged))
                    if refs: yield MailsEvent(mails=refs)
                if calendars:
                    yield CalendarsEvent(calendars=calendars)
                if proposal_id:
                    proposal = await message_access.get_message(proposal_id)
                    if proposal:
                        card = to_message_out(proposal)
                        card.context = ContextOut.model_validate(snapshot, from_attributes=True)
                        yield ProposalEvent(message=card)
                if suggestions: yield SuggestionsEvent(items=suggestions)
                yield DoneEvent(conversation_id=conversation.id, message_id=answer.id, stopped=False)
                return
            payload = calls
            if voice_metrics is not None:
                voice_metrics.tool_rounds += 1
            used_ids = {call['id'] for call in payload if call['id']}
            for call in payload:
                if not call['id']:
                    id = f'call_{uuid.uuid4().hex}'
                    while id in used_ids: id = f'call_{uuid.uuid4().hex}'
                    call['id'] = id
                    used_ids.add(id)
            # Candidate values live only in the deletable candidate row, not in
            # historical tool arguments. Runtime calls retain their original input.
            persisted_payload = json.loads(json.dumps(payload))
            for entry in persisted_payload:
                if entry.get('function', {}).get('name') == 'propose_preference':
                    entry['function']['arguments'] = '{}'
            await message_access.add_message(conversation_id=conversation.id, role='assistant', content='', tool_payload_json=json.dumps(persisted_payload), context_id=snapshot.id)
            messages.append({'role':'assistant', 'content':round_text, 'tool_calls':payload})
            for call in payload:
                if not await chat_context.is_live(snapshot.id):
                    if all_text: await save(True)
                    yield ErrorEvent(code='context_revoked')
                    return
                name = call['function']['name']
                try:
                    args = json.loads(call['function']['arguments'] or '{}')
                    if not isinstance(args, dict): raise ValueError
                except (ValueError, TypeError):
                    args = None
                if args is None:
                    content = json.dumps({'error':'invalid_arguments'})
                else:
                    try:
                        status = await tool_status(name, args, ctx)
                        if status: yield status
                        if not await chat_context.is_live(snapshot.id):
                            if all_text: await save(True)
                            yield ErrorEvent(code='context_revoked')
                            return
                        outcome = await execute_tool(name, args, ctx)
                        content = outcome.content
                        for id in outcome.mails:
                            if id not in mails and len(mails) < 10: mails.append(id)
                        for ref in outcome.calendars:
                            if ref not in calendars and len(calendars) < 10: calendars.append(ref)
                        if name == 'suggest_followups': suggestions = outcome.suggestions
                        if outcome.proposal_message: proposal_id = outcome.proposal_message.id
                    except (asyncio.CancelledError, GeneratorExit): raise
                    except Exception as error:
                        logger.warning('%s', type(error).__name__)
                        content = json.dumps({'error':'tool_failed'})
                await message_access.add_message(conversation_id=conversation.id, role='tool', context_id=snapshot.id, tool_name=name, tool_payload_json=json.dumps({'tool_call_id':call['id']}), content=content)
                messages.append({'role':'tool', 'name':name, 'tool_call_id':call['id'], 'content':content})
            yield StatusEvent(code='thinking')
    except (asyncio.CancelledError, GeneratorExit):
        if not saved:
            task = asyncio.create_task(save(True))
            try: await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.shield(task)
        raise
    except Exception as error:
        logger.warning('%s', type(error).__name__)
        if all_text: await save(True)
        saved = True
        yield ErrorEvent(code='internal')
