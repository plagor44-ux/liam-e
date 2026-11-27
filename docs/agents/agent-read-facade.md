# Agent read facade

Part of the agent instructions: [AGENTS.md](../../AGENTS.md) links here from its index, and its rules apply here too.

`api/mcp/registry.py` enumerates the four explicitly approved mail read operations;
`api/mcp/mail.py` reads only the existing INBOX index through accessors. The scope comes from a
`ReadGrant`, never request parameters. The default verifier in `api/mcp/access.py` rejects all
requests with 401. A future native transport must, after positive proof of access, bind its own
`ReadAccessVerifier` to `get_read_access_verifier` and call the same services with its verified
grant. Grants are limited to 100 account IDs; foreign selections return 403, unknown and foreign
mail IDs both return 404. Reading by ID also remains within the INBOX index: a mail moved since
then responds like a missing one. An empty grant returns empty results, never all accounts.
Relative links `/mail/<uuid>` avoid an unconfigured external origin. Mail data is foreign content,
not instructions. The index state is reported; provider freshness remains unknown. A successful
sync may contain only a limited page; without a stored completeness marker, the index remains
conservatively incomplete even with status `ok`. The timestamp denotes the last attempt, not
necessarily a success. The reduced spec is not runtime authorization; production exposure remains
blocked until access is proven. The Uvicorn access log removes query strings for these routes even
if the log entry format changes; gateway and proxy logs must independently receive the same
protection before release. The export is reproduced with `bunx nx run api:gen-agent-openapi`.

## Agent access switch

The owner's switch "Zugriff von außen" / "Outside access" (#197, table `agent_access`) is the
emergency stop inside liam-e. It is off by default for new and existing installations: a missing
row means off.

- **Where it is checked.** `ensure_agent_access()` in `api/mcp/access.py` runs in
  `require_read_access()` before the verifier, so no verifier code runs while access is off. The
  four services in `api/mcp/mail.py` check it again, so a native transport that calls them with a
  grant cannot bypass it. A transport that is not one route per tool must call it for every
  request and every message, including `initialize` and `tools/list`. A structural test fails when
  an agent route lacks the gate.
- **What a caller sees.** While off: 403 `agent_access_disabled` with `Cache-Control: no-store`
  and no `WWW-Authenticate`, so MCP clients do not restart a sign-in. If the switch cannot be
  read, the answer is 503 `agent_access_unavailable`. The gate never fails open.
- **Agent callers on `/api/v1`.** The Studio gateway forwards neither `x-api-key` nor
  `Authorization` to the app (#157), so the caller gate (`agent_caller_policy` in
  `api/mcp/exposure.py`) asks the Users API what the forwarded caller token belongs to
  (`api/caller_identity.py`, #200). These count as agents: an `x-api-key` header, a token whose
  `GET /v1/users/me` answer has a non-null `api_key`, a request without a caller token, and a
  token the Users API refuses. While off they get 403 `agent_access_disabled`. While on they may
  use only the reading GETs on the read-only list in AGENTS.md; everything else is 403
  `agent_operation_refused`. A user session passes without a switch read, and so does every
  request when no gateway is configured (local runs). That a workspace-key request through the
  gateway arrives with a token whose answer carries `api_key` follows the Users API contract but
  has not been checked live; `tools/gateway_access_probe.py` checks it.
- **Identity lookups.** The gate looks a token up with the request's own bearer and keeps a
  definite answer per app instance, keyed by a hash of gateway URL and token: a user or a refusal
  for 60 s, an API key for 10 min, a bounded number of tokens. Concurrent requests with one token
  share the lookup in flight. A failed lookup never becomes a verdict. It is not repeated within
  one request, and the same token is not asked about again for 10 s, so a slow Users API costs
  one wait per token rather than one per request. The owner check (`api/owner_access.py`) uses
  the same lookup and never accepts a stale answer: without a fresh one, turning access on
  answers 503 `caller_identity_unavailable` like the gate, and the audio owner routes refuse.
- **When the Users API is down.** The gate lets the token's last definite answer stand in, within
  a grace period counted from that lookup: 15 minutes for a user, an hour for an API key or a
  refusal. The stand-in keeps its kind, so a known key or refusal stays refused and never becomes
  a user. Without such an answer the request fails closed: every method, reads included, gets 503
  `caller_identity_unavailable` with `Retry-After: 5`, logged as
  `agent_caller=unverified outcome=refused`. So the owner's browser keeps working through an
  outage of up to 15 minutes, and a new token gets nothing until the lookup works again. A gateway
  token on an app without a configured gateway counts as such a failed lookup, so a deployment
  that lacks the gateway setting refuses every gateway request.
- **What the owner binding does here.** Nothing: the gate never reads `AUDIO_STUDIO_OWNER`. A
  missing or malformed binding only makes turning access on answer 409
  `agent_access_owner_unbound` and refuses the audio owner routes.
- **How fast it applies.** The switch has no cache. Every gated request reads the one row, so a
  change applies to the next request on every app instance. A request that already passed the gate
  finishes with at most one bounded response.
- **Who switches.** `agent_access_disable` passes the caller gate for every caller, takes no
  revision and makes no identity lookup, so it always works, also during a Users API outage, and
  the settings page offers it even when it cannot load the switch state. It always advances the
  revision, also when access is already off, so a turn-on confirmed earlier (say in another tab,
  while its identity lookup was still running) fails with
  409 `agent_access_conflict` instead of reopening access. `agent_access_enable` needs the bound
  owner's own browser session (`api/owner_access.py`, binding `AUDIO_STUDIO_OWNER`) and the
  current revision. Both changes take the same row lock. All three switch operations are
  browser-only.

The switch does not revoke anything at the gateway. Tokens and refresh grants stay valid there;
only deleting the app ends them (#157, L9). Data an agent has already read cannot be recalled.
Callers the Users API reports as user sessions, such as an MCP sign-in token (#157), pass the
`/api/v1` gate like the owner's browser, so the AGENTS.md import rule still applies, and the
`/api/mcp` binding PR (#36) must not rely on this gate to keep MCP clients away from `/api/v1`.
With the switch on, the read facade and `/api/mcp` still answer 401 to everyone because
`DenyReadAccess` stays unconditionally bound in production until that separate binding PR
installs a caller verifier and flags `/api/mcp` in `mistral.json` after #157 is GO, while
workspace-key callers can already use the read-only `/api/v1` GETs; the settings page says both.

## Native MCP transport (`/api/mcp`, #36)

- **Server (`api/mcp/server.py`):** stateless `MCPServer` (`mcp==2.3.0`, JSON responses, both
  `2026-07-28` and `2025-11-25` protocol eras) whose tools are generated strictly from
  `registry.OPERATIONS` with read-only annotations and fixed error codes. No resources, prompts,
  subscriptions, sampling, or elicitation handlers are registered. SDK loggers are kept off `DEBUG`
  so tool arguments are never logged.
- **Mount (`api/mcp/transport.py`):** mounted in `api/main.py` as a single non-OpenAPI route at
  `/api/mcp` with `session_manager.run()` inside the app lifespan. Every request passes through
  `ensure_agent_access(path_group="mcp")`, explicit `TransportSecuritySettings` (Host 421, Origin
  403; absent Origin allowed for server-side clients; fixed-code logs without header values),
  POST-only enforcement (405 `Allow: POST` for GET/DELETE), a body size cap (413), the shared
  `get_read_access_verifier()` dependency bound into a request-scoped `ContextVar`, and an
  in-process per-principal rate limit (429).
- **Grant (`mcp_settings`):** installation-wide account list managed at `/settings/mcp`
  (`GET`/`PATCH /api/v1/settings/mcp`, browser-only GET and mutating PATCH), intersected with
  active accounts and cleaned up in the same transaction when `mail_account.delete_account` runs.
- **Guards:** `apps/api/tests/test_mcp_transport.py` verifies the closed non-OpenAPI route/mount
  inventory, both protocol eras, access/security rejections, side-effect spies, and log privacy.
  Setup and revocation details live in [docs/mcp.md](../mcp.md).
