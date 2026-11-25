<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/readme-header-dark.png">
    <img alt="liam-e" src="docs/assets/readme-header-light.png" width="100%">
  </picture>
</h1>

liam-e brings several mail accounts into one inbox and adds an assistant you talk to in chat.
The assistant searches your mail, reads and summarizes messages and drafts replies, but it sends
or changes nothing until you confirm its proposal. liam-e is a single-user app for
[Mistral Studio](https://docs.mistral.ai/studio/apps/deploy): each installation belongs to one
person, and the Studio deployment decides who can open it. The interface is in German and
English.

liam-e is under active development. Issues and pull requests are welcome; see
[Contributing](#contributing).

## Features

| Area | What it does |
| --- | --- |
| **Inbox and accounts** | One inbox for Gmail, Outlook, iCloud, Yahoo and other IMAP/SMTP providers. Search, filter and read mail; archive, trash and mark it read with undo and configurable swipe gestures. Summaries and detected deadlines appear on the mail, and newsletters can be unsubscribed. [Provider matrix](docs/providers-and-operations.md) |
| **Chat** | Ask about the accounts you have allowed in chat, add a mail as context, pick the Mistral model and read streamed answers. New mail, replies and forwards, and changes to mail, calendar and tasks, arrive as proposal cards that you confirm or cancel. [Chat and confirmation flow](docs/architecture.md#from-mail-sync-to-confirmation) |
| **Calendar** | Month, week, day and list views over Google calendars (through the Studio connector) and ICS subscriptions. Single events without other guests in your own Google calendars can be created, changed and deleted. |
| **Tasks** | One-off and recurring tasks with due dates, created by hand or proposed in chat. |
| **Personal settings** | Templates for your own instructions, preferences such as answer length or signature (off until you turn them on), skills such as a daily briefing, swipe actions and app language. |
| **Voice** | Dictation into the chat and read-aloud of answers through Mistral's audio models, once a deployment enables them. |

Gmail and Outlook connect through Mistral Studio connectors or your own OAuth2 client. iCloud,
Yahoo and other providers connect through IMAP/SMTP with an app password. Not every path has
been tested live: the [provider guide](docs/providers-and-operations.md) shows what code and
tests cover and what still needs evidence at the provider. Chat answers need server-side
Mistral access.

## Security model

- **Nothing is sent or changed without you.** The assistant may search, read and summarize.
  Sending, replying, forwarding, mail changes and calendar or task changes from chat run only
  after you confirm the proposal card; the server enforces this, not the model. Actions you
  start yourself in the inbox run directly.
- **Credentials are encrypted.** OAuth tokens and IMAP secret references are encrypted with
  Fernet before they are stored; the key is a Studio workspace secret. IMAP app passwords stay
  in Studio's workspace secrets, and liam-e stores only the secret's name.
- **One user, access from the deployment.** liam-e has no user management. The Studio
  deployment and its gateway login decide who may open it.
- **Narrow data paths.** The chat sees only the mail accounts you allow in settings. Mail and
  calendar text reaches the model labelled as untrusted data. Outgoing HTTPS calls are checked
  against private destinations and DNS rebinding.

The full rules are in [AGENTS.md](AGENTS.md#security). Report suspected vulnerabilities
privately as described in [SECURITY.md](SECURITY.md).

## Start locally

With Docker, `docker compose up --build` starts the database, sample data, API and UI in one step
([Compose guide](docs/local-development.md#start-everything-with-compose)).

You need Bun 1.4.0, Node.js 24, uv (Python 3.14) and a local Postgres 16 with pgvector. The
synthetic sample data shows the app without mail accounts or Mistral access. The
[local development guide](docs/local-development.md) covers each step, including a disposable
database container.

```bash
bun run install-all
export DATABASE_URL='postgresql+asyncpg://postgres:test-only@127.0.0.1:55432/liam_e_dev'
bunx nx run db:migrate
bash tools/uv.sh run --no-sync python -m api.mail.dev_seed
MAIL_WORKER_ENABLED=false MISTRAL_SECRETS_ENABLED=false TELEMETRY_ENABLED=false bunx nx run api:serve
```

In a second terminal:

```bash
VITE_API_URL=http://127.0.0.1:3000 bun run dev:web
```

The UI runs at [localhost:3001](http://localhost:3001). The
[health check](http://127.0.0.1:3000/api/health) is available directly on the API.

## Deploy

liam-e runs as an app in Mistral Studio. [`mistral.json`](mistral.json) describes the web module,
the API module and a managed Postgres database, and carries no Studio IDs. In your own checkout,
`mistral deploy` therefore creates a new app in your workspace; configure its runtime values and
secrets in Studio as the [deployment guide](docs/providers-and-operations.md#deployment-in-mistral-studio)
describes.

Keep the app's IDs out of the repository: the hygiene test rejects a `deployment` block in
`mistral.json`. Put them into the git-ignored `mistral.deployment.json` instead and deploy later
commits from `origin/main` with:

```bash
bash tools/deploy.sh <commit>
```

The script deploys a clean copy of the commit to the existing app and stops if the ID file is
missing, so it never creates a second app.

## Contributing

Issues and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) explains the setup,
the checks and the rules, and the [Code of Conduct](CODE_OF_CONDUCT.md) applies in all project
spaces. Report vulnerabilities privately ([SECURITY.md](SECURITY.md)), not in an issue.

## Further documentation

- [Architecture and domains](docs/architecture.md)
- [Providers, synchronization and Studio deployment](docs/providers-and-operations.md)
- [Local development and checks](docs/local-development.md)
- [UI and text rules](DESIGN.md)
- [OpenWiki](openwiki/quickstart.md), generated and possibly behind the code

The [minimal Mistral chat app](apps/chat-app/README.md) is a separate sub-application.
The mail app uses [`apps/web`](apps/web/) and [`apps/api`](apps/api/).

## License

liam-e is licensed under the [Apache License, Version 2.0](LICENSE).
[NOTICE](NOTICE) lists the third-party material it contains or builds on: the Mistral Apps
scaffold and agent skills, the FastAPI skill (MIT), the font (OFL-1.1), the icons (ISC) and
the origin of the bundled audio sample. Dependencies keep their own licenses.
