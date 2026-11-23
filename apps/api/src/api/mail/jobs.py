"""Durable mail jobs and periodic account polling."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from db.accessors import mail_account, mail_job, mail_sync_state
from env.mail import env as mail_env

from api.locales import resolve_locale
from api.mail import insights, sync

_handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {}
_claim_handlers: dict[str, Callable[[dict[str, Any], str, int], Awaitable[None]]] = {}
_logger = logging.getLogger(__name__)


def register_handler(kind: str, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
    _handlers[kind] = handler


def register_claim_handler(kind: str, handler: Callable[[dict[str, Any], str, int], Awaitable[None]]) -> None:
    _claim_handlers[kind] = handler


async def enqueue_sync(account_id: str) -> bool:
    return await mail_job.enqueue("sync", {"account_id": account_id}, dedupe_key=f"sync:{account_id}") is not None


async def enqueue_insights(account_id: str) -> bool:
    return await mail_job.enqueue("insights", {"account_id": account_id}, dedupe_key=f"insights:{account_id}") is not None


async def _sync(payload: dict[str, Any]) -> None:
    result = await sync.sync_account(payload["account_id"])
    if result.status == "ok" and (result.new_message_ids or result.refresh_insights):
        await enqueue_insights(payload["account_id"])


async def _insights(payload: dict[str, Any]) -> None:
    # Background triage still needs unread deadlines and reply flags in browser mode.
    locale = await resolve_locale()
    verified_ids = payload.get("message_ids")
    limit = len(verified_ids) if verified_ids is not None else mail_env.mail_insight_batch_limit
    await insights.fill_missing(limit=limit, days=mail_env.mail_insight_days, locale=locale,
                               account_id=payload.get("account_id"), verified_ids=verified_ids)


async def _sync_with_lease(payload: dict, job_id: str, attempt: int) -> None:
    async def heartbeat():
        interval = max(0.01, min(60.0, mail_env.mail_sync_stale_minutes * 20.0))
        while True:
            await asyncio.sleep(interval)
            if not await mail_job.renew(job_id, attempt):
                raise RuntimeError("Mail sync claim lost")

    work = asyncio.create_task(_sync(payload))
    lease = asyncio.create_task(heartbeat())
    try:
        done, _ = await asyncio.wait((work, lease), return_when=asyncio.FIRST_COMPLETED)
        if lease in done:
            await lease
        await work
    finally:
        work.cancel()
        lease.cancel()
        await asyncio.gather(work, lease, return_exceptions=True)


async def _rules_with_lease(payload: dict, job_id: str, attempt: int) -> None:
    from api.mail import rules

    async def apply():
        prepared = []
        snapshot = dict(payload["rules"])
        for rule_id, version in payload["rules"]:
            prepared.append(await rules.prepare_rule_run(rule_id, version, payload["message_id"], snapshot=snapshot))
        for run in prepared:
            await rules.dispatch_rule_run(run, recover=attempt > 1)

    async def heartbeat():
        interval = max(0.01, min(60.0, mail_env.mail_sync_stale_minutes * 20.0))
        while True:
            await asyncio.sleep(interval)
            if not await mail_job.renew(job_id, attempt):
                raise RuntimeError("Mail rule claim lost")

    work, lease = asyncio.create_task(apply()), asyncio.create_task(heartbeat())
    try:
        done, _ = await asyncio.wait((work, lease), return_when=asyncio.FIRST_COMPLETED)
        if lease in done:
            await lease
        await work
    finally:
        work.cancel()
        lease.cancel()
        await asyncio.gather(work, lease, return_exceptions=True)


register_claim_handler("sync", _sync_with_lease)
register_claim_handler("mail_rule_apply", _rules_with_lease)
register_handler(mail_job.VERIFIED_INSIGHTS, _insights)
register_handler("insights", _insights)


async def run_once(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    job = await mail_job.claim_next(now, stale_after=timedelta(minutes=mail_env.mail_sync_stale_minutes))
    if job is None:
        return False
    attempt = job.attempts
    try:
        payload = json.loads(job.payload_json)
        if job.kind in _claim_handlers:
            await _claim_handlers[job.kind](payload, job.id, attempt)
        else:
            await _handlers[job.kind](payload)
    except Exception as error:
        retry_at = None if isinstance(error, LookupError) or attempt >= 3 else now + timedelta(seconds=30 * attempt)
        applied = await mail_job.fail(job.id, attempt, type(error).__name__, retry_at=retry_at)
    else:
        applied = await mail_job.finish(job.id, attempt)
    if not applied:
        _logger.warning("Job claim was lost before completion")
    return applied


async def enqueue_due_syncs(now: datetime, *, older_than_minutes: float | None, skip_auth_expired: bool) -> list[str]:
    queued = []
    for account in await mail_account.list_accounts():
        if not account.is_active:
            continue
        state = await mail_sync_state.get_state(account.id)
        if state and skip_auth_expired and state.last_error == "auth_expired":
            continue
        if older_than_minutes is not None and state and state.last_sync_at and state.last_sync_at >= now - timedelta(minutes=older_than_minutes):
            continue
        if await enqueue_sync(account.id):
            queued.append(account.id)
    return queued


def poll_due(now: datetime, last_poll: datetime | None, interval_minutes: int) -> bool:
    return interval_minutes > 0 and (last_poll is None or now - last_poll >= timedelta(minutes=interval_minutes))


async def worker_loop(
    stop: asyncio.Event, *, clock: Callable[[], datetime] | None = None,
    wait: Callable[[asyncio.Event, float], Awaitable[None]] | None = None,
) -> None:
    clock = clock or (lambda: datetime.now(timezone.utc))
    wait = wait or _wait_for_stop
    last_poll = None
    last_calendar_poll = None
    while not stop.is_set():
        now = clock()
        if poll_due(now, last_calendar_poll, 1):
            last_calendar_poll = now
            try:
                from api.calendar.feed_jobs import enqueue_due
                await enqueue_due(now)
            except Exception:
                _logger.warning("Calendar scheduling failed")
        interval = mail_env.mail_sync_interval_minutes
        if poll_due(now, last_poll, interval):
            last_poll = now
            try:
                await enqueue_due_syncs(now, older_than_minutes=interval / 2, skip_auth_expired=True)
            except Exception:
                pass  # Retry on the next tick without exposing provider data.
        try:
            if await run_once():
                continue
        except Exception:
            pass  # Database failures must not terminate the worker.
        await wait(stop, max(0.01, mail_env.mail_worker_poll_seconds))


async def _wait_for_stop(stop: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except TimeoutError:
        pass
