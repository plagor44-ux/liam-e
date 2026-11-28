"""The caller kind behind the Studio gateway, from the Users API answer for the caller token (#200)."""

import asyncio
from collections.abc import Iterator
import logging
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import respx
from starlette.datastructures import State
from starlette.requests import HTTPConnection
from utils.mistral import CallerCredentials, MistralNotConfiguredError, install_caller_credentials

import api.caller_identity as identity_module
from api.caller_identity import FRESH_SECONDS, RETRY_SECONDS, STALE_SECONDS, CallerKind, caller_identity

PROXY = "https://gateway.example.test/mistral"
WHO = PROXY + "/v1/users/me"
TOKEN = "forwarded-caller-token-test-only"
USER = "11111111-1111-4111-8111-111111111111"
ORGANIZATION = "33333333-3333-4333-8333-333333333333"
WORKSPACE = "44444444-4444-4444-8444-444444444444"
KEY = "55555555-5555-4555-8555-555555555555"
IDENTITY = {"id": USER, "organization": {"id": ORGANIZATION}, "workspace": {"id": WORKSPACE}, "api_key": None}
KEY_IDENTITY = {**IDENTITY, "api_key": {"id": KEY}}

pytestmark = pytest.mark.anyio


def _token(token: str = TOKEN) -> None:
    install_caller_credentials(lambda: CallerCredentials(server_url=PROXY, token=token))


def _request(app: object, headers: list[tuple[bytes, bytes]] | None = None) -> HTTPConnection:
    """A fresh request on ``app``; each one has its own request state, as in the server."""
    return HTTPConnection({"type": "http", "app": app, "method": "GET", "path": "/", "headers": headers or []})


def _own(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "api.caller_identity"]


@pytest.fixture
def app() -> SimpleNamespace:
    return SimpleNamespace(state=State())


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(identity_module, "_now", lambda: now[0])
    return now


@pytest.fixture
def who() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        yield router.get(WHO).mock(return_value=httpx.Response(200, json=IDENTITY))


async def test_a_user_session_is_a_user_with_its_scope(app, who):
    _token()
    identity = await caller_identity(_request(app))
    assert identity.kind is CallerKind.USER
    assert (identity.user_id, identity.organization_id, identity.workspace_id) == (
        UUID(USER), UUID(ORGANIZATION), UUID(WORKSPACE),
    )
    request = who.calls.last.request
    assert request.headers["authorization"] == "Bearer " + TOKEN
    assert "x-api-key" not in request.headers


@pytest.mark.parametrize("api_key", [{"id": KEY}, False, "key", 0])
async def test_any_non_null_api_key_marks_a_key_caller(app, who, api_key):
    who.mock(return_value=httpx.Response(200, json={**IDENTITY, "api_key": api_key}))
    _token()
    identity = await caller_identity(_request(app))
    assert identity.kind is CallerKind.API_KEY
    assert identity.user_id is None


async def test_an_api_key_marks_a_key_caller_whatever_else_the_answer_holds(app, who):
    who.mock(return_value=httpx.Response(200, json={"api_key": {"id": KEY}}))
    _token()
    assert (await caller_identity(_request(app))).kind is CallerKind.API_KEY


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_token_is_rejected(app, who, status):
    who.mock(return_value=httpx.Response(status))
    _token()
    assert (await caller_identity(_request(app))).kind is CallerKind.REJECTED


@pytest.mark.parametrize(("answer", "reason"), [
    (httpx.Response(500), "status_500"),
    (httpx.Response(429), "status_429"),
    (httpx.Response(302, headers={"location": "https://other.example.test/"}), "status_302"),
    (httpx.Response(200, content=b"not json"), "shape"),
    (httpx.Response(200, json=[IDENTITY]), "shape"),
    (httpx.Response(200, json={**IDENTITY, "organization": None}), "shape"),
    (httpx.Response(200, json={k: v for k, v in IDENTITY.items() if k != "api_key"}), "shape"),
    (httpx.Response(200, json={**IDENTITY, "id": "not-a-uuid"}), "shape"),
    (httpx.Response(200, content=b"x" * (16 * 1024 + 1)), "oversized"),
    (httpx.ConnectError("down"), "transport"),
    (httpx.ReadTimeout("slow"), "transport"),
])
async def test_a_failed_lookup_is_unavailable_with_a_fixed_reason(app, who, caplog, answer, reason):
    if isinstance(answer, Exception):
        who.mock(side_effect=answer)
    else:
        who.mock(return_value=answer)
    _token()
    caplog.set_level(logging.INFO, logger="api.caller_identity")
    assert (await caller_identity(_request(app))).kind is CallerKind.UNAVAILABLE
    assert _own(caplog) == [f"caller_identity=unavailable reason={reason}"]


async def test_a_slow_lookup_gives_up(app, who, monkeypatch, caplog):
    import asyncio

    async def stall(_request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=IDENTITY)

    monkeypatch.setattr(identity_module, "_LOOKUP_SECONDS", 0.01)
    who.mock(side_effect=stall)
    _token()
    caplog.set_level(logging.INFO, logger="api.caller_identity")
    assert (await caller_identity(_request(app))).kind is CallerKind.UNAVAILABLE
    assert "caller_identity=unavailable reason=timeout" in caplog.text


async def test_without_a_gateway_there_is_no_caller_to_look_up(app, who):
    assert (await caller_identity(_request(app))).kind is CallerKind.NO_GATEWAY
    assert not who.calls


@pytest.mark.parametrize("header", [b"x-apps-token", b"x-space-token"])
async def test_a_gateway_token_without_a_configured_gateway_is_unverifiable(app, who, caplog, header):
    caplog.set_level(logging.INFO, logger="api.caller_identity")
    identity = await caller_identity(_request(app, [(header, TOKEN.encode())]))
    assert identity.kind is CallerKind.UNAVAILABLE
    assert "caller_identity=unavailable reason=gateway_unconfigured" in caplog.text
    assert not who.calls


async def test_a_gateway_request_without_a_caller_token_has_no_token(app, who):
    def refuse():
        raise MistralNotConfiguredError("no caller token")

    install_caller_credentials(refuse)
    assert (await caller_identity(_request(app))).kind is CallerKind.NO_TOKEN
    _token("")
    assert (await caller_identity(_request(app))).kind is CallerKind.NO_TOKEN
    assert not who.calls


@pytest.mark.parametrize(("body", "kind"), [
    (IDENTITY, CallerKind.USER), (KEY_IDENTITY, CallerKind.API_KEY), (None, CallerKind.REJECTED),
])
async def test_a_definite_answer_is_reused_while_fresh(app, who, clock, body, kind):
    who.mock(return_value=httpx.Response(200, json=body) if body else httpx.Response(401))
    _token()
    assert (await caller_identity(_request(app))).kind is kind
    clock[0] += FRESH_SECONDS[kind] - 1
    assert (await caller_identity(_request(app))).kind is kind
    assert who.call_count == 1
    clock[0] += 2
    assert (await caller_identity(_request(app))).kind is kind
    assert who.call_count == 2


async def test_a_key_verdict_outlives_a_user_verdict():
    assert FRESH_SECONDS[CallerKind.API_KEY] > FRESH_SECONDS[CallerKind.USER]
    assert set(FRESH_SECONDS) == {CallerKind.USER, CallerKind.API_KEY, CallerKind.REJECTED}


async def test_answers_are_kept_per_token_and_per_app(who):
    first, second = SimpleNamespace(state=State()), SimpleNamespace(state=State())
    _token()
    await caller_identity(_request(first))
    await caller_identity(_request(first))
    assert who.call_count == 1
    await caller_identity(_request(second))
    assert who.call_count == 2
    _token("another-caller-token-test-only")
    await caller_identity(_request(first))
    assert who.call_count == 3


async def test_the_cache_holds_hashes_not_tokens(app, who):
    _token()
    await caller_identity(_request(app))
    cache = app.state.caller_identity_cache
    assert len(cache) == 1
    assert TOKEN not in repr(cache._entries) and TOKEN not in "".join(cache._entries)


async def test_a_failed_lookup_is_retried_after_a_short_pause_and_never_kept(app, who, clock):
    who.mock(return_value=httpx.Response(500))
    _token()
    assert (await caller_identity(_request(app))).kind is CallerKind.UNAVAILABLE
    who.mock(return_value=httpx.Response(200, json=IDENTITY))
    # Within the pause the token is not asked about again, and nothing was stored as a verdict.
    clock[0] += RETRY_SECONDS - 1
    for _ in range(5):
        assert (await caller_identity(_request(app))).kind is CallerKind.UNAVAILABLE
    assert who.call_count == 1
    clock[0] += 2
    assert (await caller_identity(_request(app))).kind is CallerKind.USER
    assert who.call_count == 2


async def test_the_pause_is_per_token(app, who):
    who.mock(return_value=httpx.Response(500))
    _token()
    await caller_identity(_request(app))
    who.mock(return_value=httpx.Response(200, json=IDENTITY))
    _token("another-caller-token-test-only")
    assert (await caller_identity(_request(app))).kind is CallerKind.USER
    assert who.call_count == 2


def _held(who: respx.Route) -> tuple[asyncio.Event, asyncio.Event, list[int]]:
    """Hold every Users API answer until ``release`` is set; ``started`` is set once one is asked.

    ``asked`` counts the requests as they arrive: respx records a call only once it is answered.
    """
    started, release, asked = asyncio.Event(), asyncio.Event(), [0]

    async def answer(_request):
        asked[0] += 1
        started.set()
        await release.wait()
        return httpx.Response(200, json=IDENTITY)

    who.mock(side_effect=answer)
    return started, release, asked


async def test_concurrent_requests_for_one_token_share_one_lookup(app, who):
    started, release, asked = _held(who)
    _token()
    pending = [asyncio.create_task(caller_identity(_request(app))) for _ in range(5)]
    await started.wait()
    await asyncio.sleep(0.01)  # every request has reached the lookup
    release.set()
    assert {identity.kind for identity in await asyncio.gather(*pending)} == {CallerKind.USER}
    assert asked == [1]


async def test_a_slow_outage_costs_one_shared_wait_then_the_pause(app, who, monkeypatch, caplog):
    asked = [0]

    async def stall(_request):
        asked[0] += 1
        await asyncio.sleep(1)
        return httpx.Response(200, json=IDENTITY)

    monkeypatch.setattr(identity_module, "_LOOKUP_SECONDS", 0.05)
    who.mock(side_effect=stall)
    _token()
    caplog.set_level(logging.INFO, logger="api.caller_identity")
    identities = await asyncio.gather(*(caller_identity(_request(app)) for _ in range(5)))
    assert {identity.kind for identity in identities} == {CallerKind.UNAVAILABLE}
    assert asked == [1]
    assert _own(caplog) == ["caller_identity=unavailable reason=timeout"]
    assert (await caller_identity(_request(app))).kind is CallerKind.UNAVAILABLE
    assert asked == [1]


async def test_a_cancelled_request_leaves_the_shared_lookup_to_the_others(app, who):
    started, release, asked = _held(who)
    _token()
    first = asyncio.create_task(caller_identity(_request(app)))
    await started.wait()
    second = asyncio.create_task(caller_identity(_request(app)))
    await asyncio.sleep(0.01)
    first.cancel()
    release.set()
    assert (await second).kind is CallerKind.USER
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await caller_identity(_request(app))).kind is CallerKind.USER
    assert asked == [1]


async def test_a_lookup_left_on_another_event_loop_is_not_joined(app, who):
    _token()
    await caller_identity(_request(app))
    cache = app.state.caller_identity_cache
    key = identity_module._key(PROXY, TOKEN)
    cache._entries.clear()
    cache._inflight[key] = SimpleNamespace(done=lambda: False, get_loop=lambda: object())
    assert (await caller_identity(_request(app))).kind is CallerKind.USER
    assert who.call_count == 2


async def test_one_request_never_repeats_a_failed_lookup(app, who):
    who.mock(return_value=httpx.Response(500))
    _token()
    request = _request(app)
    assert (await caller_identity(request, allow_stale=True)).kind is CallerKind.UNAVAILABLE
    assert (await caller_identity(request)).kind is CallerKind.UNAVAILABLE
    assert who.call_count == 1


@pytest.mark.parametrize(("body", "kind"), [
    (IDENTITY, CallerKind.USER), (KEY_IDENTITY, CallerKind.API_KEY), (None, CallerKind.REJECTED),
])
async def test_only_allow_stale_falls_back_to_the_last_answer(app, who, clock, caplog, body, kind):
    """The stand-in keeps its kind: a stale refusal or key never becomes a user."""
    who.mock(return_value=httpx.Response(200, json=body) if body else httpx.Response(401))
    _token()
    await caller_identity(_request(app))
    clock[0] += FRESH_SECONDS[kind] + 1
    who.mock(return_value=httpx.Response(503))
    caplog.set_level(logging.INFO, logger="api.caller_identity")

    assert (await caller_identity(_request(app))).kind is CallerKind.UNAVAILABLE
    assert (await caller_identity(_request(app), allow_stale=True)).kind is kind
    assert f"caller_identity=stale kind={kind.value}" in caplog.text

    # The same request's owner check still sees no fresh answer.
    request = _request(app)
    assert (await caller_identity(request, allow_stale=True)).kind is kind
    assert (await caller_identity(request)).kind is CallerKind.UNAVAILABLE


@pytest.mark.parametrize(("body", "kind"), [(IDENTITY, CallerKind.USER), (KEY_IDENTITY, CallerKind.API_KEY)])
async def test_a_stale_answer_expires_after_its_grace(app, who, clock, body, kind):
    who.mock(return_value=httpx.Response(200, json=body))
    _token()
    await caller_identity(_request(app))
    who.mock(return_value=httpx.Response(503))
    clock[0] += STALE_SECONDS[kind] - 1
    assert (await caller_identity(_request(app), allow_stale=True)).kind is kind
    clock[0] += 1
    assert (await caller_identity(_request(app), allow_stale=True)).kind is CallerKind.UNAVAILABLE


async def test_a_user_grace_is_short_and_a_refusal_grace_is_long():
    assert STALE_SECONDS[CallerKind.USER] == 15 * 60
    assert STALE_SECONDS[CallerKind.API_KEY] >= STALE_SECONDS[CallerKind.USER]
    assert STALE_SECONDS[CallerKind.REJECTED] >= STALE_SECONDS[CallerKind.USER]
    assert set(STALE_SECONDS) == set(FRESH_SECONDS)


async def test_the_cache_is_bounded_and_drops_the_least_recent_token(app, who, monkeypatch):
    monkeypatch.setattr(identity_module, "_MAX_ENTRIES", 2)
    for token in ("a-test-only", "b-test-only"):
        _token(token)
        await caller_identity(_request(app))
    _token("a-test-only")
    await caller_identity(_request(app))  # a is now the most recent
    _token("c-test-only")
    await caller_identity(_request(app))
    assert len(app.state.caller_identity_cache) == 2
    calls = who.call_count
    _token("a-test-only")
    await caller_identity(_request(app))
    assert who.call_count == calls
    _token("b-test-only")
    await caller_identity(_request(app))
    assert who.call_count == calls + 1


async def test_the_repr_and_logs_carry_no_token_or_id(app, who, clock, caplog):
    caplog.set_level(logging.DEBUG)
    _token()
    identity = await caller_identity(_request(app))
    assert repr(identity) == "CallerIdentity(kind='user')"
    clock[0] += FRESH_SECONDS[CallerKind.USER] + 1
    who.mock(return_value=httpx.Response(500, json=IDENTITY))
    await caller_identity(_request(app), allow_stale=True)
    for value in (TOKEN, USER, ORGANIZATION, WORKSPACE, KEY):
        assert value not in caplog.text
    own = "\n".join(_own(caplog))
    assert own and PROXY not in own and "gateway.example.test" not in own
