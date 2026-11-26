<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/readme-header-dark.png">
    <img alt="liam-e" src="docs/assets/readme-header-light.png" width="100%">
  </picture>
</h1>

[![CI](https://github.com/plagor44-ux/liam-e/actions/workflows/ci.yml/badge.svg)](https://github.com/plagor44-ux/liam-e/actions/workflows/ci.yml)
[![CodeQL](https://github.com/plagor44-ux/liam-e/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/plagor44-ux/liam-e/actions/workflows/github-code-scanning/codeql)
[![License: Apache-2.0](https://img.shields.io/github/license/plagor44-ux/liam-e)](LICENSE)

liam-e brings several mail accounts into one inbox and adds an assistant you talk to in chat.
The assistant searches your mail, reads and summarizes messages and drafts replies, but it sends
or changes nothing until you confirm its proposal. liam-e is a single-user app for
[Mistral Studio](https://docs.mistral.ai/studio/apps/deploy): each installation belongs to one
person, and the Studio deployment decides who can open it. The interface is in German and
English.

liam-e is under active development. Issues and pull requests are welcome; see
[Contributing](#contributing).

**[Documentation](#further-documentation) · [Start locally](#start-locally) · [Deploy](#deploy) ·
[Roadmap](docs/roadmap.md) · [Contributing](CONTRIBUTING.md) ·
[Discussions](https://github.com/plagor44-ux/liam-e/discussions) · [Security](SECURITY.md)**

<img alt="The liam-e inbox with an opened mail, its summary and due date, showing synthetic sample data" src="docs/assets/screenshot-inbox-light.png">

<details>
<summary>More screenshots</summary>

A proposal card in chat: the assistant suggests unsubscribing from five newsletters, and nothing
happens until you confirm.

<img alt="A chat proposal card listing five newsletters to unsubscribe from, with confirm and cancel buttons" src="docs/assets/screenshot-chat-dark.png">

On a phone:

<img alt="The inbox on a narrow phone screen" src="docs/assets/screenshot-mobile-inbox-light.png" width="260">

A short tour from the inbox to an opened mail and a proposal card in chat:

<img alt="Animation: the inbox, an opened mail with its summary, then a chat proposal card" src="docs/assets/demo.gif" width="800">

All screenshots use the synthetic sample data from `api.mail.dev_seed`.

</details>

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

## Why liam-e

- **Confirmation is enforced by the server.** The assistant can search, read and draft on its
  own, but sending and every change it proposes wait for your confirmation. The model cannot
  skip that step.
- **One index for all accounts.** Gmail, Outlook and any IMAP account sit in the same inbox and
  the same search, so one question in chat covers all of them.
- **Built on Mistral.** Chat, summaries and voice use Mistral's models, and the app runs in
  Mistral Studio.
- **One person, one installation.** The mail index, the chat history and the encrypted
  credentials or references to them live in the database of your own Studio app.

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

## What you need

| Goal | What you need |
| --- | --- |
| Look around with sample data | Bun, Node.js, uv and Postgres with pgvector on your machine. No mail account and no Mistral access. |
| New chat answers locally | A Mistral API key as well ([steps](docs/local-development.md#chat-with-a-mistral-api-key)). |
| Your own mailboxes | Access at your mail provider: a Studio connector, your own OAuth2 client or an IMAP app password ([provider guide](docs/providers-and-operations.md)). |
| Deploy | A Mistral Studio organization whose plan includes Apps ([Mistral's Apps overview](https://docs.mistral.ai/studio/apps/overview)). |

## Start locally

With Docker, `docker compose up --build` starts the database, sample data, API and UI in one step
([Compose guide](docs/local-development.md#start-everything-with-compose)). GitHub Codespaces and
VS Code Dev Containers start the same stack from `.devcontainer/`
([guide](docs/local-development.md#codespaces-and-dev-containers)).

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
bun run dev:web
```

The UI runs at [localhost:3001](http://localhost:3001) and reaches the API through its own `/api`
proxy. The [health check](http://127.0.0.1:3000/api/health) is also available directly on the API.

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
spaces. Issues labelled `good first issue` or `help wanted` are a good start, and the
[roadmap](docs/roadmap.md) shows the direction. Ask questions and share ideas in
[Discussions](https://github.com/plagor44-ux/liam-e/discussions). Report vulnerabilities
privately ([SECURITY.md](SECURITY.md)), not in an issue.

## Built to work with coding agents

The repository is set up so that coding agents can contribute under the same rules as people:

- [AGENTS.md](AGENTS.md) holds the architecture, commands, conventions and the security and
  publication rules. Its index points to the domain rules in [docs/agents/](docs/agents/), which
  agents read before working in an area. [CLAUDE.md](CLAUDE.md) only imports AGENTS.md.
- [DESIGN.md](DESIGN.md) governs UI tokens, components and how the interface text sounds.
- Every issue follows one of the [issue forms](.github/ISSUE_TEMPLATE/);
  `tools/issue_form.py` creates and checks issues in that shape.
- CI guards the rules: a hygiene test rejects tenant IDs, private links, home paths and
  realistic-looking email addresses, gitleaks scans the whole history for secrets, and a drift
  check keeps the generated API client in step with the API.
- [`openwiki/`](openwiki/quickstart.md) is a generated index of the code base for orientation;
  it may lag behind the code.
- `.agents/skills/` holds agent skills for the stack; [NOTICE](NOTICE) names their origin.

## Further documentation

- [Architecture and domains](docs/architecture.md)
- [Providers, synchronization and Studio deployment](docs/providers-and-operations.md)
- [Local development and checks](docs/local-development.md)
- [Roadmap](docs/roadmap.md)
- [UI and text rules](DESIGN.md)
- [OpenWiki](openwiki/quickstart.md), generated and possibly behind the code

The [minimal Mistral chat app](apps/chat-app/README.md) is a separate sub-application.
The mail app uses [`apps/web`](apps/web/) and [`apps/api`](apps/api/).

## License

liam-e is licensed under the [Apache License, Version 2.0](LICENSE).
[NOTICE](NOTICE) lists the third-party material it contains or builds on: the Mistral Apps
scaffold and agent skills, the FastAPI skill (MIT), the font (OFL-1.1), the icons (ISC) and
the origin of the bundled audio sample. Dependencies keep their own licenses.
