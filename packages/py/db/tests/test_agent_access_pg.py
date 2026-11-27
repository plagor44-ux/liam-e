"""Live contracts of the owner's agent access switch: off by default, CAS to enable, free disable."""

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(not os.getenv('RUN_PG_CONTRACT'), reason='RUN_PG_CONTRACT is not set')


async def _rows():
    from sqlalchemy import func, select
    from db.engine import get_session_maker
    from db.models.agent_access import AgentAccess

    async with get_session_maker()() as session:
        return (await session.execute(select(func.count()).select_from(AgentAccess))).scalar_one()


def test_a_missing_row_is_off_and_reading_creates_none(pg_db):
    async def run():
        from db.accessors import agent_access

        assert await agent_access.is_enabled() is False
        current = await agent_access.state()
        assert (current.enabled, current.revision, current.changed_at) == (False, 1, None)
        assert await _rows() == 0

    asyncio.run(run())


def test_disable_needs_no_row_no_revision_and_is_idempotent(pg_db):
    async def run():
        from db.accessors import agent_access

        first = await agent_access.disable()
        second = await agent_access.disable()
        assert (first.enabled, first.revision) == (False, 1)
        assert (second.enabled, second.revision) == (False, 1)
        assert await _rows() == 0
        assert await agent_access.is_enabled() is False

    asyncio.run(run())


def test_enable_is_a_compare_and_swap_and_disable_always_wins(pg_db):
    async def run():
        from db.accessors import agent_access

        on = await agent_access.enable(1)
        assert (on.enabled, on.revision) == (True, 2) and on.changed_at is not None
        assert await agent_access.is_enabled() is True
        with pytest.raises(agent_access.StaleRevision):
            await agent_access.enable(1)
        again = await agent_access.enable(2)
        assert (again.enabled, again.revision, again.changed_at) == (True, 2, on.changed_at)

        off = await agent_access.disable()
        assert (off.enabled, off.revision) == (False, 3) and off.changed_at >= on.changed_at
        assert await agent_access.is_enabled() is False
        assert (await agent_access.disable()).revision == 3
        with pytest.raises(agent_access.StaleRevision):
            await agent_access.enable(2)
        assert await agent_access.is_enabled() is False
        assert await _rows() == 1

    asyncio.run(run())


def test_parallel_enables_of_one_revision_turn_on_once(pg_db):
    async def run():
        from db.accessors import agent_access

        results = await asyncio.gather(*[agent_access.enable(1) for _ in range(4)], return_exceptions=True)
        assert sum(isinstance(result, agent_access.StaleRevision) for result in results) == 3
        current = await agent_access.state()
        assert (current.enabled, current.revision) == (True, 2)
        assert await _rows() == 1

    asyncio.run(run())


def test_disable_succeeds_against_a_racing_enable(pg_db):
    async def run():
        from db.accessors import agent_access

        await agent_access.enable(1)
        results = await asyncio.gather(agent_access.disable(), agent_access.enable(2), return_exceptions=True)
        assert not isinstance(results[0], Exception)
        # Whichever ran last decides; a disable is never refused.
        await agent_access.disable()
        assert await agent_access.is_enabled() is False

    asyncio.run(run())


def test_enable_refuses_a_malformed_revision(pg_db):
    async def run():
        from db.accessors import agent_access

        for bad in (0, -1, True, '1', 1.0, None):
            with pytest.raises(ValueError):
                await agent_access.enable(bad)
        assert await _rows() == 0

    asyncio.run(run())


def test_the_database_default_is_off(pg_db):
    async def run():
        from sqlalchemy import text
        from db.accessors import agent_access
        from db.engine import get_session_maker

        async with get_session_maker().begin() as session:
            await session.execute(text("INSERT INTO agent_access (owner_scope, revision) VALUES ('installation', 1)"))
        assert await agent_access.is_enabled() is False

    asyncio.run(run())
