# Worker and periodic sync

Part of the agent instructions: [AGENTS.md](../../AGENTS.md) links here from its index, and its rules apply here too.

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
