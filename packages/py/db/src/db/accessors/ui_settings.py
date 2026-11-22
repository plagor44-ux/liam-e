"""Serialized owner-scoped UI settings: one row, created once, updated by revision.

The owner is the fixed server-side scope ``OWNER_SCOPE`` (like the prompt library), and the row
is unique on it, so every caller shares the same configuration. A first read initializes it
atomically — ``INSERT … ON CONFLICT DO NOTHING`` plus a re-read — so parallel first requests end
up with exactly one valid row instead of duplicates or failures. Updates are a compare-and-swap
on ``revision`` under the row lock: a writer whose expected revision no longer matches fails
with ``StaleRevision`` instead of silently overwriting a newer assignment.
"""

from sqlalchemy import select, update as sql_update
from sqlalchemy.dialects.postgresql import insert

from db.engine import get_session_maker
from db.models._common import new_id, utcnow
from db.models.ui_settings import BROWSER_LOCALE, OWNER_SCOPE, UiSettings

#: The actions a swipe direction may be assigned to; anything else is invalid.
SWIPE_ACTIONS = ("none", "archive", "toggle_read", "trash")
UI_LOCALES = (BROWSER_LOCALE, "de", "en")
SPEECH_PACES = ("slow", "normal", "fast")
#: The canonical first assignment of a freshly initialized configuration.
DEFAULT_SWIPE_LEFT = "archive"
DEFAULT_SWIPE_RIGHT = "toggle_read"


_UNSET = object()


class StaleRevision(Exception):
    """The caller's expected revision no longer matches the stored one."""


def _validated(action: str) -> str:
    if action not in SWIPE_ACTIONS:
        raise ValueError(f"unknown swipe action: {action!r}")
    return action


async def _initial(session) -> UiSettings:
    """Insert the canonical first row if it is missing, then read the one that exists."""
    await session.execute(
        insert(UiSettings)
        .values(
            id=new_id(),
            owner_scope=OWNER_SCOPE,
            swipe_left=DEFAULT_SWIPE_LEFT,
            swipe_right=DEFAULT_SWIPE_RIGHT,
            revision=1,
            updated_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["owner_scope"])
    )
    return (
        await session.execute(
            select(UiSettings).where(UiSettings.owner_scope == OWNER_SCOPE)
        )
    ).scalar_one()


async def get_or_create() -> UiSettings:
    """The one configuration, initializing it exactly once on the first read."""
    async with get_session_maker()() as session:
        row = await _initial(session)
        await session.commit()
        return row


async def _locked(session) -> UiSettings:
    """The configuration row under the write lock, initializing it if it is missing."""
    row = (
        await session.execute(
            select(UiSettings)
            .where(UiSettings.owner_scope == OWNER_SCOPE)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        await _initial(session)
        row = (
            await session.execute(
                select(UiSettings)
                .where(UiSettings.owner_scope == OWNER_SCOPE)
                .with_for_update()
            )
        ).scalar_one()
    return row


async def update(
    expected_revision: int, *, swipe_left: str | None = None, swipe_right: str | None = None, locale: str | None = None, speech_pace: str | None = None, voice_model: str | None = None, voice_de: str | None | object = _UNSET, voice_en: str | None | object = _UNSET
) -> UiSettings:
    """Apply changes atomically while the caller still holds the current revision.

    A missing row is initialized with the canonical defaults first, so an update that arrives
    before any read still meets a defined state (and ``expected_revision`` 1). With several
    concurrent writers on the same revision, the row lock serializes them and all but the first
    fail with ``StaleRevision`` — no update is lost silently.
    """
    changes: dict[str, str | None] = {}
    if swipe_left is not None:
        changes["swipe_left"] = _validated(swipe_left)
    if swipe_right is not None:
        changes["swipe_right"] = _validated(swipe_right)
    if locale is not None:
        if locale not in UI_LOCALES:
            raise ValueError("unknown UI locale")
        changes["locale"] = locale
    if speech_pace is not None:
        if speech_pace not in SPEECH_PACES:
            raise ValueError("unknown speech pace")
        changes["speech_pace"] = speech_pace
    if voice_model is not None:
        if not voice_model.strip() or len(voice_model) > 200:
            raise ValueError("invalid voice model")
        changes["voice_model"] = voice_model
    for key, value in (("voice_de", voice_de), ("voice_en", voice_en)):
        if value is not _UNSET:
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 100):
                raise ValueError("invalid voice")
            changes[key] = value
    if not changes:
        raise ValueError("an update needs at least one UI setting")
    async with get_session_maker()() as session:
        row = await _locked(session)
        if row.revision != expected_revision:
            raise StaleRevision(expected_revision)
        for field, value in changes.items():
            setattr(row, field, value)
        row.revision += 1
        row.updated_at = utcnow()
        session.add(row)
        await session.commit()
        return row


async def remember_browser_locale(locale: str) -> None:
    """Store the last request locale as background metadata, without changing user settings."""
    if locale not in ("de", "en"):
        raise ValueError("unknown browser locale")
    async with get_session_maker()() as session:
        await _initial(session)
        await session.execute(sql_update(UiSettings).where(UiSettings.owner_scope == OWNER_SCOPE, UiSettings.browser_locale.is_distinct_from(locale)).values(browser_locale=locale))
        await session.commit()
