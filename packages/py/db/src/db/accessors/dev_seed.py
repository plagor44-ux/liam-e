"""Persist the local development seed without touching unrelated rows."""

from sqlalchemy import delete

from db.engine import get_session_maker
from db.models.chat_conversation import ChatConversation
from db.models.chat_message import ChatMessage
from db.models.mail_account import MailAccount
from db.models.mail_insight import MailInsight
from db.models.mail_job import MailJob
from db.models.mail_message import MailMessage
from db.models.mail_sync_state import MailSyncState


async def replace_conversations(conversations: list[ChatConversation]) -> None:
    """Remove seeded conversations and all their messages, then restore the conversations."""
    ids = [row.id for row in conversations]
    async with get_session_maker()() as session:
        from db.accessors import user_preference, preference_candidate
        await user_preference.lock(session)
        await preference_candidate.expire_conversations(session, ids)
        await session.execute(delete(ChatMessage).where(ChatMessage.conversation_id.in_(ids)))
        await session.execute(delete(ChatConversation).where(ChatConversation.id.in_(ids)))
        session.add_all(conversations)
        await session.commit()


async def replace_mail_data(accounts: list[MailAccount], states: list[MailSyncState], messages: list[MailMessage], insights: list[MailInsight], jobs: list[MailJob]) -> None:
    """Replace only the provided deterministic IDs in a single transaction."""
    async with get_session_maker()() as session:
        await session.execute(delete(MailJob).where(MailJob.id.in_([j.id for j in jobs])))
        await session.execute(delete(MailInsight).where(MailInsight.message_id.in_([m.id for m in messages])))
        await session.execute(delete(MailSyncState).where(MailSyncState.account_id.in_([a.id for a in accounts])))
        await session.execute(delete(MailMessage).where(MailMessage.id.in_([m.id for m in messages])))
        await session.execute(delete(MailAccount).where(MailAccount.id.in_([a.id for a in accounts])))
        session.add_all([*accounts, *states, *messages, *insights, *jobs])
        await session.commit()


async def replace_filing(destinations, filings, labels, rules, runs=(), usage=()) -> None:
    """Replace only the deterministic filing rows provided by the local seed."""
    from db.models.mail_folder import MailFolder
    from db.models.mail_filing import MailFiling
    from db.models.mail_label_assignment import MailLabelAssignment
    from db.models.mail_rule import MailRule
    from db.models.mail_rule_run import MailRuleRun
    from db.models.mail_filing_usage import MailFilingUsage

    async with get_session_maker().begin() as session:
        message_ids = {row.message_id for row in filings} | {row.message_id for row in labels}
        rule_ids = [row.id for row in rules]
        await session.execute(delete(MailRuleRun).where(MailRuleRun.rule_id.in_(rule_ids)))
        await session.execute(delete(MailRule).where(MailRule.id.in_(rule_ids)))
        await session.execute(delete(MailFiling).where(MailFiling.message_id.in_(message_ids)))
        await session.execute(delete(MailLabelAssignment).where(MailLabelAssignment.message_id.in_(message_ids)))
        await session.execute(delete(MailFilingUsage).where(MailFilingUsage.destination_id.in_([row.id for row in destinations])))
        await session.execute(delete(MailFolder).where(MailFolder.id.in_([row.id for row in destinations])))
        session.add_all([*destinations, *filings, *labels, *rules, *runs, *usage])
        await session.flush()
        from db.accessors import mail_filing_usage
        await mail_filing_usage.prune_in_session(session)
