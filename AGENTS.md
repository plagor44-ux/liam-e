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

### UI routes

- `/` — chat start with greeting, account status, quick actions and composer; optionally with mail context.
- `/chat/$conversationId` — conversation with history, replies and proposal cards.
- `/mail` — inbox with list and filters; `/mail/$messageId` — opened mail with reading pane.
- `/accounts` — account overview, connect, sign in again and remove.
- `/tasks` and `/tasks/$taskId` — task list, manual creation and due dates with history.
- `/settings` and `/settings/prompts` — settings with template library; `settingsSections` is the navigation entry point for further implemented areas.
- `/settings/prompts/new` and `/settings/prompts/$promptId` — create or edit a template in a separate view at all screen sizes.
- `/settings/preferences`, `/settings/preferences/new` and `/settings/preferences/$preferenceId` — preferences, usage toggle and separate create/edit views, including on mobile.

### App language

`/settings/language` saves the single owner’s language in the shared `ui_settings` row with the
same revision check as swipe settings. Existing rows default to `browser`. In automatic mode
the web server uses the browser’s `Accept-Language` list, with English as the fallback when
neither German nor English is accepted. A cookie mirrors an explicit setting after the browser
confirms it with the API. It lets subsequent documents start in that language during hydration.
A new device starts with its browser preference until its first settings read. Re-focusing a tab
reloads the setting from the server.

Chat and insight requests send the browser language. The saved override always takes precedence;
in browser mode requests use their browser hint, and background jobs use the last confirmed
browser hint or English if none exists. Chat replies, drafts, speech recognition and speech voice
follow that effective language regardless of the current message language. Insights store their
language and are regenerated when it differs, while seed accounts never call the model. Changes to the
mail body continue to invalidate its insight. Direct preference requests support both languages
(for example “Remember this”). Automatic suggestions retain the existing German phrase and
evidence checks.

### Mail domain (`apps/api/src/api/mail/`)

- `agent.py` — streamed chat turn, tool loop and stored history.
- `agent_tools.py` — read-only agent tools and proposal creation. Task tools delegate to `api/tasks/chat.py`; typed task commands and their local execution live in `api/tasks/proposals.py`.
- `agent_prompt.py` — System instructions for the agent, including the effective app language as the response and draft language.
- `chat_events.py` — typed streaming events; `chat_schemas.py` — chat output models.
- `actions.py` — index changes and one-time undo; `action_backends.py` — provider actions.
- `proposals.py` — store, confirm, cancel and execute proposals after confirmation.
- `newsletters.py` — detect newsletters and execute supported unsubscribe methods.
- `insights.py` — generate and cache summaries and deadlines; without mail text there is no model call, but an empty summary.
- `original.py` — retrieve mail headers and source read-only from the provider; none is stored.
- `jobs.py` — durable jobs and periodic sync scheduling.
- `sync.py` — apply incremental index changes and reread outdated body text; `sync_connector.py`, `sync_gmail.py`,
  `sync_graph.py`, `sync_imap.py` — fetching, parsing and rereading (`fetch_bodies`) per provider; `sync_types.py` — shared sync types and helpers,
  including `body_text`/`html_to_text` (one text version for all providers: `text/plain` first, unless it is only a short
  placeholder alongside longer HTML; otherwise HTML without styles, scripts, comments and invisible padding characters, with paragraphs and
  bullets), `BODY_VERSION` and `reread_http` (rereading via REST, evaluated per mail).
- `send.py` — sending via SMTP, Gmail, Graph or connector after confirmation.
- `connectors.py` — access to Mistral Studio connectors and their tools.
- `providers.py` — provider registry, OAuth endpoints and IMAP/SMTP presets.
- `service.py` — account and credential management; `crypto.py` — Fernet encryption.
- `secrets.py` — resolve workspace secrets at runtime.
- `netguard.py` — secure outgoing HTTPS calls against private destinations and DNS rebinding.
- `transport.py` — mail host policy and TLS transports.
- `headers.py` — process address and unsubscribe headers.
- `state.py` — derive account status and colors from accounts, sync status and jobs.
- `models_catalog.py` — cache the workspace's chat/tool-capable models for ten minutes. Public
  uncached text input and output rates are dated snapshots from Mistral's price list or a linked
  model card. A moving alias needs a unique workspace binding to a priced ID. Missing catalogue
  data yields unknown availability. Updating prices requires checking the linked source and date.
- `dev_seed.py` — populate the local database with sample data from the design boards. Seed accounts never reach a provider: sending and unsubscribing are rejected, actions only change the index, syncs are skipped, and summaries are never generated by a model (only seeded ones are retrievable; otherwise `503 insight_unavailable`).

Data flow: An account connects through a connector, OAuth2 redirect or IMAP app password.
OAuth credentials or the IMAP secret reference are stored **Fernet-encrypted** in
`mail_credential`; connector accounts need no credential entry there. A sync job fills
`mail_message`. `POST /api/v1/chat` or `/api/v1/chat/stream` lets the agent work on the index.
It creates proposals for changes and sending; `POST /api/v1/chat/proposals/{proposal_id}/confirm`
executes selected entries after confirmation, `.../cancel` discards them. The agent changes or
sends nothing without a confirmed card. Individual inbox actions initiated by the user instead
run directly through the mail action endpoints.

A trash action moves the selected provider message and stores its local folder and undo record
together after the provider confirms the move. Trash and undo invalidate older sync batches for
that account. A later listing that repeats a trashed message needs a current, message-specific
INBOX read before it can bring the message back. Gmail uses the message ID, Graph also tracks
the ID from before the move, and IMAP checks the source mailbox's UIDVALIDITY and UID.
Normal polls also check existing INBOX/TRASH duplicates with no new provider events. Label
providers compare the exact message ID, Graph uses the recorded pre-move ID, and IMAP checks
the current INBOX UID flags. An ambiguous Studio thread keeps its existing rows until the
provider supplies message-specific evidence.

Studio also rotates point reads through stored trash, so a restore outside the recent search
window can reach the inbox without a new message in the thread. For native moves the action
records the exact departing inbox ID, including an undo that moves a restored message back
to trash. IMAP binds that ID to the source UIDVALIDITY. Its sync cursor stores `UIDVALIDITY:last_uid`.
The worker rechecks `\Deleted` deliveries in `IMAP_DELETED` rows until the flag clears, the UID
disappears, or the mailbox generation changes.

### API endpoints

Inventory from `apps/api/openapi.json`; each row names method, path and `operation_id`.

| Router group | Method | Path | `operation_id` |
| --- | --- | --- | --- |
| Health | GET | `/api/health` | `health` |
| Agent read facade | GET | `/api/agent-read/v1/mail/accounts` | `liam_mail_accounts` |
| Agent read facade | GET | `/api/agent-read/v1/mail/messages` | `liam_mail_search` |
| Agent read facade | GET | `/api/agent-read/v1/mail/messages/{message_id}` | `liam_mail_read` |
| Agent read facade | GET | `/api/agent-read/v1/mail/summary` | `liam_mail_summary` |
| Audio | GET | `/api/v1/audio/capabilities` | `audio_capabilities_get` |
| Audio | POST | `/api/v1/audio/transcriptions` | `audio_transcribe` |
| Audio | POST | `/api/v1/audio/speech` | `audio_speech` |
| Audio live test | POST | `/api/v1/audio/live/status` | `audio_live_status` |
| Audio live test | POST | `/api/v1/audio/live/speech` | `audio_live_speech` |
| Audio device check | GET | `/api/v1/audio/device-test` | `audio_device_test_get` |
| Audio device check | POST | `/api/v1/audio/device-test/recording` | `audio_device_test_record` |
| Audio device check | GET | `/api/v1/audio/device-test/sample` | `audio_device_test_sample` |
| Calendar | GET | `/api/v1/calendar/connections/google/calendars` | `calendar_google_calendars_list` |
| Calendar | POST | `/api/v1/calendar/connections/google` | `calendar_google_connect` |
| Calendar | POST | `/api/v1/calendar/feeds/validate` | `calendar_feed_validate` |
| Calendar | POST | `/api/v1/calendar/feeds` | `calendar_feed_subscribe` |
| Calendar | POST | `/api/v1/calendar/feeds/validations/discard` | `calendar_feed_validation_discard` |
| Calendar | GET | `/api/v1/calendar/sources` | `calendar_sources_list` |
| Calendar | PATCH | `/api/v1/calendar/sources/{source_id}` | `calendar_source_update` |
| Calendar | DELETE | `/api/v1/calendar/sources/{source_id}` | `calendar_source_delete` |
| Calendar | POST | `/api/v1/calendar/sources/{source_id}/sync` | `calendar_source_sync` |
| Calendar | GET | `/api/v1/calendar/occurrences` | `calendar_occurrences_list` |
| Calendar | GET | `/api/v1/calendar/occurrences/{occurrence_id}` | `calendar_occurrence_get` |
| Calendar | GET | `/api/v1/calendar/occurrences/{occurrence_id}/live` | `calendar_occurrence_live` |
| Calendar | PATCH | `/api/v1/calendar/occurrences/{occurrence_id}` | `calendar_event_update` |
| Calendar | DELETE | `/api/v1/calendar/occurrences/{occurrence_id}` | `calendar_event_delete` |
| Calendar | POST | `/api/v1/calendar/events` | `calendar_event_create` |
| Calendar | GET | `/api/v1/calendar/writes/{write_id}` | `calendar_write_get` |
| Calendar | POST | `/api/v1/calendar/writes/{write_id}/check` | `calendar_write_check` |
| Templates | GET | `/api/v1/prompts` | `prompts_list` |
| Templates | POST | `/api/v1/prompts` | `prompt_create` |
| Templates | GET | `/api/v1/prompts/{id}` | `prompt_get` |
| Templates | PATCH | `/api/v1/prompts/{id}` | `prompt_update` |
| Templates | DELETE | `/api/v1/prompts/{id}` | `prompt_delete` |
| Preferences | GET | `/api/v1/preferences` | `preferences_list` |
| Preferences | GET | `/api/v1/preferences/candidates` | `preference_candidates_list` |
| Preferences | POST | `/api/v1/preferences/candidates/{candidate_id}/propose` | `preference_candidate_propose` |
| Preferences | POST | `/api/v1/preferences` | `preference_create` |
| Preferences | PATCH | `/api/v1/preferences/{id}` | `preference_update` |
| Preferences | DELETE | `/api/v1/preferences/{id}` | `preference_delete` |
| Preferences | GET | `/api/v1/preferences/contexts/{context_id}` | `preference_context_get` |
| Settings | GET | `/api/v1/settings` | `settings_get` |
| Settings / independent preference opt-ins | PATCH | `/api/v1/settings` | `settings_update` |
| Settings | GET | `/api/v1/settings/calendar` | `calendar_settings_get` |
| Settings | PATCH | `/api/v1/settings/calendar` | `calendar_settings_update` |
| Settings | GET | `/api/v1/settings/chat` | `chat_settings_get` |
| Settings | PATCH | `/api/v1/settings/chat` | `chat_settings_update` |
| Settings | GET | `/api/v1/settings/ui` | `settings_ui_get` |
| Settings | PATCH | `/api/v1/settings/ui` | `settings_ui_update` |
| Settings | POST | `/api/v1/settings/ui/browser-locale` | `settings_ui_browser_locale_confirm` |
| Skills | GET | `/api/v1/skills` | `skills_list` |
| Skills | POST | `/api/v1/skills` | `skill_create` |
| Skills | GET | `/api/v1/skills/tools` | `skill_tools_list` |
| Skills | GET | `/api/v1/skills/{skill_id}` | `skill_get` |
| Skills | PATCH | `/api/v1/skills/{skill_id}` | `skill_update` |
| Skills | DELETE | `/api/v1/skills/{skill_id}` | `skill_delete` |
| Chat | POST | `/api/v1/chat` | `chat_turn` |
| Chat | POST | `/api/v1/chat/stream` | `chat_stream` |
| Chat | GET | `/api/v1/chat/conversations` | `chat_conversations_list` |
| Chat | PATCH | `/api/v1/chat/conversations/{conversation_id}` | `chat_conversation_update` |
| Chat | GET | `/api/v1/chat/references/{message_id}` | `chat_mail_reference_get` |
| Chat | GET | `/api/v1/chat/history` | `chat_history` |
| Chat | GET | `/api/v1/chat/models` | `chat_models_list` |
| Chat | POST | `/api/v1/chat/proposals/{proposal_id}/confirm` | `chat_proposal_confirm` |
| Chat | POST | `/api/v1/chat/proposals/{proposal_id}/cancel` | `chat_proposal_cancel` |
| Chat | POST | `/api/v1/chat/proposals/{proposal_id}/check` | `chat_proposal_check` |
| Mail / accounts | GET | `/api/v1/mail/accounts` | `mail_accounts_list` |
| Mail / accounts | POST | `/api/v1/mail/accounts/imap` | `mail_account_connect_imap` |
| Mail / accounts | GET | `/api/v1/mail/accounts/oauth/{provider}/authorize` | `mail_account_oauth_authorize` |
| Mail / accounts | GET | `/api/v1/mail/accounts/oauth/{provider}/callback` | `mail_account_oauth_callback` |
| Mail / accounts | GET | `/api/v1/mail/accounts/options` | `mail_account_options` |
| Mail / accounts | DELETE | `/api/v1/mail/accounts/{account_id}` | `mail_account_delete` |
| Mail / accounts | PATCH | `/api/v1/mail/accounts/{account_id}` | `mail_account_update` |
| Mail / accounts | POST | `/api/v1/mail/accounts/{account_id}/sync` | `mail_account_sync` |
| Mail / actions | POST | `/api/v1/mail/actions/undo` | `mail_actions_undo` |
| Mail / connectors | GET | `/api/v1/mail/connectors` | `mail_connectors_list` |
| Mail / connectors | POST | `/api/v1/mail/connectors/{name}/connect` | `mail_account_connect_connector` |
| Mail / connectors | GET | `/api/v1/mail/connectors/{name}/tools` | `mail_connector_tools_list` |
| Mail / messages | GET | `/api/v1/mail/messages` | `mail_messages_list` |
| Mail / messages | POST | `/api/v1/mail/messages/actions` | `mail_messages_bulk_action` |
| Mail / messages | GET | `/api/v1/mail/messages/{message_id}` | `mail_message_get` |
| Mail / messages | POST | `/api/v1/mail/messages/{message_id}/actions` | `mail_message_action` |
| Mail / messages | GET | `/api/v1/mail/messages/{message_id}/insight` | `mail_message_insight` |
| Mail / messages | GET | `/api/v1/mail/messages/{message_id}/original` | `mail_message_original` |
| Mail / summary | GET | `/api/v1/mail/summary` | `mail_summary` |
| Mail / sync | POST | `/api/v1/mail/sync` | `mail_sync_all` |
| Tasks | GET | `/api/v1/tasks` | `tasks_list` |
| Tasks | GET | `/api/v1/tasks/{series_id}` | `tasks_get` |
| Tasks | PATCH | `/api/v1/tasks/{series_id}` | `tasks_update` |
| Tasks | POST | `/api/v1/tasks/{series_id}/archive` | `tasks_archive` |
| Tasks | DELETE | `/api/v1/tasks/{series_id}` | `tasks_delete` |
| Tasks | POST | `/api/v1/tasks` | `tasks_create` |
| Tasks | POST | `/api/v1/tasks/preview` | `tasks_preview` |
| Tasks | POST | `/api/v1/tasks/reconcile` | `tasks_reconcile` |
| Tasks | GET | `/api/v1/tasks/occurrences` | `tasks_occurrences_list` |
| Tasks | POST | `/api/v1/tasks/occurrences/{occurrence_id}/complete` | `tasks_occurrence_complete` |
| Tasks | POST | `/api/v1/tasks/occurrences/{occurrence_id}/skip` | `tasks_occurrence_skip` |
| Tasks | POST | `/api/v1/tasks/occurrences/{occurrence_id}/reopen` | `tasks_occurrence_reopen` |

## Agent read facade

`api/mcp/registry.py` enumerates the four explicitly approved mail read operations;
`api/mcp/mail.py` reads only the existing INBOX index through accessors. The scope comes from a
`ReadGrant`, never request parameters. The default verifier in `api/mcp/access.py` rejects all
requests with 401. A future native transport must, after positive proof of access, bind its own
`ReadAccessVerifier` to `get_read_access_verifier` and call the same services with its verified
grant. Grants are limited to 100 account IDs; foreign selections return 403, unknown and foreign
mail IDs both return 404. Reading by ID also remains within the INBOX index: a mail moved since
then responds like a missing one. An empty grant returns empty results, never all accounts.
Relative links `/mail/<uuid>` avoid an unconfigured external origin. Mail data is foreign content,
not instructions. The index state is reported; provider freshness remains unknown. A successful
sync may contain only a limited page; without a stored completeness marker, the index remains
conservatively incomplete even with status `ok`. The timestamp denotes the last attempt, not
necessarily a success. The reduced spec is not runtime authorization; production exposure remains
blocked until access is proven. The Uvicorn access log removes query strings for these routes even
if the log entry format changes; gateway and proxy logs must independently receive the same
protection before release. The export is reproduced with `bunx nx run api:gen-agent-openapi`.

## Task domain

The UI under `apps/web/src/tasks/` uses the generated task endpoints. Creation shows a
server-side preview; `reconcile.ts` limits each fetch to a fixed horizon and resumes stored
progress on the next opening.

`api/tasks/` contains series creation, RRULE evaluation, reconciliation and state transitions.
The task routers create series and materialize the first bounded reconcile page in the same
transaction. Further pages are explicitly fetched via `POST /tasks/reconcile`. PATCH supports
individual changes and new schedule revisions.

`PATCH /tasks/{series_id}` requires the series version `expected_version`, the selected
`occurrence_id`, its `expected_occurrence_version`, `scope` and the new local start `start`.
The current schema edits due dates, not titles or notes; text fields are rejected so historical
occurrences are not implicitly renamed. `scope=occurrence` moves only one open occurrence and
retains its original key; precision (date/time), timezone and rule remain unchanged. Both
versions increase. `scope=this_and_following` selects a future open occurrence of the current
revision; its actual due date and original key must both still be in the future. Already due
occurrences are not used as split points. Stored fetch progress must prove completeness of the
old revision up to the exclusive split point; otherwise the split is rejected with
`409 task_version_conflict` and must be retried after further reconciliation. The old rule ends
exclusively before the original key; open future occurrences from there are replaced. Past,
completed and skipped occurrences remain, including already moved past exceptions. The new
revision may change date/time and timezone but does not start before the local split day; the
start must match the new rule. Without `rrule` the old rule remains; explicit `rrule: null`
creates a one-off task. A new revision needs a timezone even if the old date revision still has
`null`. Collisions with completed occurrences are checked using the same COUNT/DST evaluation as
materialization, including outside the first fetch page; they return `409 task_revision_conflict`
without changing tasks. Other version/state conflicts return `409 task_version_conflict`. All
changes and the first new reconcile page are in the caller's transaction; old revision cursors
are rejected.

Archiving and permanent deletion require the series `expected_version`. Archiving blocks new
materialization and cancels only open future occurrences in a conditional update; date-only tasks
due today and already due and completed occurrences remain. Reopening future occurrences of an
archived series is blocked. DELETE additionally requires explicit `confirm_history_loss: true`
in the JSON body and removes series, revisions and occurrences in the same transaction. Both
services take a session and do not commit themselves. Series locks serialize these actions with
reconcile and state transitions; stale versions yield 409, missing series 404.

`GET /tasks` and `GET /tasks/{series_id}` read only stored data in the server-side scope `app`.
Both require the inclusive local date window `due_from`/`due_to` (at most 366 days). The list is
paginated by series ID (`limit` 1–100, `cursor`) and filters `status=active|archived` and
`source=all|unlinked|account`; `account` requires `source_account_id`, `unlinked` means without a
source mail. Source references are not joined to live mail/account rows: removed sources leave
tasks and their filter association intact. The list contains series with overlapping rule validity
or a stored due date in the window; a matching series need not have a due date every day. Detail
returns the current revision and its own paginated occurrence list, optionally with occurrence
status. Per series, `complete` applies exclusively to the indicated window, independently of
occurrence pagination or status filter. It is derived conservatively from stored progress; a timed
day is complete only when the entire local day is covered. GET materializes nothing and calls no
model.

Reconcile takes a series ID, optional revision ID and cursor, `limit` (1–100) and `advance`.
Without a cursor, the call resumes stored progress under lock; a supplied stale cursor yields 409.
The response names revision ID, cursor, target time and `complete` (only for the fixed window from
series start to target). Only after completion, `advance: true` with its exact cursor starts a new
run up to server time plus 30 days. The last checked position remains in the completion cursor;
progress and insertion happen together in the caller's transaction.

`POST /tasks/preview` writes nothing and requires start, end and timezone. The window is limited
to 366 days and the page to 100 candidates; `complete: false` means the preview does not show the
whole window. It reports skipped local times and impossible month days of monthly rules. Under the
MCP inventory this read-only POST also remains outside approved agent interfaces.

Under RFC 5545, `COUNT` counts only existing due dates: nonexistent local times do not consume a
count. Preview and persistence use the same parser. Previously completed fetch runs of timed COUNT
series with an old completion marker are reevaluated page by page on the next reconcile; original
occurrence keys prevent duplicates and preserve state. This adds missing final occurrences without
a data migration.

New tasks require a valid IANA timezone even without a time; if missing, creation returns
`422 task_invalid`. The date remains a date. Old date revisions with `timezone: null` use UTC for
day boundaries without rewriting the data. Today is already due; overdue begins only the next local
day. A new series revision can explicitly set the timezone.

### Tasks in chat

`task_source=none|all|unlinked|account|account_and_unlinked` and, where applicable,
`task_source_account_id` form a separate visible scope in the composer. With mail account X
selected, the options are “Keine Aufgaben”, “Aufgaben dieses Kontos” and “Alle Aufgaben” (UI
text). The last explicitly stores `{"source":"account_and_unlinked","account_id":X}` and permits
only tasks of X plus personal tasks without account or mail association, never other accounts
(including their tasks without source mail). Reading, proposing and confirming enforce the same
boundary. Without a mail account selection, `none|all|unlinked|account` remain unchanged;
`account_and_unlinked` requires a selected mail account. The scope is in
`chat_context.task_scope_json` per turn; legacy history receives no access through the server
default `{"source":"none"}`. Any change requires a new conversation when history exists. Resolver
and agent recheck all historical contexts before model requests.
`search_tasks`/`get_task` read without reconcile, with an inclusive date window up to 90 days,
series and occurrence cursors and completeness per series/window; text is in
`untrusted_task_data`. `propose_task` creates a card for creation, due-date changes, completion,
skipping, reopening or archiving, never permanent deletion. Sources and display data come from
server-side reads. The shared proposal model binds target versions and source; new tasks can be
edited in the card without changing the source. Confirm accepts only a typed creation `task_edit`
for this. The preview is reread when the schedule changes. Claim, renewed source/version check,
task mutation and successful completion are in exactly one DB transaction. Errors before commit
leave the card `pending`; a second confirm after commit returns 409 `proposal_not_pending`, as in
the mail path, without a second effect. Mail/sending actions retain their existing provider path.
A removed source mail is not replaced. “Weitere Aktionen → Aufgabe vorschlagen” on an open mail
starts a new conversation with explicit mail and task account scope; mail text is not copied as a
task description. After confirm the card invalidates the `tasks` query group.

## Calendar domain

The UI at `/calendar` is a calendar with the views month, week, day and list. View and date are
in the URL (`/calendar?view=week&date=2026-11-18`); invalid values fall back to week (on mobile:
day) and today. Week and day show an all-day row and an hour grid with a now line, placed on the
wall clock of the hour labels (`wallMinute`, `dayHours` in `grid.ts`; a nonexistent hour gets no
slot) with lanes that count the drawn minimum height; month shows six
weeks from Monday with up to three entries per day and “+N weitere”; list is the earlier agenda
with its from and to fields. `useRangeOccurrences` loads the visible range from the index endpoint
and follows up to ten cursor pages of 100; beyond that the view says that not everything was
loaded. `/calendar/$occurrenceId` opens a stable occurrence: on desktop as a panel beside the
grid, otherwise as its own view. The detail reads the live event (`calendar_occurrence_live`) and
offers “Bearbeiten” and “Löschen” for an editable event of a writable source, otherwise “Nur lesbar”
with a fixed reason. A free slot or “Neuer Termin” opens the create dialog when a writable source
exists. A write whose outcome is `unknown` or `running` stays registered in the browser after its
dialog closes (`apps/web/src/calendar/write-state.ts`): the calendar offers “Prüfen”, blocks new
appointments while a create is open and blocks edit and delete of the affected appointment, so the
same intent is never sent again under a new write ID. A page reload forgets the registry; the
server record remains, and a check by write ID still resolves it.
`is_active` remains the permission: paused sources appear in management and as a
notice, but their appointments remain hidden until activation. They do not contribute to a “Keine Termine” claim and remain blocked for chat tools and old references (decision for #22: option A).
`CalendarSetup` in `apps/web/src/calendar/FeedSetup.tsx` offers “Kalender abonnieren”
(`FeedSetup`: name, private calendar address and explicitly displayed fallback zone) and “Google
Kalender verbinden” (`GoogleSetup`: current calendar list with checkboxes, no new selections
preselected, existing connections checked and disabled). Quoted labels here are UI text. The
Google dialog keeps the list locally only and rereads it on next opening. After connecting, the
`calendar` query group is invalidated. `webcal` visibly becomes `https`; server validation
normalizes independently again. Address and validation token remain only in dialog state, never
browser storage or query cache. Changes discard validation; subscribing and changing the address
invalidate the `calendar` query group.

Google calendars connect only through the Studio connector `google_calendar`, with no own OAuth
fallback. Reading goes through `api/calendar/google.py`, which contains shared pagination and
mapping for the production adapter and offline probe; writing is described in the next subsection. Only `list_calendars` and `list_events` are allowed
there; mail allowlists remain unchanged. Management reads all calendar pages (at most ten),
shows no descriptions and atomically creates selected sources with `capabilities=["read"]` and
an initial sync job. Repeats leave existing sources unchanged. Because it calls the provider and
returns private calendar names, `calendar_google_calendars_list` remains browser-only and does
not belong in the Studio read list.
The connection binds `connection_ref` to `google_calendar#<fingerprint>`: SHA-256 of
`google_calendar:` plus the top-level `summary` of a primary calendar event listing, shortened to
16 hexadecimal characters. Before and after each snapshot listing this identity is rechecked with
a small primary-calendar window; a missing calendar is also reported as `reconnect_required`
when identity changed. Mismatches block sync without changing the index. An account switch
exactly between the final check and storage remains possible without provider support.
A different connected Google account creates new sources, never takes over old ones. This is the
connector's available identity signal, not an immutable Google account ID; a changed primary
calendar name can therefore also require reconnection, and two accounts with the same primary
calendar name cannot be distinguished.
Snapshots read every page of the fixed window today −30 to +180 days, at most 40 pages and
10,000 occurrences. Missing pages, errors and limits never produce a partial snapshot.
Occurrence ID and `recurringEventId` preserve moved occurrences and series association; missing
cancelled occurrences disappear only on a complete snapshot replacement. Revision fence and
`replace_all` apply as for ICS. Success explicitly resets scheduling with `reset_schedule`,
independently of encrypted ICS HTTP validators. `CALENDAR_GOOGLE_SYNC_INTERVAL_MINUTES` defaults
to 5. The shared worker checks Google and ICS every minute. `reconnect_required` and
`calendar_unavailable` stop automatic attempts until manual sync or resume;
`provider_unavailable` uses bounded backoff. Removal cleans up only locally, never at Google.
Read evidence and open live questions are in `docs/superpowers/probes/2026-11-18-google-kalender.md`.

### Writing to Google calendars (#23)

Own single appointments can be created, changed and deleted through the same connector
(owner decision: path A). `api/calendar/google_write.py` allows only `create_event`,
`update_event`, `delete_event` and `get_event`; the read allowlist above is unchanged. A source is
writable when it is active, comes from Google and has the capability `write`, which a successful
snapshot sets when the listing's `accessRole` is `owner` or `writer`. An event is editable when it
is not cancelled, not part of a series, has no attendees other than the owner, has an own or
missing organizer, and has event type `default`. The type is compared case-insensitively because
the connector reports `DEFAULT`. Seed sources write only to the index and never call the connector.

The connector has no precondition and no idempotency key, so `api/calendar/writes.py` supplies both:

- **Claim:** every write carries a client-generated `write_id`. `calendar_write` claims it
  atomically under the source lock together with a hash of the request. The same ID with the same
  request returns the first outcome; with another request it is a 409. A partial unique index
  allows one running write per target event.
- **Live comparison:** before an update or delete, `get_event` reads the event. If its `updated`
  differs from the expected revision, the write ends as `not_applied` with `calendar_event_changed`
  and the current values, without sending anything. A change in Google between this read and the
  write remains possible; the dialog and the card say so.
- **Dispatch:** right before the call `mark_dispatch` records the dispatch marker and restarts the
  expiry clock in the same update. An expired claim sends nothing. Every write sends
  `notificationLevel: "NONE"`, waits at most 30 seconds and is never retried automatically.
- **Outcome:** `done` updates the index together with the record. `failed` is used only for a
  definite refusal: a 4xx text saying "not applied", a validation-error text, or a check before
  sending. Everything else is `unknown`, and a `running` record older than 120 seconds counts as
  unknown.
- **Check:** `check` resolves an unknown record by reading back; it needs read access only, so a
  calendar that lost write access since can still be reconciled. An update counts as done when
  `get_event` shows the requested values. A delete counts as done when the event is cancelled, or
  when it is missing and the calendar can still be listed. A create lists its time window without a
  text filter and compares locally: the stored dispatch marker and exactly one matching event
  with a creation time strictly after dispatch are required for success. A missing timestamp or
  one within five minutes before dispatch keeps the outcome open without proving success. An
  additional match with an indeterminate timestamp prevents a success claim. A create becomes `not_applied` when the claim expired before dispatch, or when no candidate keeps the
  outcome open after five minutes. Legacy records lack a dispatch marker and cannot prove a
  create succeeded. Terminal states clear the stored request text.
- **Source removal:** finished write records go with the source. Records whose outcome is open
  (`running` or `unknown`) stay as `unknown` without their request text, so a later check still
  reports the outcome as open instead of a failure that would invite a duplicate.

Error codes are fixed: `calendar_event_changed`, `calendar_event_gone`,
`calendar_event_not_editable`, `calendar_not_writable`, `calendar_out_of_scope`,
`calendar_time_invalid`, `calendar_unavailable`, `calendar_write_conflict`,
`calendar_write_in_progress`, `calendar_write_not_applied`, `calendar_write_rejected`,
`calendar_write_unknown`, plus the shared `proposal_context_revoked` and `reconnect_required`.
Logs name only these codes or an exception class, never titles, descriptions, locations, calendar
IDs or provider text.

Local times are `YYYY-MM-DDTHH:MM` in an IANA zone; a time that does not exist in that zone is
rejected and an ambiguous one takes the first offset. Timed appointments last more than zero and at
most 14 days. All-day appointments use an exclusive end date (the connector does not check this
itself) and span at most 366 days.

Browser routes: `POST /calendar/events` creates, `PATCH` and `DELETE /calendar/occurrences/{id}`
change and delete, `GET /calendar/occurrences/{id}/live` reads the event from Google for a dialog,
and `GET /calendar/writes/{write_id}` and `POST .../check` read or resolve a record. Answers:
201 or 200 for done, 202 for unknown or running, 409 for `calendar_event_changed` (with the
current values) and other conflicts, 403 for a source or event that may not be written, 404 for a
missing event or source. Validation errors are 422 `calendar_time_invalid` without echoing input.

In chat, `propose_calendar_change` creates a card of kind `calendar` for one create, update or
delete. It checks the turn's calendar scope, writability and live editability, and binds the live
revision. Confirmation uses the existing confirm endpoint with the card ID as `write_id`; the
context, scope, source and connection are checked again first. An unknown card, or a card left
running by an interrupted confirm, is resolved with `POST /chat/proposals/{proposal_id}/check`
(`chat_proposal_check`). A card whose write was never claimed fails, because nothing was sent: for
a running card the check first takes the card's write ID as a final `not_applied` record, so a
confirm that is still alive finds it taken at its own claim and sends nothing. Removing a calendar
deletes its finished records, so without the calendar a missing record proves nothing: such a card
ends with `calendar_unavailable`, never with "not applied". A late check preserves a card's
terminal result. The private `connection_ref` is stored with the card but never sent to the browser, SSE or history,
and cards do not keep the provider's `htmlLink`. The prompt rules are in `agent_prompt.py`.

The live run of 2026-11-18 is in `docs/superpowers/probes/2026-11-18-google-calendar-write-probe.md`.
All views group and display instants in the browser timezone; all-day entries stay on their
date. Local day boundaries are converted to instants. Because the index endpoint accepts only
whole UTC dates, the query window is rounded outward and also includes all-day dates. In the list,
windows exceeding 90 UTC days are split into adjacent subwindows, which like cursor pages are
loaded only explicitly; the grid views never exceed six weeks. Boundary results outside selected local start days are hidden and duplicate occurrence
IDs from adjacent windows merged. An empty partial page does not claim “Keine Termine” while more
pages or subwindows are missing. A state older than 24 hours gets an age notice. Grid and agenda
reuse index reads in the tab for five minutes and discard inactive entries after ten minutes.
An open calendar reads source metadata every minute while visible. A new `data_revision`
refreshes the affected index queries, and a changed source selection or configuration has its own
query key. A transient read failure keeps the last permitted rows visible with a retry. Live
event details stay separate, with `gcTime: 0`. Automatic due checks are shared across mounts for
five minutes per source revision. A queued job does not prove provider freshness. `seed_calendars` adds five synthetic sources for Google/ICS display, all-day, long title,
old state, missing coverage and pause. Their fixed IDs are skipped before enqueueing and before
adapter execution.

“Im Chat fragen” passes the selected instance as `context_occurrence_id`. Each new turn
automatically includes all active, connected sources with read permission. The settings page
`/settings/calendar` lets the owner choose or clear a writable default for new appointments.
Updates require the displayed settings version. A removed or paused default remains identifiable
as unavailable until the owner changes it. The owner can name another writable calendar in a
request. A scheduling clarification keeps the resolved source and connection with the pending
appointment, including when it uses the default, and rechecks write permission. An explicit request
to use the default again reads the current setting. Ambiguous names require clarification. Settings and cards
identify duplicate names with the local source ID. The owner can use that ID to resolve a
shared name or explicitly switch back to the default when answering a scheduling question. Each proposal binds the resolved source and
connection before confirmation. A later settings change only affects new proposals.
For multiple appointments in one owner message, a calendar choice belongs to its appointment.
Each create call identifies the corresponding request. A completed card leaves the target
of a separate unfinished appointment available for scheduling questions. Where the request
or its target is ambiguous, the owner clarifies it before a card is created. A pending card can retry a failed lookup
of either the mail grant or calendar access.
When one request creates several appointments, only the first card appears initially. The API
stores the other prepared commands with their chosen calendar and turn context. Confirming or
cancelling a card makes the next one available for its own confirmation. An uncertain write
keeps the next appointment waiting until the owner checks its outcome.
Storing, queuing and publishing a calendar card each recheck the bound connection and write
permission under a source lock. A rejected replacement leaves the original card pending.
If a queued target loses write access, the next eligible appointment can proceed to confirmation.
A later time clarification can complete an unfinished appointment even after every queued target
lost write access. An independent pending card still takes precedence.
Time questions keep each unfinished appointment's calendar and place in the same sequence,
including when a confirmation publishes another card between the question and the owner's reply.
A reply can give times for several named appointments together. When two appointments share
a title, an explicit calendar can identify which one receives the supplied time.
An explicitly excluded calendar stays excluded through scheduling questions. If the default
is excluded, the owner chooses a target. An edit or deletion carries its target only on its own card.
When the owner rules out one of several offered calendars, the chat keeps the remaining
options and rechecks the selected source. A time clarification can repeat the pending appointment's title.
Field updates keep that appointment's target. A list with an unidentified calendar requires an explicit choice.
An unavailable calendar named by the owner stays unresolved through time and duration questions.
Location changes affect the event's location. Only a separate calendar instruction selects the destination.

The API persists the full set of readable IDs in `chat_context.calendar_scope_json`. For ICS
subscriptions it also records a private fingerprint of each encrypted feed configuration.
An ordinary retry keeps that binding. Replacing a feed under the same source ID requires a
new conversation. Legacy turns without proof of the current feed also require a fresh chat.
A caller
supplying `calendar_source_ids` explicitly can still narrow that turn, including to an empty
set. Older contexts retain their original scope. A changed set of sources requires a new
conversation after history exists (409 `conversation_scope_change_requires_new`). Model requests
and confirmed actions recheck stored access. A pending card can still be confirmed after additional
sources become readable if the original scope and its bound target remain eligible. A lost explicit
appointment reference returns 404 `calendar_reference_unavailable` before the model runs. After
a provider denies access, scheduling retries preserves the block on cached appointments until
that source completes a verified snapshot. The new chat starts with the current catalogue
and carries over the unsent draft. The “Heute fällig” shortcut lists the available mail,
calendar and task sources for the current selection.

`api/mail/calendar_tools.py` provides `search_calendar` and `get_calendar_occurrence`: index
access without a provider, local date windows up to 90 days and cursor pages up to 50 occurrences;
tool parameters can only restrict the turn scope. Calendar text and source names appear in
`untrusted_calendar_data`, including state and coverage. Calendar changes use confirmation cards as described above.
`calendar_timezone` comes from the browser; older clients without it retain Europe/Berlin. Chat
responses and the SSE event `calendars` expose calendar references with only `source_id`/`occurrence_id`, no titles or private
connection references. Citations reread their display through the authorized source and stable
occurrence route; paused, removed or mismatched sources show no appointment data. Location and
description are absent from the index contract and are not pretended. The autogenerated migration
adds the column with a server default and merges the existing migration heads; it was not edited
by hand.

ICS subscriptions use the private tables `calendar_feed_config` and
`calendar_feed_validation`. `POST /calendar/feeds/validate` uses the same safe fetch and isolated
parser as jobs. Only after complete validation is the entire configuration stored encrypted; the
redacted preview contains only the chosen name, window and count. Validation tokens are valid for
exactly ten minutes and consumed once via `DELETE RETURNING` in the creation transaction. The
fixed server-side scope is `installation`; configuration including replacement source and source
revision is bound. `feed_secrets.py` uses the existing Fernet key; only a hash of random
validation tokens is stored, configuration checksums are keyed by the token. Cancellation
explicitly discards validation via `POST /calendar/feeds/validations/discard`: the token appears
only as `validation_id` in the JSON body, never in path or query; the response is 204 with
`Cache-Control: no-store`. The worker cleans up expiration every minute and on further
validations/creations. Schema errors do not return input values. `POST /calendar/feeds` creates
the source and first job atomically; with verified `source_id`/`expected_revision` the same
endpoint atomically replaces the old address. The existing generation remains until successful
sync; old validators are removed on address change. Management operations are not agent read tools.

`StoredICSAdapter` is registered in the shared dispatch. Job payloads contain only `source_id`;
windows are determined at execution as today −30 to +180 days. HTTP validators are encrypted and
activated atomically with the fully validated snapshot. A 304 re-expands stored components, even
for a new window. A valid empty snapshot clears the inventory; errors preserve generation and
coverage. Source revisions block stale snapshot commits even after pause/resume or address change.
A shared transactional advisory lock serializes queue, replacement, removal and snapshot activation;
removal deletes jobs, index, status, private configuration and finished write records (open ones
stay without text, see "Writing to Google calendars"). Historical chat text remains; active
source-scope checks block removed sources.

`CALENDAR_SYNC_INTERVAL_MINUTES` defaults to 30. The shared worker checks active ICS and Google
sources every minute, independently of mail periodicity. On opening, the agenda requests only due
syncs through the management endpoint with `due_only=true`. Manual sync has a persisted one-minute
wait. 429/5xx and transport errors receive bounded exponential backoff with jitter; after three
failures the short retry ends and the source returns to the regular interval from `last_checked_at`.
Further errors do not start another short retry; success resets the error state. 401/403/404 require
an address check and stop automatic attempts until manual sync, resume or address replacement. If a
manual sync or resume falls within the one-minute wait, it still lifts the block; sync follows after
the minute. Paused sources and seeds are not synced; stopped Studio instances do not guarantee periodicity.

`api/calendar/` contains the adapter contract, snapshot dispatch and a source-scope check. The
calendar index stores sources, events, occurrences and coverage; provider adapters must deliver
complete snapshots. Read endpoints access only the index. `GET /api/v1/calendar/occurrences` takes
a window of at most 90 days and any number of `source_ids` (at most 50) and returns occurrences in
time order; all-day entries count from midnight UTC of their first day. The cursor carries start time
and ID of the last occurrence. Instants without duration belong to the window when their instant is
at the beginning or inside, but not at the end. Complete snapshot windows extend coverage only when
they overlap or touch existing coverage; otherwise they replace it. Every stored occurrence overlaps
coverage (even if it extends beyond its edge); other occurrences are removed. Events are removed only
when absent from the current snapshot and not referenced by any remaining occurrence; included events
remain even without occurrences. `stale` means no successful snapshot exists yet or the index is
incomplete; without periodic sync, the age of the last success alone says nothing about provider freshness.

`ics_fetch.py` provides the HTTPS acquisition layer for ICS. It shares the
numeric-pinned streaming transport in `mail/netguard.py`, rejects non-public/mixed DNS
answers and mapped/transition addresses, and permits only HTTPS:443 with three same-origin
redirects within one 15-second deadline. Wire and decoded bodies each have a 5 MiB limit;
only identity/gzip and calendar MIME types are accepted. `normalize_url` explicitly returns
the HTTPS spelling (including private path/query data) for owner confirmation; never log it.
Fetch results hide body and validators from repr, and task-local filtering suppresses
httpcore traces that could expose headers or protocol content.

`ics.py` combines acquisition and isolated parsing in `ICSAdapter`, used per request by
`feed_jobs.StoredICSAdapter` and the privileged validation service. It
accepts an explicit fallback IANA zone and UTC-midnight window; `default_window` supplies
today −30 to +180 days. A 304 requires previously normalized events and re-expands them for
the requested window; it never treats an old window as fresh coverage. `ics_parse.py` uses
exactly pinned `icalendar==7.3.0` for folded/escaped properties, typed values and VTIMEZONE,
and the existing `python-dateutil==4.0.0` for lazy RRULE iteration. The hand-written fixture
corpus tests this combination; avoiding an eager recurrence wrapper lets the app enforce
its own exception, completeness and resource rules. Schema 1 in `Event.recurrence` stores
masters, exceptions, canonical rule fields, typed dates/local/UTC times, nominal/exact
durations and structured custom-zone observances, not raw ICS. Re-expansion needs only
these JSON-compatible events. Identities hash UID plus the original normalized recurrence
ID (or the non-null master sentinel), so moving an occurrence does not change its identity.
Custom VTIMEZONE instants use UTC in the shared occurrence contract while retaining the
original definition in recurrence data. Floating times never use the server's local zone.

Each parse/re-expansion uses a fresh exec subprocess (`ics_worker.py`), limited to 256 MiB
address space, 5 CPU seconds and 8 wall seconds. Timeout/cancellation kills, drains and
reaps the process before returning. IPC is capped at 16 MiB input and 32 MiB output, feed
input at 5 MiB; limits may be lowered per call, not raised. The component ceiling is 10,000
(including ignored components); the expansion/work ceiling is 50,000 candidates per feed,
including historical candidates needed for COUNT and exception validation, plus a 50,000
output ceiling. These are per-call budgets, not a global concurrency policy. Missing POSIX
resource support fails closed. Worker diagnostics are discarded, logging and networking
are disabled, and errors contain fixed codes only. Tests prove process reaping on timeout,
CPU/memory exhaustion and caller cancellation, while the API loop continues running.

VEVENT supports UID, DTSTART, DTEND/DURATION, SUMMARY/DESCRIPTION/LOCATION/STATUS,
RRULE, date/date-time RDATE/EXDATE and individual RECURRENCE-ID replacements/cancellations.
All-day ends are exclusive; DURATION P1D is nominal, PT24H and DTEND-derived durations are
exact. Local DST recurrence skips nonexistent generated candidates before applying COUNT;
ambiguous times choose the first offset. Explicit nonexistent times fail. Unsupported
scheduling METHODs other than PUBLISH, non-Gregorian CALSCALE, RANGE/THISANDFUTURE,
EXRULE, RSCALE, leap seconds and period-valued RDATE fail explicitly. Instantaneous
timed events are represented as points with equal start and end instants.
Duplicate masters/exceptions/supported properties, orphan exceptions, unresolved zones and an RRULE
whose DTSTART does not match also fail. VTODO, VALARM, attachments and external URLs are
never executed or loaded; instructions in descriptions remain plain data.

## Domain model (tables in Postgres)

- `calendar_source`, `calendar_event`, `calendar_occurrence`, `calendar_sync_state` — calendar source with connection reference and permission (`is_active`), series master per source, occurrence as half-open interval or instant without duration (all-day as date with positive interval, otherwise UTC with timezone; unique per source and `external_id`) and coverage, last success and incompleteness per source.
- `calendar_feed_config`, `calendar_feed_validation` — encrypted feed configuration and short-lived, single-use validations; no public output models.
- `calendar_write` — one record per `write_id`: source, operation, target event, request hash, status (`running`, `done`, `failed`, `unknown`, `not_applied`), fixed error code and result event; the request text is kept only while the outcome is open. A partial unique index allows one running write per target.
- `task_series`, `task_schedule_revision`, `task_occurrence` — task series, schedule revision and materialized due date; creation ID and occurrence key are unique.
- `mail_account` — connected account: `provider` e.g. `google`, `microsoft`, `imap` or connector name such as `gmail`; `auth_type` is `oauth2`, `imap_password` or `mistral_connector`.
- `mail_credential` — encrypted payload per account+kind (`oauth2_tokens` | `imap_password`), unique (`account_id`, `kind`).
- `mail_message` — mail index with `folder`, `provider_uid`, `thread_id`, `subject`, `sender`, `sender_address`, `recipients_json`, `cc_json`, `reply_to_json` (Reply-To addresses; replies go there instead of the sender; the Gmail connector does not provide them), `snippet`, `body_text`, `body_version` (text version, 0 = before versioning), `internal_date`, read/priority flags, `labels_json`, `attachments_json`, `list_unsubscribe` and `list_unsubscribe_post`; unique (`account_id`, `folder`, `provider_uid`), full-text index `search_tsv`.
- `mail_sync_state` — cursor, status, error and last sync per account.
- `mail_action` — executed action with `previous_json` and `undone_at` for undo.
- `mail_insight` — summary (`points_json`), deadline, reply need, follow-up suggestions and model per mail.
- `mail_job` — durable sync/insight queue with status, deduplication, attempts and lock time.
- `chat_conversation`, `chat_message` — history; proposals in `proposal_json` with `proposal_status`, mail references/proposals/model in `extras_json`; older drafts carry `draft_json`.
- `skill_definition`, `skill_revision` — versioned built-in and custom skills within `installation`, immutable revisions (`instructions`, `workflow_steps_json`, `allowed_tools_json`, `content_hash`) and content-free deletion tombstones.
- `user_preference` — server-side owner `installation`, key, canonical scope (`*` global or account ID), status, revision pointer and object version; removed rows remain as content-free suppression references.
- `preference_revision` — immutable value, key/scope at the time, source `manual` or `confirmed_candidate` and confirmation time per revision; deletion removes the value and sets `removed`.
- `preference_candidate` — unconfirmed value with own source message, conversation, field/scope, expected target version/revision and optional card ID; status `pending|accepted|rejected|expired`. Rejected and invalidated candidates retain only content-free references.
- `personalization_settings` — server-side singleton with independent `use_preferences` and `suggest_preferences` (both default false), version and ID and time of the last suggestion activation.
- `preference_extraction` — content-free one-time ID per source message and extractor version; binds activation and prevents repeated model calls even after errors or restart.
- `calendar_settings` — the owner’s default writable calendar for new chat proposals and its settings version. Clearing the default keeps calendar reads available.
- `ui_settings` — the single cross-device interaction configuration with fixed server-side scope (`installation`, unique): `swipe_left`/`swipe_right` from allowlist `none`, `archive`, `toggle_read`, `trash`, `revision` for conditional updates and `updated_at`; initial assignment left `archive`, right `toggle_read`, plus `locale=browser`. The last browser hint and speech pace are stored separately, with pace revision-guarded. Language overrides are `de` or `en`.

## Templates and turn context

Templates belong to fixed server-side scope `installation`; access to the single-owner app is
restricted by deployment, not a new app identity. `prompt_template` holds name, version, current
revision and modification date `updated_at` (unknown and `null` for legacy data); editing creates
a new `prompt_revision`. Selections in `chat_conversation.prompt_revision_id` remain pinned.
Conversation PATCH requires `expected_version`; `null` as revision selects the default. Template
POST accepts `name` and `text`, PATCH additionally `expected_version`, DELETE `expected_version`
as query parameter. GET of a template also returns `revisions` (newest first), so older revisions
can be explicitly selected. New turns store revision reference/hash, app prompt version and the
resolved account scope in `chat_context`; `chat_message.context_id` links history to it. The JSON
account scope contains the sorted granted `account_ids` and a `selection` (a focused account or `null` for the full grant). `preference_revision_id` remains empty for old
clients; actually used preference revisions are now in `preference_revision_ids_json` with
`preference_resolver_version` and `preferences_enabled`. Text is given to the model only as user
content, never stored as an app rule or chat extra. Deletion removes template and revision content,
leaves `removed` markers, revokes contexts and resets active selections. Already sent model requests
cannot be recalled. Proposals from revoked contexts remain open and can be cancelled, but not
confirmed (`409 proposal_context_revoked`). The atomic claim is decisive: a revocation effective
thereafter does not undo an already accepted confirmation. The agent checks revocation immediately
before each model call and tool and every ten deltas during streaming; a revoked stream is closed
and reports `context_revoked`. For a history, associated contexts are loaded in one database query.
Inbox undo requests are aborted after 15 seconds; if the result is unclear, the action remains
available for another attempt for ten more seconds. A conversation with messages can continue only with its original grant revision, effective accounts and focus.

### Server-side skill runtime (#84)

Executable tools are registered only in trusted server application code (`api/skills/registry.py`,
`api/mail/agent_tools.py`). Built-in skills (`api/skills/catalog.py`, currently `daily_briefing`
with the domain aggregation tool `collect_daily_briefing` in `api/skills/briefing.py`) and custom
skills live in `skill_definition`/`skill_revision` and reference registered tools explicitly.
Turns activate a skill only when `skill_id` or `skill_revision_id` is passed on that turn request
(turn-scoped, never sticky to later turns or other chats); `chat_context` records `skill_id`,
`skill_revision_id`, and `skill_hash`. Legacy turns without a skill never receive skill tool rights.
When active, the model receives only that revision's `allowed_tools`, instructions stay in `role="user"`,
domain scopes and confirmation cards remain mandatory, and disabling, narrowing tools, or deleting
a skill revokes affected contexts (`409 proposal_context_revoked`, mid-stream `context_revoked`). See [docs/skills.md](docs/skills.md).

Studio mail connector operations pin the named OAuth credential with `credentials_name`. The
first identity-checked sync verifies existing inbox rows in place by provider UID and follows stored pages. Pre-migration connector
messages, including archived rows, stay outside chat reads until a verified sync rewrites them. It requires a provider
Sent message from the registered mailbox and retains its opaque thread and message IDs as an
anchor alongside the verified pagination cursor. The ordinary sync cursor stays numeric for older workers. Sync and body rereads check it before and after fetching.
Writes check the mailbox immediately before dispatch. An incoming-only placeholder can change labels from the inbox only when a named credential verifies the thread, message, sender and subject and is used for the write. Chat cannot use these accounts. A verified batch publishes rows and progress atomically after checking the stored generation. A sync attempt has its own generation. The last successful proof has a separate revision. A definite mismatch still withdraws the grant while a newer poll is waiting for evidence. A later failure cannot overwrite a newer success. Sync workers renew their job lease during provider reads. Database transactions from older workers cannot insert or overwrite verified provider content. Independent new-mail and historical cursors keep new arrivals visible even when a historical page fails and needs retry. Malformed historical messages go on a per-mailbox retry list. Each poll checks up to five deferred threads alongside its normal pages. The normal scan and deferred reads require evidence that each message belongs in the inbox. When the search result supplies its labels, those labels also set its read and starred state. Historical pages and deferred reads both apply archived-message removals to the index. A single-message inbox match without labels preserves independently established read and star flags. Connector restores and archives reuse the local row for that provider UID. If an old worker already created two copies, the sync keeps the current inbox identity or the verified archive when only an unverified inbox copy left. For two verified copies, the surviving message retains the earlier local IDs as links within the same account and folder permissions. A confirmed mailbox mismatch invalidates these links. When duplicate copies lack this proof, the retired ID opens its cached content as a read-only source in the owner's mail view. Removing the account clears these copies. Chat and bulk actions resolve verified aliases before applying an action once per message. The regular 14-day listing also checks locally archived messages and queues their threads for a bounded, identity-checked content read before restoring them. When a narrow thread listing has no per-message labels, a fresh recent listing can confirm a restored message and its flags after the content read. An ambiguous sibling remains pending even when both scans see the same thread. Incomplete thread responses also remain pending. A 401 or 403 on a full-content read marks the sync as requiring reauthorization and preserves its last successful cursor. Verification and its bounded jobs for invalidated unread insights commit together. Their queue states keep older workers from claiming them during an upgrade. Each job checks the affected messages in the same account before regenerating them.
A confirmed missing or mismatched identity withdraws chat access. Temporary transport or provider failures stop the operation and leave the binding available for a later sync. Incoming-only accounts with a placeholder address can sync the ordinary inbox and keep every row outside chat. Settings show the server’s eligibility for each active account and explain incoming-only and pending-verification states. A checkbox edit preserves grants absent from the cached account list. The owner can reselect the account after a
successful verified sync. A replacement mailbox registers separately and starts without a grant.
After an anchor is lost, the next sync must establish a fresh verified binding before selection and verify indexed messages page by page. A provider reauthorization under the same credential name
between the last check and dispatch remains outside the connector's transaction guarantees.

## Chat mail access (#80)

The single owner selects active mail accounts at `/settings/chat`. The initial grant is empty,
including for existing installations. Saving the complete selection requires its current
`expected_version`. Changing its membership increments the revision. Adding an account gives it
no chat access. Reconnecting the same stored account keeps its grant. Removing the account also
removes its grant in the deletion transaction. A replacement account needs explicit selection.

Each turn records the grant revision and its effective accounts. A mail reference can narrow this
set. A changed revision, missing historical context or pre-migration history requires a new
conversation. The owner can still read the original messages and cancel an old pending proposal.
The claim for confirmation rechecks the grant under the same lock as a settings update. A claim
that already succeeded can finish. The browser sends the revision seen in its scoped inbox
summary (or the settings API) with every chat request. History shows the restart action when a grant has changed. Changing the grant stops active or loading speech. An old reply is ineligible for speech synthesis. Quick actions, draft entry points and task account choices use
the same grant. General task access includes eligible personal tasks and tasks belonging to the
granted accounts. For new mail with multiple eligible senders, the chat asks which account to use. The draft card shows the sender. Replies and forwards keep the original account. Ordinary inbox access is independent of these chat settings.

## Explicit preferences (#38)

Fixed app rules and authorized access take precedence over the current request, which takes
precedence over the selected template, which takes precedence over matching active preferences.
Values reach the model exclusively as user content, not as app rules or in chat extras or traces.
Answer length may apply globally or to one account; an account value overrides the global default
for this field. Salutation and signature require exactly one existing account. A conversation without a focused account resolves only global values; account signatures are never mixed. Existing mail,
calendar and task scope checks and confirmation mechanics remain unchanged.

`GET /settings` returns only
`{"personalization":{"use_preferences":false,"suggest_preferences":false,"version":1}}`.
PATCH accepts `{"personalization":{"expected_version":1,"use_preferences":true}}` and changes
only explicitly supplied fields. `suggest_preferences` can be enabled independently of
`use_preferences`; null and foreign sections are rejected. `/settings/ui` retains its own contract
and revision. Saving a preference is manual confirmation but never enables usage. POST accepts
`key=answer_length|salutation|signature`, `scope`, `value` and optional `status=active|paused`;
PATCH accepts `expected_version` and changes to value, scope or status. DELETE requires
`expected_version` as query parameter. Limits: 50 stored (excluding removed), 20 active values,
500 characters per value. A partial unique index secures one active value per owner/key/scope,
including global non-null scope. Version and quota conflicts return 409 without input values in
error text.

The existing turn resolver binds exact revision IDs and resolver version 1. New revisions take
effect from the next turn; historical dialogue reads only those IDs, never today's values. Without
opt-in or with explicitly set `without_preferences: true`, there is no injection. A load failure
returns `preferences_unavailable` before the model call; only the deliberate action “Ohne Präferenzen fortfahren” sets the flag for retry. There is no silent fallback. Disabling, pausing,
changing scope and deleting revoke affected contexts in the same transaction: disabling revokes
all turns with at least one used revision, the other actions turns with a revision of the changed
preference. Turns without a used preference remain valid, including open proposals. Precedence is
in the fixed app rules (`agent_prompt.py`, `APP_PROMPT_VERSION` 18). Existing live checks before
model/tool calls and after ten deltas remain authoritative; already sent requests cannot be recalled.

The shared transactional preference lock serializes quotas, changes, resolution and account
deletion. `mail_account.delete_account` removes account preferences in the account transaction;
`mail/service.py` is currently not the deletion path. Deletion removes all revision values,
retains content-free references and revokes running contexts. Historical display: “Präferenz entfernt”. Original chat messages and answers remain. Removed `user_preference` rows with key,
scope and `removed_at` are the suppression reference: messages before removal must not restore
the value; a new explicit request remains possible. Legacy data from #38 without a deletion time
gets a conservative cutoff at the first regular turn under the preference lock, before that turn's
own message is saved. Older sources stay blocked; new requests after that boundary are possible.
Removed revisions are never resolved.

### Confirmed remember cards (#39)

`propose_preference` is the only new tool. It binds the server-stored current own message to the
conversation, limits values to 500 characters and checks field, account scope and quotas from
#38. A conservative entry requires a direct request beginning with “Merke dir”, “Bitte merke dir”
or “Speichere dauerhaft/als Präferenz”. Quotations, hypothetical examples and foreign messages
alone do not open this flow; for unclear wording the agent asks for a new direct request. The text
check is not a semantic classifier and never replaces confirmation. Extra arguments such as
`confirmed` or `origin` are rejected.

The shared preference lock serializes candidate creation, quotas, confirmation and deletion.
There are at most 50 open candidates. Source/field/scope is unique, including after rejection;
repetition writes no new text into a rejected candidate. Cards contain only a candidate ID. Stored
tool arguments also contain no copy of the value. An open mail/task proposal remains unchanged
when proposing a preference; the candidate remains reviewable without a card under Einstellungen
→ Präferenzen → Vorschläge (UI text). Existing replacement of an open card by mail/task tools is
retained. A displaced preference card is not rejection: its candidate stays open, loses only its
card reference and can be reopened through “Vorschlag prüfen” once no other card is open. Cancel
applies only to the candidate's current card; a displaced or stale card (e.g. from another tab)
returns 409 `proposal_not_pending` and leaves the new card open.

`GET /preferences/candidates` reads open candidates; with `candidate_id` it reads an exact reference,
including historical cards. Source and previous/proposed values are loaded from current accessible
rows. Missing sources and accounts invalidate the reference and remove the value. The existing seed
conversation deletion path removes candidate values in the same transaction; on reread sources
missing for other reasons are also marked content-free. `POST /preferences/candidates/{candidate_id}/propose`
idempotently opens a card only when the shared slot is free. An existing card ID is reused.
`refresh: true` explicitly means rechecking after a version conflict, not confirmation. The UI links
the card in its conversation.

Confirm/Cancel use only the existing chat endpoints. Confirm accepts `preference_decision` with
`choice=replace|keep`, `expected_version` and `expected_revision_id` only for preferences. Claim,
renewed source/version/quota check, immutable revision and card completion are in one transaction.
A 409 returns current state only to the browser; the UI disables confirmation until rechecked.
Exactly equal values create no new revision. Keep and cancel remove the candidate value. Deleting a
preference also removes associated candidate values, even after acceptance; referential cards cannot
restore content. Confirmation never changes `use_preferences`. Without the usage toggle, the next
answer remains without preference injection. Automatic extraction is a separate, explicitly enabled
flow from #40; it neither confirms a preference nor opens a card.

The GETs `preferences_list`, `preference_context_get`, `preference_candidates_list` and `settings_get`
explicitly remain in `BROWSER_ONLY_GET_OPERATIONS` of the MCP inventory and outside the Studio read
list: read-only alone does not make personal values agent-safe. Write operations including
`preference_candidate_propose` are in `MUTATING_OPERATIONS`. New preferences do not become tool permissions.

### Suggestions from new own messages (#40)

`PATCH /settings` enables `suggest_preferences` with `expected_version` independently of usage.
Each re-enablement sets a new random activation ID and time boundary. Only own messages stored
afterwards are eligible; neither history nor the disabled interval is backfilled. Before enabling,
the UI explains that selected new messages go to Mistral and do not remain only in the app database.

`api/preferences/extraction.py` starts an unawaited task in the agent once the own message is
stored and the turn has passed its scope checks. A turn aborted at a mail, calendar or task
boundary does not forward its message. Input is exclusively that message's text, never history,
mail/calendar context, tool results or assistant text. More than 2,000 characters are skipped,
not truncated. The model matches the chat turn selection (`conversation.model`, otherwise
`mail_chat_model`). The existing private Mistral client uses JSON mode, no tools and at most 15
seconds runtime. There is no scheduler and no retry. A process termination can lose a suggestion;
an already stored claim remains consumed nonetheless.

Extractor version 1 requires at most three strictly validated entries with field, scope, value,
explicit permanence and a verbatim quotation from exactly this message. A conservative text filter
requires a permanence signal and rejects, among other things, quotations and hypothetical and
conditional wording. This is not proof of semantic correctness and may miss genuine wishes. A
foreign mail pasted without quotation marks or quote markers is indistinguishable; its request may
appear as an unconfirmed suggestion that only the owner can confirm. Suggestions remain unconfirmed.
Salutation and signature require the exactly selected existing mail account, read by the server
from the turn context, never an account ID invented by the model.

When suggestions are disabled, the claim ends after one unlocked read. If enabled, it is reread
under the lock so intervening disabling sends no further message to the model. Claim and result
storage each run under the shared preference lock; no transaction is open during the model call.
Before writing, opt-in, activation ID, own source message, account, deletion/rejection markers and
exact duplicates are rechecked. Disabling during the model call discards its result. One-time IDs
apply per message and extractor version; the result slot is also consumed only once. Model, JSON,
timeout and database errors affect only this task. Logs contain fixed error codes only, no content
or diagnostics.

Candidates carry server-side `origin=extractor` and `source_message_id`. They appear in the existing
suggestion list, influence no answer and open no card. `Vorschlag prüfen` (UI text) uses the #39
endpoint unchanged; contradictions remain visible for manual decision. Browser output names `origin`;
there are no new endpoints or agent tools. Confirmation enables neither toggle.

The migration was exclusively generated. The narrowly scoped comparator in `db/migrations/env.py`
removes reflected CHECKs only from own model tables whose metadata no longer contains any CHECKs.
It compares no SQL text. The generated downgrade restores `NOT suggest_preferences`; suggestions
must be disabled before such a downgrade. The PG contract checks an empty autogenerate diff after
migration to `heads`.

Offline and live evaluation:
[Preference suggestions of 18.11.2026](docs/superpowers/probes/2026-11-18-praeferenz-vorschlaege.md).
`bash tools/uv.sh run --no-sync python tools/preference_extraction_eval.py` checks twelve synthetic
cases without a model. Only explicitly appending `--live` sends those twelve fixtures through the
existing app client to Mistral; no historical data. Live, the model was right in 7 and 6 of 12 cases
respectively in two runs; validation rejected the others, so in both runs the expected result would
be stored in all twelve cases. Salutation and signature must appear verbatim in the cited evidence;
answer lengths are paraphrases bound to the message only through the evidence. Validation is thus
load-bearing and must not be relaxed without repeating the evaluation.

## Worker and periodic sync

`api/mail/jobs.py` processes the durable `mail_job` queue in the **API process**.
`MAIL_WORKER_ENABLED` (default `true`) starts the worker at API startup;
`MAIL_WORKER_POLL_SECONDS` (default `2`) controls the wait between checks.
`MAIL_SYNC_INTERVAL_MINUTES` (default `5`, `0` disables periodic sync) controls enqueueing
active accounts; recently synced accounts and accounts whose last error was `auth_expired` are
skipped. Manual sync and the OAuth callback after reconnection enqueue these accounts too; the
worker must be running to execute them. `MAIL_SYNC_STALE_MINUTES` (default `10`) determines
when a locked job is stale. After a successful sync with new mails, an insight job is enqueued
to fill in missing summaries. The worker uses the saved language, or the last confirmed browser hint in browser mode;
when no browser hint exists it falls back to English.

When the text version (`body_text` in `sync_types.py`) changes, `BODY_VERSION` is incremented.
Each subsequent sync rereads up to 25 INBOX mails with an older version from the provider
(without marking them read) and overwrites text and snippet in the same row; its summary is
discarded if the text changes or remains empty. If the provider no longer knows the mail or
returns it under a different message ID, the old text remains and only the version increases.
A mail that cannot currently be read (server error, rate limit, connection loss) is retried
later; the others still count. Its wait doubles per failure, from one hour to one day (remembered
in the API process, forgotten on restart), so a few unreadable mails do not hold up the others.
A summary is stored only while the mail still has the text read by the model; new text and
discarding the old summary occur in one transaction. A reread error leaves the sync itself
successful.

If Studio stops an inactive instance, the worker and thus polling also pause until a new request
restarts the instance. 31 minutes of idle time were measured **without** a stop; behavior for
longer periods has not been measured.

## Studio probes

Details and measurements: [Studio probes of 13.11.2026](docs/superpowers/probes/2026-11-13-studio-proben.md).
Apps do not automatically become MCP tools; only importing OpenAPI as an HTTP connector turns
operations into tools. App modules have no schedules (workflows do). The same instance ran for
20 calls each, including after 31 minutes idle; a later stop was not checked. SSE arrived through
the gateway before the stream ended, with one short pause in the first measurement. The Gmail
connector does not return complete headers in `get_thread`; newsletter detection therefore also
uses the label `CATEGORY_PROMOTIONS` for connector accounts (`CATEGORY_UPDATES` does not count,
as it also contains order confirmations and account notices). `TRASH` is supported as a system
label, `send_email` takes `replyToMessageId`; the Outlook connector returned HTTP 401 in the probe.

## Stack & commands

- **Python:** `uv` workspace (root `pyproject.toml`), FastAPI + SQLModel/Alembic. Tests: `bash tools/uv.sh run --no-sync pytest <pfad>`; live PG suites: `bunx nx run db:test-pg-contract`
- **TypeScript:** bun 1.4 + Nx + TanStack Start/Router/Query + Tailwind 4, Vite 8
- Main commands (root):
  - `bun run install-all` — installs everything including generated API client (`tools/gen-types.sh`)
  - `bunx nx run api:serve` — start API locally; `bun run dev:web` — web locally (port 3001)
  - `bun run build` / `bun run test` / `bun run check` — Nx across all projects
  - `bunx nx run api:gen-openapi` + `bash tools/gen-types.sh` — regenerate OpenAPI spec + TS client (after every API change; `gen-types.sh --check` is the drift gate)
  - `bunx nx run db:revision --message "..."` — autogenerate Alembic revision; `bunx nx run db:migrate` — apply (`heads`, not `head`)
  - `bash tools/uv.sh run --no-sync python -m api.mail.dev_seed` — fill the local database with the design-board sample data. The seed rejects database hosts other than `localhost`, `127.0.0.1` and `::1`; repeated runs replace only seed rows and associated insight/sync/chat rows. For visual checks start the API with `MAIL_WORKER_ENABLED=false` so seed accounts are not synced.

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
  `settings_ui_browser_locale_confirm` is browser-only; it stores the latest browser hint for jobs.
  `settings_ui_get` creates the default assignment in `ui_settings` once on the very first read; it never changes an existing assignment.
  The calendar write routes (`calendar_event_create`, `calendar_event_update`, `calendar_event_delete`, `calendar_write_check`), `chat_proposal_check`, `calendar_occurrence_live` and `calendar_write_get` are deliberately not in this list: they write to Google, resolve a write or read private live event data.
  `audio_capabilities_get` and the paid `audio_transcribe` and `audio_speech` operations stay browser-only. The audio subtree verifies the Studio gateway's
  caller with the Mistral Users API against the API module's `AUDIO_STUDIO_OWNER` binding.
  Workspace keys and missing identity are denied. Audio POSTs also require the configured
  browser origin. STT and TTS are gated independently
  by matching release attestations. STT validates the container, codec and duration before
  contacting Mistral. A transient PostgreSQL lock limits transcription to one active request
  per app database. The browser appends a successful transcript to the current draft.
  Speech takes a conversation and message reference, reads a visible and complete assistant
  reply from local history. The effective language selects `voice_481` for German or `voice_478`
  for English; the revision-guarded pace selects a provider-only tag, never stored in history.
  Speech attestations can set lower limits within 500 characters, 1,000,000 MP3 bytes and
  20 seconds. All audio responses use `Cache-Control: no-store`.
  The owner-only `audio_device_test_get`, `audio_device_test_record` and
  `audio_device_test_sample` operations expire independently and remain browser-only.
  Live voice is a separate test-only, owner-and-origin-bound path. Its status and speech POSTs
  remain browser-only, and the transcription WebSocket is not part of OpenAPI or the agent
  facade. `AUDIO_LIVE_TEST_ENABLED`, `AUDIO_LIVE_TEST_ORIGIN`, and `AUDIO_LIVE_TEST_UNTIL`
  must be set explicitly; the last is an offset-aware future time at most 72 hours away.
  Manual speech accepts only a saved, authorized assistant-message reference. Live voice turns use
  an owner- and origin-bound WebSocket running the same chat agent and resolved conversation context.
  Its speech provider receives only visible assistant deltas generated by that turn, grouped into
  sentences; browser-supplied speech text is never accepted. STT receives the effective locale before
  audio; the speech voice and pace are fixed for each turn. Content-free timings contain no text or
  identifiers. Scope and the live window are rechecked
  during synthesis and while playback is outstanding. The socket stays open until playback finishes
  or the turn is stopped, disconnected or revoked. A stopped assistant fragment is not fed back into
  the model for a subsequent voice turn. Live transport and provider behavior require Studio verification.
  They validate WebM and return a fixed test draft or bundled sample without calling an inference service.
  See the [audio probe](docs/superpowers/probes/2026-11-19-audio-capabilities.md).
  Every OpenAPI import turns every operation into an MCP tool; otherwise a Studio agent could confirm proposals, perform actions or connect accounts without the owner seeing it — individual tools can be disabled in Studio after import.
  New mutating operations must deliberately be added to `MUTATING_OPERATIONS` in `apps/api/tests/test_mcp_exposure.py`, otherwise the inventory test fails.

## Publication and private data

liam-e should be publishable. Everything that goes into Git (files and commit messages), and
anything on GitHub (issues, PRs, reviews, comments), therefore already counts as public.

- **Nothing about the owner or their environment:** no real names, email addresses, mail or
  calendar contents, contacts, hostnames, internal domains, IP addresses, absolute local paths,
  user or device names, or names of their own infrastructure or tools. Give paths relative to
  the repository.
- **Mistral tenant:** organization, workspace and app IDs appear only in `mistral.json`, because
  `mistral deploy` needs them there. Everywhere else use `<organization-id>`, `<workspace-id>`
  and `<app-id>`; do not write down the deployment's app or gateway URLs.
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
  `tests/test_public_repo_hygiene.py` (tenant IDs outside `mistral.json`, private links, home
  paths, realistic-looking email addresses). The allowlist is empty; an exception requires a
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
- Use the templates in `.github/` (`ISSUE_TEMPLATE/`, `pull_request_template.md`) for issues and PRs. One task produces exactly one branch and one PR, even when it covers several issues; each issue gets its own commits and closing keyword (`Closes #12. Closes #13.`). State explicitly in the PR what was not checked.

<!-- OPENWIKI:START -->

## OpenWiki

This repository has a generated `openwiki/` evidence index. It is optional just-in-time context, not required startup reading.

- Do not enumerate, preload, or search wikis at task start. Use retrieval when the user asks for it, when unfamiliar architecture or dependency behavior materially affects the task, or when source inspection leaves an important uncertainty. Stop once the question is grounded.
- When those conditions apply and OpenWiki retrieval tools are available, use `openwiki_search` for just-in-time context and `openwiki_read` for the relevant complete sections. If search returns `workspace_required`, ask which listed workspace to use and retry with its ID.
- Use `openwiki_list_workspaces` or `openwiki_list_wikis` when workspace membership itself needs to be discovered.
- If the retrieval tools are unavailable, read `openwiki/quickstart.md` and follow its links to the relevant pages.
- Treat source code and tests as authoritative. A brief's unknowns and review items are verification gaps, not automatic requirements.
- Prefer the narrowest quiet validation that proves the changed behavior. Preserve complete failure output.

The scheduled OpenWiki GitHub Actions workflow refreshes the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->
