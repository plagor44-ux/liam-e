# Local development

This guide starts the main application from [`apps/api`](../apps/api) and [`apps/web`](../apps/web) with a separate test database. The synthetic sample data lets you view the inbox and saved chat histories without a mail account or Mistral access. The [minimal chat demo](../apps/chat-app/README.md) has its own entry point and is not started for this.

## Prerequisites and installation

- Bash, Bun **1.4.0**, Node.js **24** and `uv` **0.11.17** match the tool versions in [CI](../.github/workflows/ci.yml). The installation script requires the Bun version specified in [`package.json`](../package.json). `uv` uses Python **3.14** from [`.python-version`](../.python-version) and can install the interpreter if needed.
- A locally reachable PostgreSQL instance. The example uses the same PostgreSQL 16 image with pgvector as CI and requires Podman. The same container commands work with Docker.

At the repository root, [`tools/install.sh`](../tools/install.sh) installs both workspaces and generates the typed API client.

```bash
bun run install-all
```

For a CI-like checkout with unchanged lockfiles and an existing API client, the [workflow](../.github/workflows/ci.yml) uses the following commands. This keeps a stale API client detectable by `bun run check`.

```bash
bash tools/uv.sh sync --all-packages --frozen
bun install --frozen-lockfile
```

## Configuration

The [root `.env.example`](../.env.example) declares only the names of the app's own settings that are not handled by a Studio capability. The application does not load this file. The Studio CLI scaffold creates an ignored root `.env` from the `envVars` of installed capabilities. For an existing checkout, a manually created root `.env` with the necessary overrides or direct environment variables suffice. Python reads the root `.env` independently of the invocation directory. Process variables take precedence. The settings loader treats empty values as unset, so defaults in the settings modules apply. See the [settings loader](../packages/py/env/src/env/_base.py), [database](../packages/py/env/src/env/db.py) and [mail](../packages/py/env/src/env/mail.py).

The UI has its own [`apps/web/.env.example`](../apps/web/.env.example) with a concrete value. In a fresh checkout it can be copied to `apps/web/.env`. Alternatively set `VITE_API_URL=http://127.0.0.1:3000` when starting the web app as below. The value specifies the API origin without `/api`, because the generated client already includes that path. Open the web app at `http://localhost:3001`, the API's default `CORS_ORIGIN`. For a different browser origin, set `CORS_ORIGIN` accordingly. `APP_NAME=liam-e` in the root `.env` sets the display name. See [web configuration](../apps/web/vite.config.ts) and [CORS setting](../packages/py/env/src/env/api.py).

## Create and migrate the database

Use a fresh database for the examples. The password shown belongs exclusively to the disposable loopback instance. The container remains active for the session.

```bash
podman run --rm -d --name liam-e-dev-db \
  -p 127.0.0.1:55432:5432 \
  -e POSTGRES_PASSWORD=test-only -e POSTGRES_DB=liam_e_dev \
  pgvector/pgvector:0.8.0-pg16

podman exec liam-e-dev-db pg_isready -h 127.0.0.1 -U postgres -d liam_e_dev
```

Once `pg_isready` reports “accepting connections”, set the URL and migrate. `DATABASE_URL` takes precedence over `POSTGRES_*`, through which Studio provides the database connection. The Nx command runs `alembic upgrade heads` and takes all migration branches into account. Use the same URL for migration, seed and API. You can set the variable in the root `.env` for all relevant terminals. See [DB settings](../packages/py/env/src/env/db.py), [migration command](../tools/db.sh) and [Alembic configuration](../packages/py/db/src/db/migrations/env.py).

```bash
export DATABASE_URL='postgresql+asyncpg://postgres:test-only@127.0.0.1:55432/liam_e_dev'
bunx nx run db:migrate
```

## Sample data and startup

Fill the migrated database with sample data. On repetition the script replaces its own sample rows and associated data. It accepts only database hosts `localhost`, `127.0.0.1` and `::1` without a host override in the URL. See [seed script](../apps/api/src/api/mail/dev_seed.py).

```bash
bash tools/uv.sh run --no-sync python -m api.mail.dev_seed
MAIL_WORKER_ENABLED=false MISTRAL_SECRETS_ENABLED=false TELEMETRY_ENABLED=false bunx nx run api:serve
```

Leave Mistral and provider credentials unset for this test. The API listens on `127.0.0.1:3000` by default and reloads on code changes. In a second terminal, check the API first, then start the UI.

```bash
curl -fsS http://127.0.0.1:3000/api/health
curl -fsS http://127.0.0.1:3000/api/health/ready
VITE_API_URL=http://127.0.0.1:3000 bun run dev:web
```

`/api/health` responds with HTTP 200 even if a dependency is impaired. Check `status: "ok"` and `deps.database.reachable: true` in the JSON. `/api/health/ready` reports HTTP 503 when the API should not accept requests. In the test configuration Mistral may appear as `configured: false`. See [API development startup](../apps/api/src/api/dev.py) and [health routes](../apps/api/src/api/routers/api/internal/health.py).

Open `http://localhost:3001/mail` for the inbox or `http://localhost:3001/` for chat. Sample accounts perform mail actions only in the index. Sync skips these accounts, sending and unsubscribing are blocked, and missing mail summaries are not generated for them afterwards. New AI answers require Mistral access. See [sync](../apps/api/src/api/mail/sync.py), [actions](../apps/api/src/api/mail/actions.py), [sending](../apps/api/src/api/mail/send.py), [unsubscribing](../apps/api/src/api/mail/newsletters.py) and [summaries](../apps/api/src/api/mail/insights.py).

To stop, stop API and web with `Ctrl+C`, then run `podman stop liam-e-dev-db`. The container started with `--rm` is removed along with its unbound data storage.

## Local audio browser test

The audio test starts the web app and API on loopback and migrates a disposable PostgreSQL
container. A local test identity, a synthetic chat history and provider fixtures exercise the
application without connector credentials. Chromium records from a synthetic microphone. The
API validates the browser's actual WebM/Opus recording before giving it to the fixture provider.
The test edits the transcribed draft, sends it explicitly, and plays a synthetic MP3 for the
saved assistant reply. It also checks microphone release, playback cancellation and navigation.

Use the repository's locked dependencies, FFmpeg and FFprobe with the descriptor protocol, and
a local copy of the `pgvector/pgvector:0.8.0-pg16` image. Install Playwright in a scratch directory
or provide an existing installation on `NODE_PATH`.

```bash
playwright_dir=$(mktemp -d)
npm install --prefix "$playwright_dir" --no-save --no-package-lock playwright@1.61.0
"$playwright_dir/node_modules/.bin/playwright" install chromium
NODE_PATH="$playwright_dir/node_modules" node tests/audio_e2e.cjs
```

Set `AUDIO_E2E_CHROMIUM` to run against an installed Chromium executable. The runner removes the
container and stops both processes on exit. The test-only identity and attestation live in the
test server. The Studio audio probe tracks the remaining release evidence.

## Local PWA check

`tests/pwa_matrix.cjs` checks the production build in headless Chromium. It starts the built
server on loopback behind a proxy that answers `/api` with synthetic data, so it needs no
database, API process or provider. It covers the manifest, response headers and HTML head,
installability, worker registration and immediate claim, the standalone start URL, deep links,
portrait, compact, keyboard-open and landscape viewports, and offline navigation in English and
German with an empty CacheStorage. It also simulates an update from v1 to v2 by renaming the lazily
loaded tasks route chunk and every chunk that imports it. Then it checks that open tabs keep running
without a forced reload and that a stale chunk recovers. CI runs the same check in the job
`pwa-matrix`.

```bash
bunx nx run web:build
playwright_dir=$(mktemp -d)
npm install --prefix "$playwright_dir" --no-save --no-package-lock --ignore-scripts playwright@1.61.0
"$playwright_dir/node_modules/.bin/playwright" install chromium
NODE_PATH="$playwright_dir/node_modules" node tests/pwa_matrix.cjs
```

Set `PWA_MATRIX_CHROMIUM` to run against an installed Chromium executable. The matrix does not
cover Safari or iOS. Installing on an iPhone and updating from v1 to v2 remain manual acceptance
steps on the Studio test app (#98).

## Checks

Commands and requirements for changes are under [Contributing](../CONTRIBUTING.md). PostgreSQL contract tests run only with `RUN_PG_CONTRACT=1` and a test database connection whose role may create and delete additional temporary databases. [CI](../.github/workflows/ci.yml) uses a separate PostgreSQL service for this. [Providers and operations](providers-and-operations.md) covers provider and Studio setup.
