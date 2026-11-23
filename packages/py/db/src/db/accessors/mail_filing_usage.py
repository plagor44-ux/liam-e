"""Installation-wide quick-action usage, bounded and independent of mail contents."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert

from db.engine import get_session_maker
from db.models._common import utcnow
from db.models.mail_filing_usage import MailFilingUsage
from db.models.mail_folder import MailFolder

RETENTION = timedelta(days=90)
CAPACITY = 200


@dataclass(frozen=True)
class QuickAction:
    destination: MailFolder
    use_count: int
    last_used_at: datetime
    score: float


async def _lock(session) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                          {"key": "liam-e:mail-filing-usage:installation"})


async def _prune(session, now: datetime) -> None:
    await session.execute(delete(MailFilingUsage).where(MailFilingUsage.last_used_at <= now - RETENTION))
    excess = select(MailFilingUsage.destination_id).order_by(
        MailFilingUsage.last_used_at.desc(), MailFilingUsage.destination_id.asc()).offset(CAPACITY)
    await session.execute(delete(MailFilingUsage).where(MailFilingUsage.destination_id.in_(excess)))


async def record(session, destination_ids: list[str], *, now: datetime | None = None) -> None:
    """Update usage in the caller's filing transaction, once per destination."""
    ids = sorted(set(destination_ids))
    if not ids:
        return
    now = now or utcnow()
    await _lock(session)
    await _prune(session, now)
    for destination_id in ids:
        stmt = insert(MailFilingUsage).values(destination_id=destination_id, use_count=1, last_used_at=now)
        await session.execute(stmt.on_conflict_do_update(index_elements=[MailFilingUsage.destination_id],
            set_={"use_count": MailFilingUsage.use_count + 1, "last_used_at": now}))
    await _prune(session, now)


async def cleanup(now: datetime | None = None) -> None:
    async with get_session_maker().begin() as session:
        await prune_in_session(session, now=now)


async def prune_in_session(session, *, now: datetime | None = None) -> None:
    await _lock(session)
    await _prune(session, now or utcnow())


async def list_quick_actions(now: datetime | None = None) -> list[QuickAction]:
    now = now or utcnow()
    async with get_session_maker().begin() as session:
        await _lock(session)
        await _prune(session, now)
        rows = (await session.execute(select(MailFolder, MailFilingUsage).join(
            MailFilingUsage, MailFilingUsage.destination_id == MailFolder.id))).all()
        return [QuickAction(destination=folder, use_count=usage.use_count, last_used_at=usage.last_used_at,
                            score=usage.use_count / (1 + max(0, (now - usage.last_used_at).total_seconds()) / (30 * 86400)))
                for folder, usage in rows]
