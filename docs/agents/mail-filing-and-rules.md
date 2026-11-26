# App-side mail filing and rules

Part of the agent instructions: [AGENTS.md](../../AGENTS.md) links here from its index, and its rules apply here too.

The inbox, reading pane, filing choices and settings identify destinations by kind as well as
name. Filing tags use folder and tag icons with text and wrap on narrow screens so both names
remain readable without relying on colour.
User-facing destination lists sort folders and labels by the active UI locale, ignoring case and
accents, using numeric collation and ID ties. Server inventories use the same case- and accent-insensitive numeric ordering; the UI sorts for its locale. Rule settings use the same name order and show the stored priority; this
display order never changes rule evaluation order.
Folder settings fetch the complete inventory through browser-only, ID-cursor pages of at most 100,
then sort and filter it before displaying 20 rows per page. The existing complete inventory remains
available to filing and chat; settings changes invalidate both cached reads.
Desktop inbox shortcuts use `apps/web/src/mail/inboxShortcuts.ts` as the shared definition for
handling, hints and the visible overview. Filing-menu headers show V and L once rather than on
every destination. V opens folders, L opens labels and ? opens the overview;
J/K, Enter, E, U, Delete/#, X and Escape retain their documented inbox actions. Editing fields,
composition, conflicting modifiers, menus and dialogs prevent shortcuts.
Filing records destination usage in the same transaction, without mail content or an extra write
request. The installation-scoped history is capped at 200 destinations and 90 days since last use;
deleting a destination removes its history. Expired entries are removed on reads and writes and
hourly while the worker runs. The browser-only quick-action read ranks frequency with a 30-day age
weight; the UI applies locale order to ties and shows at most five actions, fewer on narrow screens.
Already-applied destinations are excluded. Desktop rows show quick actions in an out-of-flow overlay
on hover, focus or selection, within the fixed-height snippet line, with one top-ranked destination;
mobile rows expose them in the more menu. The selection bar and reader also offer compact actions.
From an opened mail, create-and-assign creates the destination and files the verified source in one
transaction; a missing source or duplicate name leaves no partial destination. The rule editor can
prefill account, sender, domain and subject from that mail, proposing a rule name from the sender.
The form waits for source initialization before edits, permits cancellation during loading and
falls back to an empty manual draft on failure.
The form has one explicit save action; it explains the required folder or label before enabling
save, and unsaved changes retain the leave confirmation. Only selected conditions are saved, and
the source and its account are rechecked under lock during creation, including verified aliases.
List-header conditions are not supported. Unsaved rule edits use the settings leave guard.
Each destination has a validated named colour or `none`; existing rows default to `none` on upgrade.
The fixed palette is local to liam-e and appears in filing tags in the inbox and reader, with kind
icons and text retained. Rules inherit a label's appearance through assignment; they have no colour
action. Account colours, urgency and error tokens keep their separate roles.

Folders and labels are global app data, never provider folders or labels. Each indexed message has
at most one app folder and any number of labels. The owner inbox hides foldered messages; label
filters include filed and unfiled mail. App filters include the indexed inbox and archive, while
trash remains separate. Filing works across accounts regardless of provider capabilities.
Deleting a destination requires confirmation; deleting a folder is blocked while a rule references it.
Deleting a label removes it from rules and increments their versions; a rule with no remaining
folder or labels is paused so it cannot run provider actions without a filing target. Rule creation,
updates and filing lock target destinations by ID. Rule updates take these locks before any rule
row; deletion locks the destination before its affected rules. Workers lock multiple rule rows by ID, then evaluate them in priority order.
Updates recheck the rule version after acquiring the destination locks. Removing
an account also removes rules scoped to it and their runs, without widening those rules to other accounts.

Rules match account, sender address or domain, subject and recipient, in ascending priority. The
first matching folder wins and each rule stores up to 20 label IDs in its `label_ids` JSON list;
matching labels accumulate in stable order without duplicates. The generated migration carries each
legacy `label_id` into that list, and refuses a downgrade that would discard extra labels. New-message jobs and their versioned
rule snapshots commit with the sync batch. Local assignments precede provider actions; provider
actions pass through `require_action`, unsupported actions are recorded as skipped, and uncertain
writes are not retried automatically. A sleeping Studio instance delays jobs until it resumes.
The owner can preview and explicitly apply a rule to indexed provider-INBOX mail, including app-filed
mail. The preview counts current changes and already-filed matches; an editor can preview a version-checked
draft in place of its saved rule without changing it, including an active draft of a paused rule.
Confirmation checks the rule version and refuses paused rules with `409 rule_inactive`, re-evaluates
mail and reports actual app changes, provider actions previously handled or
queued, and conflicts. App assignments and first-time provider jobs commit in one transaction.
Manual application restores removed labels and preserves existing folders; automatic reruns remain
deduplicated. Runs stay unique per rule version and mail, but each provider action has one durable
identity per rule, mail and action kind across versions (`mail_rule_action`): it runs at most once
for that rule and mail, and only the run holding the claim dispatches it. A rename or any edit that
adds no action queues nothing; a newly added action runs on the next apply; an action removed and
added again stays handled for mail it already reached. Unknown, dispatched and observed outcomes
are never sent again. An explicit manual apply, never an automatic run, claims again a skip that
was recorded before any dispatch (`skipped_unsupported`, `skipped_permission` or `skipped_stale`
with `dispatched` false) and a pending claim of an older version, which can no longer dispatch;
`require_action` then checks the capability again. A run with an uncertain outcome still stops its
remaining actions. Preview and confirmation count an already claimed action as handled, or as
queued while its run can still finish it. The generated migration collapses existing runs per
action: unknown, in-flight and unrecognized states become unknown, then done, observed partial
results and permission skips win, all marked dispatched; historical permission skips stay blocked
because they cannot prove the write was never sent. Only pending claims and unsupported or stale
skips stay undispatched. Newly queued provider work uses the durable worker and recovery protocol;
an interrupted provider dispatch becomes unknown without being resent. Verified duplicate merges retain rule-run
and claim deduplication; conflicting uncertain dispatches become unknown. Run outcomes identify a folder
assignment overridden by a higher-priority match using the reserved `_folder: overridden` entry in
the run results (not a provider action). On an IMAP UIDVALIDITY change, the rebuilt page
has no reliable old/new delivery identity, so it is conservatively excluded from new-mail rule
scheduling; later incremental deliveries continue normally. Active matching rules receive a durable
rebuild notice suggesting manual apply. Normal background insight filling can still summarize recent
unread rebuilt mail within its existing limits, including on a later sync. Message dates cannot prove
delivery freshness. An account sync starting without a cursor likewise excludes the initial import
from automatic rules, including Graph continuation pages and deferred Studio history. A durable notice
per applicable rule and import identifies the account and offers manual application. Positively
identified later connector deliveries and subsequent incremental syncs run rules normally. During
Studio connector history pagination and its unfinished overlapping scan, all mixed-page messages are
treated as ambiguous, including genuinely new deliveries: the owner can apply rules manually. Once
that scan completes, unrelated incremental messages run normally even if a deferred historical thread
is still pending. Insight scheduling still uses the
complete new-message list. An in-progress import and its deferred history
are tracked in `mail_sync_state.rules_initial_import` and commit with the sync page and notice.
Chat proposals require
confirmation, recheck the saved account grant and commit the card with the local mutation. Rule
proposals require a direct request in the current stored user message. Folder and rule management
reads are browser-only and every non-GET operation is in the MCP mutating inventory.
