# Native MCP Transport and Vibe Code Setup

liam-e mounts its four curated read-only mail tools over a stateless Streamable HTTP MCP endpoint
at `/api/mcp`.

> **Current status — fail-closed until configured per deployment:** `mistral.json` flags
> `{"path": "/api/mcp", "mcp": true}`, and `get_read_access_verifier()` binds
> `StudioMcpReadVerifier` whenever `MCP_STUDIO_OWNER` is configured with valid UUIDs. Without
> `MCP_STUDIO_OWNER`, `DenyReadAccess` stays bound (`401 unauthorized` to everyone). While outside
> access (`/settings/access`) is off, every `/api/mcp` request receives `403 agent_access_disabled`
> before any verifier runs.
>
> **Owner revocation decision (28 November 2026):** Mistral Studio currently exposes no per-grant
> OAuth revocation endpoint or UI (#157, Q6 / L9). The owner accepted liam-e's app-side outside
> access switch ("Zugriff von außen", `/settings/access`) as the replacement emergency stop, with
> three accepted residual risks:
>
> 1. Issued OAuth tokens and refresh grants remain valid at the Studio gateway until the Studio app
>    is deleted (#157, L9).
> 2. Turning outside access back on allows previously issued grants to call `/api/mcp` again.
> 3. Mail data already returned to an MCP client cannot be recalled.
>
> Live Vibe Code OAuth sign-in (#157, Q8 / L6) is still **not checked** until verified against the
> test deployment.

## Prerequisites

All five conditions must hold before an MCP client can read indexed mail:

1. **Flagged Studio route (`mistral.json`).** `mistral.json` declares
   `{ "path": "/api/mcp", "mcp": true }` on the `api` module so the Studio gateway protects
   `/api/mcp` with OAuth sign-in and rejects MCP tokens on unflagged `/api` routes (#157, Q2).
2. **Host allowlist (`MCP_ALLOWED_HOSTS` or `CORS_ORIGIN`).** Studio's runtime and
   `mistralai_capabilities` do not inject the app's public hostname (`__APPS_PROXY_URL` and
   `__SPACES_PROXY_URL` point to the Mistral API proxy, not the app host). Because `CORS_ORIGIN`
   defaults to `http://localhost:3001` and `MAIL_OAUTH_REDIRECT_BASE_URL` defaults to empty,
   `allowed_hosts` otherwise contains only loopback addresses and every Studio request fails with
   `421` (`mcp_transport=host_rejected`). Set `MCP_ALLOWED_HOSTS` (or `CORS_ORIGIN`) on the
   deployment to the exact `Host` header value the API receives behind the Studio gateway. Allowed
   hosts are never derived from untrusted request headers and are never coupled to
   `AUDIO_STUDIO_OWNER`.
3. **Dedicated MCP owner binding (`MCP_STUDIO_OWNER`).** Set `MCP_STUDIO_OWNER` as a per-deployment
   environment variable (never a Workspace Secret, so test and production deployments stay
   isolated) containing `{"user_id":"...","organization_id":"...","workspace_id":"..."}` with no
   `origin` field. `StudioMcpReadVerifier` accepts only `CallerKind.USER` answers from
   `GET /v1/users/me` (`allow_stale=False`) matching all three UUIDs, rejects `x-api-key` headers
   and API-key callers, and falls back to `DenyReadAccess` (`401`) when the binding is missing or
   malformed.
4. **Outside access switch (`/settings/access`).** Off by default. Turning it on requires the bound
   owner's browser session (`AUDIO_STUDIO_OWNER`, which includes `origin`). While off, every
   `/api/mcp` request — including `initialize` and `tools/list` — answers `403 agent_access_disabled`
   with `Cache-Control: no-store` before any verifier runs.
5. **MCP account grant (`/settings/mcp`).** Empty by default and stored in `mcp_settings`
   independently of chat mail grants. Select only the active mail accounts an MCP client may read.
   Deleting an account removes it from the MCP grant in the same transaction.

Rollout rule: verify the endpoint against a dedicated test deployment with synthetic seed data first
before enabling it on a production deployment.

## `/api/v1` and MCP sign-in tokens

Live probing (#157, Q4 / Q7) showed that a Studio MCP sign-in token arrives at the app in
`x-apps-token` and answers `GET /v1/users/me` with `api_key: null` and the owner's user,
organization, and workspace IDs — identical to the owner's browser session. The Studio gateway sets
no header name that distinguishes MCP traffic from a browser session (only client-set headers such
as `mcp-*` or `sec-fetch-*` differ, and any caller can forge or omit those).

Because no cheap, reliable app-side signal separates an MCP token from the owner's browser on
`/api/v1`, liam-e explicitly relies on the Studio gateway's route scoping (#157, Q2, proven live on
26–27 November 2026): tokens issued for `{"path": "/api/mcp", "mcp": true}` receive
`403 insufficient_scope` from the gateway on all unflagged `/api` routes. Never add `"mcp": true`
to `/api`, `/api/v1`, or any mutating route.

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

## Owner setup checklist (`liam-e-test` first, then production)

Configure the three per-deployment environment variables in the Studio app's **deployment
environment** (not Workspace Secrets, so test and production apps do not share bindings). Never
commit real values, IDs, or hostnames to git.

1. **`AUDIO_STUDIO_OWNER`** (required so `/settings/access` allows turning outside access on from
   your browser):
   ```json
   {"user_id":"<user-id>","organization_id":"<organization-id>","workspace_id":"<workspace-id>","origin":"https://<app-host>"}
   ```
   Source: your Mistral user, organization, and workspace UUIDs (from Studio or `GET /v1/users/me`),
   plus the deployment's exact `https://<app-host>` origin.
2. **`MCP_STUDIO_OWNER`** (activates `StudioMcpReadVerifier` on `/api/mcp`; intentionally separate
   from `AUDIO_STUDIO_OWNER` and has no `origin` field):
   ```json
   {"user_id":"<user-id>","organization_id":"<organization-id>","workspace_id":"<workspace-id>"}
   ```
   Source: the same three Mistral UUIDs (no `origin`).
3. **`MCP_ALLOWED_HOSTS`** (prevents `421` / `mcp_transport=host_rejected` behind the gateway):
   ```text
   <app-host>
   ```
   Source: the exact `Host` header value forwarded to the API for this deployment. You may omit
   this only if `CORS_ORIGIN` or `MAIL_OAUTH_REDIRECT_BASE_URL` is already set to
   `https://<app-host>`.
4. **Deploy / restart** the `liam-e-test` app (seeded with synthetic data) so the environment
   variables take effect.
5. **Select a test mail account:** open `/settings/mcp`, check one synthetic test account, and save.
6. **Enable outside access:** open `/settings/access`, turn on "Zugriff von außen" / "Outside
   access", and confirm the notice about missing assistant sign-in disappears
   (`verifier_configured=true`).
7. **Configure Vibe Code** with the test deployment's URL:
   ```toml
   [[mcp_servers]]
   name = "liam-e"
   transport = "streamable-http"
   url = "https://<app-host>/api/mcp"

   [mcp_servers.auth]
   type = "oauth"
   ```
   Complete the Studio OAuth sign-in in Vibe Code. Note from #157: Studio's protected-resource
   metadata currently advertises the app origin rather than `/api/mcp` (`resource_relation=origin`),
   and the authorization server completes sign-in when the client sends the resource named in that
   metadata.
8. **Run the live smoke on `liam-e-test`:**
   - Confirm `tools/list` returns only the four tools above.
   - Confirm `liam_mail_accounts`, `liam_mail_summary`, `liam_mail_search`, and `liam_mail_read`
     return only the granted test account's indexed inbox mail.
   - Turn off "Zugriff von außen" in `/settings/access` and confirm the next MCP call immediately
     fails with `403 agent_access_disabled`.
9. **Production rollout:** only after the `liam-e-test` smoke passes, repeat steps 1–6 on the
   production deployment with production's own `origin` and `<app-host>`.

## Revoking access

- **Remove individual accounts:** uncheck accounts in `/settings/mcp` and save. The reduced grant
  applies on the next MCP call; an empty grant returns empty results.
- **Immediate app-side stop:** open `/settings/access` and turn off outside access. Every new
  request across all app instances immediately receives `403 agent_access_disabled`.
- **Disable the verifier:** unset `MCP_STUDIO_OWNER` on the deployment and restart. `DenyReadAccess`
  becomes active again and returns `401` to everyone.
- **Accepted gateway limitation (#157, L9; owner decision 28 November 2026):** Mistral Studio
  currently exposes no OAuth token revocation endpoint (RFC 7009) or client-management UI
  (RFC 7592). Turning off access inside liam-e stops the app from serving data, but issued OAuth
  tokens and refresh grants stay valid at the Studio gateway until the Studio app itself is
  deleted. Turning outside access back on allows previously issued grants again, and mail text
  already read by a client cannot be recalled.

## Transport settings

All settings live in `packages/py/env/src/env/mcp.py` and are declared empty in `.env.example`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MCP_STUDIO_OWNER` | `""` | Per-deployment JSON owner binding (`user_id`, `organization_id`, `workspace_id`, no `origin`). Plain env var (not `WorkspaceSecret`). When unset or malformed, `DenyReadAccess` stays bound (`401`). |
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
| `StudioMcpReadVerifier` & flagged route | `MCP_STUDIO_OWNER` + `mistral.json` (`{"path": "/api/mcp", "mcp": true}`) | Verified offline end-to-end with mocked `/v1/users/me` (`apps/api/tests/test_mcp_verifier.py`) |
| FastAPI / Starlette | `0.162.5` / `1.10.0` | Verified offline |
| Studio flagged-route sign-in (#157) | Probe run of 26–27 Nov 2026; owner decision 28 Nov 2026 | Route scoping & kill-switch proven on synthetic probe app; owner accepted app-side switch as replacement for per-grant revocation with residual risks |
| Vibe Code OAuth live smoke | `3.1.2` (recorded offline) | **Not checked** (pending live smoke on `liam-e-test`) |
| Vibe Work custom connector | — | **Not checked** |
| Studio HTTP connector import | — | **Not checked** (forbidden for full OpenAPI; see `AGENTS.md`) |
| Production deployment | — | **Not checked** (`liam-e-test` smoke first) |
