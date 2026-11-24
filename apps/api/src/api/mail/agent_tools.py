"""Read-only agent tools and explicit proposal creation."""
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from db.accessors import chat_context, mail_account, mail_insight, mail_job, mail_message, mail_sync_state
from db.models.chat_message import ChatMessage
from fastapi import HTTPException
from api.calendar.chat_target import CalendarChoice, appointment_part, appointment_parts, pending_for_request
from api.locales import DEFAULT_LOCALE, Locale
from api.mail import action_capabilities
from api.mail import headers, insights, newsletters, proposals, send, state
from api.mail.chat_events import StatusEvent
from api.skills.registry import TOOL_REGISTRY
from api.skills.runtime import check_tool_allowed
from api.skills.types import ToolOutcome


@dataclass
class TurnContext:
    conversation_id: str
    account_id: str | None
    today: date
    account_ids: list[str] = field(default_factory=list)
    context_id: str | None = None
    source_message_id: str | None = None
    calendar_source_ids: list[str] = field(default_factory=list)
    calendar_timezone: str = 'UTC'
    task_scope: dict = field(default_factory=lambda: {'source': 'none'})
    locale: Locale = DEFAULT_LOCALE
    browser_locale: Locale = DEFAULT_LOCALE
    user_text: str = ""
    calendar_choice: CalendarChoice | None = None
    allowed_tools: tuple[str, ...] | None = None
    active_skill_id: str | None = None
    active_skill_revision_id: str | None = None
    calendar_request_part: str | None = field(default=None, init=False)
    calendar_targets: dict[str, CalendarChoice] = field(default_factory=dict, init=False)
    calendar_continuations: dict[str, CalendarChoice | None] = field(default_factory=dict, init=False)
    completed_calendar_requests: set[str] = field(default_factory=set, init=False)
    calendar_sequence_id: str | None = field(default=None, init=False)

    @property
    def requested_calendar(self) -> CalendarChoice | None:
        return self.calendar_targets.get(self.calendar_request_part) if self.calendar_request_part is not None else None


def result(value, **kwargs):
    return ToolOutcome(json.dumps(value, ensure_ascii=False, default=str), **kwargs)


TOOLS = TOOL_REGISTRY.base_schemas()


def tools_for_context(ctx: TurnContext) -> list[dict]:
    """Return the exact Mistral tool schemas allowed for this turn context."""
    if ctx.allowed_tools is None:
        return TOOLS
    return TOOL_REGISTRY.schemas_for(ctx.allowed_tools)


async def tool_status(name, arguments, ctx):
    if check_tool_allowed(name, ctx.allowed_tools) is not None:
        return None
    codes = {'search_messages':'searching', 'get_message':'reading', 'get_insights':'checking_deadlines', 'find_newsletters':'finding_newsletters', 'propose_action':'preparing_proposal', 'create_draft':'preparing_proposal', 'propose_task':'preparing_proposal'}
    codes['propose_preference'] = 'preparing_proposal'
    codes['propose_calendar_change'] = 'preparing_proposal'
    tool = TOOL_REGISTRY.get(name)
    if name not in codes:
        if tool is not None and tool.status_code is not None:
            return StatusEvent(code=tool.status_code)
        return None
    count = None
    if name == 'search_messages':
        ids = ctx.account_ids
        target = arguments.get('account_id') or ctx.account_id
        count = int(target in ids) if target else len(ids)
    elif name == 'get_insights':
        count = min(len(arguments.get('message_ids', [])), 20)
    return StatusEvent(code=codes[name], count=count)


def _summary(m, insight=None):
    return {
        'id': m.id,
        'account_id': m.account_id,
        'subject': m.subject,
        'sender': m.sender,
        'date': m.internal_date,
        'snippet': m.snippet,
        'is_read': m.is_read,
        'is_flagged': m.is_flagged,
        'deadline': insight.deadline if insight else None,
        'needs_reply': insight.needs_reply if insight else None,
        'has_insight': insight is not None,
    }


async def _account_statuses(account_ids: list[str]) -> list[dict]:
    if not account_ids:
        return []
    wanted = set(account_ids)
    accounts = [a for a in await mail_account.list_accounts() if a.id in wanted]
    statuses = []
    for account in accounts:
        sync_state = await mail_sync_state.get_state(account.id)
        open_job = await mail_job.latest_for_account(account.id, kind='sync', open_only=True)
        account_state = state.account_status(account, sync_state, open_job)
        statuses.append({
            'id': account.id,
            'display_name': account.display_name,
            'provider': account.provider,
            'email_address': account.email_address,
            'is_active': account.is_active,
            'state': account_state.state,
            'error_code': account_state.error_code,
            'last_sync_at': account_state.last_sync_at,
        })
    return statuses


async def _proposal(ctx, proposal):
    from db.accessors import calendar_proposal_queue

    create = proposal.kind == 'calendar' and proposal.calendar.action == 'create'
    parts = appointment_parts(ctx.user_text, ctx.calendar_choice) if create else []
    continuing = ctx.calendar_choice and any(pending_for_request(ctx.calendar_choice, part, first=index == 0)
                                             for index, part in enumerate(parts))
    if continuing and not ctx.calendar_sequence_id:
        ctx.calendar_sequence_id = ctx.calendar_choice.sequence_id
    if create and ctx.calendar_sequence_id:
        row = await calendar_proposal_queue.append(ctx.calendar_sequence_id, conversation_id=ctx.conversation_id,
            context_id=ctx.context_id, payload=proposal.model_dump(mode='json', context={'storage': True}))
        if row is None:
            return result({'status': 'queued', 'requires_confirmation': True,
                           'note': 'This appointment will have its own confirmation card after the preceding card is resolved.'})
    elif len(parts) > 1 or (continuing and ctx.calendar_choice.appointments):
        row = await proposals.create_proposal(ctx.conversation_id, proposal, context_id=ctx.context_id,
                                              extras_json=calendar_proposal_queue.sequence_extras())
        ctx.calendar_sequence_id = row.id
    else:
        row = await proposals.create_proposal(ctx.conversation_id, proposal, context_id=ctx.context_id)
    return result({'proposal_id': row.id, 'status': 'pending', 'note': 'Der Nutzer muss den Vorschlag bestätigen.'}, proposal_message=row)


def _valid_addresses(addresses):
    return all(isinstance(a, str) and '@' in a and not any(c.isspace() or c == ',' for c in a) for a in addresses)


def unsubscribe_subtitle(total: int, address: str, method: str, link: str | None, *, locale: Locale) -> str:
    target = newsletters.parse_mailto(link) if method == 'mailto' else None
    if locale == 'en':
        subtitle = f'{total:,} {"email" if total == 1 else "emails"} · {address}'
        if target:
            subtitle += f' · Unsubscribe by email to {target.recipient}, subject “{target.subject}”'
            if target.body and target.body != target.subject:
                subtitle += f', body “{target.body}”'
        return subtitle
    count = f'{total:,}'.replace(',', '.')
    subtitle = f'{count} {"Mail" if total == 1 else "Mails"} · {address}'
    if target:
        subtitle += f' · Abmeldung per Mail an {target.recipient}, Betreff „{target.subject}“'
        if target.body and target.body != target.subject:
            subtitle += f', Text „{target.body}“'
    return subtitle


async def execute_tool(name: str, arguments: dict, ctx: TurnContext) -> ToolOutcome:
    denied = check_tool_allowed(name, ctx.allowed_tools)
    if denied is not None:
        return result({'error': denied})
    registered = TOOL_REGISTRY.get(name)
    if registered is not None and registered.skill_only:
        if registered.handler is None:
            return result({'error': 'skill_tool_unavailable'})
        return await registered.handler(arguments, ctx)
    if name == 'propose_calendar_change':
        from api.calendar import chat as calendar_chat, writes
        from db.accessors.calendar_proposal_queue import SequenceReplaced, TargetUnavailable
        try:
            command = await calendar_chat.prepare(arguments, ctx)
            outcome = await _proposal(ctx, proposals.Proposal(kind='calendar', reason=str(arguments.get('reason', ''))[:500], items=[], calendar=command))
        except writes.WriteError as error:
            return result({'error': error.code})
        except SequenceReplaced:
            return result({'error': 'proposal_not_pending'})
        except TargetUnavailable:
            return result({'error': 'calendar_not_writable'})
        if command.action == 'create':
            part = ctx.calendar_request_part
        else:
            part = appointment_part(appointment_parts(ctx.user_text, ctx.calendar_choice), arguments.get('title'), arguments.get('appointment_request'))
        if part is not None:
            ctx.completed_calendar_requests.add(part)
        return outcome
    if name == 'propose_preference':
        from api.preferences import candidates
        from db.accessors.preference_candidate import CandidateGone
        from db.accessors.user_preference import InvalidPreference, PreferenceConflict
        try:
            candidate, card = await candidates.prepare(arguments, ctx)
        except (ValueError, CandidateGone, InvalidPreference, PreferenceConflict):
            return result({'error': 'preference_invalid_or_unavailable'})
        return result({'candidate_id': candidate.id, 'proposal_id': card.id if card else candidate.proposal_id,
                       'status': candidate.status, 'requires_confirmation': True}, proposal_message=card)
    if name in ('search_tasks', 'get_task', 'propose_task'):
        from api.tasks import chat as task_chat
        if name != 'propose_task':
            return result(await task_chat.read(name, arguments, ctx.task_scope, ctx.account_ids))
        try:
            command = await task_chat.prepare(arguments, ctx)
        except (ValueError, TypeError):
            return result({'error': 'task_invalid_or_out_of_scope'})
        return await _proposal(ctx, proposals.Proposal(kind='task', reason=str(arguments.get('reason', ''))[:500], items=[], task=command))
    if name in ('search_calendar', 'get_calendar_occurrence'):
        from api.mail.calendar_tools import read_calendar
        value, refs = await read_calendar(name, arguments, ctx.calendar_source_ids, ctx.calendar_timezone)
        return result(value, calendars=refs)
    if name == 'propose_rule':
        from db.accessors import chat_message, mail_filing
        from api.mail.rule_schemas import RuleIn
        from pydantic import ValidationError
        source = await chat_message.get_message(ctx.source_message_id) if ctx.source_message_id else None
        direct = r'(?is)^\s*(?:(?:please\s+)?(?:create|make|add|set up)\s+(?:me\s+)?(?:a\s+)?rule\b|(?:bitte\s+)?(?:erstelle?|lege|richte|mach|erzeuge)\s+(?:mir\s+)?(?:eine?\s+)?regel\b)'
        if not source or source.role != 'user' or source.conversation_id != ctx.conversation_id or source.context_id != ctx.context_id or not re.match(direct, source.content):
            return result({'error': 'explicit_rule_request_required'})
        if not ctx.context_id or not await chat_context.is_live(ctx.context_id):
            return result({'error': 'context_revoked'})
        try:
            rule = RuleIn.model_validate({key: value for key, value in arguments.items() if key != 'reason'})
        except ValidationError:
            return result({'error': 'rule_invalid'})
        accounts = await mail_account.list_accounts()
        if (rule.account_id and rule.account_id not in ctx.account_ids) or (not rule.account_id and not {a.id for a in accounts if a.is_active}.issubset(set(ctx.account_ids))):
            return result({'error': 'account_out_of_scope'})
        destinations = {row.id: row.kind for row in await mail_filing.list_destinations()}
        if (rule.folder_id and destinations.get(rule.folder_id) != 'folder') or (rule.label_id and destinations.get(rule.label_id) != 'label'):
            return result({'error': 'rule_invalid'})
        return await _proposal(ctx, proposals.Proposal(kind='rule', reason=str(arguments.get('reason', ''))[:500], items=[], rule=rule))
    if name == 'propose_filing':
        message_ids = arguments.get('message_ids')
        add_labels = arguments.get('add_labels', [])
        remove_labels = arguments.get('remove_labels', [])
        if (not isinstance(message_ids, list) or not 1 <= len(message_ids) <= 50
                or any(not isinstance(value, str) or not value for value in message_ids)
                or any(not isinstance(values, list) or len(values) > 100 or any(not isinstance(value, str) or not value for value in values)
                       for values in (add_labels, remove_labels))
                or (arguments.get('folder_id') is not None and not isinstance(arguments['folder_id'], str))
                or ('set_folder' in arguments and not isinstance(arguments['set_folder'], bool))
                or set(add_labels) & set(remove_labels)):
            return result({'error': 'filing_invalid'})
    scope = ctx.account_ids
    if any(id is not None and id not in scope for id in (ctx.account_id, arguments.get('account_id'))):
        return result({'error': 'account_out_of_scope'})
    ids = arguments.get('message_ids', []) + [arguments.get('message_id'), arguments.get('reply_to_message_id')]
    verified_messages = {}
    for id in ids:
        if id and id not in verified_messages:
            row = await mail_message.get_message(id)
            if row is None or row.account_id not in scope or row.unverified_connector:
                return result({'error': 'message_not_found'})
            verified_messages[id] = row
    if arguments.get('sender_ids'):
        allowed = {s.sender_id for s in await newsletters.find_newsletters(account_ids=scope, chat_only=True)} if scope else set()
        if any(id not in allowed for id in arguments['sender_ids']):
            return result({'error': 'sender_not_found'})
    if name == 'list_accounts':
        return result(await _account_statuses(scope))
    if name == 'list_folders':
        from db.accessors import mail_filing
        return result({'untrusted_folders': [{'id': row.id, 'name': row.name, 'kind': row.kind} for row in await mail_filing.list_destinations()]})
    if name == 'propose_filing':
        from db.accessors import mail_filing
        if not ctx.context_id or not await chat_context.is_live(ctx.context_id):
            return result({'error': 'context_revoked'})
        destinations = {row.id: row.kind for row in await mail_filing.list_destinations()}
        folder_id = arguments.get('folder_id')
        add_labels, remove_labels = arguments.get('add_labels', []), arguments.get('remove_labels', [])
        set_folder = bool(arguments.get('set_folder', folder_id is not None))
        if (folder_id and destinations.get(folder_id) != 'folder') or any(destinations.get(label) != 'label' for label in add_labels + remove_labels) or not (set_folder or add_labels or remove_labels):
            return result({'error': 'filing_invalid'})
        items = []
        for message_id in dict.fromkeys(arguments.get('message_ids', [])[:50]):
            mail = verified_messages.get(message_id)
            if mail is not None:
                items.append(proposals.ProposalItem(id=mail.id, kind='mail', title=mail.subject or ('No subject' if ctx.locale == 'en' else 'Ohne Betreff'), subtitle=headers.sender_name(mail.sender)))
        if not items:
            return result({'error': 'no_valid_items'})
        command = proposals.FilingCommand(folder_id=folder_id, set_folder=set_folder, add_labels=add_labels, remove_labels=remove_labels)
        return await _proposal(ctx, proposals.Proposal(kind='filing', reason=str(arguments.get('reason', ''))[:500], items=items, filing=command))
    if name == 'search_messages':
        account_id = arguments.get('account_id') or ctx.account_id
        if scope == []:
            return result({'untrusted_mails': [], 'account_ids': [], 'account_statuses': [], 'has_more': False, 'mail_scope_empty': True})
        target_ids = [account_id] if account_id else scope
        requested = max(1, min(int(arguments.get('limit', 10)), 50))
        rows = await mail_message.list_page(
            chat_only=True,
            folder='INBOX',
            account_ids=target_ids,
            unread_only=bool(arguments.get('unread_only', False)),
            important_only=bool(arguments.get('important_only', False)),
            query=arguments.get('query', ''),
            limit=requested + 1,
        )
        has_more = len(rows) > requested
        rows = rows[:requested]
        since = arguments.get('since_days')
        if since is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(since))
            rows = [m for m in rows if m.internal_date and (m.internal_date.replace(tzinfo=timezone.utc) if m.internal_date.tzinfo is None else m.internal_date) >= cutoff]
        insight_map = await mail_insight.get_for_messages([m.id for m in rows]) if rows else {}
        statuses = await _account_statuses(target_ids)
        payload = {
            'untrusted_mails': [_summary(m, insight_map.get(m.id)) for m in rows],
            'account_ids': target_ids,
            'account_statuses': statuses,
            'has_more': has_more,
            'missing_insight_count': sum(1 for m in rows if m.id not in insight_map),
        }
        if arguments.get('important_only') and not rows:
            other = await mail_message.list_page(chat_only=True, folder='INBOX', account_ids=target_ids, limit=1)
            payload['inbox_has_other_mails'] = bool(other)
            if other:
                payload['note'] = 'Keine markierten Mails oder gespeicherten Fristen gefunden; im Posteingang liegen weitere Mails ohne Frist-Analyse.'
        return result(payload)
    if name == 'get_message':
        m = verified_messages.get(arguments.get('message_id'))
        if not m: return result({'error':'message_not_found'})
        return result({'untrusted_mail': {'id':m.id, 'account_id':m.account_id, 'folder':m.folder, 'subject':m.subject, 'sender':m.sender, 'to':json.loads(m.recipients_json), 'cc':json.loads(m.cc_json), 'reply_to':json.loads(m.reply_to_json), 'date':m.internal_date, 'body_text':m.body_text[:8000], 'has_attachments':m.has_attachments, 'is_read':m.is_read}})
    if name == 'get_insights':
        items = []
        for id in arguments.get('message_ids', [])[:20]:
            if not ctx.context_id or not await chat_context.is_live(ctx.context_id):
                return result({'error': 'context_revoked'})
            try:
                insight = await insights.get_or_compute(id, locale=ctx.browser_locale, context_id=ctx.context_id)
                items.append({'message_id':id, 'points':json.loads(insight.points_json), 'deadline':insight.deadline, 'needs_reply':insight.needs_reply, 'followups':json.loads(insight.followups_json)})
            except insights.InsightContextRevoked:
                return result({'error': 'context_revoked'})
            except insights.MessageNotFound:
                items.append({'message_id':id, 'error':'not_found'})
            except insights.InsightError:
                items.append({'message_id':id, 'error':'unavailable'})
        return result({'untrusted_insights':items})
    if name == 'find_newsletters':
        rows = await newsletters.find_newsletters(account_ids=scope, chat_only=True) if scope != [] else []
        return result({'untrusted_senders':[{k:getattr(s, k) for k in ('sender_id','address','display_name','total','unread','last_read_at','method')} for s in rows]})
    if name == 'propose_action':
        kind = arguments.get('kind')
        if kind == 'send': return result({'error':'use_create_draft'})
        if kind not in ('archive','trash','mark_read','unsubscribe'): return result({'error':'invalid_arguments'})
        items = []
        mail_targets = set()
        ids = list(dict.fromkeys(arguments.get('sender_ids' if kind == 'unsubscribe' else 'message_ids', [])))
        accounts = {}
        if kind != 'unsubscribe':
            targets = {verified_messages[id].account_id for id in ids if id in verified_messages}
            accounts = {account.id: account for account in await mail_account.list_accounts() if account.id in targets}
            for account in accounts.values():
                try:
                    await action_capabilities.require_action(account, kind)
                except HTTPException as error:
                    note = action_capabilities.REAUTH_NOTE if error.status_code == 403 else action_capabilities.AGENT_NOTE
                    return result({'error': error.detail['code'], 'note': note})
        for id in ids:
            if len(items) == 50: break
            if kind == 'unsubscribe':
                s = await newsletters.sender_by_id(id, chat_only=True)
                if not s: continue
                subtitle = unsubscribe_subtitle(s.total, s.address, s.method, s.link if s.method == 'mailto' else None, locale=ctx.locale)
                items.append(proposals.ProposalItem(id=id, kind='sender', title=s.display_name, subtitle=subtitle, count=s.total, unread=s.unread, last_read_at=s.last_read_at, method=s.method, target=s.link))
            else:
                m = verified_messages.get(id)
                if not m or m.id in mail_targets or m.folder != 'INBOX' or (kind == 'mark_read' and m.is_read): continue
                mail_targets.add(m.id)
                account = accounts.get(m.account_id)
                items.append(proposals.ProposalItem(id=m.id, kind='mail', title=m.subject or ('No subject' if ctx.locale == 'en' else 'Ohne Betreff'), subtitle=f'{headers.sender_name(m.sender)} · {account.email_address if account else ""}'))
        if not items: return result({'error':'no_valid_items'})
        return await _proposal(ctx, proposals.Proposal(kind=kind, reason=arguments.get('reason',''), items=items))
    if name == 'create_draft':
        mode = arguments.get('mode', 'new')
        if mode not in ('new','reply','reply_all','forward'): return result({'error':'invalid_arguments'})
        to = [] if mode in ('reply', 'reply_all') else arguments.get('to', [])
        cc = arguments.get('cc', [])
        if not _valid_addresses(to + cc): return result({'error':'invalid_address'})
        original = verified_messages.get(arguments.get('reply_to_message_id')) if mode != 'new' else None
        if mode != 'new' and not original: return result({'error':'message_not_found'})
        sender_id = arguments.get('account_id')
        if original:
            if sender_id is not None and sender_id != original.account_id:
                return result({'error': 'sender_must_match_original'})
            sender_id = original.account_id
        if sender_id is None:
            sender_id = ctx.account_id
        if sender_id is None:
            if len(scope) > 1:
                return result({'error': 'sender_account_required'})
            if not scope:
                return result({'error': 'no_account'})
            sender_id = scope[0]
        account = await mail_account.get_account(sender_id)
        if not account or not account.is_active:
            return result({'error': 'no_account'})
        if mode in ('reply','reply_all'):
            to, base_cc = send.reply_recipients(original, account, reply_all=mode == 'reply_all')
            seen = {address.casefold() for address in to}
            deduplicated = []
            for address in base_cc + cc:
                if address.casefold() not in seen:
                    deduplicated.append(address)
                    seen.add(address.casefold())
            cc = deduplicated
        if not to: return result({'error':'recipients_missing'})
        if not _valid_addresses(to + cc): return result({'error':'invalid_address'})
        to_text = ', '.join(to)
        new_reason = f'Email from {account.email_address} to {to_text}.' if ctx.locale == 'en' else f'Neue Mail von {account.email_address} an {to_text}.'
        reason = {'new':new_reason, 'reply':f'Antwort an {to_text}.', 'reply_all':f'Antwort an alle: {to_text}.', 'forward':f'Weiterleitung an {to_text}.'}[mode]
        draft = proposals.SendDraft(account_id=account.id, mode=mode, to=to, cc=cc, subject=arguments.get('subject') or (original.subject if original else ''), body=arguments.get('body', ''), quote=send.quote_original(original) if original else '', reply_to_message_id=original.id if original else None)
        return await _proposal(ctx, proposals.Proposal(kind='send', reason=reason, items=[], draft=draft))
    if name == 'show_mails':
        ids = []
        for id in dict.fromkeys(arguments.get('message_ids', [])):
            if id in verified_messages: ids.append(id)
            if len(ids) == 10: break
        return result({'ok':True}, mails=ids)
    if name == 'suggest_followups':
        texts = [s.strip()[:120] for s in arguments.get('texts', []) if isinstance(s, str) and s.strip()][:3]
        return result({'ok':True}, suggestions=texts)
    return result({'error':'unknown_tool'})
