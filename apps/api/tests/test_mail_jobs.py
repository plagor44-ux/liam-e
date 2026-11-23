import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from db.models.mail_account import MailAccount
from db.models.mail_sync_state import MailSyncState

from api.mail import jobs
from api.lifespan.startup import mail_worker


@pytest.fixture(autouse=True)
def fake_filing_cleanup(monkeypatch):
    from db.accessors import mail_filing_usage
    async def cleanup(now):
        pass
    monkeypatch.setattr(mail_filing_usage, 'cleanup', cleanup)


def account(store, name):
    row = MailAccount(provider="imap", auth_type="imap_password", email_address=f"{name}@example.test")
    store.accounts[row.id] = row
    return row


@pytest.mark.anyio
async def test_worker_periodically_cleans_filing_history(monkeypatch):
    from db.accessors import mail_filing_usage
    t0 = datetime.now(timezone.utc)
    now = t0
    calls = []
    stop = asyncio.Event()
    async def cleanup(at):
        calls.append(at)
    async def run_once():
        return False
    async def wait(stop, timeout):
        nonlocal now
        if now == t0:
            now = t0 + timedelta(minutes=60)
        else:
            stop.set()
    monkeypatch.setattr(mail_filing_usage, 'cleanup', cleanup)
    monkeypatch.setattr(jobs, 'run_once', run_once)
    monkeypatch.setattr(jobs.mail_env, 'mail_sync_interval_minutes', 0)
    await jobs.worker_loop(stop, clock=lambda: now, wait=wait)
    assert calls == [t0, t0 + timedelta(minutes=60)]


@pytest.mark.anyio
async def test_run_once_runs_handler_and_finishes(fake_store):
    seen = []
    async def handler(payload):
        seen.append(payload)
    jobs.register_handler("test", handler)
    await fake_store.enqueue("test", {"account_id": "a"}, dedupe_key="test:a")
    assert await jobs.run_once()
    assert seen == [{"account_id": "a"}]
    assert next(iter(fake_store.jobs.values())).status == "done"


@pytest.mark.anyio
async def test_run_once_retries_then_fails_for_good(fake_store):
    async def handler(payload):
        raise ValueError("provider text")
    jobs.register_handler("failing", handler)
    job = await fake_store.enqueue("failing", {}, dedupe_key="failing")
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    for attempt in range(1, 4):
        assert await jobs.run_once(now)
        assert job.attempts == attempt
        assert job.last_error == "ValueError"
        if attempt < 3:
            assert job.status == "queued"
            assert job.run_after == now + timedelta(seconds=30 * attempt)
            now = job.run_after
    assert job.status == "failed"


@pytest.mark.anyio
async def test_job_for_deleted_account_fails_without_retry(fake_store):
    await jobs.enqueue_sync("missing")
    assert await jobs.run_once(datetime.now(timezone.utc) + timedelta(seconds=1))
    assert next(iter(fake_store.jobs.values())).status == "failed"


@pytest.mark.anyio
async def test_poll_enqueues_due_accounts(fake_store):
    now = datetime.now(timezone.utc)
    a, b, c, d = [account(fake_store, str(i)) for i in range(4)]
    for row, minutes, error in [(a, 2, ""), (b, 7, ""), (d, 30, "auth_expired")]:
        fake_store.states[row.id] = MailSyncState(account_id=row.id, last_sync_at=now-timedelta(minutes=minutes), last_error=error)
    assert await jobs.enqueue_due_syncs(now, older_than_minutes=5, skip_auth_expired=True) == [b.id, c.id]


@pytest.mark.anyio
async def test_poll_keeps_the_configured_interval(fake_store, monkeypatch):
    t0 = datetime.now(timezone.utc)
    a = account(fake_store, "a")
    now = t0
    stop = asyncio.Event()
    monkeypatch.setattr(jobs.mail_env, "mail_sync_interval_minutes", 5)

    async def run_once():
        return False

    async def wait(stop, timeout):
        nonlocal now
        if now == t0:
            assert len(fake_store.jobs) == 1
            job = next(iter(fake_store.jobs.values()))
            assert job.dedupe_key == f"sync:{a.id}"
            claimed = await fake_store.job_claim_next(t0 + timedelta(seconds=1), stale_after=timedelta(minutes=10))
            await fake_store.job_finish(job.id, claimed.attempts)
            await fake_store.save_state(account_id=a.id, last_sync_at=t0 + timedelta(seconds=20))
            b = account(fake_store, "b")
            await fake_store.save_state(account_id=b.id, last_sync_at=t0 + timedelta(minutes=4))
            now = t0 + timedelta(minutes=5)
        else:
            stop.set()

    monkeypatch.setattr(jobs, "run_once", run_once)
    await jobs.worker_loop(stop, clock=lambda: now, wait=wait)
    assert [job.dedupe_key for job in fake_store.jobs.values()] == [f"sync:{a.id}", f"sync:{a.id}"]


@pytest.mark.anyio
async def test_worker_loop_polls_once_per_interval(monkeypatch):
    t0 = datetime.now(timezone.utc)
    monkeypatch.setattr(jobs.mail_env, "mail_sync_interval_minutes", 5)
    calls = []
    now = t0
    stop = asyncio.Event()

    async def enqueue(now, *, older_than_minutes, skip_auth_expired):
        calls.append((now, older_than_minutes, skip_auth_expired))
        if len(calls) == 1:
            raise RuntimeError("temporary failure")

    async def run_once():
        return False

    passes = 0
    async def wait(stop, timeout):
        nonlocal now, passes
        passes += 1
        if passes == 1:
            assert len(calls) == 1  # Poll at startup, despite its failure.
            now = t0 + timedelta(minutes=4)
        elif passes == 2:
            assert len(calls) == 1
            now = t0 + timedelta(minutes=5)
        else:
            stop.set()

    monkeypatch.setattr(jobs, "enqueue_due_syncs", enqueue)
    monkeypatch.setattr(jobs, "run_once", run_once)
    await jobs.worker_loop(stop, clock=lambda: now, wait=wait)
    assert calls == [(t0, 2.5, True), (t0 + timedelta(minutes=5), 2.5, True)]

    monkeypatch.setattr(jobs.mail_env, "mail_sync_interval_minutes", 0)
    calls.clear()
    stop = asyncio.Event()
    async def stop_wait(stop, timeout):
        stop.set()
    await jobs.worker_loop(stop, clock=lambda: t0, wait=stop_wait)
    assert calls == []


@pytest.mark.anyio
async def test_calendar_scheduler_failure_logs_only_a_fixed_warning(monkeypatch, caplog):
    import logging
    from api.calendar import feed_jobs

    stop = asyncio.Event()
    monkeypatch.setattr(jobs.mail_env, "mail_sync_interval_minutes", 0)

    async def fail(now):
        raise RuntimeError("https://calendar.example/test-only?token=test-only")

    async def run_once():
        return False

    async def wait(stop, timeout):
        stop.set()

    monkeypatch.setattr(feed_jobs, "enqueue_due", fail)
    monkeypatch.setattr(jobs, "run_once", run_once)
    with caplog.at_level(logging.WARNING, logger=jobs.__name__):
        await jobs.worker_loop(stop, wait=wait)
    records = [record for record in caplog.records if record.name == jobs.__name__]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage() == "Calendar scheduling failed"
    assert records[0].exc_info is None and records[0].args == ()
    assert "calendar.example" not in caplog.text
    assert "test-only" not in caplog.text and "RuntimeError" not in caplog.text


@pytest.mark.anyio
async def test_worker_not_started_when_disabled(monkeypatch):
    from fastapi import FastAPI
    monkeypatch.setattr(mail_worker.mail_env, "mail_worker_enabled", False)
    app = FastAPI()
    await mail_worker.startup(app)
    assert not hasattr(app.state, "mail_worker")
