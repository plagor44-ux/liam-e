"""Local-only Gmail Studio action and sync scheduling regressions."""

import asyncio
import logging

import httpx
import pytest

from api.mail import connector_throttle


def status_error(status, retry_after=None):
    request = httpx.Request("POST", "https://example.test")
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.HTTPStatusError("private fixture detail", request=request,
                                response=httpx.Response(status, request=request, headers=headers))


@pytest.mark.anyio
@pytest.mark.parametrize("status,write", [(429, False), (503, False), (429, True)])
async def test_retry_within_budget_obeys_retry_after(monkeypatch, status, write):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(connector_throttle, "sleep", sleep)
    attempts = 0

    async def call():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise status_error(status, "1")
        return "confirmed"

    async with connector_throttle.action("fixture"):
        assert await connector_throttle.tool(call, write=write) == "confirmed"
    assert attempts == 2 and delays == [1]


@pytest.mark.anyio
async def test_long_retry_after_and_ambiguous_503_write_are_not_replayed(monkeypatch):
    async def no_sleep(delay):
        pytest.fail("must not sleep")

    monkeypatch.setattr(connector_throttle, "sleep", no_sleep)

    async def rejected():
        raise status_error(429, "60")

    async def uncertain():
        raise status_error(503)

    async with connector_throttle.action("long-retry-after"):
        with pytest.raises(connector_throttle.RateLimited):
            await connector_throttle.tool(rejected, write=True)

    async def must_not_call():
        pytest.fail("must honor the cooldown across actions")

    async with connector_throttle.action("long-retry-after"):
        with pytest.raises(connector_throttle.RateLimited):
            await connector_throttle.tool(must_not_call, write=False)
    async with connector_throttle.action("uncertain"):
        with pytest.raises(httpx.HTTPStatusError):
            await connector_throttle.tool(uncertain, write=True)


@pytest.mark.anyio
async def test_sync_yields_between_requests_while_other_accounts_run():
    first_started = asyncio.Event()
    first_finishes = asyncio.Event()
    action_started = asyncio.Event()
    action_finishes = asyncio.Event()
    order = []

    async def first():
        order.append("sync first")
        first_started.set()
        await first_finishes.wait()

    async def record(value):
        order.append(value)

    async def sync():
        with connector_throttle.sync("fixture"):
            await connector_throttle.tool(first, write=False)
            await connector_throttle.tool(lambda: record("sync next"), write=False)

    async def write():
        async with connector_throttle.action("fixture"):
            order.append("action")
            action_started.set()
            await action_finishes.wait()
            await connector_throttle.tool(lambda: record("action confirmed"), write=False)

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(sync())
        await first_started.wait()
        tasks.create_task(write())
        await asyncio.sleep(0)
        first_finishes.set()
        await action_started.wait()
        async with connector_throttle.action("other"):
            await record("independent action")
        action_finishes.set()

    assert order == ["sync first", "action", "independent action", "action confirmed", "sync next"]



@pytest.mark.anyio
async def test_action_transport_logging_suppresses_credential_urls(caplog):
    logger = logging.getLogger("httpx")
    with caplog.at_level(logging.INFO, logger="httpx"):
        async with connector_throttle.action("fixture"):
            logger.info("sensitive fixture url and pin")
        logger.info("unrelated transport")
    assert "sensitive fixture" not in caplog.text
    assert "unrelated transport" in caplog.text


@pytest.mark.anyio
async def test_write_retry_runs_after_rechecking_the_binding(monkeypatch):
    order = []

    async def sleep(delay):
        order.append("wait")

    async def verify():
        order.append("verify pin")

    async def write():
        order.append("write")
        if order == ["write"]:
            raise status_error(429)

    monkeypatch.setattr(connector_throttle, "sleep", sleep)
    async with connector_throttle.action("repeat"):
        await connector_throttle.tool(write, write=True, before_retry=verify)
    assert order == ["write", "wait", "verify pin", "write"]


@pytest.mark.anyio
async def test_sync_retry_after_blocks_the_waiting_action(monkeypatch):
    clock = 100.0
    monkeypatch.setattr(connector_throttle, "monotonic", lambda: clock)
    started, action_queued = asyncio.Event(), asyncio.Event()
    calls = []

    async def limited():
        calls.append("sync")
        started.set()
        await action_queued.wait()
        raise status_error(429, "60")

    async def provider():
        calls.append("provider")

    async def poll():
        with connector_throttle.sync("sync-first"):
            with pytest.raises(httpx.HTTPStatusError):
                await connector_throttle.tool(limited, write=False)
            with pytest.raises(connector_throttle.RateLimited):
                await connector_throttle.tool(provider, write=False)

    async def action():
        await started.wait()
        action_queued.set()
        async with connector_throttle.action("sync-first"):
            with pytest.raises(connector_throttle.RateLimited):
                await connector_throttle.tool(provider, write=True)

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(poll())
        tasks.create_task(action())
    assert calls == ["sync"]
    clock += 61
    async with connector_throttle.action("sync-first"):
        await connector_throttle.tool(provider, write=True)
    assert calls == ["sync", "provider"]


@pytest.mark.anyio
@pytest.mark.parametrize("slow_verification", [True, False])
async def test_retry_deadline_bounds_verification_but_never_cancels_a_write(monkeypatch, slow_verification):
    monkeypatch.setattr(connector_throttle, "_RETRY_BUDGET_SECONDS", 0.03)
    monkeypatch.setattr(connector_throttle, "_BACKOFF", (0, 0))
    calls = []

    async def verify():
        calls.append("verify")
        if slow_verification:
            await asyncio.Event().wait()

    async def write():
        calls.append("send")
        if calls == ["send"]:
            raise status_error(429)
        await asyncio.sleep(0.05)
        return "confirmed"

    async with connector_throttle.action("verify-budget"):
        if slow_verification:
            with pytest.raises(connector_throttle.RateLimited) as error:
                await connector_throttle.tool(write, write=True, before_retry=verify)
            assert error.value.retryable
            assert calls == ["send", "verify"]
        else:
            assert await connector_throttle.tool(write, write=True, before_retry=verify) == "confirmed"
            assert calls == ["send", "verify", "send"]
