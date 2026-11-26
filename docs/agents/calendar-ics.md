# ICS calendar subscriptions

Part of the agent instructions: [AGENTS.md](../../AGENTS.md) links here from its index, and its rules apply here too.

## Subscriptions, validation and snapshots

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

## Sync scheduling

`CALENDAR_SYNC_INTERVAL_MINUTES` defaults to 30. The shared worker checks active ICS and Google
sources every minute, independently of mail periodicity. On opening, the agenda requests only due
syncs through the management endpoint with `due_only=true`. Manual sync has a persisted one-minute
wait. 429/5xx and transport errors receive bounded exponential backoff with jitter; after three
failures the short retry ends and the source returns to the regular interval from `last_checked_at`.
Further errors do not start another short retry; success resets the error state. 401/403/404 require
an address check and stop automatic attempts until manual sync, resume or address replacement. If a
manual sync or resume falls within the one-minute wait, it still lifts the block; sync follows after
the minute. Paused sources and seeds are not synced; stopped Studio instances do not guarantee periodicity.

## Fetching

`ics_fetch.py` provides the HTTPS acquisition layer for ICS. It shares the
numeric-pinned streaming transport in `mail/netguard.py`, rejects non-public/mixed DNS
answers and mapped/transition addresses, and permits only HTTPS:443 with three same-origin
redirects within one 15-second deadline. Wire and decoded bodies each have a 5 MiB limit;
only identity/gzip and calendar MIME types are accepted. `normalize_url` explicitly returns
the HTTPS spelling (including private path/query data) for owner confirmation; never log it.
Fetch results hide body and validators from repr, and task-local filtering suppresses
httpcore traces that could expose headers or protocol content.

## Parsing

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
