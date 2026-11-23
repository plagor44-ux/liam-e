"""Chat history and proposal bookkeeping.

Proposals are confirmed via ``POST /api/v1/chat/proposals/{proposal_id}/confirm``.
"""

from collections.abc import Collection
import json

from sqlalchemy import select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from db.accessors import calendar_proposal_queue, calendar_source, chat_context, chat_settings, mail_message, user_preference
from db.engine import get_session_maker
from db.models.chat_message import ChatMessage
from db.models.chat_context import ChatContext
from db.models.mail_account import MailAccount, ProviderActionUnsupported
from db.models import mail_account as account_model
from db.models.mail_message import MailMessage


async def add_message(
    *,
    conversation_id: str,
    role: str,
    content: str = "",
    tool_name: str = "",
    tool_payload_json: str = "",
    draft_json: str = "",
    proposal_json: str = "",
    proposal_status: str = "",
    stopped: bool = False,
    extras_json: str = "",
    context_id: str | None = None,
) -> ChatMessage:
    async with get_session_maker()() as session:
        message = ChatMessage(
            conversation_id=conversation_id,
            context_id=context_id,
            role=role,
            content=content,
            tool_name=tool_name,
            tool_payload_json=tool_payload_json,
            draft_json=draft_json,
            proposal_json=proposal_json,
            proposal_status=proposal_status,
            stopped=stopped,
            extras_json=extras_json,
        )
        session.add(message)
        await session.commit()
        return message


async def claim_proposal(message_id: str, *, required_accounts: Collection[str] = (),
                         mail_ids: Collection[str] = (), label_write: bool = False) -> ChatMessage | None:
    async with get_session_maker()() as session:
        message = await claim_proposal_in_session(session, message_id)
        if message is not None and (required_accounts or mail_ids):
            context = await session.get(ChatContext, message.context_id)
            permitted = set(json.loads(context.account_scope_json)['account_ids'])
            targets = set(required_accounts)
            if mail_ids:
                rows = (await session.execute(select(MailMessage.account_id, MailMessage.unverified_connector).where(
                    mail_message.matches_ids(mail_ids)).distinct())).all()
                if any(unverified for _, unverified in rows):
                    raise chat_settings.AccountNotGranted
                targets.update(account for account, _ in rows)
            if not targets.issubset(permitted):
                raise chat_settings.AccountNotGranted
            if label_write and targets:
                accounts = await session.scalars(select(MailAccount).where(MailAccount.id.in_(targets)))
                if any(not account_model.label_writes_supported(a.provider, a.auth_type) for a in accounts):
                    raise ProviderActionUnsupported
        await session.commit()
        return message


async def claim_proposal_in_session(session: AsyncSession, message_id: str) -> ChatMessage | None:
    """Claim a local mutation without committing; UPDATE holds the row lock.

    The caller must keep claim, domain mutation and finish in one transaction.
    A rollback restores pending. Provider actions must keep using claim_proposal.
    The context predicate deliberately matches the existing claim boundary.
    """
    await user_preference.lock(session)
    context = (await session.execute(select(ChatContext).join(ChatMessage,
        (ChatMessage.context_id == ChatContext.id) & (ChatMessage.conversation_id == ChatContext.conversation_id)
    ).where(ChatMessage.id == message_id, ChatMessage.proposal_status == "pending"))).scalar_one_or_none()
    if not await chat_settings.permits_context(session, context):
        return None
    if not await chat_context.permits_skill_in_session(session, context):
        return None
    if not await calendar_source.permits_context(session, context):
        return None
    result = await session.execute(
        update(ChatMessage).where(
            ChatMessage.id == message_id, ChatMessage.role == "proposal",
            ChatMessage.proposal_status == "pending",
            select(ChatContext.id).where(
                ChatContext.id == ChatMessage.context_id, ChatContext.revoked.is_(False)
            ).exists(),
        ).values(proposal_status="running").returning(ChatMessage)
    )
    return result.scalar_one_or_none()


async def finish_proposal_in_session(
    session: AsyncSession, message_id: str, *, proposal_json: str,
) -> ChatMessage:
    """Finish a claimed local mutation; never commit independently of its effect."""
    result = await session.execute(
        update(ChatMessage).where(
            ChatMessage.id == message_id, ChatMessage.role == "proposal",
            ChatMessage.proposal_status == "running",
        ).values(proposal_json=proposal_json, proposal_status="done").returning(ChatMessage)
    )
    return result.scalar_one()


async def cancel_proposal(message_id: str) -> ChatMessage | None:
    """Cancel only a pending proposal; never interrupt an executing confirmation."""
    async with get_session_maker()() as session:
        result = await session.execute(
            update(ChatMessage)
            .where(ChatMessage.id == message_id, ChatMessage.proposal_status == "pending")
            .values(proposal_status="cancelled")
            .returning(ChatMessage)
        )
        message = result.scalar_one_or_none()
        await calendar_proposal_queue.release(session, message)
        await session.commit()
        return message


async def set_proposal(
    message_id: str, *, proposal_json: str, proposal_status: str, expected_statuses: tuple[str, ...] | None = None,
) -> ChatMessage | None:
    statement = update(ChatMessage).where(ChatMessage.id == message_id)
    if expected_statuses is not None:
        statement = statement.where(ChatMessage.proposal_status.in_(expected_statuses))
    async with get_session_maker()() as session:
        result = await session.execute(
            statement.values(proposal_json=proposal_json, proposal_status=proposal_status).returning(ChatMessage)
        )
        message = result.scalar_one_or_none()
        await calendar_proposal_queue.release(session, message)
        await session.commit()
        return message


async def open_proposal(conversation_id: str) -> ChatMessage | None:
    async with get_session_maker()() as session:
        result = await session.execute(select(ChatMessage).where(
            ChatMessage.conversation_id == conversation_id, ChatMessage.proposal_status == "pending"
        ))
        return result.scalar_one_or_none()


async def _cancel_open_proposals(session: AsyncSession, conversation_id: str) -> int:
    from db.accessors import preference_candidate
    from db.models.preference_candidate import PreferenceCandidate
    await user_preference.lock(session)
    candidates = (await session.execute(select(PreferenceCandidate).where(
        PreferenceCandidate.conversation_id == conversation_id,
        PreferenceCandidate.status == 'pending', PreferenceCandidate.proposal_id.is_not(None),
    ))).scalars()
    for candidate in candidates:
        # Leave the candidate available for a later review.
        await preference_candidate.detach(session, candidate)
    result = await session.execute(
        update(ChatMessage).where(ChatMessage.conversation_id == conversation_id, ChatMessage.proposal_status == "pending")
        .values(proposal_status="cancelled").returning(ChatMessage.id)
    )
    return len(result.scalars().all())


async def cancel_open_proposals(conversation_id: str) -> int:
    async with get_session_maker().begin() as session:
        return await _cancel_open_proposals(session, conversation_id)


async def replace_with_calendar_proposal(card: ChatMessage, source_id: str, connection_ref: str) -> ChatMessage:
    async with get_session_maker().begin() as session:
        await _cancel_open_proposals(session, card.conversation_id)
        if not await calendar_proposal_queue.can_offer(session, card.conversation_id, card.context_id, source_id, connection_ref):
            raise calendar_proposal_queue.TargetUnavailable
        session.add(card)
        return card


async def get_message(message_id: str) -> ChatMessage | None:
    async with get_session_maker()() as session:
        return await session.get(ChatMessage, message_id)


async def list_messages(conversation_id: str) -> list[ChatMessage]:
    async with get_session_maker()() as session:
        result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at)
        )
        return list(result.scalars().all())
