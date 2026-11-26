# Chat: templates, turn context, skills and mail access

Part of the agent instructions: [AGENTS.md](../../AGENTS.md) links here from its index, and its rules apply here too.

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

## Server-side skill runtime (#84)

Executable tools are registered only in trusted server application code (`api/skills/registry.py`,
`api/mail/agent_tools.py`). Built-in skills (`api/skills/catalog.py`, currently `daily_briefing`
with the domain aggregation tool `collect_daily_briefing` in `api/skills/briefing.py`, and
`skill_creator`) and custom
skills live in `skill_definition`/`skill_revision` and reference registered tools explicitly.
Turns activate a skill only when `skill_id` or `skill_revision_id` is passed on that turn request
(turn-scoped, never sticky to later turns or other chats); `chat_context` records `skill_id`,
`skill_revision_id`, and `skill_hash`. Legacy turns without a skill never receive skill tool rights.
When active, the model receives only that revision's `allowed_tools`, instructions stay in `role="user"`,
domain scopes and confirmation cards remain mandatory, and disabling, narrowing tools, or deleting
a skill revokes affected contexts (`409 proposal_context_revoked`, mid-stream `context_revoked`). See [docs/skills.md](../skills.md).

Skills drafted in the chat (#97): the built-in `skill_creator` (`api/skills/creator.py`) reads no
mail, calendar or task data and has only `list_authorable_tools`, `propose_skill` and
`suggest_followups`; both creator tools are `builtin_only`. A tool flagged `authorable` in the
registry is approved for chat-drafted skills: read and display tools plus `propose_action`,
`create_draft`, `propose_task`, `propose_calendar_change`, `propose_filing` and
`collect_daily_briefing`, never `propose_rule` or `propose_preference`. `propose_skill` stores a
pending card of kind `skill`; confirming it with an optional `skill_edit` (slug, name, description,
instructions) claims the card, rechecks that its context ran the active built-in creator under the
catalog lock, validates again and creates the custom skill in one transaction. Refusals leave the
card pending. Slugs are unique and names unique case-insensitively (`skill_name_conflict`); `tools`
and `skill_creator` are reserved. The web offers the creator only as the „Skill erstellen“ slash
action, which starts a new conversation; built-in skill names are localized by slug. A created
skill is an ordinary custom skill and may use its read tools within the turn's scopes.

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
