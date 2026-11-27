"""Live Postgres contracts for the MCP account grant table (D4)."""

import asyncio
import os

import httpx
import pytest
from fastapi import FastAPI

from api.routers.api.v1.settings import mcp as mcp_settings_route
from db.accessors import mail_account, mcp_settings
from db.accessors.prompt_template import StaleVersion

pytestmark = pytest.mark.skipif(not os.getenv("RUN_PG_CONTRACT"), reason="RUN_PG_CONTRACT is not set")


def test_mcp_settings_version_conflict_effective_grant_and_account_deletion(pg_db):
    async def run():
        first = await mail_account.create_account(
            provider="imap", auth_type="imap_password", email_address="mcp-one@example.org", display_name="MCP One"
        )
        second = await mail_account.create_account(
            provider="imap", auth_type="imap_password", email_address="mcp-two@example.org", display_name="MCP Two"
        )

        # Missing row defaults to empty list, version 1, without inserting a row.
        initial = await mcp_settings.get()
        assert (initial.version, initial.account_ids) == (1, [])
        assert await mcp_settings.effective_account_ids() == frozenset()

        # Concurrent updates serialize on the installation grant lock; one wins, one gets StaleVersion.
        results = await asyncio.gather(
            mcp_settings.update(1, [first.id]),
            mcp_settings.update(1, [second.id]),
            return_exceptions=True,
        )
        assert sum(isinstance(item, StaleVersion) for item in results) == 1

        both = await mcp_settings.update(2, [second.id, first.id, first.id])
        assert (both.version, both.account_ids) == (3, sorted([first.id, second.id]))
        assert await mcp_settings.effective_account_ids() == frozenset({first.id, second.id})

        # No-op save keeps version unchanged.
        same = await mcp_settings.update(3, [first.id, second.id])
        assert same.version == 3

        # Account deletion removes the ID from mcp_settings in the same transaction and bumps version.
        assert await mail_account.delete_account(first.id) is True
        after_delete = await mcp_settings.get()
        assert (after_delete.version, after_delete.account_ids) == (4, [second.id])
        assert await mcp_settings.effective_account_ids() == frozenset({second.id})

        # Stale version and deleted account ID are both rejected.
        with pytest.raises(StaleVersion):
            await mcp_settings.update(3, [])
        with pytest.raises(mcp_settings.AccountNotAvailable):
            await mcp_settings.update(4, [first.id])

        # HTTP route contract: GET / PATCH with 409 conflict and 422 invalid account.
        app = FastAPI()
        app.include_router(mcp_settings_route.router, prefix="/settings/mcp")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/settings/mcp")).json() == {"account_ids": [second.id], "version": 4}
            conflict = await client.patch("/settings/mcp", json={"expected_version": 3, "account_ids": []})
            assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "version_conflict"
            invalid = await client.patch("/settings/mcp", json={"expected_version": 4, "account_ids": [first.id]})
            assert invalid.status_code == 422 and invalid.json()["detail"]["code"] == "mcp_settings_invalid"
            cleared = await client.patch("/settings/mcp", json={"expected_version": 4, "account_ids": []})
            assert cleared.status_code == 200 and cleared.json() == {"account_ids": [], "version": 5}

    asyncio.run(run())
