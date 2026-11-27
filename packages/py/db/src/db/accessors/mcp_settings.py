"""Owner-managed mail account grants for the MCP read transport.

A missing row means no accounts are granted (version 1) and reading never creates a row.
Updates serialize on the installation grant lock shared with account deletion so deleting an
account removes its ID from the MCP grant in the same transaction.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.accessors import user_preference
from db.accessors.prompt_template import StaleVersion
from db.engine import get_session_maker
from db.models.mail_account import MailAccount
from db.models.mcp_settings import McpSettings
from db.models.prompt_template import OWNER_SCOPE

MAX_GRANT_ACCOUNTS = 100


class AccountNotAvailable(ValueError):
    """At least one requested account is missing, inactive, or exceeds the grant limit."""


async def in_session(session: AsyncSession) -> McpSettings:
    return await session.get(McpSettings, OWNER_SCOPE) or McpSettings()


async def get() -> McpSettings:
    async with get_session_maker()() as session:
        return await in_session(session)


async def active_ids(session: AsyncSession, account_ids: list[str]) -> list[str]:
    if not account_ids:
        return []
    rows = await session.execute(
        select(MailAccount.id).where(
            MailAccount.id.in_(account_ids),
            MailAccount.is_active.is_(True),
        )
    )
    return sorted(rows.scalars().all())


async def effective_account_ids() -> frozenset[str]:
    """Return the stored MCP grant intersected with currently active mail accounts."""
    async with get_session_maker()() as session:
        row = await in_session(session)
        if not row.account_ids:
            return frozenset()
        return frozenset(await active_ids(session, row.account_ids[:MAX_GRANT_ACCOUNTS]))


async def update(expected_version: int, account_ids: list[str]) -> McpSettings:
    ids = sorted(set(account_ids))
    if len(ids) > MAX_GRANT_ACCOUNTS:
        raise AccountNotAvailable
    async with get_session_maker()() as session:
        await user_preference.lock(session)
        row = await in_session(session)
        if row.version != expected_version:
            raise StaleVersion
        if ids != await active_ids(session, ids):
            raise AccountNotAvailable
        if ids != row.account_ids:
            row.account_ids = ids
            row.version += 1
            session.add(row)
        await session.commit()
        return row


async def remove_account(session: AsyncSession, account_id: str) -> None:
    """Remove a deleted account from the MCP grant inside the caller's grant-locked transaction."""
    row = await in_session(session)
    if account_id in row.account_ids:
        row.account_ids = [item for item in row.account_ids if item != account_id]
        row.version += 1
        session.add(row)
