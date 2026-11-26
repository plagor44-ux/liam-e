# liam-e — universal mail app in Mistral Studio

A Mistral app (see `mistral.json`) that manages **all mail accounts in one place** — Gmail,
Outlook, iCloud, Yahoo and any other providers — and enables searching, reading, summarizing and
(after confirmation) replying/sending across all accounts through **chat with an AI agent**.

## Agreed requirements (agreed with the user on 2026-11-13)

| Topic | Decision |
| --- | --- |
| Provider connections | Providers available as **Mistral Studio connectors** (e.g. `gmail`) use the existing connector (`api/mail/connectors.py`, REST `/v1/connectors/...` with the app key — OAuth credentials are in Mistral, no own OAuth app needed). Others: Gmail & Outlook through **own OAuth2**, iCloud, Yahoo and all others through **IMAP/SMTP with an app password** |
| Data storage | **Sync to managed Postgres**: credentials encrypted at rest, mail index (metadata + body texts), chat history |
| Sending mail | Agent may freely search/read/summarize; **send/reply/forward only after explicit confirmation** of the draft in chat |
| Scope | Feature in `apps/api` + `apps/web`; deploy via existing `mistral.json` to Mistral Studio; **single user**; UI in German or English |

## Architecture

```
apps/web  (TanStack Start, Port 3001)                   Browser UI: /, /chat/$conversationId, /mail, /accounts
   │  typed hey-api client (src/api/generated, generated — never edit by hand)
   ▼  same-origin /api
apps/api  (FastAPI, Port 3000, Route "/api")            Router tree = URL tree, groups with access rule
   │  api/mail/                                         Mail, chat and worker logic
   ▼
packages/py/db   SQLModel models (drop-in in db/models/) + accessors (db/accessors/) + Alembic
packages/py/env  Settings modules (env/<name>.py), workspace secrets via WorkspaceSecret
managed Postgres (module in mistral.json)              Mail index, credentials, chat history
```

[docs/architecture.md](docs/architecture.md) describes the parts and their runtime in more detail.

## Read before changing an area

Domain rules live in on-demand notes under `docs/agents/`. Before you change code in an area, read
its note; the rules in this file apply everywhere.

| Before working on | Read |
| --- | --- |
| Web routes (`apps/web/src/routes/`), app language, chat and job language | [docs/agents/web-ui.md](docs/agents/web-ui.md) and [DESIGN.md](DESIGN.md) |
| `apps/api/src/api/mail/`: accounts, provider actions, undo, trash, stored HTML, Studio mail connector | [docs/agents/mail.md](docs/agents/mail.md) |
| App folders, labels, filing and mail rules | [docs/agents/mail-filing-and-rules.md](docs/agents/mail-filing-and-rules.md) |
| `apps/api/src/api/mcp/` (agent read facade) | [docs/agents/agent-read-facade.md](docs/agents/agent-read-facade.md) |
| `apps/api/src/api/tasks/`, `apps/web/src/tasks/`, tasks in chat | [docs/agents/tasks.md](docs/agents/tasks.md) |
| `apps/api/src/api/calendar/`, `apps/web/src/calendar/`, Google calendar reads and writes (#23), calendar in chat | [docs/agents/calendar.md](docs/agents/calendar.md) |
| ICS subscriptions, fetching and parsing (`feed*.py`, `ics*.py`) | [docs/agents/calendar-ics.md](docs/agents/calendar-ics.md) |
| Tables and models in `packages/py/db/` | [docs/agents/data-model.md](docs/agents/data-model.md) |
| Templates, turn context, skills (#84, #97), chat mail access (#80) | [docs/agents/chat.md](docs/agents/chat.md) and [docs/skills.md](docs/skills.md) |
| `apps/api/src/api/preferences/` (#38, #39, #40) | [docs/agents/preferences.md](docs/agents/preferences.md) |
| `jobs.py`, the worker, periodic sync and body rereads | [docs/agents/worker-and-sync.md](docs/agents/worker-and-sync.md) |
| Behaviour of the Studio platform and its connectors | [docs/agents/studio-probes.md](docs/agents/studio-probes.md) |
| `apps/api/src/api/audio/`, `apps/web/src/audio/`, speech and live voice | [docs/agents/audio.md](docs/agents/audio.md) |
| Local stack, Docker Compose, the devcontainer and seed data | [docs/local-development.md](docs/local-development.md) |
| Providers, Studio setup and deployment | [docs/providers-and-operations.md](docs/providers-and-operations.md) |
| Publishing the repository | [docs/publishing.md](docs/publishing.md) |

## Stack & commands

- **Python:** `uv` workspace (root `pyproject.toml`), FastAPI + SQLModel/Alembic. Tests: `bash tools/uv.sh run --no-sync pytest <path>`; live PG suites: `RUN_PG_CONTRACT=1 bunx nx run db:test-pg-contract` (without the variable every suite skips)
- **TypeScript:** bun 1.4 + Nx + TanStack Start/Router/Query + Tailwind 4, Vite 8
- Main commands (root):
  - `bun run install-all` — installs everything including generated API client (`tools/gen-types.sh`)
  - `bunx nx run api:serve` — start API locally; `bun run dev:web` — web locally (port 3001)
  - `docker compose up --build --watch` — database, migration, sample data, API and web in containers (`compose.yaml`, Docker with BuildKit); see [docs/local-development.md](docs/local-development.md)
  - `bun run build` / `bun run test` / `bun run check` — Nx across all projects
  - `bunx nx run api:gen-openapi` + `bash tools/gen-types.sh` — regenerate OpenAPI spec + TS client (after every API change; `gen-types.sh --check` is the drift gate)
  - `bunx nx run db:revision --message "..."` — autogenerate Alembic revision; `bunx nx run db:migrate` — apply (`heads`, not `head`)
  - `bash tools/uv.sh run --no-sync python -m api.mail.dev_seed` — fill the local database with the design-board sample data. The seed rejects database hosts other than `localhost`, `127.0.0.1` and `::1`; repeated runs replace only seed rows and associated insight/sync/chat rows. For visual checks start the API with `MAIL_WORKER_ENABLED=false` so seed accounts are not synced.
  - `bash tools/deploy.sh <commit>` — deploy a commit from `origin/main` to the existing Studio app; the IDs come from the git-ignored `mistral.deployment.json` ([docs/providers-and-operations.md](docs/providers-and-operations.md)).
  - `bash tools/uv.sh run --no-sync python tools/issue_form.py forms|skeleton <form>|check <form> <file>|create <form> --title ... --body-file <file>|verify <number>` — create and check issues in the shape of an issue form (`--dry-run` prints the `gh` command).

## Conventions

### Python
- **Settings:** each new setting belongs in its own module `packages/py/env/src/env/<name>.py` (`class Env(BaseEnv)` + `env = Env()`); declare every name **empty** in the root `.env.example`. Mark secrets with `Annotated[str | None, WorkspaceSecret()] = None` (in Studio from workspace secrets, locally from `.env`).
- **DB:** one model per file in `packages/py/db/src/db/models/<table>.py` (drop-in, automatically discovered by the loader; **no cross-table foreign keys**, references as string columns), one accessor module per table in `db/accessors/` — ORM queries only in accessors. Migrations: one branch per owner, `branch_labels` set, `down_revision = None` at the root.
- **Router:** directory tree = URL tree under `apps/api/src/api/routers/`. Every module with `router` is served; every package needs an access rule (`dependencies` in `__init__.py`, `()` = anonymous — never omit it, or imports fail). `operation_id` is unique and becomes the MCP tool name in Studio. `[name]` in a file/directory = path parameter `{name}`; `route.py` = index.
- **Code, comments, and docstrings in English.** UI copy lives in the typed German and English catalogues.
- Never log or commit secrets; `.env` is gitignored.

### TypeScript (apps/web)
- TanStack Router: one file per route under `src/routes/`; claim the landing page via `staticData: { landing: <rank> }`. After new routes run `bunx nx run web:gen-routes`.
- API access only through the generated client (`src/api/generated/`, committed to git, never edit manually) + `unwrap`/`dataOr404` from `src/api/unwrap.ts`; `VITE_API_URL` configures the base URL. Calls whose result depends on HTTP status (404/409/502 for mail actions, syncs and accounts) may call the generated client without `unwrap` and branch on status themselves.
- Styling with Tailwind 4; mark technical literals such as paths and class names with `// i18n-exempt`. Tests: `bun test` (jsdom setup in `src/test/setup.js`).
- Tooltips and menus are attached to `<body>` and are never positioned by CSS or styled through an ancestor (no `top`/`left`/`right`/`bottom` on `.le-menu`/`.le-tooltip`, no `.le-x .le-menu`); positioning comes from `src/ds/floating.ts` (`pos` or `placement` is only the preferred side). `src/ds/floating.test.tsx` checks this. See DESIGN.md §5, “Floating elements”.

## Security
- OAuth client secrets and `MAIL_TOKEN_ENCRYPTION_KEY` are **workspace secrets** (`WorkspaceSecret`), never in code; locally only in `.env`. **Secret names in Mistral Studio are lowercase** — resolution is case-insensitive: secret `mail_token_encryption_key` fills `MAIL_TOKEN_ENCRYPTION_KEY` (see the docstring of `env/_mistral_secrets.py`).
- **Exactly one separate workspace secret per IMAP account (iCloud, Yahoo, …)** contains the app password, e.g. `yahoo-app-password`, `icloud-app-password` (lowercase). On connect, the secret name is passed instead of the password (`password_secret`); the app resolves it at runtime via `GET /v1/secrets/values` (`api/mail/secrets.py`) and stores only the **reference** (encrypted), never the value, in `mail_credential` — rotation in Studio works without reconnecting.
- Mail credentials are Fernet-encrypted before writing to `mail_credential` (`api/mail/crypto.py`); the key is never persisted.
- The agent **never changes or sends without confirmation**: `create_draft` and action tools create proposals; only `POST /api/v1/chat/proposals/{proposal_id}/confirm` executes the selection, provided the turn context has not been revoked at claim time. `.../cancel` discards it even after revocation.
- CORS remains restricted to the app's own origin (`CORS_ORIGIN`).
- Never import liam-e as an HTTP connector in Studio, or import only these read-only operations: `calendar_occurrence_get`, `calendar_occurrences_list`, `calendar_sources_list`, `chat_conversations_list`, `chat_history`, `chat_models_list`, `health`, `liam_mail_accounts`, `liam_mail_read`, `liam_mail_search`, `liam_mail_summary`, `mail_account_options`, `mail_accounts_list`, `mail_connector_tools_list`, `mail_connectors_list`, `mail_message_get`, `mail_message_insight`, `mail_message_original`, `mail_messages_list`, `mail_summary`, `prompt_get`, `prompts_list`, `settings_ui_get`, `tasks_get`, `tasks_list`, `tasks_occurrences_list`
  `mail_message_insight` generates a missing summary with the model and stores it in `mail_insight` (model cost, no mail changes; never for seed accounts, where only seeded summaries are available).
  `mail_message_original` fetches the mail from the provider read-only, without marking it read, and stores nothing.
  `mail_message_html_get` is browser-only and returns bounded, untrusted stored HTML; it is never
  in the agent read list. Its rendering rules are in
  [docs/agents/mail.md](docs/agents/mail.md#stored-mail-html).
  `settings_ui_browser_locale_confirm` is browser-only; it stores the latest browser hint for jobs.
  `settings_ui_get` creates the default assignment in `ui_settings` once on the very first read; it never changes an existing assignment.
  The calendar write routes (`calendar_event_create`, `calendar_event_update`, `calendar_event_delete`, `calendar_write_check`), `chat_proposal_check`, `calendar_occurrence_live` and `calendar_write_get` are deliberately not in this list: they write to Google, resolve a write or read private live event data.
  `audio_capabilities_get` and the paid `audio_transcribe` and `audio_speech` operations stay browser-only.
  The audio, device-check and live-voice boundaries are in
  [docs/agents/audio.md](docs/agents/audio.md).
  Every OpenAPI import turns every operation into an MCP tool; otherwise a Studio agent could confirm proposals, perform actions or connect accounts without the owner seeing it — individual tools can be disabled in Studio after import.
  New mutating operations must deliberately be added to `MUTATING_OPERATIONS` in `apps/api/tests/test_mcp_exposure.py`, otherwise the inventory test fails.

## Publication and private data

liam-e should be publishable. Everything that goes into Git (files and commit messages), and
anything on GitHub (issues, PRs, reviews, comments), therefore already counts as public.

- **Nothing about the owner or their environment:** no real names, email addresses, mail or
  calendar contents, contacts, hostnames, internal domains, IP addresses, absolute local paths,
  user or device names, or names of their own infrastructure or tools. Give paths relative to
  the repository.
- **Mistral tenant:** organization, workspace and app IDs never appear in the repository, not even
  in `mistral.json`. Write `<organization-id>`, `<workspace-id>` and `<app-id>`, and do not write
  down the deployment's app or gateway URLs. Deploy with `bash tools/deploy.sh <commit>`: it reads
  the IDs from the git-ignored `mistral.deployment.json` and writes the `deployment` block only
  into a temporary copy ([docs/providers-and-operations.md](docs/providers-and-operations.md)).
  Never commit that file or print its values. Older commits still hold the IDs
  ([docs/publishing.md](docs/publishing.md)).
- **No links to private resources:** name Claude artifacts, sessions and private documents instead
  of linking them (e.g. the “liam-e” design system). The only exception is the `Claude-Session`
  attribution line in commits and PR descriptions.
- **Test data is synthetic:** addresses under `example.org`, `example.com`, `example.net`,
  `*.example` or `*.test`, invented names and content. Provider presets (`gmail.com`,
  `icloud.com` …) are configuration; tests for them use placeholders such as `me@` or `x@`.
  Never use a real mail, appointment or headers as a fixture, even shortened. Screenshots only
  with seed data from `api.mail.dev_seed`.
- **Credentials never in plaintext:** no passwords, app passwords, tokens, API keys, OAuth codes,
  cookies or Fernet keys in code, tests, docs, `.env.example`, issues, PRs, commit messages and
  logs, or in assignments to other models. Values exist only as workspace secrets in Studio or
  locally in `.env` (gitignored). Agents do not read `.env` or output values from it; secret
  *names* such as `yahoo-app-password` are uncritical. Tests use visible disposable values
  (`test-only`) or keys generated at runtime.
- **A committed secret is considered burned:** rotate first, then remove it; history retains it.
  Never “solve” it through an allowlist in `.gitleaks.toml`, through `.gitleaksignore` or a
  `gitleaks:allow` comment (CI ignores such comments).
- **Logs and error messages** contain no mail contents, addresses, template text, tokens or
  secret values.
- **Local agent state** (`.superpowers/`, `.codegraph/`, `.remember/`,
  `.claude/settings.local.json`, `.claude/worktrees/`, Playwright output) remains untracked;
  `.gitignore` covers it.
- **Enforcement:** CI job `secrets` (gitleaks over the entire history with `.gitleaks.toml`) and
  `tests/test_public_repo_hygiene.py` (tenant IDs in any tracked file, a `deployment` block in
  `mistral.json`, private links, home paths, realistic-looking email addresses). The allowlist is empty; an exception requires a
  reviewed false positive, applies only to the rule, file and specific value, and gives the
  justification in the entry. CI actions are pinned to commit SHAs.
- What remains before publication is in [docs/publishing.md](docs/publishing.md).

## Working practices for agents
- Documentation, issues, pull requests and commit messages are English. UI wording follows the German and English catalogues (see #65). Add a matching key in both catalogues for new UI text.
- Before any work on the UI (`apps/web`, text, including assistant response text), read `DESIGN.md`; it governs tokens, components, responsive behavior and how text sounds.
- Minimal diffs; copy existing patterns (see module docstrings — they document the conventions locally).
- After API changes always run `gen-types` and commit `apps/api/openapi.json` + `apps/web/src/api/generated`.
- Table changes always require a migration (`db:revision` + `db:migrate`), never hand-edited `versions/` files.
- Before committing: run `bun run check` (builds everything, checks UI types with `tsc` and the generated client) and the affected test suites.
- Every issue follows exactly one form in `.github/ISSUE_TEMPLATE/`: that form's `### <label>` sections in the form's order and nothing else (an empty optional field reads `_No response_`, checkboxes list every option), plus that form's label. GitHub enforces the forms only in the browser. The API and `gh issue create` accept any body, and gh cannot fill in a form (its `--template` works only interactively and lists no YAML forms). Create issues with `tools/issue_form.py create` after starting from its `skeleton`; it checks the sections, sets the label and starts the title with the form's title prefix (`Eval: ` for evaluations). After editing an issue, `tools/issue_form.py verify <number>` must pass.
- PRs use `.github/pull_request_template.md`. One task produces exactly one branch and one PR, even when it covers several issues; each issue gets its own commits and closing keyword (`Closes #12. Closes #13.`). State explicitly in the PR what was not checked.
- Keep this file short (about 200 lines, well under the 32 KiB that Codex reads by default). Put domain detail in the matching `docs/agents/` note and add a row to the index above. Do not import those notes here: an import loads them into every session. `CLAUDE.md` only imports this file for Claude Code; add instructions here, not there.

<!-- OPENWIKI:START -->

## OpenWiki

This repository has a generated `openwiki/` evidence index. It is optional just-in-time context, not required startup reading.

- Do not enumerate, preload, or search wikis at task start. Use retrieval when the user asks for it, when unfamiliar architecture or dependency behavior materially affects the task, or when source inspection leaves an important uncertainty. Stop once the question is grounded.
- When those conditions apply and OpenWiki retrieval tools are available, use `openwiki_search` for just-in-time context and `openwiki_read` for the relevant complete sections. If search returns `workspace_required`, ask which listed workspace to use and retry with its ID.
- Use `openwiki_list_workspaces` or `openwiki_list_wikis` when workspace membership itself needs to be discovered.
- If the retrieval tools are unavailable, read `openwiki/quickstart.md` and follow its links to the relevant pages.
- Treat source code and tests as authoritative. A brief's unknowns and review items are verification gaps, not automatic requirements.
- Prefer the narrowest quiet validation that proves the changed behavior. Preserve complete failure output.

There is no scheduled OpenWiki workflow; the pages are regenerated locally on request and may lag behind the code. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and regenerating OpenWiki.

<!-- OPENWIKI:END -->
