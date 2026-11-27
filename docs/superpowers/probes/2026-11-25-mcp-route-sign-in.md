# Studio MCP-route sign-in (#157, prepared 2026-11-25)

Status: **INCOMPLETE (live run of 26 and 27 November 2026).** This file is the runbook and the
result record for #157 (parent #36). The sign-in through the flagged route works once the client
sends the resource that Studio's metadata names. The gateway keeps the token away from the app's
unflagged routes, and the Mistral API rejects it. The owner found no way to revoke it, so the
revocation check could not run. L6 (Vibe Code), L7, L8 and L9 have not run. "Result of the run"
below has the verdict; the steps are kept as they were run. On 27 November the owner chose to test
whether deleting the probe app ends its tokens (L9, step 8); that test has not run yet.

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
| Probe tool | `tools/mcp_route_probe.py` | Offline by default. With `--live` it runs the matrix below against the probe app and prints one redacted line per case and a verdict. With `--live --kill-switch-step` it runs the L9 kill-switch test of step 8 instead. |
| Probe app | `tools/probes/mcp_route_app/` | A standalone Starlette app, not part of the uv workspace. Unflagged `/api` routes (health, whoami, an in-memory counter and write route) and a test-only MCP stub on the flagged routes `/mcp` and `/api/mcp`. It holds no data and calls no provider or model. |
| Manifest | `tools/probes/mcp_route_app/mistral.json` | One module with the routes `/api`, `/mcp` (`"mcp": true`) and `/api/mcp` (`"mcp": true`). No `deployment` block and no IDs. |
| Deploy helper | `tools/probes/mcp_route_app/deploy.sh` | Exports the probe directory at a commit on `origin/main` into a temporary copy, writes the deployment block into that copy only and runs `mistral deploy --from-disk`. A first deploy needs `--create-app liam-e-mcp-route-probe`, checks the target workspace read-only for an app of that name and passes the name to the CLI. Refuses liam-e's apps. `--delete` checks the probe app as a dry run, and `--delete --apply` deletes it (step 8). |
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
| L9 | Kill-switch test and cleanup (step 8): one more sign-in with `--kill-switch-step`; at the pause the worker deletes the probe app with `deploy.sh --delete --apply`; the probe checks whether calls and refresh still work | Registers a fourth probe client and issues a token, then removes the app. The probe clients stay registered: there is no client management (RFC 7592). | Owner consents in the browser and gives the go |
| L10 | Commit the redacted results in this file in a follow-up PR | Public repository change | Worker, owner merges |

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
  press Enter. The next call then shows whether the token still works. The tool cannot tell
  "nothing revoked" from "revoked, still works" and reports `revocation_not_effective` in both
  cases; "Result of the run" explains how the run of 27 November reads it.

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

- The attempt registers one more client. It needs the owner's go like every attempt. Step 8 cleans
  up after all probe clients.
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

### 8. Kill-switch test and cleanup (L9)

Owner decision of 27 November 2026: L9 tests whether deleting the probe app works as an emergency
stop for the probe's tokens, and deletes the app with it. There is no support request to Mistral,
and L6 stays unapproved. L9 needs the merged PR that adds `--kill-switch-step` and the guarded
delete, and the owner's explicit go.

Before the run:

1. Put the probe checkout at that merge commit, detached and clean. The commit changes `deploy.sh`
   only, not the served app, so the app is not deployed again.
2. Use a fresh `TMPDIR` with mode `0700` outside every Git checkout (#190), and set
   `MCP_ROUTE_PROBE_URL` as in step 4.
3. Run the dry run, which changes nothing:

   ```bash
   bash tools/probes/mcp_route_app/deploy.sh --delete
   ```

   It must end with "Nothing was deleted; run again with --apply to delete it." Otherwise stop.

The run:

1. Start the probe. It signs in as in step 4a, which registers a fourth probe client; the owner
   consents in the browser.

   ```bash
   bash tools/uv.sh run --no-sync python tools/mcp_route_probe.py --live --kill-switch-step --resource-from-metadata
   ```

   Before it pauses, it runs the baseline `k0`: `tools/list`, a refresh grant, and `tools/list` with
   the refreshed token. If one of them fails, it stops with `baseline_failed` and does not pause.
2. At the prompt "Delete the probe app now", run this in a second shell, then press Enter in the
   probe's shell at once:

   ```bash
   bash tools/probes/mcp_route_app/deploy.sh --delete --apply
   ```

   If the helper refuses, nothing was deleted: do not press Enter. End the probe with Ctrl-C (its
   tokens exist only in its memory), record the helper's message and stop. If it fails after the
   CLI's delete, check once more, read-only, whether the probe app is still listed. Press Enter if
   it is gone; otherwise end the probe as above. Nothing is deleted by hand.
3. The probe repeats the three requests at once (`k1`) and again after 120 seconds (`k2`), then
   prints `kill_switch`. It refreshes with the newest refresh token, in case the server rotates
   them. This mode prints no verdict line; `kill_switch` is the result of L9.

The helper's guards, which the dry run checks as well:

- It reads the app ID only from `tools/probes/mcp_route_app/mistral.deployment.json` and refuses
  `MCP_ROUTE_PROBE_DEPLOYMENT_FILE`.
- liam-e's config directory (`$XDG_CONFIG_HOME/liam-e`, by default `$HOME/.config/liam-e`) must name
  at least one app ID, and the recorded ID must be none of them or of liam-e's other deployment
  files.
- The workspace listing must show the recorded app under the exact name `liam-e-mcp-route-probe`,
  and no other app may carry that name or label.
- With `--apply` it runs `mistral apps delete --yes`, because the helper has already asked and the
  CLI needs `--yes` off a terminal. It keeps the CLI's output to itself and lists the workspace
  again. It removes the `appId` from the deployment file only once no app of the probe's ID or
  name is listed. Then it checks that the liam-e apps listed before are still listed unchanged
  (name, label, `updatedAt`). It prints counts, never IDs.

How to read the result: a request counts as stopped with 400, 401, 403, 404, 410 or a connection
error. A stopped call counts only while the access token is within its own `expires_in`, less 10
seconds (`within_lifetime=yes`).

| `kill_switch` | When | Meaning |
| --- | --- | --- |
| `effective` | After the delay, no request returned 2xx, the refresh grant was stopped, and the call was stopped within the token's lifetime. Also when this held right after the deletion and only the token's expiry blurs the second check. | Deleting the app ends its tokens and grant. `immediate=not_effective` means that it took effect only within the 120 seconds. |
| `not_effective` | After the delay, a call or the refresh grant still returned 2xx (`reasons`: `call_accepted`, `refresh_accepted`, `refreshed_call_accepted`) | Deleting the app does not end a leaked token or grant. |
| `unclear` | Anything else, for example a 5xx or a timeout (`call_unclear`, `refresh_unclear`), an expired or unknown lifetime (`access_token_expired`, `lifetime_unknown`), or a stop before the pause | No conclusion; record the reasons. |

Afterwards:

1. Record yes/no: the helper's listing check passed, so no app named `liam-e-mcp-route-probe`
   remains and liam-e's apps are listed unchanged.
2. Record that the probe clients stay registered. The authorization server offers no client
   management (RFC 7592), and the owner found no place to remove them. That makes four clients:
   one from each of the three attempts and one from L9.
3. Unset `MCP_ROUTE_PROBE_URL` and remove the temporary directory.
4. Keep the deployment file: it now holds no `appId`.

### 9. Record (L10)

Fill in the tables below. Change the status line to GO, INCOMPLETE or BLOCKED with the date of the
run. Run `tests/test_public_repo_hygiene.py` before committing. A PR that records a complete run
says "Closes #157"; a partial record, like the one of 27 November, says "Refs #157".

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
  `resource` parameter and finds nothing for `<app>/mcp`. The third attempt (step 4a) bore this
  out: with the metadata's value the sign-in completed (see "Result of the run").

## Result of the run (26 and 27 November 2026)

### Attempts

| Attempt | Time (Europe/Berlin) | `resource` sent | Outcome |
| --- | --- | --- | --- |
| 1 | 26 November, 23:32 to 23:37 | the route URL `<app>/mcp` | `Q1 sign-in: timeout`, `stop: no_token` |
| 2 | 27 November, 05:47 to 05:52 | the route URL `<app>/mcp` | after consent a "Not Found" page on a Mistral address; `Q1 sign-in: timeout`, `stop: no_token` |
| 3 | 27 November, 06:42 to 06:48 | the metadata's resource (`--resource-from-metadata`) | callback and code exchange succeeded; the matrix ran with L4 and L5 |

The cause of the first two failures was the `resource` parameter. Studio's protected-resource
metadata names the app's origin, not the route (`resource_relation=origin` on `/mcp` and
`/api/mcp`, not `other_host`), which RFC 9728, Section 3.3, does not allow. The authorization
server completed the sign-in only when the request carried that origin value. Attempt 3 also ran
the wider callback of #189, but its callback arrived as a plain `GET` on the exact path, which the
earlier callback accepted too. The resource was therefore the change that mattered. Each attempt
registered one client, so three probe clients exist.

### Verdict

**INCOMPLETE.** The tool's automated verdict is `INCOMPLETE` with the reason
`revocation_not_checked` once the inapplicable revocation line is set aside (see "Automated
verdict" below). Q6 (revocation) and Q8 (Vibe Code, L6) are open. Q5 is optional and did not run.

- **Route scoping.** The gateway rejects the MCP token on the app's unflagged API routes: write,
  whoami and health all return 403 `insufficient_scope`, and the write had no effect. The
  Mistral API rejects the token too (401). The token is scoped to the app's origin, not to one
  route: it works on every flagged route (`"mcp": true`), here both `/mcp` and `/api/mcp`. No
  result of this run is bound to a single route.
- **Revocation.** The owner found no way to revoke the probe's access in the UI. The
  authorization server's metadata names no revocation endpoint (`revocation_endpoint=no`), and
  the public documentation describes none (see "Revocation: what was checked"). The refresh grant
  stayed live. The refresh token's lifetime is unknown. The three probe clients and their grant on
  the server side remain until L9, and whether L9 ends the grant is not documented.
- **Automated verdict.** The tool printed `verdict: BLOCKED reasons=revocation_not_effective`.
  That reason does not apply, because nothing was revoked. The tool cannot tell "revoked, still
  works" from "nothing to revoke": after the pause it always treats a working call or refresh as a
  failed revocation. The two `401 invalid_token` answers after the pause are read as expiry, which
  is assumed, not executed: the tool records `expires_in` only as a bucket (at most five minutes),
  and Enter came more than five minutes after both tokens were issued. The refresh grant
  still returned 200, which shows that the grant was live, not that a revocation failed. Without
  the revocation step the verdict is `INCOMPLETE revocation_not_checked`, and every other automated
  condition holds: `S256`, write rejected and without effect, unflagged reads rejected, Mistral API
  rejected, identity `user`.
- **Consequence for liam-e.** Exposing the agent read facade in production (#36, #54) stays
  blocked. The open question is no longer route scoping but an emergency stop. That could be a
  revocation on Mistral's side, deleting the app as a kill switch (its effect on grants is not
  documented), or a kill switch inside the app.

### Revocation: what was checked

Live, 27 November 2026:

- The authorization server's metadata names no revocation endpoint (`revocation_endpoint=no`).
- The owner found no revocation option in the Mistral UI; the exact places searched were not
  recorded.

Public Mistral documentation, read on 27 November 2026 without signing in. Each point is "checked,
not documented":

1. No user-facing page for connected or authorized applications in Studio, the console or Le Chat.
   Checked: the help centre (only an article on single sign-on), the Studio pages on apps (overview
   and deploy), connector management, MCP connectors in Le Chat, and the announcement of more
   control over connectors (June 2026). The connector pages cover the opposite direction, with
   Mistral as the OAuth client.
2. No revocation endpoint (RFC 7009) and no client management (RFC 7592). The app gateway's
   authorization server and its discovery metadata are not described publicly at all. The MCP
   page of the documentation covers only the workspace MCP server, with an API key as bearer and
   no OAuth or dynamic client registration.
3. Not documented: what deleting an app does to tokens and grants already issued (the deploy page,
   the CLI reference for `mistral apps delete`, managed apps), and the lifetime of the gateway's
   access and refresh tokens. Inference only, unverified: tokens probably stop working once the
   gateway no longer routes to a deleted app; whether grants survive is unknown.
4. No changelog entry (up to about 22 October 2026) and no note about the gap. Not searched: GitHub
   issue trackers and Discord.

### Headers the app receives

Both the MCP token (Q4, Q7) and the owner's browser session (step 3) reach the app as
`x-apps-token`. Neither brings an `Authorization`, `x-api-key` or cookie header to the app. With
both, the gateway also forwards:

- `x-space-token`;
- the identity headers `x-user-id`, `x-user-email`, `x-user-name`, `x-user-given-name` and
  `x-user-family-name`, the last three also with an `-encoded` variant;
- forwarding headers (`x-forwarded-*`, `x-request-id`), CDN headers and routing headers of the
  hosting platform.

Only the names were recorded. Their values, and what `x-space-token` means, were not examined. The
two kinds of request differ only in headers that the client sets itself. MCP requests carry
`mcp-method`, `mcp-name`, `mcp-protocol-version` and content headers; the browser carries
`sec-fetch-*`, `sec-ch-ua*`, `accept-language`, `priority` and `upgrade-insecure-requests`. So no
header name that the gateway sets distinguishes MCP traffic from a browser session.

### Not checked

- Any revocation, whether the grant ends on its own, and the refresh token's lifetime.
- L6 (Vibe Code, second half of Q8), L7 (Q5), L8 and L9.
- The values and meaning of the forwarded headers, including whether `x-space-token` works as a
  credential.
- Whether the browser's 405 on `GET /mcp` and `GET /api/mcp` in step 3 came from the gateway or
  from the stub. With the token, the stub answers 405 with `Allow` (Q9).
- Whether Mistral APIs other than `GET /v1/models` accept the token.
- Whether the authorization server accepts any `resource` other than the metadata's value. Only
  the route URL and that value were tried.

## Result matrix

Filled in from the run of 26 and 27 November 2026. "Executed" means the probe or the owner observed
it in this run; "assumed" marks anything inferred.

| # | Question | Probe lines | GO needs | Result | Executed or assumed |
| --- | --- | --- | --- | --- | --- |
| Q1 | Discovery: 401 with `resource_metadata` on a flagged route; PRM and authorization server metadata (`code_challenge_methods_supported`, `registration_endpoint`, `client_id_metadata_document_supported`, scopes) | `q1.challenge`, `q1.prm`, `q1.as`, `q1.register`, `q1.sign_in`, `q1.token` | `S256` present | 401 with `bearer[resource_metadata]` on both flagged routes. The metadata comes from the challenge on the app's host; `resource_relation=origin`, an RFC 9728 mismatch. Authorization server: `S256` yes, `plain` no, registration yes, CIMD no, refresh grant yes, auth method `none` yes, no revocation endpoint, scopes `openid email profile`. Registration 201 without a secret, redirect URI echoed. Sign-in only with the metadata's resource (attempt 3): callback `GET`, exact path, `code` and `state` in the query, no `iss`. Code exchange 200, `expires_in` at most 5 minutes, refresh token issued. | executed |
| Q2 | Route binding: does the flagged-route token reach the unflagged write route? | `q2.baseline`, `q2.write`, `q2.whoami`, `q2.health`, `q2.before`, `q2.after`, `q2.effect` | rejected by the gateway, counter unchanged | Without a credential `/api/health` answers 401 `session_expired`. With the MCP token, write, whoami and health answer 403 `insufficient_scope` with a bearer challenge. Counter unchanged in the same instance (`write_took_effect=no`). Meets the GO condition. | executed |
| Q3 | Resource binding: is the token accepted outside the flagged route or the app? | `q7.tools`, `q7.whoami`, `q3.mistral_api` | documented; acceptance outside the app is BLOCKED | The `/mcp` token also works on the second flagged route `/api/mcp` (`tools/list` and whoami 200): it is bound to the app's origin, not the route. The Mistral API (`GET /v1/models`) rejects it with 401. Not accepted outside the app. | executed |
| Q4 | Identity: token header, user rather than API key, owner fingerprint, MCP traffic distinguishable from a browser session | `q4.whoami`, browser reading, step 5 | owner verifiable | Token header `x-apps-token`; no `Authorization`, `x-api-key` or cookie; identity `user`. Same instance, user and workspace fingerprints as the browser reading; Q4 and Q7 match. No header name set by the gateway distinguishes MCP traffic from the browser session (see "Headers the app receives"). | executed |
| Q5 | Can another workspace member obtain a token? | step 7 | documented | Not run (L7 optional, not approved). | not run |
| Q6 | Expiry, refresh and revocation: where the owner revokes, and whether the next call fails | `q1.token` (`expires_in` bucket), `q6.refresh`, `q6.refreshed_call`, `q6.revoked`, `q6.revoked_original`, `q6.revoked_refresh` | revocation blocks the next call | Access token at most 5 minutes; refresh 200 and refreshed call 200. No revocation path found by the owner, no revocation endpoint, none documented. After the pause: both access tokens 401 `invalid_token` (expired), refresh 200, so the grant is still live and nothing was revoked. Open. | executed; revocation not performed |
| Q7 | Prefix overlap: flagged `/api/mcp` under unflagged `/api`, and which rule wins | `q7.challenge`, `q7.prm`, `q7.tools`, `q7.whoami`, browser reading | documented; decides the #36 endpoint path | `/api/mcp` under the unflagged `/api` gets the flagged-route treatment: 401 bearer challenge, its own metadata (`resource_relation=origin`), and it accepts the `/mcp` token; whoami reports `route=/api/mcp`. In the browser both flagged routes answered `GET` with 405; who answered is open. | executed; source of the browser's 405 open |
| Q8 | Clients: the probe's own client and Vibe Code with OAuth (version); connector and Vibe Work optional | `q8.tools`, `q8.initialize`, `q8.initialized`, `q8.handshake_tools`, step 6 | Vibe Code reaches the stub | The probe's own client: `tools/list` 200 with the 3 expected tools, in both eras (`initialize` 200 with `2025-11-25`, `notifications/initialized` 202, `tools/list` 200). Vibe Code not run (L6). | probe client executed; Vibe Code not run |
| Q9 | Pass-through of `MCP-Protocol-Version`, JSON and SSE answers, 405 for GET | `q9.discover`, `q9.sse`, `q9.mismatch`, `q9.unsupported`, `q9.get` | no interference | `server/discover` 200 (per-request era supported). `MCP-Protocol-Version` `2026-07-28` and `Mcp-Method` arrive at the app. SSE answer 200 as `text/event-stream`. Header mismatch 400 `-32020`, unsupported version 400 `-32022`, `GET /mcp` 405 with `Allow`. No interference. | executed |

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
| Mistral CLI | `v0.18.1` |
| Vibe Code | not run (L6) |
| Probe commit | app deployed from `ee43cd09` (#188); attempts 1 and 2 ran the tool from `ee43cd09`, attempt 3 from `a6eaf857` (#189). The probe app did not change between the two. |
| Probe app visibility | workspace |
| Date and time of the run (Europe/Berlin) | L1 on 26 November 2026, 23:22 to 23:23; sign-in attempts as listed under "Attempts"; matrix with L4 and L5 on 27 November 2026, 06:42:50 to 06:48:45 |

### Probe output

Paste the tool's stdout lines here after redaction. Replace the values of `instance`,
`user_fingerprint` and `workspace_fingerprint` with `<redacted>`. Keep the "same instance" and
"fingerprint matches" results from step 5 below the block.

```text
Q2 baseline GET /api/health, no credential: 401 content=json error=session_expired headers=set-cookie
Q1 challenge /mcp, no credential: 401 content=json error=unauthorized challenge=bearer[resource_metadata] headers=www-authenticate
Q1 resource metadata /mcp: 200 source=challenge same_host=yes fields=authorization_servers,bearer_methods_supported,resource,scopes_supported resource_relation=origin authorization_servers=1 scopes=openid+email+profile
Q1 authorization server: 200 issuer_same_host=yes issuer_matches=yes s256=yes plain=no registration_endpoint=yes cimd=no refresh_grant=yes auth_method_none=yes revocation_endpoint=no endpoints_https=yes scopes=openid+email+profile
Q7 challenge /api/mcp, no credential: 401 content=json error=unauthorized challenge=bearer[resource_metadata] headers=www-authenticate
Q7 resource metadata /api/mcp: 200 source=challenge same_host=yes fields=authorization_servers,bearer_methods_supported,resource,scopes_supported resource_relation=origin authorization_servers=1 scopes=openid+email+profile
Q1 client registration: 201 content=json headers=set-cookie client_id=yes secret_issued=no registration_redirect_echoed=yes
Q1 sign-in: callback state_matches=yes code=yes error=- duration=<=1m method=GET path=exact source=query params=code,state resource_sent=metadata
Q1 code exchange: 200 content=json headers=- bearer=yes expires_in=<=5m refresh_token=yes scope=openid+email+profile
Q9 modern server/discover: 200 content=json headers=- modern_supported=yes result_type=yes
Q8 modern tools/list: 200 content=json headers=- tools=3 expected_tools=yes
Q4 probe_whoami on /mcp: 200 content=json headers=- instance=<redacted> route=/mcp token_header=x-apps-token authorization_header=no x_api_key_header=no cookie_header=no identity=user user_fingerprint=<redacted> workspace_fingerprint=<redacted> protocol_header=2026-07-28 mcp_method_header=yes header_names=<see "Headers the app receives">
Q2 counter before: 200 content=json headers=- instance=<redacted> protocol_header=2026-07-28 count=0
Q9 probe_sse: 200 content=sse headers=- instance=<redacted> protocol_header=2026-07-28 count=0
Q9 header mismatch: 400 content=json rpc_error=-32020 headers=-
Q9 unsupported version: 400 content=json rpc_error=-32022 headers=-
Q9 GET /mcp: 405 content=none headers=allow
Q8 handshake initialize: 200 content=json headers=- protocol_version=2025-11-25
Q8 handshake notifications/initialized: 202 content=none headers=-
Q8 handshake tools/list: 200 content=json headers=- tools=3 expected_tools=yes
Q2 POST /api/probe/write, MCP token: 403 content=json error=insufficient_scope challenge=bearer[error] challenge_error=insufficient_scope headers=www-authenticate
Q2 GET /api/probe/whoami, MCP token: 403 content=json error=insufficient_scope challenge=bearer[error] challenge_error=insufficient_scope headers=www-authenticate
Q2 GET /api/health, MCP token: 403 content=json error=insufficient_scope challenge=bearer[error] challenge_error=insufficient_scope headers=www-authenticate
Q2 counter after: 200 content=json headers=- instance=<redacted> protocol_header=2026-07-28 count=0
Q2 write effect: compared same_instance=yes write_took_effect=no
Q7 tools/list on /api/mcp, /mcp token: 200 content=json headers=- tools=3 expected_tools=yes
Q7 probe_whoami on /api/mcp, /mcp token: 200 content=json headers=- instance=<redacted> route=/api/mcp token_header=x-apps-token authorization_header=no x_api_key_header=no cookie_header=no identity=user user_fingerprint=<redacted> workspace_fingerprint=<redacted> protocol_header=2026-07-28 mcp_method_header=yes header_names=<see "Headers the app receives">
Q3 Mistral API GET /v1/models, MCP token: 401 content=json headers=set-cookie
Q6 refresh grant: 200 content=json headers=- bearer=yes expires_in=<=5m refresh_token=yes scope=openid+email+profile
Q6 tools/list with the refreshed token: 200 content=json headers=- tools=3 expected_tools=yes
Q6 call after revocation: 401 content=json error=invalid_token challenge=bearer[error,resource_metadata] challenge_error=invalid_token headers=www-authenticate
Q6 original token after revocation: 401 content=json error=invalid_token challenge=bearer[error,resource_metadata] challenge_error=invalid_token headers=www-authenticate
Q6 refresh after revocation: 200 content=json headers=- bearer=yes expires_in=<=5m refresh_token=yes scope=openid+email+profile
verdict: BLOCKED reasons=revocation_not_effective
```

Attempt 3 on 27 November 2026, with `--live --q3-mistral-api --revocation-step
--resource-from-metadata`. Two changes besides the redaction: the list of header names is replaced
by a pointer to "Headers the app receives", and `same_instance` in the write-effect line, which
the redaction caught by mistake, shows its recorded boolean. The `verdict` line is the tool's own;
"Verdict" above explains why its reason does not apply.

Step 5: same instance yes; user fingerprint matches yes; workspace fingerprint matches yes.

## Assumptions checked offline only

These come from the installed CLI `v0.18.1` and from reading its local dev gateway. Before the run
none of them had been confirmed against Studio; "Answered by the run" below lists what the run of
26 and 27 November settled.

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
- `mistral apps delete <app> --organization <id> --workspace <id> --yes` deletes the app without a
  prompt; the CLI's help says `--yes` is required off a terminal. Whether the deletion ends the
  app's tokens and grants is not documented, and is what L9 tests.
- The runtime starts the container from `/srv` with the probe's start command (`--app-dir /srv`
  protects against another working directory). It also injects the gateway proxy URL as for liam-e
  (`__APPS_PROXY_URL` or `__SPACES_PROXY_URL`).
- The gateway in Studio behaves like the CLI's dev gateway for flagged routes. Whether it forwards
  the caller token as `x-apps-token` or `x-space-token`, and whether it passes `MCP-Protocol-Version`,
  `Mcp-Method` and SSE through unchanged, is exactly what Q4 and Q9 test.

Answered by the run:

- The CLI accepted the probe manifest with both flagged routes and printed no lint notice (L1).
- `--from-disk` deployed the exported copy and wrote the new `appId` into it; the helper recorded
  it, and the listing showed one app of the probe's name (L1). A `.git` in a parent directory of
  the temporary copy makes the CLI fail instead (#190).
- The app reached the gateway proxy: the identity lookup returned `user` (Q4).
- The gateway forwards the caller token as `x-apps-token` and also sends `x-space-token` (Q4). It
  passes `MCP-Protocol-Version`, `Mcp-Method` and SSE through unchanged (Q9).

The other assumptions remain unconfirmed.

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
