"""Live Postgres contracts for the scoped agent mail index services."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("RUN_PG_CONTRACT"), reason="RUN_PG_CONTRACT is not set")


def test_read_services_use_only_selects_and_scope_every_query(pg_db, monkeypatch):
    async def run():
        from api.mcp import mail
        from api.mcp.access import ReadGrant
        from db.accessors import agent_access, mail_account, mail_message, mail_sync_state
        from db.engine import get_engine
        from sqlalchemy import event
        from sqlalchemy.orm import Session

        first = await mail_account.create_account(provider="imap", auth_type="imap_password", email_address="a@example.org", display_name="A")
        second = await mail_account.create_account(provider="imap", auth_type="imap_password", email_address="b@example.org", display_name="B")
        now = datetime.now(UTC)
        await mail_sync_state.save_state(account_id=first.id, status="ok", last_sync_at=now)
        one = await mail_message.upsert_message(account_id=first.id, folder="INBOX", provider_uid="1", subject="Example", internal_date=now)
        two = await mail_message.upsert_message(account_id=first.id, folder="INBOX", provider_uid="2", subject="Example", internal_date=now - timedelta(seconds=1), is_read=True)
        foreign = await mail_message.upsert_message(account_id=second.id, folder="INBOX", provider_uid="3", subject="Example", internal_date=now)
        trashed = await mail_message.upsert_message(account_id=first.id, folder="TRASH", provider_uid="4", subject="Example", internal_date=now)
        await agent_access.enable(1)

        def forbidden(*args, **kwargs):
            pytest.fail("read service attempted a write")

        statements = []

        def check_statement(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
            assert statement.lstrip().upper().startswith("SELECT"), "non-read SQL"

        engine = get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", check_statement)
        with monkeypatch.context() as patch:
            patch.setattr(Session, "flush", forbidden)
            patch.setattr(Session, "commit", forbidden)
            grant = ReadGrant(frozenset({first.id}))
            assert [a.id for a in (await mail.liam_mail_accounts(grant)).accounts] == [first.id]
            page = await mail.liam_mail_search(grant, query="Example", limit=1)
            assert [m.id for m in page.untrusted_messages] == [one.id]
            page = await mail.liam_mail_search(grant, query="Example", limit=1, cursor=page.next_cursor)
            assert [m.id for m in page.untrusted_messages] == [two.id]
            assert (await mail.liam_mail_read(grant, one.id)).untrusted_message.id == one.id
            with pytest.raises(Exception) as error:
                await mail.liam_mail_read(grant, foreign.id)
            assert error.value.status_code == 404
            with pytest.raises(Exception) as error:
                await mail.liam_mail_read(grant, trashed.id)
            assert error.value.status_code == 404
            summary = await mail.liam_mail_summary(grant)
            assert (summary.total, summary.unread) == (2, 1)
            assert summary.index[0].last_sync_at is not None
            assert (await mail.liam_mail_search(ReadGrant(frozenset()), query="Example")).untrusted_messages == []
        event.remove(engine, "before_cursor_execute", check_statement)
        assert statements

    asyncio.run(run())


def test_two_app_instances_observe_disable_over_shared_postgres(pg_db):
    async def run():
        import httpx
        from api.main import create_app
        from api.mcp.access import ReadGrant, get_read_access_verifier
        from db.accessors import agent_access, mail_account
        from mistralai_capabilities.fastapi.hooks import FastAPIHooks

        account = await mail_account.create_account(
            provider="imap", auth_type="imap_password", email_address="shared@example.org", display_name="Shared"
        )

        class GrantingVerifier:
            async def verify(self, request):
                return ReadGrant(frozenset({account.id}))

        app_a = create_app(hooks=FastAPIHooks.empty())
        app_b = create_app(hooks=FastAPIHooks.empty())
        app_a.dependency_overrides[get_read_access_verifier] = GrantingVerifier
        app_b.dependency_overrides[get_read_access_verifier] = GrantingVerifier

        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a), base_url="http://test") as client_a,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b), base_url="http://test") as client_b,
        ):
            off = await client_a.get("/api/agent-read/v1/mail/accounts")
            assert off.status_code == 403 and off.json() == {"detail": "agent_access_disabled"}

            await agent_access.enable(1)
            ok_a = await client_a.get("/api/agent-read/v1/mail/accounts")
            ok_b = await client_b.get("/api/agent-read/v1/mail/accounts")
            assert ok_a.status_code == 200 and [a["id"] for a in ok_a.json()["accounts"]] == [account.id]
            assert ok_b.status_code == 200 and [a["id"] for a in ok_b.json()["accounts"]] == [account.id]

            await agent_access.disable()
            refused = await client_b.get("/api/agent-read/v1/mail/accounts")
            assert refused.status_code == 403 and refused.json() == {"detail": "agent_access_disabled"}
            assert refused.headers.get("cache-control") == "no-store"
            assert "www-authenticate" not in refused.headers
            assert account.id not in refused.text

    asyncio.run(run())
