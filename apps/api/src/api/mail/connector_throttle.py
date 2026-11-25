"""Coordinate connector requests and bound retries for a message action."""

import logging
import math
from asyncio import Lock, get_running_loop, sleep, timeout
from collections.abc import Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from weakref import WeakValueDictionary

import httpx

CODE = "provider_rate_limited"
_RETRY_BUDGET_SECONDS = 3.0
_BACKOFF = (0.25, 0.75)
_gates: WeakValueDictionary[str, Lock] = WeakValueDictionary()
_cooldowns: dict[str, float] = {}
_sync_account: ContextVar[str | None] = ContextVar("connector_sync_account", default=None)


class RateLimited(RuntimeError):
    """The connector throttled a request in the action flow."""

    def __init__(self, *, retryable: bool = True, labels: list[str] | None = None):
        super().__init__(CODE)
        self.retryable = retryable
        self.labels = labels


@dataclass
class _Budget:
    account_id: str
    deadline: float | None = None
    attempts: int = 0


_action_budget: ContextVar[_Budget | None] = ContextVar("connector_action_budget", default=None)


class _PrivateTransportLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Transport logs contain credential-bearing URLs or headers. The action
        # service reports the fixed kind, provider and failure separately.
        return _action_budget.get() is None


for _logger in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy"):
    logging.getLogger(_logger).addFilter(_PrivateTransportLog())


def _gate(account_id: str) -> Lock:
    lock = _gates.get(account_id)
    if lock is None:
        lock = Lock()
        _gates[account_id] = lock
    return lock


@asynccontextmanager
async def action(account_id: str):
    # The same gate protects the provider sequence and the caller's index commit.
    async with _gate(account_id):
        token = _action_budget.set(_Budget(account_id))
        try:
            yield
        finally:
            _action_budget.reset(token)


@contextmanager
def sync(account_id: str | None) -> Iterator[None]:
    token = _sync_account.set(account_id)
    try:
        yield
    finally:
        _sync_account.reset(token)


def _retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(0, (date - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


def _check_cooldown(account_id: str) -> None:
    if _cooldowns.get(account_id, 0) > monotonic():
        raise RateLimited()


def _record_cooldown(account_id: str, delay: float) -> None:
    now = monotonic()
    for key, until in tuple(_cooldowns.items()):
        if until <= now:
            del _cooldowns[key]
    _cooldowns[account_id] = now + delay


async def tool[T](
    call: Callable[[], Awaitable[T]], *, write: bool,
    before_retry: Callable[[], Awaitable[None]] | None = None,
) -> T:
    budget = _action_budget.get()
    if budget is None:
        account_id = _sync_account.get()
        if account_id is None:
            return await call()
        # FIFO admission between requests lets actions interrupt a poll's burst.
        async with _gate(account_id):
            _check_cooldown(account_id)
            return await call()

    _check_cooldown(budget.account_id)
    loop = get_running_loop()
    retry = False
    while True:
        try:
            if retry and budget.deadline is not None:
                async with timeout(budget.deadline - loop.time()):
                    if before_retry is not None:
                        await before_retry()
                    return await call()
            return await call()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status not in (429, 503):
                raise
            retry_after = _retry_after(error.response.headers.get("Retry-After"))
            # A write receiving 503 may have executed. Never replay it.
            if status == 503 and write:
                _record_cooldown(budget.account_id, max(retry_after, _BACKOFF[-1]))
                raise
            if budget.deadline is None:
                budget.deadline = loop.time() + _RETRY_BUDGET_SECONDS
            delay = max(_BACKOFF[min(budget.attempts, len(_BACKOFF) - 1)], retry_after)
            if budget.attempts >= len(_BACKOFF) or delay >= budget.deadline - loop.time():
                _record_cooldown(budget.account_id, delay)
                raise RateLimited() from None
            budget.attempts += 1
            await sleep(delay)
            retry = True
        except TimeoutError:
            if retry and not write:
                raise RateLimited() from None
            raise
