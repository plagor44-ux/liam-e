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
- **Workspace-key callers.** A request to `/api/v1` with an `x-api-key` header counts as an
  agent. While off it gets 403 `agent_access_disabled`. While on it may use only the reading GETs
  on the read-only list in AGENTS.md; everything else is 403 `agent_operation_refused`. Whether
  the gateway forwards `x-api-key` to the app has not been checked live.
- **How fast it applies.** There is no cache. Every gated request reads the one row, so a change
  applies to the next request on every app instance. A request that already passed the gate
  finishes with at most one bounded response.
- **Who switches.** `agent_access_disable` takes no revision and makes no identity lookup, so it
  always works. `agent_access_enable` needs the bound owner's own browser session
  (`api/owner_access.py`, binding `AUDIO_STUDIO_OWNER`) and the current revision. All three
  switch operations are browser-only.

The switch does not revoke anything at the gateway. Tokens and refresh grants stay valid there;
only deleting the app ends them (#157, L9). Data an agent has already read cannot be recalled.
Callers the app cannot tell apart from the owner's browser are not covered, so the AGENTS.md
import rule still applies. With the switch on, the read facade still answers 401 until a real
`ReadAccessVerifier` exists (#36, #54); the settings page says so.
