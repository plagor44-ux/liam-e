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


def test_disable_needs_no_revision_stays_off_and_always_advances(pg_db):
    async def run():
        from db.accessors import agent_access

        first = await agent_access.disable()
        second = await agent_access.disable()
        assert (first.enabled, first.revision, first.changed_at) == (False, 2, None)
        assert (second.enabled, second.revision, second.changed_at) == (False, 3, None)
        assert await _rows() == 1
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
        repeated = await agent_access.disable()
        assert (repeated.revision, repeated.changed_at) == (4, off.changed_at)
        with pytest.raises(agent_access.StaleRevision):
            await agent_access.enable(3)
        assert await agent_access.is_enabled() is False
        assert await _rows() == 1

    asyncio.run(run())


def test_a_turn_on_confirmed_before_a_turn_off_cannot_reopen_access(pg_db):
    """A page holding revision n, then a turn-off elsewhere: the late turn-on must fail."""
    async def run():
        from db.accessors import agent_access

        # Never switched: the page shows the implicit revision 1.
        page = await agent_access.state()
        await agent_access.disable()
        with pytest.raises(agent_access.StaleRevision):
            await agent_access.enable(page.revision)
        assert await agent_access.is_enabled() is False

        # Already off with a stored row: a second turn-off still invalidates the page.
        page = await agent_access.state()
        await agent_access.disable()
        with pytest.raises(agent_access.StaleRevision):
            await agent_access.enable(page.revision)
        assert await agent_access.is_enabled() is False

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


def test_a_racing_disable_always_leaves_access_off(pg_db):
    async def run():
        from db.accessors import agent_access

        for _ in range(6):
            page = await agent_access.state()
            off, on = await asyncio.gather(agent_access.disable(), agent_access.enable(page.revision), return_exceptions=True)
            assert not isinstance(off, Exception) and off.enabled is False
            assert isinstance(on, agent_access.StaleRevision) or not isinstance(on, Exception)
            # Whichever reached the row first, the turn-off ran last or invalidated the turn-on.
            assert await agent_access.is_enabled() is False
        assert await _rows() == 1

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
