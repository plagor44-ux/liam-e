# Native MCP Transport and Vibe Code Setup

liam-e mounts its four curated read-only mail tools over a stateless Streamable HTTP MCP endpoint
at `/api/mcp`.

> **Current status — fail-closed (401 to everyone):** `get_read_access_verifier()` keeps
> `DenyReadAccess` unconditionally bound in production until the #157 gateway prerequisite (or an
> owner-approved replacement, plus the workspace-key probe) is complete and a separate follow-up
> binding PR installs the Studio caller verifier and flags `/api/mcp` in `mistral.json`. Until that
> binding PR lands, `/api/mcp` answers `403 agent_access_disabled` while outside access is off and
> `401 unauthorized` to everyone while it is on — even when accounts are selected in
> `/settings/mcp`. Vibe Code cannot read mail from `/api/mcp` yet.

## Prerequisites for later enablement

When the separate follow-up binding PR activates MCP access, all five prerequisites must be
satisfied before an MCP client can read indexed mail:

1. **Flagged Studio route (follow-up binding PR).** Declare `{ "path": "/api/mcp", "mcp": true }`
   in `mistral.json` so the Studio gateway protects `/api/mcp` with OAuth sign-in and rejects MCP
   tokens on unflagged `/api` routes (#157). Not enabled in `mistral.json` yet.
2. **Host allowlist (`MCP_ALLOWED_HOSTS` or `CORS_ORIGIN`).** By default `CORS_ORIGIN` is
   `http://localhost:3001` and `MAIL_OAUTH_REDIRECT_BASE_URL` is empty, so `allowed_hosts` contains
   only loopback addresses and every Studio request gets `421` (`mcp_transport=host_rejected`). Set
   `MCP_ALLOWED_HOSTS` (or `CORS_ORIGIN`) on the deployment to the exact `Host` header value the
   API receives behind the Studio gateway.
3. **Caller verifier and owner binding (follow-up binding PR).** Bind a `ReadAccessVerifier` that
   verifies the Studio owner identity (per-deployment binding, separate from `AUDIO_STUDIO_OWNER`)
   and reads `mcp_settings`. Until that binding PR lands, `DenyReadAccess` stays unconditionally
   bound and answers `401` to everyone.
4. **Outside access switch (`/settings/access`).** Off by default. Turning it on requires the bound
   owner's browser session. While off, every `/api/mcp` request — including `initialize` and
   `tools/list` — answers `403 agent_access_disabled` with `Cache-Control: no-store` before any
   verifier runs.
5. **MCP account grant (`/settings/mcp`).** Empty by default and stored in `mcp_settings`
   independently of chat mail grants. Select only the active mail accounts an MCP client may read.
   Deleting an account removes it from the MCP grant in the same transaction.

Rollout rule: verify the endpoint against a dedicated test deployment with synthetic seed data first
before enabling it on a production deployment.

## Exposed tools (read-only)

The MCP server (`apps/api/src/api/mcp/server.py`) generates its tool list directly from
`api/mcp/registry.py` and registers no resources, prompts, sampling, or elicitation handlers:

| Tool | Behaviour |
| --- | --- |
| `liam_mail_accounts` | Lists only the active mail accounts granted in `/settings/mcp`. |
| `liam_mail_search` | Searches the local `INBOX` index of granted accounts (max 50 hits per page, 1,000-character query limit, scope-bound cursor). |
| `liam_mail_read` | Reads stored plaintext (up to 20,000 characters) of one `INBOX` message without contacting a provider or marking it read. |
| `liam_mail_summary` | Counts total and unread messages in the local `INBOX` index of granted accounts with no model or provider call. |

All tools carry read-only, non-destructive, idempotent, closed-world annotations (documentation
hints only) and return bounded envelopes with `schema_version`, `scope`, `index` (`stale` and
`incomplete` flags), `truncated`, and `untrusted_*` fields. Returned mail content is untrusted
external data, never instructions. Changing or sending mail is impossible over MCP.

## Vibe Code configuration (target setup after activation)

Once the follow-up binding PR flags `/api/mcp` and binds a real caller verifier (after #157 GO or
an owner-approved replacement), add a `[[mcp_servers]]` entry to your Vibe Code configuration
using the deployed app's placeholder base URL:

```toml
[[mcp_servers]]
name = "liam-e"
transport = "streamable-http"
url = "https://<app-host>/api/mcp"

[mcp_servers.auth]
type = "oauth"
```

### Verification checklist

1. Set `MCP_ALLOWED_HOSTS` (or `CORS_ORIGIN`) on the deployment to the `Host` header value the API
   receives behind the Studio gateway (otherwise requests fail with `421` / `mcp_transport=host_rejected`).
2. In liam-e, open `/settings/mcp`, select one test account, and save.
3. Open `/settings/access` and turn on outside access.
4. In Vibe Code, complete the Studio OAuth sign-in for `liam-e`.
   Note from #157: Studio's protected-resource metadata currently advertises the app origin rather
   than the `/api/mcp` route path (`resource_relation=origin`), and the authorization server
   completes sign-in when the client sends the resource named in that metadata.
5. Confirm that `tools/list` shows only the four tools above and that `liam_mail_accounts`,
   `liam_mail_summary`, `liam_mail_search`, and `liam_mail_read` return only the granted account's
   indexed inbox mail.

## Revoking access

- **Remove individual accounts:** uncheck accounts in `/settings/mcp` and save. The reduced grant
  applies on the next MCP call; an empty grant returns empty results.
- **Immediate app-side stop:** open `/settings/access` and turn off outside access. Every new
  request across all app instances immediately receives `403 agent_access_disabled`.
- **Disable the verifier (after activation):** until the follow-up binding PR lands,
  `DenyReadAccess` is unconditionally bound in production. Once activated, unsetting the
  deployment's owner binding returns the verifier to failing closed with `401`.
- **Gateway limitation (#157, L9):** Mistral Studio currently exposes no OAuth token revocation
  endpoint (RFC 7009) or client-management UI (RFC 7592). Turning off access inside liam-e stops
  the app from serving data, but issued OAuth tokens and refresh grants stay valid at the Studio
  gateway until the Studio app itself is deleted. Mail text already read by a client cannot be
  recalled.

## Transport settings

All settings live in `packages/py/env/src/env/mcp.py` and are declared empty in `.env.example`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MCP_ALLOWED_HOSTS` | `""` | Additional comma-separated allowed `Host` values. Only hosts parsed from `CORS_ORIGIN` (default `http://localhost:3001`, loopback only) and `MAIL_OAUTH_REDIRECT_BASE_URL` (default empty) are added alongside it, so set `MCP_ALLOWED_HOSTS` (or `CORS_ORIGIN`) on Studio deployments to avoid `421`. |
| `MCP_ALLOWED_ORIGINS` | `""` | Additional comma-separated allowed `Origin` values (added alongside `CORS_ORIGIN` and `MAIL_OAUTH_REDIRECT_BASE_URL`; absent `Origin` is allowed for server-side clients). |
| `MCP_MAX_BODY_BYTES` | `65536` | Maximum HTTP request body size in bytes (`413` above limit). |
| `MCP_CALL_TIMEOUT_SECONDS` | `15.0` | Per-tool execution timeout (`tool_timeout` error code on expiry). |
| `MCP_RATE_LIMIT_REQUESTS` | `60` | Per-principal in-process request cap per window (`429` with `Retry-After`). |
| `MCP_RATE_LIMIT_WINDOW_SECONDS` | `60.0` | Sliding window duration in seconds for the per-principal rate limit. |

## Versions and verification status

| Component | Version / Status | Verification |
| --- | --- | --- |
| MCP Python SDK (`mcp`) | `2.3.0` (pinned) | Verified offline via ASGI tests (`apps/api/tests/test_mcp_transport.py`) |
| Protocol eras | `2026-07-28` (stateless per-request `_meta`) and `2025-11-25` (stateless handshake) | Verified offline (JSON responses, `405` on GET/DELETE, `400` on version mismatch) |
| FastAPI / Starlette | `0.162.5` / `1.10.0` | Verified offline |
| Studio flagged-route sign-in (#157) | Probe run of 26–27 Nov 2026 | Route scoping & kill-switch proven on synthetic probe app; per-grant revocation open |
| Vibe Code OAuth live smoke | `3.1.2` (recorded offline) | **Not checked** (requires test-app deployment and owner approval) |
| Vibe Work custom connector | — | **Not checked** |
| Studio HTTP connector import | — | **Not checked** (forbidden for full OpenAPI; see `AGENTS.md`) |
| Production deployment | — | **Not checked** (test app first, owner approval required) |
