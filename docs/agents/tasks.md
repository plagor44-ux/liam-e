# Task domain

Part of the agent instructions: [AGENTS.md](../../AGENTS.md) links here from its index, and its rules apply here too.

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

## Tasks in chat

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
