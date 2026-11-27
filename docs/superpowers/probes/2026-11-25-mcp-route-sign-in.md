# Studio MCP-route sign-in (#157, prepared 2026-11-25)

Status: **OPEN. The live run is not approved.** This file is the runbook and the result skeleton for
#157 (parent #36). The offline part is in the repository: the probe tool, the synthetic probe app
and their tests. Nothing has been deployed and nobody has signed in. The owner decided on
25 November 2026 that the live run needs a **separate, explicit approval** after the offline part
is merged. Until then every result cell below stays empty.

## Why this probe exists

#34 (`2026-11-18-gateway-zugang.md`) found two ways through the Studio app gateway: the owner's
browser session and a workspace API key in `x-api-key`. Both reach every route of an app, so #34
ended BLOCKED. #36 (native MCP transport) and #54 (external clients) stay blocked until a credential
is proven to reach only the MCP route.

The Mistral CLI `v0.18.1` accepts app routes declared as `{"path": "/mcp", "mcp": true}`. On deploy
it prints an "MCP server" URL for each flagged route. Its local dev gateway turns on OAuth client
authentication for these routes: Protected Resource Metadata, a bearer challenge, authorization
server metadata, dynamic client registration, PKCE and a workspace membership check. Public Mistral
documentation does not describe the flag. #34 could not test any of this, because liam-e declares no
flagged route. This probe answers whether a token from that sign-in reaches only the flagged route,
which identity the app sees, and whether revocation takes effect. It uses a synthetic app only and
never touches liam-e's data.

## What is in the repository

| Part | Path | Role |
| --- | --- | --- |
| Probe tool | `tools/mcp_route_probe.py` | Offline by default. With `--live` it runs the matrix below against the probe app and prints one redacted line per case and a verdict. |
| Probe app | `tools/probes/mcp_route_app/` | A standalone Starlette app, not part of the uv workspace. Unflagged `/api` routes (health, whoami, an in-memory counter and write route) and a test-only MCP stub on the flagged routes `/mcp` and `/api/mcp`. It holds no data and calls no provider or model. |
| Manifest | `tools/probes/mcp_route_app/mistral.json` | One module with the routes `/api`, `/mcp` (`"mcp": true`) and `/api/mcp` (`"mcp": true`). No `deployment` block and no IDs. |
| Deploy helper | `tools/probes/mcp_route_app/deploy.sh` | Exports the probe directory at a commit on `origin/main` into a temporary copy, writes the deployment block into that copy only and runs `mistral deploy --from-disk`. A first deploy needs `--create-app liam-e-mcp-route-probe`, checks the target workspace read-only for an app of that name and passes the name to the CLI. Refuses liam-e's apps. Also deletes the probe app. |
| Tests | `apps/api/tests/test_probe_mcp_route.py`, `apps/api/tests/test_probe_mcp_route_app.py`, `apps/api/tests/test_probe_mcp_route_deploy.py`, `tests/test_public_repo_hygiene.py` | Offline tests for the tool, the app and the manifest rules, and the deploy helper against a fake CLI. |

The MCP stub answers both protocol eras that #36 must support (owner decision D6). The handshake
era (`2025-11-25` and older) uses `initialize`. The per-request era (`2026-07-28`) uses
`MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` on every request. The stub is not the #36
transport.

The whoami route and the `probe_whoami` tool report:

- which header carries the caller token;
- whether `Authorization`, `x-api-key` or a cookie arrived;
- the identity kind that `GET {proxy}/v1/users/me` returns: `user`, `api_key`, `unknown`,
  `lookup_rejected`, `lookup_failed`, `no_token` or `no_proxy`;
- the user and workspace as salted fingerprints of 12 hexadecimal characters.

The salt is random per process and is never stored. A fingerprint therefore matches only within the
same `instance`, which the report also names. The reports never contain an ID, a token or a header
value.

## Approvals the live run needs

The owner approves each line explicitly, by name, before it runs. An approval for one line does not
cover another. All live steps need the merged offline PR first.

| # | Step | Effect | Who |
| --- | --- | --- | --- |
| L1 | Deploy the synthetic app `liam-e-mcp-route-probe` into one named workspace | Creates one new Studio app from public, synthetic code. liam-e is not changed. | Owner's CLI login |
| L2 | Owner sign-in through the probe's flagged route | Registers one OAuth client named "liam-e MCP route probe (test-only)" and issues a token for the owner | Owner, in their browser |
| L3 | Run the probe matrix with that token against the probe app | Calls only the probe app's routes; one write to the in-memory counter | Owner, or a worker session with the owner present |
| L4 | Optional Q3: send the token once to `GET https://api.mistral.ai/v1/models` (`--q3-mistral-api`) | Read-only. If the API accepts the token, it lists models, and the result is BLOCKED. | Owner |
| L5 | Optional Q6: revoke the probe's grant during the run (`--revocation-step`) | Ends the probe's access. If this is skipped, the verdict cannot exceed INCOMPLETE. | Owner |
| L6 | Q8: add the probe's `/mcp` route to Vibe Code with OAuth, sign in, call the probe tools, remove the entry | A second OAuth client and token for the owner | Owner |
| L7 | Optional Q5: a second workspace member signs in through the probe | Only if such an account exists | That member |
| L8 | Optional: import the **probe app** (never liam-e) as a Studio connector, or try Vibe Work | Exposes only the probe's three read tools | Owner |
| L9 | Delete the probe app (`deploy.sh --delete`) and revoke any remaining grants | Removes the app; on a terminal the CLI asks for its name first, and off a terminal it refuses | Owner |
| L10 | Commit the redacted results in this file in a follow-up PR with "Closes #157" | Public repository change | Worker, owner merges |

## Live steps

Run every command from a checkout of `origin/main` that contains the merged offline PR. Each step
names its approval. Stop at the first stop condition.

### 0. Preconditions (no approval needed)

1. Record the versions: `mistral --version`, `vibe --version` and the commit of the checkout.
   Offline on 2026-11-25 these were `mistral v0.18.1` and `vibe 3.1.2`.
2. Check that the CLI is logged in as the owner: `mistral login` if needed. Do not copy its output
   into this file.
3. Run the offline gates once: `bash tools/uv.sh run --no-sync pytest apps/api/tests/test_probe_mcp_route.py apps/api/tests/test_probe_mcp_route_app.py apps/api/tests/test_probe_mcp_route_deploy.py tests/test_public_repo_hygiene.py -q`.
4. `bash tools/uv.sh run --no-sync python tools/mcp_route_probe.py` (without `--live`) prints the
   case list and sends nothing.

### 1. Deployment file (part of L1)

Copy `tools/probes/mcp_route_app/mistral.deployment.example.json` to
`tools/probes/mcp_route_app/mistral.deployment.json`. Fill in the target `organizationId` and
`workspaceId` as lowercase UUIDs. Git ignores the file, and so does the image build
(`**/mistral.deployment.json` in `.dockerignore`). Never use liam-e's `appId`: the helper refuses it.

The helper refuses every `appId` that a liam-e deployment file names: `mistral.deployment.json` at
the checkout root, the file `LIAM_E_DEPLOYMENT_FILE` names, and each file in the colon-separated
list `MCP_ROUTE_PROBE_LIAM_E_FILES`. Name the deployment files of all of liam-e's apps there, for
example its test and production apps. The helper stops if none of these files names an `appId`.

### 2. Deploy (L1)

```bash
bash tools/probes/mcp_route_app/deploy.sh <merged-commit> --create-app liam-e-mcp-route-probe
```

The CLI (`v0.18.1`) creates a new app without asking. Without `--name` it names the app after the
directory it runs in. The confirmation therefore happens in the helper, before the CLI runs:

1. While the deployment file holds no `appId`, the helper refuses to deploy unless
   `--create-app liam-e-mcp-route-probe` is given, spelled exactly.
2. It lists the target workspace read-only (`mistral apps list --json`) and refuses if an app named
   `liam-e-mcp-route-probe` already exists there. If that is the probe app from an earlier run,
   record its `appId` in the deployment file and deploy without `--create-app`.
3. It runs `mistral deploy --from-disk --name liam-e-mcp-route-probe` with the organization and
   workspace from the deployment file.
4. It writes the new `appId` into the deployment file without printing it, even if the build fails
   after the app was created. Then it lists the workspace again and fails unless the new app is the
   only app named `liam-e-mcp-route-probe`.

Later runs update the recorded app. They refuse `--create-app` and need the recorded app to be the
only app of that name in the workspace.

Record without URLs: whether the CLI accepted the manifest, whether it printed an "MCP server" line
for `/mcp` and for `/api/mcp`, and the text of any lint warning.

Stop and report if any of these happens:

- the helper refuses before the CLI runs, for example because an app of the probe's name exists;
- the listing after the deploy does not show the new app as the only app of the probe's name;
- the CLI rejects the manifest or the `"mcp": true` flag;
- `--from-disk` refuses the exported copy, which has no Git metadata;
- the build fails because of the module path `"."`, the build context or the start command's
  working directory;
- the deploy asks for broader rights than creating one app.

### 3. Browser reading (part of L3)

The owner opens these probe routes in their browser, signed in to Studio as usual, and keeps the
readings locally, outside the repository:

- `<app>/api/health`;
- `<app>/api/probe/whoami`: `instance`, `token_header`, `identity` and both fingerprints;
- `<app>/api/probe/counter`;
- `<app>/api/mcp` and `<app>/mcp` in the address bar: the status the browser session gets on a
  flagged route.

### 4. Probe matrix (L2, L3, optionally L4 and L5)

```bash
read -rs MCP_ROUTE_PROBE_URL && export MCP_ROUTE_PROBE_URL   # the probe app's https base URL, no path
bash tools/uv.sh run --no-sync python tools/mcp_route_probe.py --live --q3-mistral-api --revocation-step
unset MCP_ROUTE_PROBE_URL
```

Leave out `--q3-mistral-api` or `--revocation-step` if L4 or L5 is not approved.

- The tool checks discovery, registers a client and opens the browser for the owner's sign-in. The
  loopback callback listens only on `127.0.0.1` and waits at most 300 seconds.
- The callback takes the first GET or POST on any path except `/favicon.ico`, so a redirect to
  `/callback/` or an answer posted as a form (`response_mode=form_post`) still arrives. It reads
  `code`, `state`, `error` and `iss` from the query or the form body. The `q1.sign_in` line names
  the `method`, the `path` class (`exact`, `trailing_slash` or `other`), the `source` of the
  parameters (`query`, `form` or `none`) and their sorted names (`params`), never a value. The code
  exchange always sends the registered redirect URI.
- If no browser opens, `--show-sign-in-url` prints the sign-in URL to stderr. Never copy that URL
  anywhere.
- At the revocation pause, the owner revokes the probe's grant wherever Studio or the Mistral
  console offers that, and records where it was. If no such place exists, record "none found" and
  press Enter. The next call then shows whether the token still works.

The tool prints one line per case and ends with `verdict: GO-CANDIDATE`, `INCOMPLETE` or `BLOCKED`
plus fixed reasons. The lines hold statuses, fixed codes, field names, booleans, scope names and
time buckets. The `q4.whoami` and `q7.whoami` lines also show `instance` and the fingerprints, for
the comparison in step 5. Three sign-in fields help when no token arrives:

- `q1.prm` and `q7.prm`: `resource_relation` says how the resource the metadata names relates to
  the route URL: `equal` (a trailing slash aside), `origin`, `parent`, `other_path`, `other_host`
  (another scheme, host or port) or `absent`.
- `q1.register`: `registration_redirect_echoed` is `yes` if the registration answer lists the
  redirect URI unchanged, `no` if it lists others, and `absent` if it lists none.
- `stop`: `sign_in_error` if the callback carried `error`, `no_token` for any other sign-in without
  a token, and `no_advertised_resource` (see step 4a). `error` shows a fixed code such as
  `access_denied` or `invalid_target`, or `other`.

### 4a. Third attempt: send the metadata's resource

By default the sign-in and token requests send `resource=<app>/mcp`. With
`--resource-from-metadata` they send, verbatim, the resource that the `/mcp` route's
protected-resource metadata names. This applies to the authorization request, the code exchange
and the refresh grant. The third attempt runs step 4 with that flag:

```bash
read -rs MCP_ROUTE_PROBE_URL && export MCP_ROUTE_PROBE_URL   # the probe app's https base URL, no path
bash tools/uv.sh run --no-sync python tools/mcp_route_probe.py --live --q3-mistral-api --revocation-step \
  --resource-from-metadata
unset MCP_ROUTE_PROBE_URL
```

It tests the main candidate from the first two attempts (see "Findings before the third attempt"):
the authorization server may look up the app by the `resource` parameter and fail after consent.

- The attempt registers one more client. It needs the owner's go like every attempt, and step 8
  revokes the grants of all probe clients.
- If the metadata names no https resource, the tool stops with `stop: no_advertised_resource`
  before it registers a client.
- Without the flag nothing changes; the `q1.sign_in` line then shows `resource_sent=route`.

What the lines show, and what it means:

| Case | Lines | Meaning |
| --- | --- | --- |
| Token received | `Q1 sign-in: callback state_matches=yes code=yes error=- … resource_sent=metadata`, then `Q1 code exchange: 200 … bearer=yes`; the matrix runs and ends with a verdict as in step 4 | The authorization server accepts the metadata's resource, where `<app>/mcp` failed twice. The earlier attempts most likely failed on the `resource` parameter. #36 has to send the value its metadata names. |
| Sign-in error | `Q1 sign-in: callback … error=<code> … resource_sent=metadata`, then `stop: sign_in_error`; the verdict is `INCOMPLETE` with the reason `sign_in_error` | The redirect now reaches the probe, but the server refuses. `invalid_target` means it rejects the resource (RFC 8707), `access_denied` that consent was declined; record the code. |
| Still a timeout | `Q1 sign-in: timeout resource_sent=metadata`, then `stop: no_token` | No answer reached the probe with either value, so the resource is not the only cause. Record where the browser ended up (the loopback address or a Mistral address, no URL) and do not repeat the attempt without a new diagnosis. |

A sign-in that reaches the callback but whose code exchange fails shows `Q1 code exchange: 400
error=<code>` and then `stop: no_token`. Record that code as well.

### 5. Fingerprint comparison (part of L3)

Compare `user_fingerprint` and `workspace_fingerprint` from `q4.whoami` with the browser reading
from step 3. The comparison counts only when both readings show the same `instance`. If the
instance differs, Studio served another process: repeat step 3 and compare again. Record only
"same instance yes/no" and "user fingerprint matches yes/no", never the values.

### 6. Vibe Code (L6, Q8)

Add the probe's `/mcp` route to Vibe Code as a Streamable HTTP server with OAuth (`vibe mcp add`;
see its `--help` in the recorded version). Sign in, list the tools and call `probe_whoami` and
`probe_sse`. Record:

- the Vibe version;
- whether the sign-in completed;
- whether the three tools were listed;
- the `identity` and `protocol_header` that `probe_whoami` returned.

Then remove the entry again. If Vibe Code cannot sign in, record the step at which it failed, using
fixed text only.

### 7. Optional checks (L7, L8)

- **Q5**, only if a second workspace member exists: that member runs step 4 with only `--live`.
  Record whether a token was issued, the identity kind, and whether the fingerprint differs from the
  owner's in the same instance.
- **Studio connector or Vibe Work**, only with L8: the probe app only. Record whether the tools
  appeared and whether a call reached the stub.

### 8. Cleanup (L9)

```bash
bash tools/probes/mcp_route_app/deploy.sh --delete
```

The helper first checks that the recorded app is listed under the probe's name. The CLI then asks
for the app's name before it deletes anything; the helper never passes `--yes`, so this step needs
a terminal and refuses without one. Then:

1. Check that `mistral apps list` no longer shows `liam-e-mcp-route-probe`, and record yes/no.
2. Revoke any OAuth grants that remain for the probe's clients and for the Vibe entry.
3. Unset `MCP_ROUTE_PROBE_URL`.
4. Keep the deployment file: it now holds no `appId`.

### 9. Record (L10)

Fill in the tables below. Change the status line to GO, INCOMPLETE or BLOCKED with the date of the
run. Run `tests/test_public_repo_hygiene.py` before committing. The follow-up PR says "Closes #157".

## Findings before the third attempt

From the two sign-in attempts of 26 November 2026, both with the probe's default
`resource=<app>/mcp`. These are findings for #157; the result matrix below stays open until the
run is complete.

- **The metadata's resource does not match the route.** On both flagged routes, `/mcp` and
  `/api/mcp`, the tool fetched the protected-resource metadata from the URL in the bearer challenge
  (`source=challenge`), on the app's own host (`same_host=yes`). The `resource` it names was not the
  route URL, even ignoring a trailing slash (`resource_matches=no`, in both attempts). RFC 9728,
  Section 3.3, requires that value to be identical to the URL the client used for the request when
  the metadata came from the challenge; otherwise the client must not use the metadata. A client
  that follows RFC 9728 therefore cannot use Studio's metadata for these routes. How the two differ
  was not recorded; the tool now prints it as `resource_relation`.
- **The sign-in failed before the redirect.** Both attempts ended with `Q1 sign-in: timeout` and
  `stop: no_token`. After the owner's consent in the second attempt, a "Not Found" page appeared on
  a Mistral address, not on the loopback address. The authorization server therefore did not send
  the browser back to the probe at all. The main candidate is that it looks up the app by the
  `resource` parameter and finds nothing for `<app>/mcp`. Step 4a tests this.

## Result matrix

Fill in after the live run. "Executed" means the probe or the owner observed it in this run;
"assumed" marks anything inferred.

| # | Question | Probe lines | GO needs | Result | Executed or assumed |
| --- | --- | --- | --- | --- | --- |
| Q1 | Discovery: 401 with `resource_metadata` on a flagged route; PRM and authorization server metadata (`code_challenge_methods_supported`, `registration_endpoint`, `client_id_metadata_document_supported`, scopes) | `q1.challenge`, `q1.prm`, `q1.as`, `q1.register`, `q1.sign_in`, `q1.token` | `S256` present | | |
| Q2 | Route binding: does the flagged-route token reach the unflagged write route? | `q2.baseline`, `q2.write`, `q2.whoami`, `q2.health`, `q2.before`, `q2.after`, `q2.effect` | rejected by the gateway, counter unchanged | | |
| Q3 | Resource binding: is the token accepted outside the flagged route or the app? | `q7.tools`, `q7.whoami`, `q3.mistral_api` | documented; acceptance outside the app is BLOCKED | | |
| Q4 | Identity: token header, user rather than API key, owner fingerprint, MCP traffic distinguishable from a browser session | `q4.whoami`, browser reading, step 5 | owner verifiable | | |
| Q5 | Can another workspace member obtain a token? | step 7 | documented | | |
| Q6 | Expiry, refresh and revocation: where the owner revokes, and whether the next call fails | `q1.token` (`expires_in` bucket), `q6.refresh`, `q6.refreshed_call`, `q6.revoked`, `q6.revoked_original`, `q6.revoked_refresh` | revocation blocks the next call | | |
| Q7 | Prefix overlap: flagged `/api/mcp` under unflagged `/api`, and which rule wins | `q7.challenge`, `q7.prm`, `q7.tools`, `q7.whoami`, browser reading | documented; decides the #36 endpoint path | | |
| Q8 | Clients: the probe's own client and Vibe Code with OAuth (version); connector and Vibe Work optional | `q8.tools`, `q8.initialize`, `q8.initialized`, `q8.handshake_tools`, step 6 | Vibe Code reaches the stub | | |
| Q9 | Pass-through of `MCP-Protocol-Version`, JSON and SSE answers, 405 for GET | `q9.discover`, `q9.sse`, `q9.mismatch`, `q9.unsupported`, `q9.get` | no interference | | |

### Verdict rules

- **BLOCKED** if Q1 lacks `S256`, or Q2 is accepted or the counter changed. The same holds if the
  token works outside the probe app (Q3), Q4 shows no verifiable owner as a `user` identity, or
  revocation does not block the next call (Q6). The tool reports these as fixed reasons. Record the
  concrete finding and return to the owner with the fallback options from #157: a relay outside
  Studio, hardening the normal routes to the owner's browser session, or waiting.
- **GO** only if the tool reports `GO-CANDIDATE`, the fingerprints match in the same instance, Q7
  is documented, Vibe Code reaches the stub (Q8), and Q9 shows no interference.
- **INCOMPLETE** in every other case, for example a skipped Q3 or Q6, an unclear status, or a
  client that failed without a security finding. Name what is missing.

A GO is a probe result, not a released capability: #36 starts only after it.

### Environment of the run

| Part | Version or state |
| --- | --- |
| Mistral CLI | |
| Vibe Code | |
| Probe commit | |
| Probe app visibility | |
| Date and time of the run (Europe/Berlin) | |

### Probe output

Paste the tool's stdout lines here after redaction. Replace the values of `instance`,
`user_fingerprint` and `workspace_fingerprint` with `<redacted>`. Keep the "same instance" and
"fingerprint matches" results from step 5 below the block.

```text
(not run)
```

## Assumptions checked offline only

These come from the installed CLI `v0.18.1` and from reading its local dev gateway. None of them
has been confirmed against Studio:

- The CLI's module schema and lint accept the probe manifest, including `"mcp": true`, the module
  path `"."`, the `build` block and a start command that uses `$PORT`.
- `mistral deploy --from-disk` accepts a directory without Git metadata, and on first deploy
  writes the deployment block with the new `appId` into the copy's `mistral.json`. If it does not,
  the helper stops and asks to record the `appId` by hand.
- A first deploy creates the app without a confirmation prompt; the CLI asks only when the plan's
  app limit is reached. The app's name is `--name` or, without it, the deploy directory's name in
  lowercase. `--yes` on `deploy` only lets the CLI commit and push `mistral.json` without asking.
- `mistral apps list --json` returns the organization, the workspace and an `apps` list whose
  entries carry `id`, `name` and `label`. The helper's pre-check and post-check read that shape.
- What `--name` does when an app of that name already exists is unknown. The helper's pre-check
  keeps that case from happening.
- The runtime starts the container from `/srv` with the probe's start command (`--app-dir /srv`
  protects against another working directory). It also injects the gateway proxy URL as for liam-e
  (`__APPS_PROXY_URL` or `__SPACES_PROXY_URL`).
- The gateway in Studio behaves like the CLI's dev gateway for flagged routes. Whether it forwards
  the caller token as `x-apps-token` or `x-space-token`, and whether it passes `MCP-Protocol-Version`,
  `Mcp-Method` and SSE through unchanged, is exactly what Q4 and Q9 test.

Not tested by design: sending the probe's token to liam-e or to any other app. That would be a live
call to liam-e, which #157 excludes. Q3 covers the platform API through `--q3-mistral-api` and the
second flagged route through `/api/mcp`.

## Never in this file

This repository is public (AGENTS.md, "Publication and private data"). Never record any of these:

- URLs: app, gateway, MCP server, sign-in, redirect or metadata hosts;
- organization, workspace, app or client IDs;
- tokens, authorization codes, PKCE values, fingerprints or the `instance` value;
- names, email addresses, response bodies, or the CLI's target and URL output.

Record statuses, fixed codes, field names, booleans, scope names, time buckets and versions only.
