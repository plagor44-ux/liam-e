# Mail providers and operations

## Connection methods

The accounts view offers Gmail and Outlook as soon as the appropriate Studio connector is listed
or own OAuth login is configured. A listed connector takes precedence in the form. The list
indicates its existence. Login and tool scope depend on the connection in Studio. iCloud, Yahoo
and other IMAP providers are available through “Anderer Anbieter” (UI text).
[Accounts form](../apps/web/src/accounts/ConnectPanel.tsx),
[Accounts API](../apps/api/src/api/routers/api/v1/mail/accounts.py).

| Provider and access | Evidenced by code and isolated tests | Evidence in Studio or at the provider |
| --- | --- | --- |
| Gmail through Studio (`gmail`, `google_mail`) | Sync through search and full thread. Read and folder actions through labels. Sending through `send_email`, replies with `replyToMessageId`. Original view missing. Without Reply-To, the reply goes to the sender. `CATEGORY_PROMOTIONS` can identify a newsletter. | The read probe of 13.11.2026 demonstrates the response format, documented system labels and `replyToMessageId` parameter. Successful sending and changes such as trash or spam require separate live evidence. |
| Gmail through own Google OAuth2 | Incremental sync via Gmail History, read and folder actions via labels or Gmail trash, sending via Gmail API and read-only original view. | Registered OAuth client with suitable callback URL and authorized scopes. Check login, token refresh and mail actions in the intended account. |
| Outlook through own Microsoft OAuth2 | Microsoft Graph Delta for the inbox, read and folder actions via Graph, sending via Graph including reply or forwarding draft at the provider, read-only original view. | Registered OAuth client with suitable callback URL. Login, consent and mail actions depend on the account and its tenant rules. |
| Outlook through Studio (`outlook`) | Listing and registration are implemented. Sync, sending and labels use the shared connector path with the tool format evidenced for Gmail. | The tool list responded with HTTP 401 on 13.11.2026. Authentication and compatible tool scope for this path remain open. |
| iCloud and Yahoo through IMAP/SMTP | Host presets and shared IMAP/SMTP path. Sync via IMAP UIDs, read and folder actions via IMAP, sending via SMTP and read-only original view. | Check the account's app password and permissions. Tests use an IMAP fake and simulate TLS/SMTP transport. |
| Fastmail through IMAP/SMTP | Presets for Fastmail's published domains, including `fastmail.com`, `fastmail.fm` and regional domains. Uses the shared IMAP/SMTP path. | Settings and domain list verified against official documentation; no live authentication or provider connectivity tested. |
| Other IMAP/SMTP providers | The same IMAP/SMTP path with own allowed hosts. Different ports and SMTP STARTTLS are configurable through the API. Folder actions depend on identifiable system folders and server capabilities. | Check host approval, public DNS resolution, TLS, login and folder behavior with the respective provider. |

Fastmail's provider-owned domains are listed explicitly in the
[provider registry](../apps/api/src/api/mail/providers.py), following its official
[domain list](https://www.fastmail.help/hc/en-us/articles/6239365719055-What-domains-can-I-use-with-Fastmail).
Custom domains and unlisted subdomains are not inferred; explicit server settings for those
addresses can be submitted through the API. Only the two standard mail servers are added to
the default host allowlist:

| Protocol | Server | Port | Encryption |
| --- | --- | --- | --- |
| IMAP | `imap.fastmail.com` | 993 | Implicit TLS, not STARTTLS |
| SMTP | `smtp.fastmail.com` | 465 | Implicit TLS, not STARTTLS |

These are Fastmail's [official server settings](https://www.fastmail.help/hc/en-us/articles/1500000278342-Server-names-and-ports).
For SMTP port 587 with STARTTLS, submit `smtp_host`, `smtp_port` and `smtp_starttls` through
the API; the web form does not expose port or STARTTLS fields. The preset uses port 465.
Connect with the account's full Fastmail sign-up/login address: liam-e uses that address as
the login username, and Fastmail does not accept aliases for authentication. A matching
preset domain selects server settings; it does not make an alias a valid login address.
External access requires an app-specific password, not the normal account password, and is
unavailable on Basic plans. See [IMAP, POP, and SMTP](https://www.fastmail.help/hc/en-us/articles/1500000279921-IMAP-POP-and-SMTP).

The [provider registry](../apps/api/src/api/mail/providers.py) defines OAuth scopes and host
presets. The shared paths for [mail actions](../apps/api/src/api/mail/action_backends.py),
[sending](../apps/api/src/api/mail/send.py) and
[original view](../apps/api/src/api/mail/original.py) are covered by
[sync tests](../apps/api/tests/test_mail_sync.py),
[action tests](../apps/api/tests/test_mail_action_backends.py),
[sending tests](../apps/api/tests/test_mail_send_replies.py) and
[original-view tests](../apps/api/tests/test_mail_original.py).
The [Studio probes of 13.11.2026](superpowers/probes/2026-11-13-studio-proben.md) document
individual measurements in a test app. They apply to the state at that time.

The sync adapters retrieve the inbox. The setting `MAIL_SYNC_FOLDERS` does not currently extend
their retrieval scope. The web UI lists the inbox. The list API can additionally query archive
and trash from the local index. Spam is a mail action and a stored state without its own list
filter. Reconciliation of actions performed directly at the provider is limited depending on
the adapter. The Gmail connector lacks unsubscribe headers.
Automatic unsubscription requires the one-click headers or a supported `mailto:` link and a
confirmed proposal card.
[List API](../apps/api/src/api/routers/api/v1/mail/messages.py),
[Connector sync](../apps/api/src/api/mail/sync_connector.py),
[Newsletter](../apps/api/src/api/mail/newsletters.py).

## Credentials and configuration

- **Studio connector.** Sign in to the mail account under “Auth” (UI text) on the connector in Studio. The API module calls the tools with its own Mistral access, normally via `MISTRAL_API_KEY`. The account receives no local entry in `mail_credential`. Expired connector access is renewed in Studio. [Connector client](../apps/api/src/api/mail/connectors.py), [Mistral access](../packages/py/utils/src/utils/mistral.py).
- **Own OAuth2.** Provide the client ID and client secret as `MAIL_OAUTH_GOOGLE_CLIENT_ID` and `MAIL_OAUTH_GOOGLE_CLIENT_SECRET`, or as the corresponding `MAIL_OAUTH_MICROSOFT_*` variables. Set `MAIL_OAUTH_REDIRECT_BASE_URL` to the app's base URL. Register the callback URL with the provider using the pattern `<App-Basis-URL>/api/v1/mail/accounts/oauth/<provider>/callback` with `google` or `microsoft`, respectively. liam-e encrypts OAuth tokens and also stores refreshed tokens encrypted. [OAuth routes](../apps/api/src/api/routers/api/v1/mail/accounts.py), [Credential service](../apps/api/src/api/mail/service.py).
- **IMAP/SMTP.** Create one workspace secret per account with the app password, for example `yahoo-app-password`. The form contains the secret name. liam-e stores it encrypted together with the server configuration and reads the password when needed. A new value under the same name takes effect on next access. The API alternatively accepts `password` as an encrypted stored app password. Put own server names, comma-separated, in the runtime variable `MAIL_IMAP_ALLOWED_HOSTS`. The application checks the host against the allowlist and for public DNS addresses. The transport checks TLS certificates. [Secret resolution](../apps/api/src/api/mail/secrets.py), [Mail transport](../apps/api/src/api/mail/transport.py).
- **Fernet key.** `MAIL_TOKEN_ENCRYPTION_KEY` protects credential payloads and is used to sign OAuth state. Keep the same key available across updates. Changing it makes data already encrypted with it unreadable. [Fernet use](../apps/api/src/api/mail/crypto.py), [OAuth state](../apps/api/src/api/mail/providers.py).

For fields marked `WorkspaceSecret` in settings, the API can load workspace secrets at startup.
To do so, set `MISTRAL_SECRETS_ENABLED=true` in the API module and `MISTRAL_API_KEY` from the
same workspace with access to secret values. The workspace secrets there are named, for example,
`mail_token_encryption_key` and `mail_oauth_google_client_secret`. Nonempty runtime values and
the local `.env` take precedence. `MAIL_IMAP_ALLOWED_HOSTS` and `MAIL_OAUTH_REDIRECT_BASE_URL`
are ordinary runtime variables. An IMAP account's password reference is resolved at runtime
through a separate retrieval path.
[Mail settings](../packages/py/env/src/env/mail.py),
[Configuration order](../packages/py/env/src/env/_base.py),
[Workspace secrets](../packages/py/env/src/env/_mistral_secrets.py).

## Sync and data storage

A manual sync queues a job in Postgres. A `202` response means the job was accepted. Pending
jobs for the same account are deduplicated. The worker runs in the API process. By default, it
waits two seconds when the queue is empty and queues due syncs for active accounts every five
minutes. Periodic sync skips accounts whose last sync was less than half an interval ago, as
well as accounts with `auth_expired`. A manual sync or renewed OAuth login can queue them again.
`MAIL_SYNC_INTERVAL_MINUTES=0` disables periodic sync. `MAIL_WORKER_ENABLED=false` stops job
execution.
[Worker startup](../apps/api/src/api/lifespan/startup/mail_worker.py),
[Job loop](../apps/api/src/api/mail/jobs.py),
[Postgres queue](../packages/py/db/src/db/accessors/mail_job.py).

Sync stores the provider cursor and last state per account. The index holds metadata, shortened
text versions and attachment metadata. The original view retrieves supported originals on demand
from the provider read-only and does not store them. A successful sync with new mails queues a
job for missing AI summaries. The mail index and chat history are application data in Postgres.
Fernet is limited to credential payloads. Removing an account deletes the local credentials,
indexed mails, summaries, actions, jobs and sync state. The mails remain at the provider.
[Sync](../apps/api/src/api/mail/sync.py),
[Mail model](../packages/py/db/src/db/models/mail_message.py),
[Remove account](../apps/api/src/api/routers/api/v1/mail/accounts.py).

Jobs remain in the database even if the API process stops. Processing pauses during a stop.
After `MAIL_SYNC_STALE_MINUTES` (default ten minutes), a worker can reclaim an orphaned job.
In the Studio probe on 13.11.2026 the same process was still running at the end of the
31-minute idle window. [Measurements](superpowers/probes/2026-11-13-studio-proben.md).

## Deployment in Mistral Studio

1. Merge the approved commit into `main` on the GitHub repository configured as `origin`. The
   GitHub Actions in this repository check code, tests and secrets. [CI](../.github/workflows/ci.yml).
2. Configure runtime values and secrets in Studio. Runtime configuration comes from Studio; the
   deploy copy holds no `.env`. In particular, check access to the API module and the intended Studio
   workspace. liam-e is designed for a single user. The versioned API relies on gateway login
   and access restrictions. [API access rule](../apps/api/src/api/routers/api/v1/__init__.py),
   [Security policy](../SECURITY.md).
3. Run `mistral login`, then `bash tools/deploy.sh <commit>` from a checkout of this
   repository. The commit must be on `origin/main`. `mistral.json` carries no `deployment`
   block, so the script reads `<organization-id>`, `<workspace-id>` and `<app-id>` from the
   git-ignored file `mistral.deployment.json` at the checkout root, or from the file that
   `LIAM_E_DEPLOYMENT_FILE` names:

   ```json
   {"organizationId": "<organization-id>", "workspaceId": "<workspace-id>", "appId": "<app-id>"}
   ```

   Without that file the script stops, because a deploy without the block creates a new app.
   Disk deploy and probe staging uses a private directory under `$XDG_CACHE_HOME/liam-e`
   (default `$HOME/.cache/liam-e`) or an explicit `TMPDIR`. The CLI receives the private
   directory as its own `TMPDIR` and the wrapper removes it on exit. A `.git` entry anywhere
   above the staging directory stops the deploy before any CLI request. Allocate staging
   for a sandboxed release preflight before launching the sandbox, as shown below.
   It clones the commit into a temporary directory outside the working tree (tracked files
   only, so no `.env`), writes the block into that copy's `mistral.json`, checks with
   `mistral apps list` that the app exists and runs `mistral deploy --from-disk` there without
   `--yes`. Check the target the CLI displays. The copy cannot push and is removed afterwards.
   A plain `mistral deploy` in another checkout creates an app of its own, which is how a
   separate installation starts. `--from-disk` is experimental in the CLI;
   [Publication](publishing.md#deploying-without-ids-in-the-repository) describes the way back.
   [Script](../tools/deploy.sh), [Manifest](../mistral.json),
   [Studio deployment](https://docs.mistral.ai/studio/apps/deploy).
4. Through the manifest, Studio provides the API under `/api`, web under `/` and managed
   Postgres. The API module uses injected `POSTGRES_*` values unless `DATABASE_URL` is set.
   Its pre-start step waits for Postgres and runs Alembic to `heads`. Check module status in
   Studio, API startup logs with `mistral apps logs api` and `/api/health` at the authenticated
   gateway. Test web, sync and confirmed actions with synthetic data in a designated test
   workspace. [DB settings](../packages/py/env/src/env/db.py),
   [Pre-start](../packages/py/cli/src/cli/commands/pre_start.py),
   [Migrations at startup](../packages/py/cli/src/cli/commands/migrations.py),
   [Health](../apps/api/src/api/routers/api/internal/health.py).

Studio provides the app and its API. Releasing operations as MCP tools is a separate step.
The full OpenAPI also includes operations that perform actions. For HTTP connectors follow
the read-only allowlist in the [agent rules](../AGENTS.md#security) and the
[MCP inventory](../apps/api/tests/test_mcp_exposure.py).

### Sandboxed release preflight

Define this function in a Bash shell outside the sandbox. Start in the release checkout at
the revision to check, with a home directory outside the shared system temporary directory.
The helper checks the staging ancestry and creates a `0700` directory under
`$HOME/.cache/liam-e`. The function sets `TMPDIR` before it creates the checkout or starts
the sandbox launcher, and removes the staging directory when the launcher exits.

```bash
sandboxed_release_preflight() (
  staging="$(env -u TMPDIR -u XDG_CACHE_HOME python3 -I tools/deploy_temp.py liam-e-preflight)" || exit
  trap 'rm -rf -- "$staging"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  export TMPDIR="$staging"
  git clone --quiet --no-local -- . "$TMPDIR/checkout" || exit
  cd -- "$TMPDIR/checkout" || exit
  "$@"
)
```

Pass your sandbox launcher and its arguments to `sandboxed_release_preflight`. Run the
launcher in the foreground. If it overrides its working directory or filters the environment,
configure it to use the current checkout and forward the exported `TMPDIR` to the sandbox.
