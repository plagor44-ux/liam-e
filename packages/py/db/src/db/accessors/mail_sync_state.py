"""Accessor for ``mail_sync_state`` rows: one sync-progress row per account."""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy import case, select, text, update
from db.accessors import user_preference

from db.engine import get_session_maker
from db.models._common import utcnow
from db.models.mail_account import MailAccount
from db.models.mail_sync_state import MailSyncState


async def get_state(account_id: str) -> MailSyncState | None:
    async with get_session_maker()() as session:
        result = await session.execute(
            select(MailSyncState).where(MailSyncState.account_id == account_id)
        )
        return result.scalar_one_or_none()


async def list_for_accounts(account_ids: list[str]) -> list[MailSyncState]:
    """Read stored sync progress for the authorized accounts only."""
    async with get_session_maker()() as session:
        result = await session.execute(select(MailSyncState).where(MailSyncState.account_id.in_(account_ids)))
        return list(result.scalars().all())


async def save_state(
    *,
    account_id: str,
    cursor: str | None = "",
    status: str = "ok",
    last_error: str = "",
    last_sync_at: datetime | None = None,
    connector_verified: bool | None = None,
    connector_cursor: str | None = None,
    claim_poll: bool = False,
) -> MailSyncState:
    async with get_session_maker()() as session:
        stmt = select(MailSyncState).where(MailSyncState.account_id == account_id)
        if claim_poll:
            if await session.get(MailAccount, account_id, with_for_update=True) is None:
                raise LookupError("Mail account no longer exists")
            stmt = stmt.with_for_update()
        result = await session.execute(stmt)
        state = result.scalar_one_or_none()
        if state is None:
            state = MailSyncState(account_id=account_id)
        if claim_poll:
            # Also distinguish overlapping polls when neither advances the provider cursor.
            state.connector_generation += 1
        if connector_verified is not None:
            state.connector_verified = connector_verified
        if connector_cursor is not None:
            state.connector_cursor = connector_cursor
        if cursor is not None:
            if not cursor and state.cursor:
                state.rules_initial_import = ""
            state.cursor = cursor
        state.status = status
        state.last_error = last_error
        state.last_sync_at = last_sync_at if last_sync_at is not None else utcnow()
        state.updated_at = utcnow()
        session.add(state)
        await session.commit()
        return state


async def delete_for_account(account_id: str) -> None:
    async with get_session_maker()() as session:
        result = await session.execute(
            select(MailSyncState).where(MailSyncState.account_id == account_id)
        )
        for state in result.scalars().all():
            await session.delete(state)
        await session.commit()


class StaleMailSync(Exception):
    """A newer poll, a folder action or an identity change has superseded this read."""


async def fence_folder_change(session, account_id: str) -> MailAccount:
    """Invalidate a fetched but unpublished page in the same commit as the local move."""
    account = await session.get(MailAccount, account_id, with_for_update=True)
    if account is None:
        raise LookupError("Mail account no longer exists")
    await session.execute(update(MailSyncState).where(MailSyncState.account_id == account_id).values(
        connector_generation=MailSyncState.connector_generation + 1,
        status=case((MailSyncState.status == "syncing", "idle"), else_=MailSyncState.status),
    ))
    return account


def same_connector_attempt(state: MailSyncState, previous: MailSyncState) -> bool:
    return (state.connector_generation == previous.connector_generation
            and state.connector_cursor == previous.connector_cursor)


@asynccontextmanager
async def sync_transaction(account_id: str, previous: MailSyncState | None, *, verified: bool, connector: bool = True):
    """Publish a still-current poll atomically. Only a proven connector can mark rows verified."""
    async with get_session_maker().begin() as session:
        if connector:
            await user_preference.lock(session)
        account = await session.get(MailAccount, account_id, with_for_update=True)
        state = (await session.execute(select(MailSyncState).where(
            MailSyncState.account_id == account_id).with_for_update())).scalar_one_or_none()
        if (state is None or previous is None or not same_connector_attempt(state, previous)
                or account is None):
            raise StaleMailSync
        if verified:
            await session.execute(text("SELECT set_config('liam.verified_connector', :account, true)"), {'account': account_id})
        yield session, state


async def fail_poll(account_id: str, previous: MailSyncState, code: str) -> None:
    """Only the still-current sync attempt may publish its error."""
    try:
        async with sync_transaction(account_id, previous, verified=False) as (_, state):
            state.status, state.last_error = "error", code
            state.last_sync_at = state.updated_at = utcnow()
    except StaleMailSync:
        return



async def record_connector_write(
    account_id: str, proof: tuple[str, str], *, outcome: Literal["confirmed", "unchanged", "blocked"],
) -> bool:
    """Update the dispatching binding and retry generation, and report whether it is paused."""
    async with get_session_maker().begin() as session:
        state = await session.scalar(select(MailSyncState).where(MailSyncState.account_id == account_id).with_for_update())
        evidence = state.write_evidence() if state else None
        if evidence is None or evidence[:2] != proof:
            return False
        if evidence[2] == "blocked":
            return True
        assert state is not None
        if outcome == evidence[2] == "unchanged":
            outcome = "blocked"
        state.connector_write_evidence = f"{proof[0]}:{proof[1]}:{outcome}"
        return outcome == "blocked"


async def retry_connector_writes(account_id: str) -> bool:
    """Reopen the current binding without dispatching or repeating a provider write."""
    async with get_session_maker().begin() as session:
        state = await session.scalar(select(MailSyncState).where(MailSyncState.account_id == account_id).with_for_update())
        evidence = state.write_evidence() if state else None
        if evidence is None:
            return False
        assert state is not None
        if evidence[2] == "blocked":
            state.connector_write_evidence = f"{evidence[0]}:{uuid4().hex}:ready"
        return True
