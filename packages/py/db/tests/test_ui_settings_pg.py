"""Live unique and revision contracts of the owner-scoped UI settings."""

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(not os.getenv('RUN_PG_CONTRACT'), reason='RUN_PG_CONTRACT is not set')


def test_parallel_first_reads_create_exactly_one_row(pg_db):
    async def run():
        from sqlalchemy import func, select
        from db.accessors import ui_settings as settings
        from db.engine import get_session_maker
        from db.models.ui_settings import OWNER_SCOPE, UiSettings

        rows = await asyncio.gather(*[settings.get_or_create() for _ in range(8)])
        assert len({row.id for row in rows}) == 1
        assert rows[0].owner_scope == OWNER_SCOPE
        assert rows[0].swipe_left == settings.DEFAULT_SWIPE_LEFT
        assert rows[0].swipe_right == settings.DEFAULT_SWIPE_RIGHT
        assert rows[0].revision == 1
        async with get_session_maker()() as session:
            count = (await session.execute(select(func.count()).select_from(UiSettings))).scalar_one()
        assert count == 1

    asyncio.run(run())


def test_parallel_updates_of_the_same_revision_lose_none(pg_db):
    async def run():
        from db.accessors import ui_settings as settings

        await settings.get_or_create()
        results = await asyncio.gather(
            settings.update(1, swipe_left='trash'),
            settings.update(1, swipe_left='none', swipe_right='none'),
            return_exceptions=True,
        )
        assert sum(isinstance(result, settings.StaleRevision) for result in results) == 1
        current = await settings.get_or_create()
        winner = next(result for result in results if not isinstance(result, settings.StaleRevision))
        assert current.revision == 2
        assert current.swipe_left == winner.swipe_left
        assert current.swipe_right == winner.swipe_right

    asyncio.run(run())


def test_parallel_updates_before_any_read_initialize_once(pg_db):
    async def run():
        from db.accessors import ui_settings as settings

        results = await asyncio.gather(
            settings.update(1, swipe_left='toggle_read'),
            settings.update(1, swipe_right='trash'),
            return_exceptions=True,
        )
        assert sum(isinstance(result, settings.StaleRevision) for result in results) == 1
        current = await settings.get_or_create()
        assert current.revision == 2
        assert len({result.id for result in results if not isinstance(result, Exception)}) == 1

    asyncio.run(run())


def test_sequential_updates_validate_and_refuse_stale_revisions(pg_db):
    async def run():
        from db.accessors import ui_settings as settings

        row = await settings.update(1, swipe_left='trash')  # No read yet: this initializes.
        assert row.revision == 2
        assert row.swipe_left == 'trash'
        assert row.swipe_right == settings.DEFAULT_SWIPE_RIGHT
        with pytest.raises(ValueError):
            await settings.update(2, swipe_left='spam')
        with pytest.raises(ValueError):
            await settings.update(2, swipe_left='delete')
        with pytest.raises(ValueError):
            await settings.update(2)
        assert (await settings.get_or_create()).revision == 2
        row = await settings.update(2, swipe_right='archive')
        assert row.revision == 3 and row.swipe_right == 'archive'
        with pytest.raises(settings.StaleRevision):
            await settings.update(2, swipe_left='none')
        assert (await settings.get_or_create()).revision == 3
        row = await settings.update(3, swipe_left='none', swipe_right='toggle_read')
        assert row.revision == 4 and row.swipe_left == 'none' and row.swipe_right == 'toggle_read'

    asyncio.run(run())


def test_language_updates_share_revision_without_overwriting_swipes(pg_db):
    async def run():
        from db.accessors import ui_settings as settings

        initial = await settings.get_or_create()
        assert initial.locale == 'browser'
        revised = await settings.update(initial.revision, locale='en')
        assert (revised.swipe_left, revised.swipe_right) == (initial.swipe_left, initial.swipe_right)
        with pytest.raises(settings.StaleRevision):
            await settings.update(initial.revision, locale='de')
        with pytest.raises(ValueError):
            await settings.update(revised.revision, locale='fr')
        updated = await settings.update(revised.revision, swipe_right='trash')
        assert updated.locale == 'en'
        restored = await settings.update(updated.revision, locale='browser')
        assert restored.swipe_right == 'trash'
        assert (await settings.get_or_create()).locale == 'browser'

    asyncio.run(run())


def test_speech_pace_revision_and_browser_hint_are_independent(pg_db):
    async def run():
        from db.accessors import ui_settings as settings
        row = await settings.get_or_create()
        assert row.speech_pace == 'normal' and row.browser_locale is None
        await settings.remember_browser_locale('de')
        after = await settings.get_or_create()
        assert after.browser_locale == 'de' and after.revision == row.revision
        with pytest.raises(ValueError): await settings.remember_browser_locale('fr')
        with pytest.raises(ValueError): await settings.update(row.revision, speech_pace='faster')
        updated = await settings.update(row.revision, speech_pace='fast')
        assert updated.speech_pace == 'fast' and updated.browser_locale == 'de'
        reread = await settings.get_or_create()
        assert reread.speech_pace == 'fast' and reread.revision == updated.revision
        combined = await settings.update(reread.revision, locale='en', speech_pace='slow')
        assert combined.locale == 'en' and combined.speech_pace == 'slow'
        assert (await settings.get_or_create()).speech_pace == 'slow'
        with pytest.raises(settings.StaleRevision): await settings.update(row.revision, speech_pace='slow')
    asyncio.run(run())


def test_voice_model_default_and_revision(pg_db):
    async def run():
        from db.accessors import ui_settings as settings
        row = await settings.get_or_create()
        assert row.voice_model == 'chat'
        with pytest.raises(ValueError): await settings.update(row.revision, voice_model=' ')
        updated = await settings.update(row.revision, voice_model='mistral-small-latest')
        assert updated.voice_model == 'mistral-small-latest'
        with pytest.raises(settings.StaleRevision): await settings.update(row.revision, voice_model='chat')
        assert (await settings.get_or_create()).voice_model == 'mistral-small-latest'
    asyncio.run(run())


def test_voice_model_migration_preserves_existing_settings(pg_db):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from db import engine
    from db.accessors import ui_settings as settings

    project = Path(__file__).resolve().parents[1]
    config = Config(str(project / 'alembic.ini'))
    config.set_main_option('script_location', str(project / 'src/db/migrations'))
    asyncio.run(engine.dispose_engine())
    command.downgrade(config, 'b8657d439a54')
    async def seed():
        async with engine.get_session_maker()() as session:
            await session.execute(text("INSERT INTO ui_settings (id, owner_scope, swipe_left, swipe_right, locale, speech_pace, revision, updated_at) VALUES ('synthetic', 'installation', 'trash', 'none', 'de', 'fast', 7, now())"))
            await session.commit()
    asyncio.run(seed())
    asyncio.run(engine.dispose_engine())
    command.upgrade(config, 'heads')
    async def verify():
        row = await settings.get_or_create()
        assert (row.voice_model, row.locale, row.speech_pace, row.swipe_left, row.revision) == ('chat', 'de', 'fast', 'trash', 7)
    asyncio.run(verify())
