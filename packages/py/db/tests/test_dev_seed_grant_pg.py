"""The local seed grants only its chat-eligible accounts and keeps every other grant (#163)."""

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("RUN_PG_CONTRACT"), reason="RUN_PG_CONTRACT is not set")


async def accounts_with_states():
    from db.engine import get_session_maker
    from db.models.chat_settings import ChatSettings
    from db.models.mail_account import MailAccount
    from db.models.mail_sync_state import MailSyncState

    rows = [
        MailAccount(id="imap", email_address="me@example.org", provider="imap", auth_type="imap_password"),
        MailAccount(id="connector", email_address="x@example.org", provider="gmail", auth_type="mistral_connector"),
        MailAccount(id="other", email_address="other@example.org", provider="imap", auth_type="imap_password"),
    ]
    async with get_session_maker()() as session:
        session.add_all(rows)
        # An unverified Studio connector is not eligible for chat, as in Settings → Chat.
        session.add(MailSyncState(id="connector-sync", account_id="connector", status="ok"))
        session.add(ChatSettings(account_ids=["other"], version=3))
        await session.commit()


def test_seed_grant_adds_only_eligible_accounts_and_is_stable_on_repeat(pg_db):
    async def run():
        from db.accessors import chat_settings, dev_seed

        await accounts_with_states()
        await dev_seed.grant_chat_accounts(["imap", "connector"])
        first = await chat_settings.get()
        assert first.account_ids == ["imap", "other"]
        assert first.version == 4
        # Negative case: repeating the seed adds nothing, so the revision and seeded contexts stay current.
        await dev_seed.grant_chat_accounts(["imap", "connector"])
        again = await chat_settings.get()
        assert (again.account_ids, again.version) == (["imap", "other"], 4)

    asyncio.run(run())
