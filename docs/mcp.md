# Native MCP Transport and Vibe Code Setup

liam-e exposes its four curated read-only mail tools over a stateless Streamable HTTP MCP endpoint
at `/api/mcp`. The endpoint fails closed by default: until the deployment binds its Studio owner,
the owner turns on outside access, and the owner grants at least one mail account, no mail data is
readable.

## Security prerequisites

All four layers must be satisfied before an MCP client can read indexed mail:

1. **Flagged Studio route.** `mistral.json` declares `{ "path": "/api/mcp", "mcp": true }` so the
   Studio gateway protects `/api/mcp` with OAuth sign-in and rejects MCP tokens on unflagged `/api`
   routes (#157).
2. **Per-deployment owner binding (`MCP_STUDIO_OWNER`).** Set the deployment environment variable
   `MCP_STUDIO_OWNER` to JSON naming only the owner's IDs (no `origin`, separate from
   `AUDIO_STUDIO_OWNER`):

   ```json
   {"user_id": "<user-id>", "organization_id": "<organization-id>", "workspace_id": "<workspace-id>"}
   ```

   When `MCP_STUDIO_OWNER` is unset or malformed, liam-e keeps `DenyReadAccess` bound and answers
   `401` to every request on `/api/mcp`.
3. **Outside access switch (`/settings/access`).** Off by default. Turning it on requires the bound
   owner's browser session. While off, every `/api/mcp` request — including `initialize` and
   `tools/list` — answers `403 agent_access_disabled` with `Cache-Control: no-store` before any
   verifier runs.
4. **MCP account grant (`/settings/mcp`).** Empty by default and stored in `mcp_settings`
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

## Vibe Code configuration

Add a `[[mcp_servers]]` entry to your Vibe Code configuration using the deployed app's placeholder
base URL:

```toml
[[mcp_servers]]
name = "liam-e"
transport = "streamable-http"
url = "https://<app-host>/api/mcp"

[mcp_servers.auth]
type = "oauth"
```

### Verification checklist

1. In liam-e, open `/settings/mcp`, select one test account, and save.
2. Open `/settings/access` and turn on outside access.
3. In Vibe Code, complete the Studio OAuth sign-in for `liam-e`.
   Note from #157: Studio's protected-resource metadata currently advertises the app origin rather
   than the `/api/mcp` route path (`resource_relation=origin`), and the authorization server
   completes sign-in when the client sends the resource named in that metadata.
4. Confirm that `tools/list` shows only the four tools above and that `liam_mail_accounts`,
   `liam_mail_summary`, `liam_mail_search`, and `liam_mail_read` return only the granted account's
   indexed inbox mail.

## Revoking access

- **Remove individual accounts:** uncheck accounts in `/settings/mcp` and save. The reduced grant
  applies on the next MCP call; an empty grant returns empty results.
- **Immediate app-side stop:** open `/settings/access` and turn off outside access. Every new
  request across all app instances immediately receives `403 agent_access_disabled`.
- **Disable the verifier:** unset `MCP_STUDIO_OWNER` and redeploy so `DenyReadAccess` is bound
  again.
- **Gateway limitation (#157, L9):** Mistral Studio currently exposes no OAuth token revocation
  endpoint (RFC 7009) or client-management UI (RFC 7592). Turning off access inside liam-e stops
  the app from serving data, but issued OAuth tokens and refresh grants stay valid at the Studio
  gateway until the Studio app itself is deleted. Mail text already read by a client cannot be
  recalled.

## Transport settings

All settings live in `packages/py/env/src/env/mcp.py` and are declared empty in `.env.example`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MCP_STUDIO_OWNER` | unset | Per-deployment owner binding JSON (`user_id`, `organization_id`, `workspace_id`). Unset keeps `DenyReadAccess`. |
| `MCP_ALLOWED_HOSTS` | `""` | Additional comma-separated allowed `Host` values (app host from `CORS_ORIGIN` / `MAIL_OAUTH_REDIRECT_BASE_URL` is included automatically). |
| `MCP_ALLOWED_ORIGINS` | `""` | Additional comma-separated allowed `Origin` values (`CORS_ORIGIN` is included automatically; absent `Origin` is allowed for server-side clients). |
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
