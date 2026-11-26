# Preferences

Part of the agent instructions: [AGENTS.md](../../AGENTS.md) links here from its index, and its rules apply here too.

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
in the fixed app rules (`agent_prompt.py`, `APP_PROMPT_VERSION`). Existing live checks before
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

## Confirmed remember cards (#39)

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

## Suggestions from new own messages (#40)

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
[Preference suggestions of 18.11.2026](../superpowers/probes/2026-11-18-praeferenz-vorschlaege.md).
`bash tools/uv.sh run --no-sync python tools/preference_extraction_eval.py` checks twelve synthetic
cases without a model. Only explicitly appending `--live` sends those twelve fixtures through the
existing app client to Mistral; no historical data. Live, the model was right in 7 and 6 of 12 cases
respectively in two runs; validation rejected the others, so in both runs the expected result would
be stored in all twelve cases. Salutation and signature must appear verbatim in the cited evidence;
answer lengths are paraphrases bound to the message only through the evidence. Validation is thus
load-bearing and must not be relaxed without repeating the evaluation.
