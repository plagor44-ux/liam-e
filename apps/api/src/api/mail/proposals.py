"""Persist proposals and execute them only after an explicit, atomic confirmation."""

import json
import logging
from datetime import datetime
from typing import Literal

from db.accessors import chat_settings
from db.accessors import chat_message as message_access
from db.accessors import chat_context as context_access
from db.accessors import mail_account as account_access
from db.accessors import mail_message as mail_access
from db.models.mail_account import ProviderActionUnsupported
from db.models.chat_message import ChatMessage
from db.engine import get_session_maker
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError

from api.mail import action_capabilities
from api.mail import actions, netguard, newsletters, send, service, transport
from api.errors import refuse
from api.tasks import patch as task_patch
from api.tasks import proposals as task_proposals
from api.tasks import state as task_state
from api.preferences import candidates as preference_candidates
from api.calendar.proposals import CalendarCommand
from api.calendar import writes as calendar_writes

logger = logging.getLogger(__name__)
ProposalKind = Literal["send", "archive", "trash", "mark_read", "unsubscribe", "task", "preference", "calendar"]


class ProposalItem(BaseModel):
    id: str
    kind: Literal["mail", "sender"]
    title: str
    subtitle: str
    count: int | None = None
    unread: int | None = None
    last_read_at: datetime | None = None
    method: str | None = None
    target: str | None = None
    selected: bool = True


class SendDraft(BaseModel):
    account_id: str
    mode: Literal["new", "reply", "reply_all", "forward"]
    to: list[str]
    cc: list[str]
    subject: str
    body: str
    quote: str
    reply_to_message_id: str | None


class ItemProblem(BaseModel):
    id: str
    code: str


class ManualStep(BaseModel):
    id: str
    link: str | None


class ProposalResult(BaseModel):
    done: int = 0
    failed: list[ItemProblem] = Field(default_factory=list)
    manual: list[ManualStep] = Field(default_factory=list)
    undo_action_ids: list[str] = Field(default_factory=list)
    task_series_id: str | None = None
    preference_outcome: Literal['saved', 'unchanged', 'kept'] | None = None
    preferences_enabled: bool | None = None
    calendar_write_id: str | None = None
    calendar_status: str | None = None


class Proposal(BaseModel):
    kind: ProposalKind
    reason: str
    items: list[ProposalItem]
    draft: SendDraft | None = None
    task: task_proposals.TaskProposal | None = None
    preference: preference_candidates.PreferenceReference | None = None
    calendar: CalendarCommand | None = None
    irreversible: bool = False
    result: ProposalResult | None = None

    @model_validator(mode="after")
    def validate_kind(self):
        self.irreversible = self.kind in {"send", "unsubscribe"} or (self.calendar is not None and self.calendar.action == "delete")
        if (self.kind == "calendar") != (self.calendar is not None):
            raise ValueError("calendar requires one typed command")
        if self.kind == "calendar" and (self.items or self.draft or self.task or self.preference):
            raise ValueError("calendar cannot contain other actions")
        if self.kind == "send" and self.draft is None:
            raise ValueError("send requires a draft")
        if (self.kind == 'task') != (self.task is not None):
            raise ValueError('task requires exactly one typed command')
        if self.kind == 'task' and (self.items or self.draft):
            raise ValueError('task commands do not select mail items')
        if (self.kind == 'preference') != (self.preference is not None):
            raise ValueError('preference requires a candidate reference')
        if self.kind == 'preference' and (self.items or self.draft or self.task):
            raise ValueError('preference cannot contain mail or task actions')
        expected = "sender" if self.kind == "unsubscribe" else "mail"
        if self.kind != "send" and any(item.kind != expected for item in self.items):
            raise ValueError("item kind does not match proposal")
        return self


class ProposalNotFoundError(Exception):
    pass


class ProposalStateError(Exception):
    pass


class ProposalContextRevokedError(Exception):
    pass


class ProposalSelectionError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def proposal_view(message: ChatMessage) -> Proposal | None:
    if message.role == "proposal":
        return Proposal.model_validate_json(message.proposal_json)
    if message.role == "draft":
        old = json.loads(message.draft_json)
        return Proposal(kind="send", reason="", items=[], draft=SendDraft(
            account_id=old["account_id"], mode="new", to=old["to"], cc=[],
            subject=old["subject"], body=old["body"], quote="", reply_to_message_id=None,
        ))
    return None


async def create_proposal(conversation_id: str, proposal: Proposal, *, context_id: str | None = None, extras_json: str = "") -> ChatMessage:
    fields = dict(conversation_id=conversation_id, role="proposal", content=proposal.reason,
        proposal_json=proposal.model_dump_json(context={"storage": True}), proposal_status="pending",
        context_id=context_id, extras_json=extras_json)
    for attempt in range(2):
        try:
            if proposal.calendar is not None:
                return await message_access.replace_with_calendar_proposal(
                    ChatMessage(**fields), proposal.calendar.source_id, proposal.calendar.connection_ref)
            await message_access.cancel_open_proposals(conversation_id)
            return await message_access.add_message(**fields)
        except IntegrityError:
            if attempt:
                raise
    raise AssertionError("unreachable")


async def _get_proposal(proposal_id: str) -> ChatMessage:
    message = await message_access.get_message(proposal_id)
    if message is None or message.role != "proposal":
        raise ProposalNotFoundError
    return message


async def cancel(proposal_id: str) -> ChatMessage:
    row = await _get_proposal(proposal_id)
    proposal = proposal_view(row)
    if proposal and proposal.kind == 'preference':
        return await preference_candidates.cancel(proposal, proposal_id)
    message = await message_access.cancel_proposal(proposal_id)
    if message is None:
        raise ProposalStateError
    return message


async def _send(draft: SendDraft, result: ProposalResult) -> None:
    def fail(code: str):
        result.failed.append(ItemProblem(id="draft", code=code))

    try:
        account = await account_access.get_account(draft.account_id)
        if account is None:
            fail("account_gone")
            return
        original = None
        if draft.mode != "new":
            original = await mail_access.get_message(draft.reply_to_message_id) if draft.reply_to_message_id else None
            if original is None:
                fail("item_gone")
                return
        await send.send_message(
            account, to=draft.to, cc=draft.cc, subject=draft.subject,
            body=draft.body + ("\n\n" + draft.quote if draft.quote else ""),
            reply_to=original if draft.mode in {"reply", "reply_all"} else None,
            forward_of=original if draft.mode == "forward" else None,
            reply_all=draft.mode == "reply_all",
        )
        result.done = 1
    except (transport.MailHostNotAllowedError, netguard.BlockedAddressError):
        fail("host_not_allowed")
    except service.CredentialError:
        fail("credentials_invalid")
    except send.SeedAccountError:
        fail("send_failed")
    except Exception as error:
        logger.warning("%s", type(error).__name__)
        fail("send_failed")


async def _apply(item: ProposalItem, kind: ProposalKind, result: ProposalResult, mail_targets: set[str]) -> None:
    def fail(code: str):
        result.failed.append(ItemProblem(id=item.id, code=code))

    try:
        if kind == "unsubscribe":
            sender = await newsletters.sender_by_id(item.id, chat_only=True)
            account = await account_access.get_account(sender.account_id) if sender else None
            if sender is None or account is None:
                fail("item_gone")
                return
            if (sender.method != item.method or (sender.link or None) != (item.target or None)
                    or (item.method != "none" and not item.target)):
                fail("item_changed")
                return
            outcome = await newsletters.unsubscribe(sender, account)
            if outcome.status == "failed":
                fail(outcome.error_code)
            else:
                result.undo_action_ids.extend(outcome.archived_action_ids)
                if outcome.status == "manual":
                    result.manual.append(ManualStep(id=item.id, link=outcome.link))
                else:
                    result.done += 1
            return
        mail = await mail_access.get_message(item.id)
        if mail is None:
            fail("item_gone")
            return
        if mail.id in mail_targets:
            item.selected = False
            return
        mail_targets.add(mail.id)
        if mail.folder != "INBOX" or mail.unverified_connector:
            fail("item_gone")
            return
        action = await actions.apply_action(item.id, kind)
        result.undo_action_ids.append(action.id)
        result.done += 1
    except actions.ActionUnsupportedError:
        fail("action_unsupported")
    except HTTPException as error:
        if error.status_code != 404:
            logger.warning("%s", type(error).__name__)
        code = error.detail.get("code") if isinstance(error.detail, dict) else None
        fail(code if code == action_capabilities.ERROR_CODE else "item_gone" if error.status_code == 404 else "action_failed")
    except Exception as error:
        logger.warning("%s", type(error).__name__)
        fail("action_failed")


async def _confirm_task(message: ChatMessage, edit: task_proposals.TaskCreateCommand | None = None) -> ChatMessage:
    """One local transaction; retries use the existing not-pending conflict."""
    async with get_session_maker()() as session:
        claimed = await message_access.claim_proposal_in_session(session, message.id)
        if claimed is None:
            current = await message_access.get_message(message.id)
            if current and current.proposal_status == 'pending' and (not current.context_id or not await context_access.is_live(current.context_id)):
                raise ProposalContextRevokedError
            raise ProposalStateError
        proposal = proposal_view(claimed)
        assert proposal is not None and proposal.task is not None
        if edit is not None:
            if not isinstance(proposal.task, task_proposals.TaskCreateCommand) or (
                edit.source_account_id, edit.source_message_id
            ) != (proposal.task.source_account_id, proposal.task.source_message_id):
                raise ProposalSelectionError('selection_invalid')
            proposal.task = edit
        context = await context_access.get_context_in_session(session, claimed.context_id) if claimed.context_id else None
        if context is None or context.revoked or context.conversation_id != claimed.conversation_id:
            raise ProposalContextRevokedError
        scope = json.loads(context.task_scope_json)
        account_scope = json.loads(context.account_scope_json)
        source = proposal.task.source_account_id
        selection = account_scope.get('selection')
        from api.tasks.chat import allowed_in_selection
        if not allowed_in_selection(scope, selection, account_scope.get('account_ids', []), source, proposal.task.source_message_id):
            raise refuse(403, 'task_scope_invalid', 'Die Aufgabe gehört nicht zur gewählten Auswahl.')
        try:
            series_id = await task_proposals.execute(session, proposal.task, proposal_id=claimed.id)
        except task_patch.RevisionConflict:
            raise refuse(409, 'task_revision_conflict', 'Die neue Wiederholung überschneidet sich mit einer erledigten Fälligkeit. Wähle einen anderen Zeitplan.') from None
        except task_state.StateConflict:
            raise refuse(409, 'task_version_conflict', 'Die Aufgabe wurde inzwischen geändert. Prüfe den aktuellen Stand.') from None
        except task_state.StateNotFound:
            raise refuse(404, 'task_not_found', 'Die Aufgabe ist nicht mehr verfügbar.') from None
        except task_proposals.TaskSourceGone:
            raise refuse(409, 'task_source_gone', 'Die verknüpfte Mail ist nicht mehr verfügbar.') from None
        except ValueError:
            raise refuse(422, 'task_invalid', 'Die Aufgabe konnte nicht geändert werden. Prüfe ihre Angaben.') from None
        proposal.result = ProposalResult(done=1, task_series_id=series_id)
        updated = await message_access.finish_proposal_in_session(session, claimed.id, proposal_json=proposal.model_dump_json())
        await session.commit()
        return updated


async def _store_calendar(message: ChatMessage, proposal: Proposal, outcome: calendar_writes.WriteOutcome) -> ChatMessage:
    code = outcome.error_code or ("calendar_write_not_applied" if outcome.status == "not_applied" else None)
    proposal.result = ProposalResult(done=int(outcome.status == "done"), calendar_write_id=outcome.write_id,
        calendar_status=outcome.status, failed=[ItemProblem(id=message.id, code=code)] if code else [])
    status = "done" if outcome.status == "done" else "unknown" if outcome.status in {"unknown", "running"} else "failed"
    updated = await message_access.set_proposal(
        message.id, proposal_json=proposal.model_dump_json(context={"storage": True}), proposal_status=status,
        expected_statuses=("running", "unknown"),
    )
    if updated is None:
        # A late check must return the result already stored by a concurrent confirm.
        updated = await message_access.get_message(message.id)
    if updated is None:  # The card vanished concurrently, e.g. with its conversation.
        raise ProposalNotFoundError
    return updated


async def _unclaimed_outcome(proposal_id: str) -> calendar_writes.WriteOutcome:
    """Without a write record nothing was dispatched: the claim precedes every provider call."""
    from db.accessors import calendar_write
    if await calendar_write.get(proposal_id) is None:
        return calendar_writes.WriteOutcome(write_id=proposal_id, status="failed", error_code="calendar_unavailable")
    return calendar_writes.WriteOutcome(write_id=proposal_id, status="unknown", error_code="calendar_write_unknown")


async def _confirm_calendar(message: ChatMessage) -> ChatMessage:
    from db.accessors import calendar_source, calendar_write
    claimed = await message_access.claim_proposal(message.id)
    if claimed is None:
        current = await message_access.get_message(message.id)
        if current and current.proposal_status == "pending" and (not current.context_id or not await context_access.is_live(current.context_id)):
            raise ProposalContextRevokedError
        raise ProposalStateError
    proposal = proposal_view(claimed)
    assert proposal is not None and proposal.calendar is not None
    command = proposal.calendar
    try:
        context = await context_access.get_context(claimed.context_id) if claimed.context_id else None
        if context is None or context.revoked or context.conversation_id != claimed.conversation_id:
            raise calendar_writes.WriteError("proposal_context_revoked")
        if command.source_id not in json.loads(context.calendar_scope_json):
            raise calendar_writes.WriteError("calendar_out_of_scope", 403)
        entries = await calendar_source.catalog(ids=[command.source_id])
        if not entries or calendar_source.chat_access(*entries[0]) != "writable":
            raise calendar_writes.WriteError("calendar_not_writable", 403)
        source, _ = entries[0]
        if source.connection_ref != command.connection_ref:
            raise calendar_writes.WriteError("reconnect_required")
        outcome = await calendar_writes.execute(command.request(message.id), write_id=message.id)
    except calendar_writes.WriteError as error:
        outcome = calendar_writes.WriteOutcome(write_id=message.id, status="failed", error_code=error.code)
        if error.code == "calendar_write_conflict":
            stored = await calendar_write.get(message.id)
            # A card check may have settled the claim while this confirm was still alive.
            if (stored is not None and stored.request_hash == calendar_write.ABANDONED_REQUEST_HASH
                    and stored.source_id == command.source_id and stored.operation == command.action
                    and stored.status == "not_applied"):
                outcome = calendar_writes.WriteOutcome(write_id=message.id, status="not_applied", error_code=stored.error_code)
    except Exception as error:
        # The write may already have reached the provider. Never release the card for retry.
        logger.warning("calendar_confirm_failed %s", type(error).__name__)
        outcome = await _unclaimed_outcome(message.id)
    return await _store_calendar(message, proposal, outcome)


async def recheck_calendar_card(proposal_id: str) -> ChatMessage:
    """Resolve an unknown card, or a running one whose confirm call died after claiming it."""
    message = await _get_proposal(proposal_id)
    proposal = proposal_view(message)
    if proposal is None or proposal.kind != "calendar" or message.proposal_status not in {"unknown", "running"}:
        raise ProposalStateError
    from db.accessors import calendar_write
    if await calendar_write.get(proposal_id) is None:
        if message.proposal_status != "running":
            return await _store_calendar(message, proposal, await _unclaimed_outcome(proposal_id))
        # The confirm died between its card claim and its write claim, or is there right now.
        # Taking the card's write ID settles the card; a confirm that is still alive then finds
        # the ID taken at its own claim and sends nothing. If the confirm won, read its write.
        command = proposal.calendar
        taken = await calendar_write.abandon(proposal_id, source_id=command.source_id, operation=command.action)
        if taken == "abandoned":
            return await _store_calendar(message, proposal, calendar_writes.WriteOutcome(
                write_id=proposal_id, status="not_applied", error_code="calendar_write_not_applied"))
        if taken == "source_gone":
            # The removed calendar took its finished records along: the write may have been
            # applied, so the card must not say otherwise, and nothing is left to check.
            return await _store_calendar(message, proposal, calendar_writes.WriteOutcome(
                write_id=proposal_id, status="failed", error_code="calendar_unavailable"))
    # get() first expires a running record older than 120 s, then checks an unknown one.
    outcome = await calendar_writes.get(proposal_id)
    return await _store_calendar(message, proposal, outcome)


async def confirm(proposal_id: str, *, selected_ids: list[str], body: str | None = None,
                  task_edit: task_proposals.TaskCreateCommand | None = None,
                  preference_decision: preference_candidates.PreferenceDecision | None = None) -> ChatMessage:
    message = await _get_proposal(proposal_id)
    proposal = proposal_view(message)
    assert proposal is not None
    if proposal.kind == 'preference':
        if selected_ids or body is not None or task_edit is not None or preference_decision is None:
            raise ProposalSelectionError('selection_invalid')
        return await preference_candidates.confirm(proposal_id, preference_decision)
    if preference_decision is not None:
        raise ProposalSelectionError('selection_invalid')
    if proposal.kind == 'calendar':
        if selected_ids or body is not None or task_edit is not None:
            raise ProposalSelectionError('selection_invalid')
        return await _confirm_calendar(message)
    if proposal.kind == 'task':
        if selected_ids or body is not None:
            raise ProposalSelectionError('selection_invalid')
        return await _confirm_task(message, task_edit)
    if task_edit is not None:
        raise ProposalSelectionError('selection_invalid')
    selected = set(selected_ids)
    if not selected.issubset({item.id for item in proposal.items}):
        raise ProposalSelectionError("selection_invalid")
    if proposal.kind != "send" and not selected:
        raise ProposalSelectionError("selection_empty")
    required_accounts, mail_ids = set(), set()
    if proposal.kind == 'send' and proposal.draft:
        required_accounts.add(proposal.draft.account_id)
        if proposal.draft.reply_to_message_id:
            mail_ids.add(proposal.draft.reply_to_message_id)
    elif proposal.kind == 'unsubscribe':
        required_accounts.update(id.partition('|')[0] for id in selected)
    else:
        mail_ids = selected
    try:
        claimed = await message_access.claim_proposal(proposal_id, required_accounts=required_accounts, mail_ids=mail_ids,
            label_write=proposal.kind in ("archive", "trash", "mark_read"))
    except ProviderActionUnsupported:
        raise action_capabilities.unsupported() from None
    except chat_settings.AccountNotGranted:
        raise ProposalContextRevokedError from None
    if claimed is None:
        current = await message_access.get_message(proposal_id)
        if current and current.proposal_status == "pending" and (not current.context_id or not await context_access.is_live(current.context_id)):
            raise ProposalContextRevokedError
        raise ProposalStateError
    result = ProposalResult()
    proposal.result = result
    status = "failed"
    try:
        for item in proposal.items:
            item.selected = item.id in selected
        if proposal.kind == "send":
            assert proposal.draft is not None
            if body is not None:
                proposal.draft.body = body
            await _send(proposal.draft, result)
        else:
            mail_targets: set[str] = set()
            for item in proposal.items:
                if item.selected:
                    await _apply(item, proposal.kind, result, mail_targets)
        status = "done" if not result.failed else "partial" if result.done or result.manual else "failed"
    finally:
        updated = await message_access.set_proposal(proposal_id, proposal_json=proposal.model_dump_json(), proposal_status=status)
    assert updated is not None
    return updated
