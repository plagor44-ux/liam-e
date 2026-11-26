# Calendar domain

Part of the agent instructions: [AGENTS.md](../../AGENTS.md) links here from its index, and its rules apply here too.

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

## Writing to Google calendars (#23)

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

## Views, time zones and caching

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

## Calendar in chat

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

## Calendar index and adapter contract

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
