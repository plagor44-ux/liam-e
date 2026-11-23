"""Tests for the local development seed's database safety guard."""

import json
from unittest.mock import AsyncMock
from db.accessors import calendar_seed

import pytest

from datetime import UTC, datetime

from api.mail import dev_seed, headers
from api.routers.api.v1.mail.schemas import AttachmentOut
from api.mail.dev_seed import SeedRefusedError, db_env, require_local_database
from db.models.chat_conversation import ChatConversation
from db.models.chat_message import ChatMessage
from db.models.mail_account import MailAccount
from api.mail.proposals import proposal_view
from api.mail.chat_schemas import ChatExtrasOut
from db.models.mail_message import MailMessage


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://u:p@db.example.com:5432/x",
    "postgresql+asyncpg://u:p@10.0.0.5:5432/x",
    "postgresql+asyncpg:///x",
    "postgresql+asyncpg:///x?host=/var/run/postgresql",
    "postgresql+asyncpg://u:p@localhost/x?host=db.example.com",
])
def test_refuses_nonlocal_database(monkeypatch, url):
    monkeypatch.setattr(db_env, "database_url", url)
    with pytest.raises(SeedRefusedError) as error:
        require_local_database()
    assert "u:p" not in str(error.value)


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "[::1]"])
def test_allows_local_database(monkeypatch, host):
    monkeypatch.setattr(db_env, "database_url", f"postgresql+asyncpg://u:p@{host}/x")
    require_local_database()


@pytest.mark.anyio
async def test_task_seed_uses_services_and_only_deletes_its_own_series(monkeypatch):
    from types import SimpleNamespace
    from db import engine
    from db.accessors import task_occurrence, task_series
    from api.tasks import create, lifecycle, reconcile, state

    deleted, created, completed, archived = [], [], [], []
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def commit(self): pass
    monkeypatch.setattr(engine, 'get_session_maker', lambda: Session)
    async def list_series(_session, **kwargs):
        if kwargs['after'] is not None: return []
        return [SimpleNamespace(id='a', version=1, creation_key='board-seed-task:old'),
                SimpleNamespace(id='b', version=1, creation_key='personal')]
    async def delete(_session, series_id, **_kwargs): deleted.append(series_id)
    async def make(_session, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=kwargs['creation_key'], version=1), True
    async def catch_up(_session, **_kwargs): return 'revision', 'cursor', True
    async def instances(_session, **_kwargs): return [SimpleNamespace(id='occ', version=1)]
    async def transition(_session, occurrence_id, **_kwargs): completed.append(occurrence_id)
    async def archive(_session, series_id, **_kwargs): archived.append(series_id)
    monkeypatch.setattr(task_series, 'list_page', list_series)
    monkeypatch.setattr(task_occurrence, 'list_page', instances)
    monkeypatch.setattr(lifecycle, 'delete', delete)
    monkeypatch.setattr(lifecycle, 'archive', archive)
    monkeypatch.setattr(create, 'create', make)
    monkeypatch.setattr(reconcile, 'reconcile_current', catch_up)
    monkeypatch.setattr(state, 'transition', transition)
    await dev_seed.seed_tasks(datetime(2026, 11, 13, 12, tzinfo=UTC))
    assert deleted == ['a']
    assert len(created) == 8
    assert {row['rule'] for row in created} == {None, 'FREQ=DAILY', 'FREQ=WEEKLY', 'FREQ=MONTHLY'}
    assert sum(bool(row.get('source_message_id')) for row in created) == 1
    assert completed == ['occ']
    assert archived == ['board-seed-task:archived']


@pytest.mark.anyio
async def test_seed_is_repeatable_and_preserves_other_rows(fake_store, monkeypatch):
    monkeypatch.setattr(db_env, "database_url", "postgresql+asyncpg://localhost/test")
    outsider = MailAccount(id="other", email_address="other@example.org", provider="imap", auth_type="imap_password")
    other_mail = MailMessage(id="other-mail", account_id=outsider.id, provider_uid="other")
    fake_store.accounts[outsider.id] = outsider
    fake_store.messages[other_mail.id] = other_mail
    insights = {}
    outsider_conversation = ChatConversation(id="other-conversation", title="Other")
    outsider_chat = ChatMessage(conversation_id=outsider_conversation.id, role="user", content="Other")
    conversations = {outsider_conversation.id: outsider_conversation}
    chats = {outsider_chat.id: outsider_chat}

    async def replace_conversations(rows):
        ids = {row.id for row in rows}
        for id in list(chats):
            if chats[id].conversation_id in ids:
                del chats[id]
        conversations.update({row.id: row for row in rows})

    async def add_message(**kwargs):
        row = ChatMessage(**kwargs)
        chats[row.id] = row
        return row

    async def cancel_open_proposals(conversation_id):
        for row in chats.values():
            if row.conversation_id == conversation_id and row.proposal_status == "pending":
                row.proposal_status = "cancelled"

    monkeypatch.setattr(dev_seed.seed_access, "replace_conversations", replace_conversations)
    monkeypatch.setattr(dev_seed.chat_access, "add_message", add_message)
    monkeypatch.setattr(dev_seed.chat_access, "cancel_open_proposals", cancel_open_proposals)

    async def replace(accounts, states, messages, rows, jobs):
        for account in accounts:
            fake_store.accounts.pop(account.id, None)
        for state in states:
            fake_store.states.pop(state.account_id, None)
        for message in messages:
            fake_store.messages.pop(message.id, None)
            insights.pop(message.id, None)
        fake_store.jobs.update({row.id: row for row in jobs})
        fake_store.accounts.update({row.id: row for row in accounts})
        fake_store.states.update({row.account_id: row for row in states})
        fake_store.messages.update({row.id: row for row in messages})
        insights.update({row.message_id: row for row in rows})

    monkeypatch.setattr(dev_seed.seed_access, "replace_mail_data", replace)
    async def skip_tasks(_now):
        pass
    monkeypatch.setattr(dev_seed, "seed_tasks", skip_tasks)
    monkeypatch.setattr(dev_seed, "seed_filing", AsyncMock())
    monkeypatch.setattr(calendar_seed, "replace", AsyncMock())
    now = datetime(2026, 11, 13, 12, 30, tzinfo=UTC)
    for _ in range(2):
        result = await dev_seed.seed(now)
        assert (result.accounts, result.mails, result.unread, result.insights, result.conversations, result.proposals) == (4, 67, 47, 1, 4, 1)
        assert len(conversations) == 5
        assert len(chats) == 8
        assert conversations[outsider_conversation.id] == outsider_conversation
        assert chats[outsider_chat.id] == outsider_chat
        proposals = [row for row in chats.values() if row.proposal_status == "pending"]
        assert len(proposals) == 1
        proposal = proposal_view(proposals[0])
        assert proposal is not None and proposal.kind == "unsubscribe"
        assert [item.title for item in proposal.items] == ["Wohnwelt Deals", "Laufschuh-Club", "TechDigest Weekly", "Gartenfreunde Post", "ReiseRadar"]
        assert [item.count for item in proposal.items] == [23, 11, 14, 6, 7]
        assert proposal.items[0].subtitle == "23 Mails · newsletter@wohnwelt-deals.example · Abmeldung per Mail an abmelden@wohnwelt-deals.example, Betreff „unsubscribe“"
        for item in proposal.items:
            account_id, address = item.id.split("|", 1)
            matching = [m for m in fake_store.messages.values() if m.account_id == account_id and m.sender_address == address]
            assert item.selected and len(matching) == item.count
        first_id = dev_seed.seed_id("conversation", "was-ist-heute-wichtig")
        first = [row for row in chats.values() if row.conversation_id == first_id]
        assert [row.role for row in first] == ["user", "assistant"]
        extras = ChatExtrasOut.model_validate_json(first[1].extras_json)
        assert extras.mails == [dev_seed.seed_id("mail", key) for key in ("hausverwaltung-nebenkosten", "meyer-angebot-4471", "kita-elternabend")]
        assert extras.suggestions == ["Antworten entwerfen", "Nur die Hausverwaltung", "Was steht in der Abrechnung?"]
        assert extras.model == "mistral-medium-latest"
        assert first[1].content.startswith("Auf drei Mails solltest du heute antworten.\n\n")
        assert conversations[first_id].updated_at == datetime(2026, 11, 13, 7, 57, tzinfo=UTC)
        assert len(fake_store.accounts) == 5
        assert len(fake_store.messages) == 68
        assert len(insights) == 1
        assert sum(not mail.is_read and mail.folder == "INBOX" for mail in fake_store.messages.values() if mail.id != "other-mail") == 47
        assert fake_store.states[dev_seed.seed_id("account", "arbeit")].last_error == "auth_expired"
        job = fake_store.jobs[dev_seed.seed_id("job", "familie")]
        assert job.status == "running"
        assert job.locked_at == datetime(2026, 11, 13, 12, 29, tzinfo=UTC)
        assert fake_store.accounts["other"] == outsider
        assert fake_store.messages["other-mail"] == other_mail
        house = fake_store.messages[dev_seed.seed_id("mail", "hausverwaltung-nebenkosten")]
        assert house.internal_date == datetime(2026, 11, 13, 7, 57, tzinfo=UTC)
        assert headers.sender_name(house.sender) == "Hausverwaltung Kurz"
        attachments = [AttachmentOut.model_validate(item) for item in json.loads(house.attachments_json)]
        assert len(attachments) == 1 and attachments[0].content_type == "application/pdf"
        for name, slug, *_ in dev_seed.NEWSLETTERS:
            row = fake_store.messages[dev_seed.seed_id("mail", f"{slug}-1")]
            assert headers.sender_name(row.sender) == name
        assert insights[house.id].deadline.isoformat() == "2026-11-20"


@pytest.mark.anyio
async def test_seed_refuses_before_access(monkeypatch):
    monkeypatch.setattr(db_env, "database_url", "postgresql+asyncpg://u:p@db.example.com/x")

    async def forbidden(*args):
        pytest.fail("database accessor called")

    monkeypatch.setattr(dev_seed.seed_access, "replace_mail_data", forbidden)
    with pytest.raises(SeedRefusedError):
        await dev_seed.seed()


def test_main_refusal(monkeypatch, capsys):
    monkeypatch.setattr(db_env, "database_url", "postgresql+asyncpg://u:p@db.example.com/x")
    with pytest.raises(SystemExit) as error:
        dev_seed.main()
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Database host refused: db.example.com\n"
