"""ASGI app of the MCP-route probe (#157). Routes match ``../mistral.json``.

Unflagged ``/api`` route (the gateway's ordinary sign-in):

- ``GET /api/health``: liveness, no identity lookup.
- ``GET /api/probe/whoami``: the identity report for a browser session.
- ``GET /api/probe/counter``: the write counter and the instance ID.
- ``POST /api/probe/write``: increments the in-memory counter. This is the negative case: a token
  from the MCP-route sign-in must not reach it (Q2).

Flagged routes ``/mcp`` and ``/api/mcp`` (``"mcp": true``): the MCP stub in ``mcp_stub``. Both are
served so the probe can compare a separate route with one under the unflagged ``/api`` prefix (Q7).
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from probe_app import identity, mcp_stub
from probe_app.state import ProbeState

NO_STORE = mcp_stub.NO_STORE
MCP_ROUTES = ("/mcp", "/api/mcp")


def create_app(*, lookup: identity.IdentityLookup = identity.lookup_identity, state: ProbeState | None = None) -> Starlette:
    probe = state or ProbeState()

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok"}, headers=NO_STORE)

    async def whoami(request: Request) -> Response:
        report = await identity.report(
            request.headers, list(request.headers.keys()),
            route="/api", salt=probe.salt, instance=probe.instance, lookup=lookup,
        )
        return JSONResponse(report, headers=NO_STORE)

    async def counter(_request: Request) -> Response:
        return JSONResponse(probe.snapshot(), headers=NO_STORE)

    async def write(_request: Request) -> Response:
        await probe.increment()
        return JSONResponse(probe.snapshot(), headers=NO_STORE)

    def mcp_endpoint(route: str):
        async def endpoint(request: Request) -> Response:
            if request.method != "POST":
                return mcp_stub.method_not_allowed()
            return await mcp_stub.handle(request, route=route, state=probe, lookup=lookup)

        return endpoint

    routes = [
        Route("/api/health", health, methods=["GET"]),
        Route("/api/probe/whoami", whoami, methods=["GET"]),
        Route("/api/probe/counter", counter, methods=["GET"]),
        Route("/api/probe/write", write, methods=["POST"]),
        *(Route(path, mcp_endpoint(path), methods=["GET", "POST", "DELETE"]) for path in MCP_ROUTES),
    ]
    app = Starlette(routes=routes)
    app.state.probe = probe
    return app


app = create_app()
