"""Confirmed chat filing stays within its saved mail grant."""

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("RUN_PG_CONTRACT"), reason="RUN_PG_CONTRACT is not set")


def test_filing_card_is_atomic_and_checks_saved_account_scope(pg_db):
    async def run():
        from api.mail import proposals
        from db.accessors import chat_context, chat_settings, mail_filing, mail_message
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        a = MailAccount(provider="google", auth_type="oauth2", email_address="a@example.test")
        b = MailAccount(provider="google", auth_type="oauth2", email_address="b@example.test")
        async with get_session_maker().begin() as session:
            session.add(a); session.add(b)
        await chat_settings.update(1, [a.id])
        conversation, context, _ = await chat_context.resolve(conversation_id=None, title="Test", model=None, revision_id=None,
            account_id=a.id, app_prompt_version="test-only")
        first = await mail_message.upsert_message(account_id=a.id, folder="INBOX", provider_uid="one")
        foreign = await mail_message.upsert_message(account_id=b.id, folder="INBOX", provider_uid="two")
        folder = await mail_filing.create_destination("Projects", "folder")
        items = [proposals.ProposalItem(id=first.id, kind="mail", title="First", subtitle=""),
                 proposals.ProposalItem(id=foreign.id, kind="mail", title="Foreign", subtitle="")]
        card = await proposals.create_proposal(conversation.id, proposals.Proposal(kind="filing", reason="File", items=items,
            filing=proposals.FilingCommand(folder_id=folder.id)), context_id=context.id)
        with pytest.raises(proposals.ProposalContextRevokedError):
            await proposals.confirm(card.id, selected_ids=[first.id, foreign.id])
        assert (await mail_filing.for_message(first.id)).folder_id is None
        assert (await mail_filing.for_message(foreign.id)).folder_id is None
        done = await proposals.confirm(card.id, selected_ids=[first.id])
        assert done.proposal_status == "done"
        assert (await mail_filing.for_message(first.id)).folder_id == folder.id
        with pytest.raises(proposals.ProposalStateError):
            await proposals.confirm(card.id, selected_ids=[first.id])

    asyncio.run(run())


def test_rule_card_requires_confirmation_and_cannot_exceed_scope(pg_db):
    async def run():
        from api.mail import proposals
        from api.mail.rule_schemas import RuleIn
        from db.accessors import chat_context, chat_settings, mail_filing, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        a = MailAccount(provider="google", auth_type="oauth2", email_address="a@example.test")
        b = MailAccount(provider="google", auth_type="oauth2", email_address="b@example.test")
        async with get_session_maker().begin() as session:
            session.add(a); session.add(b)
        await chat_settings.update(1, [a.id])
        conversation, context, _ = await chat_context.resolve(conversation_id=None, title="Test", model=None, revision_id=None,
            account_id=a.id, app_prompt_version="test-only")
        folder = await mail_filing.create_destination("Projects", "folder")
        command = RuleIn(name="File", account_id=a.id, sender_domain="example.test", folder_id=folder.id)
        card = await proposals.create_proposal(conversation.id, proposals.Proposal(kind="rule", reason="Rule", items=[], rule=command), context_id=context.id)
        assert await mail_rule.list_rules() == []
        assert (await proposals.confirm(card.id, selected_ids=[])).proposal_status == "done"
        assert len(await mail_rule.list_rules()) == 1
        global_rule = command.model_copy(update={"account_id": None})
        card = await proposals.create_proposal(conversation.id, proposals.Proposal(kind="rule", reason="Rule", items=[], rule=global_rule), context_id=context.id)
        with pytest.raises(proposals.ProposalContextRevokedError):
            await proposals.confirm(card.id, selected_ids=[])
        assert len(await mail_rule.list_rules()) == 1

    asyncio.run(run())


def test_rule_tool_requires_explicit_current_user_request(pg_db):
    async def run():
        import json
        from datetime import date
        from api.mail.agent_tools import TurnContext, execute_tool
        from db.accessors import chat_context, chat_message, chat_settings, mail_filing
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount

        account = MailAccount(provider="google", auth_type="oauth2", email_address="a@example.test")
        async with get_session_maker().begin() as session:
            session.add(account)
        await chat_settings.update(1, [account.id])
        conversation, context, _ = await chat_context.resolve(conversation_id=None, title="Test", model=None, revision_id=None,
            account_id=account.id, app_prompt_version="test-only")
        folder = await mail_filing.create_destination("Projects", "folder")
        source = await chat_message.add_message(conversation_id=conversation.id, role="user", content="Summarize my email", context_id=context.id)
        ctx = TurnContext(conversation.id, account.id, date.today(), account_ids=[account.id], context_id=context.id, source_message_id=source.id)
        args = {"name": "Projects", "account_id": account.id, "sender_domain": "example.test", "folder_id": folder.id, "reason": "Example"}
        denied = await execute_tool("propose_rule", args, ctx)
        assert denied.proposal_message is None
        assert json.loads(denied.content)["error"] == "explicit_rule_request_required"
        request = await chat_message.add_message(conversation_id=conversation.id, role="user", content="Create a rule for these messages", context_id=context.id)
        ctx.source_message_id = request.id
        allowed = await execute_tool("propose_rule", args, ctx)
        assert allowed.proposal_message is not None
        assert allowed.proposal_message.proposal_status == "pending"

    asyncio.run(run())


def test_filing_tool_opens_card_without_provider_write(pg_db):
    async def run():
        import json
        from datetime import date
        from api.mail.agent_tools import TurnContext, execute_tool
        from db.accessors import chat_context, chat_settings, mail_filing, mail_message
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount
        from db.models.mail_sync_state import MailSyncState
        from api.mail.connector_mailbox import encode

        account = MailAccount(provider="gmail", auth_type="mistral_connector", email_address="a@example.test")
        async with get_session_maker().begin() as session:
            session.add(account)
            session.add(MailSyncState(account_id=account.id, connector_verified=True,
                                      connector_cursor=encode(("named", "anchor", "proof"), "")))
        await chat_settings.update(1, [account.id])
        conversation, context, _ = await chat_context.resolve(conversation_id=None, title="Test", model=None, revision_id=None,
            account_id=account.id, app_prompt_version="test-only")
        from sqlalchemy import text
        async with get_session_maker().begin() as session:
            await session.execute(text("SELECT set_config('liam.verified_connector', :account, true)"), {"account": account.id})
            message = await mail_message.upsert_message(account_id=account.id, folder="INBOX", provider_uid="one", session=session)
        folder = await mail_filing.create_destination("Projects", "folder")
        ctx = TurnContext(conversation.id, account.id, date.today(), account_ids=[account.id], context_id=context.id)
        loaded = await mail_message.get_message(message.id)
        assert loaded is not None and loaded.account_id == account.id and not loaded.unverified_connector
        outcome = await execute_tool("propose_filing", {"message_ids": [message.id], "folder_id": folder.id, "reason": "File"}, ctx)
        assert json.loads(outcome.content).get("status") == "pending", outcome.content
        assert (await mail_filing.for_message(message.id)).folder_id is None
        from api.mail import proposals
        await proposals.confirm(outcome.proposal_message.id, selected_ids=[message.id])
        assert (await mail_filing.for_message(message.id)).folder_id == folder.id

    asyncio.run(run())
