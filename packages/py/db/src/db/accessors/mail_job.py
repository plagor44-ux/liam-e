"""Concurrent durable job queue."""

import json
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import and_, case, cast, delete, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert

from db.accessors import mail_sync_state
from db.engine import get_session_maker
from db.models._common import new_id, utcnow
from db.models.mail_job import MailJob


# Base-version workers claim only "queued" or stale "running" jobs. Keep both phases of
# a targeted refresh outside their protocol until a worker that knows its payload finishes.
VERIFIED_INSIGHTS = "verified_insights"
PENDING_LABEL_CHECK = "pending_label_check"
DEFERRED_SYNC_LABEL_CHECK = "deferred_sync"
READY_REFRESH = "ready_insights"
RUNNING_REFRESH = "processing_insights"
RUNNING = ("running", RUNNING_REFRESH)


async def enqueue(kind: str, payload: dict, *, dedupe_key: str, run_after: datetime | None = None) -> MailJob | None:
    row = MailJob(kind=kind, payload_json=json.dumps(payload), dedupe_key=dedupe_key, run_after=run_after or utcnow())
    stmt = insert(MailJob).values(**row.model_dump()).on_conflict_do_nothing().returning(MailJob)
    async with get_session_maker()() as session:
        result = (await session.execute(stmt)).scalar_one_or_none()
        await session.commit()
        return result


def queue_verified_insights(session, account_id: str, message_ids: list[str], limit: int) -> None:
    """Commit bounded refresh jobs with the verified mail batch that invalidated its insights."""
    if limit <= 0:
        return
    for offset in range(0, len(message_ids), limit):
        payload = {"account_id": account_id, "message_ids": message_ids[offset:offset + limit]}
        # Each invalidation is durable even while an earlier job for the same mail is running.
        session.add(MailJob(kind=VERIFIED_INSIGHTS, status=READY_REFRESH, payload_json=json.dumps(payload), dedupe_key=f"insights-refresh:{new_id()}"))


async def queue_mail_rules(session, account_id: str, message_ids: list[str]) -> None:
    """Commit rule snapshots with the newly indexed messages that triggered them."""
    from db.models.mail_rule import MailRule

    if not message_ids:
        return
    rules = (await session.execute(select(MailRule.id, MailRule.version).where(
        MailRule.is_active.is_(True), or_(MailRule.account_id.is_(None), MailRule.account_id == account_id)
    ).order_by(MailRule.position, MailRule.created_at, MailRule.id))).all()
    if not rules:
        return
    for message_id in dict.fromkeys(message_ids):
        row = MailJob(kind="mail_rule_apply", payload_json=json.dumps({"account_id": account_id,
            "message_id": message_id, "rules": [[rule_id, version] for rule_id, version in rules]}),
            dedupe_key=f"mail-rule:{message_id}")
        await session.execute(insert(MailJob).values(**row.model_dump()).on_conflict_do_nothing())


async def claim_next(now: datetime, *, stale_after: timedelta) -> MailJob | None:
    stmt = select(MailJob).where(or_(and_(MailJob.status.in_(("queued", READY_REFRESH)), MailJob.run_after <= now), and_(MailJob.status.in_(RUNNING), MailJob.locked_at < now - stale_after))).order_by(MailJob.run_after, MailJob.id).with_for_update(skip_locked=True).limit(1)
    async with get_session_maker()() as session:
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            row.status = RUNNING_REFRESH if row.kind == VERIFIED_INSIGHTS else "running"
            row.locked_at = now
            row.attempts += 1
            row.updated_at = now
            await session.commit()
        return row


async def renew(job_id: str, attempt: int) -> bool:
    now = utcnow()
    stmt = (update(MailJob).where(MailJob.id == job_id, MailJob.status.in_(RUNNING), MailJob.attempts == attempt)
            .values(locked_at=now, updated_at=now).returning(MailJob.id))
    async with get_session_maker().begin() as session:
        return (await session.execute(stmt)).scalar_one_or_none() is not None


async def finish(job_id: str, attempt: int) -> bool:
    stmt = (update(MailJob).where(MailJob.id == job_id, MailJob.status.in_(RUNNING), MailJob.attempts == attempt)
            .values(status="done", locked_at=None, updated_at=utcnow()).returning(MailJob.id))
    async with get_session_maker()() as session:
        applied = (await session.execute(stmt)).scalar_one_or_none() is not None
        await session.commit()
        return applied


async def fail(job_id: str, attempt: int, error_code: str, *, retry_at: datetime | None) -> bool:
    values = dict(status="queued" if retry_at is not None else "failed", locked_at=None,
                  last_error=error_code, updated_at=utcnow())
    if retry_at is not None:
        values["status"] = case((MailJob.kind == VERIFIED_INSIGHTS, READY_REFRESH), else_="queued")
        values["run_after"] = retry_at
    stmt = (update(MailJob).where(MailJob.id == job_id, MailJob.status.in_(RUNNING), MailJob.attempts == attempt)
            .values(**values).returning(MailJob.id))
    async with get_session_maker()() as session:
        applied = (await session.execute(stmt)).scalar_one_or_none() is not None
        await session.commit()
        return applied


def _account(account_id: str):
    return cast(MailJob.payload_json, JSONB)["account_id"].astext == account_id


async def latest_for_account(account_id: str, *, kind: str | None = None, open_only: bool = False) -> MailJob | None:
    stmt = select(MailJob).where(_account(account_id))
    if kind is not None:
        stmt = stmt.where(MailJob.kind == kind)
    if open_only:
        stmt = stmt.where(MailJob.status.in_(("queued", READY_REFRESH, *RUNNING)))
    async with get_session_maker()() as session:
        return (await session.execute(stmt.order_by(MailJob.created_at.desc(), MailJob.id.desc()).limit(1))).scalar_one_or_none()


async def queue_connector_label_check(
    account_id: str, binding: str, message_id: str, thread_id: str, provider_uid: str,
) -> None:
    """Persist one read-only check per delivery and binding outside the worker claim states."""
    async with get_session_maker().begin() as session:
        # A poll may have read the mailbox before the uncertain write.
        await mail_sync_state.fence_folder_change(session, account_id)
        await queue_connector_label_checks(session, account_id, binding, [(message_id, thread_id, provider_uid)])


async def queue_connector_label_checks(
    session, account_id: str, binding: str, deliveries: list[tuple[str, str, str]], *,
    allow_full_fallback: bool = False,
) -> None:
    """Enqueue exact reads within the caller's fenced sync or action transaction."""
    jobs = []
    for message_id, thread_id, provider_uid in deliveries:
        target = {"account_id": account_id, "binding": binding, "message_id": message_id,
                  "thread_id": thread_id, "provider_uid": provider_uid}
        key = str(uuid5(NAMESPACE_URL, "connector-label-check:" + json.dumps(target)))
        if allow_full_fallback:
            target["source"] = DEFERRED_SYNC_LABEL_CHECK
        job = MailJob(id=key, kind=PENDING_LABEL_CHECK, status=PENDING_LABEL_CHECK,
                      payload_json=json.dumps(target), dedupe_key=key)
        jobs.append(job.model_dump())
    if not jobs:
        return
    stmt = insert(MailJob).values(jobs)
    if allow_full_fallback:
        stmt = stmt.on_conflict_do_nothing()
    else:
        # An uncertain write supersedes a deferred sync read for the same delivery.
        stmt = stmt.on_conflict_do_update(index_elements=[MailJob.id],
            set_={"payload_json": stmt.excluded.payload_json}, where=MailJob.payload_json != stmt.excluded.payload_json)
    await session.execute(stmt)


async def connector_label_checks(account_id: str) -> list[MailJob]:
    async with get_session_maker()() as session:
        return list(await session.scalars(select(MailJob).where(
            MailJob.status == PENDING_LABEL_CHECK, _account(account_id),
        ).order_by(MailJob.updated_at, MailJob.id).limit(5)))


async def finish_connector_label_checks(session, completed: list[str], pending: list[str]) -> None:
    if completed:
        await session.execute(delete(MailJob).where(MailJob.id.in_(completed)))
    if pending:
        await session.execute(update(MailJob).where(MailJob.id.in_(pending)).values(updated_at=utcnow()))


async def delete_for_account(account_id: str) -> None:
    async with get_session_maker()() as session:
        await session.execute(delete(MailJob).where(_account(account_id)))
        await session.commit()
