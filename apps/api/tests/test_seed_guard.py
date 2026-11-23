"""Sample accounts never perform provider operations."""

from datetime import UTC, datetime
from uuid import uuid4
from unittest.mock import AsyncMock

import httpx
import pytest
from db.accessors import calendar_seed
from db.accessors.mail_message import NewsletterRow
from db.models.chat_message import ChatMessage

from api.mail import actions, connectors, dev_seed, newsletters, proposals, send, sync, transport
from api.mail.seed_ids import SEED_ACCOUNT_IDS, is_seed_account, seed_id

pytestmark = pytest.mark.anyio


@pytest.fixture
async def seeded(fake_store, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("outbound operation called for a seed account")

    for name in ("call_write_tool", "call_read_tool", "call_read_tool_json"):
        monkeypatch.setattr(connectors, name, forbidden)
    for name in ("open_smtp", "open_imap"):
        monkeypatch.setattr(transport, name, forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(newsletters.netguard, "safe_https_post", forbidden)
    monkeypatch.setattr(dev_seed.db_env, "database_url", "postgresql+asyncpg://localhost/test")
    chats = {}

    async def replace(accounts, states, messages, insights, jobs):
        fake_store.accounts.update({row.id: row for row in accounts})
        fake_store.states.update({row.account_id: row for row in states})
        fake_store.messages.update({row.id: row for row in messages})

    async def add(**kwargs):
        row = ChatMessage(**kwargs)
        chats[row.id] = row
        return row

    async def claim(id, **targets):
        row = chats[id]
        if row.proposal_status != "pending":
            return None
        row.proposal_status = "running"
        return row

    async def update(id, **kwargs):
        for key, value in kwargs.items():
            setattr(chats[id], key, value)
        return chats[id]

    monkeypatch.setattr(dev_seed.seed_access, "replace_mail_data", replace)
    monkeypatch.setattr(calendar_seed, "replace", AsyncMock())
    monkeypatch.setattr(dev_seed.seed_access, "replace_conversations", AsyncMock())
    # The task seed writes through real sessions; its own tests cover it (test_dev_seed.py).
    monkeypatch.setattr(dev_seed, "seed_tasks", AsyncMock())
    monkeypatch.setattr(dev_seed, "seed_filing", AsyncMock())
    monkeypatch.setattr(dev_seed.chat_access, "add_message", add)
    monkeypatch.setattr(dev_seed.chat_access, "cancel_open_proposals", AsyncMock())
    monkeypatch.setattr(proposals.message_access, "get_message", AsyncMock(side_effect=lambda id: chats.get(id)))
    monkeypatch.setattr(proposals.message_access, "claim_proposal", claim)
    monkeypatch.setattr(proposals.message_access, "set_proposal", update)
    await dev_seed.seed(datetime(2026, 11, 13, 12, tzinfo=UTC))
    return fake_store, chats


async def test_confirm_seeded_unsubscribe_is_blocked(seeded, monkeypatch):
    store, chats = seeded
    row = next(row for row in chats.values() if row.proposal_status == "pending")
    item = proposals.proposal_view(row).items[0]
    account_id, address = item.id.split("|", 1)
    mail = next(m for m in store.messages.values() if m.account_id == account_id and m.sender_address == address)
    async def rows(*, account_ids=None, chat_only=False):
        return [NewsletterRow(account_id, address, mail.sender, 23, 23, None, mail.list_unsubscribe, "", "[]")]
    monkeypatch.setattr(newsletters.mail_message, "newsletter_senders", rows)
    await proposals.confirm(row.id, selected_ids=[item.id])
    assert proposals.proposal_view(row).result.failed[0].code == "unsubscribe_blocked"
    assert not store.actions


@pytest.mark.parametrize("key", ["privat", "arbeit", "verein", "familie"])
async def test_seed_send_refused_and_proposal_maps_error(seeded, key):
    store, _ = seeded
    account = store.accounts[seed_id("account", key)]
    with pytest.raises(send.SeedAccountError):
        await send.send_message(account, to=["x@example.org"], subject="X", body="X")
    result = proposals.ProposalResult()
    draft = proposals.SendDraft(account_id=account.id, mode="new", to=["x@example.org"], cc=[], subject="X", body="X", quote="", reply_to_message_id=None)
    await proposals._send(draft, result)
    assert result.failed[0].code == "send_failed"


@pytest.mark.parametrize("method,link", [("one_click", "https://example.org/leave"), ("mailto", "mailto:leave@example.org"), ("link", "https://example.org/leave"), ("none", None)])
async def test_all_unsubscribe_methods_blocked(seeded, method, link):
    store, _ = seeded
    account = store.accounts[seed_id("account", "privat")]
    sender = newsletters.NewsletterSender("id", account.id, "news@example.org", "News", 1, 1, None, method, link)
    outcome = await newsletters.unsubscribe(sender, account)
    assert (outcome.status, outcome.error_code) == ("failed", "unsubscribe_blocked")
    assert not store.actions


async def test_archive_and_undo_are_index_only(seeded):
    store, _ = seeded
    mail = store.messages[seed_id("mail", "meyer-angebot-4471")]
    original = mail.model_dump()
    action = await actions.apply_action(mail.id, "archive")
    assert mail.folder == "ARCHIVE"
    result = await actions.undo_actions([action.id], now=datetime.now(UTC))
    assert result.restored == [action.id]
    for key in ("folder", "is_read", "provider_uid", "labels_json"):
        assert getattr(mail, key) == original[key]


async def test_sync_seed_keeps_state_and_fetches_nothing(seeded, monkeypatch):
    store, _ = seeded
    for provider in (sync.sync_connector, sync.sync_gmail, sync.sync_graph, sync.sync_imap):
        monkeypatch.setattr(provider, "fetch_changes", AsyncMock(side_effect=AssertionError("fetch")))
    for account_id in SEED_ACCOUNT_IDS:
        before = store.states[account_id].model_dump()
        result = await sync.sync_account(account_id)
        assert result.synced == 0
        assert store.states[account_id].model_dump() == before
    for provider in (sync.sync_connector, sync.sync_gmail, sync.sync_graph, sync.sync_imap):
        provider.fetch_changes.assert_not_awaited()


def test_only_seed_ids_are_seed_accounts():
    assert len(SEED_ACCOUNT_IDS) == 4
    assert not is_seed_account(str(uuid4()))
