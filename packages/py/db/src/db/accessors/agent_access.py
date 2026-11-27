"""The owner's agent access switch: read on every gated request, changed by the owner.

A missing row means off, and reading never creates one, so a fresh or migrated installation
starts with agent access off. There is deliberately no cache: every gated request reads the one
row, so a change committed here applies to the next request on every app instance.

Turning access off is one upsert with no revision check: it always succeeds and always advances
the revision under the row lock, even when access is already off or no row exists yet. That
invalidates every turn-on still holding an older revision, for example one confirmed in another
tab whose identity lookup was still running, so a completed turn-off is never undone by it.
Turning access on is a compare-and-swap on ``revision`` under the same row lock and fails with
``StaleRevision`` when the caller's page is out of date. ``changed_at`` records the last change
of ``enabled``.
"""

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert

from db.engine import get_session_maker
from db.models._common import utcnow
from db.models.agent_access import AgentAccess
from db.models.prompt_template import OWNER_SCOPE


class StaleRevision(Exception):
    """The caller's expected revision no longer matches the stored one."""


async def is_enabled() -> bool:
    """Whether agent access is on; a missing row is off."""
    async with get_session_maker()() as session:
        enabled = (
            await session.execute(select(AgentAccess.enabled).where(AgentAccess.owner_scope == OWNER_SCOPE))
        ).scalar_one_or_none()
    return enabled is True


async def state() -> AgentAccess:
    """The current switch, or the off default (revision 1) when no row exists yet."""
    async with get_session_maker()() as session:
        return await session.get(AgentAccess, OWNER_SCOPE) or AgentAccess()


async def disable() -> AgentAccess:
    """Turn access off and advance the revision. No revision check; it always succeeds.

    A missing row is created as off with revision 2, one past the implicit revision 1 a page
    shows before any row exists, so a turn-on still expecting revision 1 fails.
    """
    now = utcnow()
    async with get_session_maker().begin() as session:
        await session.execute(
            insert(AgentAccess)
            .values(owner_scope=OWNER_SCOPE, enabled=False, revision=2, changed_at=None)
            .on_conflict_do_update(
                index_elements=[AgentAccess.owner_scope],
                set_={
                    "enabled": False,
                    "revision": AgentAccess.revision + 1,
                    "changed_at": case((AgentAccess.enabled.is_(True), now), else_=AgentAccess.changed_at),
                },
            )
        )
        return await session.get(AgentAccess, OWNER_SCOPE, populate_existing=True)


async def enable(expected_revision: int) -> AgentAccess:
    """Turn access on while the caller still holds the current revision.

    A missing row is created as off with revision 1 first, so the first enable expects
    revision 1. Enabling while already on with the current revision changes nothing.
    """
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("expected_revision must be a positive integer")
    async with get_session_maker().begin() as session:
        await session.execute(
            insert(AgentAccess).values(owner_scope=OWNER_SCOPE, enabled=False, revision=1).on_conflict_do_nothing()
        )
        row = await session.get(AgentAccess, OWNER_SCOPE, with_for_update=True, populate_existing=True)
        if row.revision != expected_revision:
            raise StaleRevision(expected_revision)
        if not row.enabled:
            row.enabled = True
            row.revision += 1
            row.changed_at = utcnow()
        return row
