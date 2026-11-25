# Server-Side Skill Runtime & `#81` Contract

This document defines the backend skill architecture, security boundaries,
versioning/revocation rules, the executable `daily_briefing` vertical slice,
the chat-drafted skills of Issue `#97`, and the coordination/integration
contract for Issue `#84` and Issue `#81`.

## 1. Core separation: Tools, Skills, and Templates

1. **Trusted Tool Registry (`apps/api/src/api/skills/registry.py`)**
   - Executable tools are registered **only in server-side Python application code**.
   - Neither skill descriptions/instructions nor untrusted mail, calendar, or task
     data can define code, unlock unregistered tools, or bypass scope checks.
   - Tools carry `skill_only: bool` so skill-specific backend tools (such as
     `collect_daily_briefing`) are never part of the default base chat toolset.
   - `authorable: bool` approves a tool for skills drafted in the chat (`#97`).
     The approved tools are the read and display tools plus `propose_action`,
     `create_draft`, `propose_task`, `propose_calendar_change`, `propose_filing`
     and `collect_daily_briefing`. `propose_rule` and `propose_preference` are
     never authorable.
   - `builtin_only: bool` reserves a tool for built-in skills; such a tool can
     never be authorable and no custom skill can reference it
     (`skill_tool_not_authorable`). The creator's own tools are built-in only.

2. **Versioned Skill Catalog (`skill_definition`, `skill_revision`)**
   - Each skill has a stable `id`, unique `slug` (for example `daily_briefing`),
     `origin` (`builtin` | `custom`), `status` (`active` | `disabled`),
     `version`, current `revision_id`, and `updated_at`.
   - Each `skill_revision` stores immutable snapshots of `name`, `description`,
     `instructions`, structured `workflow_steps_json`, `allowed_tools_json`,
     and `content_hash`.
   - Custom skills validate every referenced tool against `TOOL_REGISTRY` at
     creation/update (`422 skill_tool_unknown`) and ensure every workflow step's
     `tool_names` is a subset of `allowed_tools`.
   - Built-in skills (`apps/api/src/api/skills/catalog.py`) synchronize
     idempotently into `skill_definition`/`skill_revision` while preserving the
     owner's `status` (`active` or `disabled`).

3. **Existing Personal Templates (`prompt_template`, `prompt_revision`)**
   - Existing templates and `/api/v1/prompts` remain fully functional and
     unchanged.
   - Legacy conversations and existing `prompt_revision` rows retain
     `skill_id = NULL`, `skill_revision_id = NULL`, `skill_hash = NULL` and
     never receive skill tool permissions retroactively.
   - Issue `#81` handles `/` slash discovery in the composer and any UI
     migration/unification between templates and skills on top of this contract.
   - Whether custom tools can later be authored without code (no-code editor)
     remains an open product decision outside `#84`.

## 2. Activation Lifecycle, Turn Duration, and Follow-Up Turns

- **Explicit activation only:** A skill is active **only** when the turn
  request (`POST /api/v1/chat` or `POST /api/v1/chat/stream`) explicitly passes
  `skill_id` (UUID `id` or `slug`) and/or `skill_revision_id`.
- **No implicit carryover:**
  - Starting a conversation without `skill_id` leaves `skill_revision_id = None`
    and exposes only the standard base chat tools.
  - Follow-up turns in the same conversation that omit `skill_id` /
    `skill_revision_id` return immediately to standard chat mode without the
    skill's tools or workflow.
  - Passing `skill_id` on a follow-up turn inside an existing conversation
    activates that skill for that turn (supporting `/` mid-conversation in `#81`),
    without altering `chat_conversation` or leaking into other conversations.
- **Tool-set enforcement:**
  - When a skill is active, the model receives **only** `allowed_tools` from the
    resolved `skill_revision`.
  - Both `tools_for_turn` (before the model call) and `execute_tool` (at tool
    dispatch) enforce `allowed_tools` fail-closed (`tool_not_permitted` /
    `unknown_tool` / `skill_tool_unavailable`).
- **Scope & confirmation invariants:**
  - Account (`#80`), calendar (`#82`), task, and preference scopes remain
    strictly enforced across composite skill tool calls.
  - Skill instructions and workflows are injected only in `role: "user"`
    (never as app/system rules) and never persist text into `chat_context`,
    `chat_message.extras_json`, or logs.
  - All write/send actions continue to create `pending` proposals and execute
    only via `POST /api/v1/chat/proposals/{proposal_id}/confirm` after explicit
    user confirmation; multi-step workflows never self-confirm or retry
    uncertain external writes.

## 3. Revision Binding, Disabling, Deletion, and Error Codes

- `chat_context` records `skill_id`, `skill_revision_id`, and `skill_hash`
  under row lock (`lock_skill_revision` before `ChatConversation`).
- **Disabling (`status = "disabled"`), narrowing `allowed_tools`, or deleting
  a skill** atomically revokes affected `chat_context` rows (`revoked = True`)
  in the same database transaction.
- **Deletion** of a custom skill wipes text/steps/tools from its
  `skill_revision` rows (`removed = True`) and clears `skill_hash = None` on
  affected `chat_context` rows, leaving content-free tombstones.
- Open proposals from a revoked skill context cannot be confirmed
  (`409 proposal_context_revoked`), and in-flight streams terminate cleanly with
  `context_revoked`.

### HTTP / Domain Error Codes

| Code | HTTP | Meaning |
| --- | --- | --- |
| `skill_not_found` | `404` | Unknown `skill_id` / `slug`. |
| `skill_revision_unavailable` | `404` | Missing, removed, or mismatched `skill_revision_id`. |
| `skill_disabled` | `409` | Requested skill is currently disabled. |
| `skill_tool_unknown` | `422` | Skill definition references a tool not in `TOOL_REGISTRY`. |
| `skill_tool_unavailable` | `422` | Pinned skill revision requires a tool absent from current server code. |
| `skill_builtin_protected` | `403` | Attempted structural edit or deletion of a `builtin` skill. |
| `skill_conflict` / `skill_quota` | `409` | Duplicate `slug` or custom skill storage limit reached. |
| `skill_name_conflict` | `409` | Another skill already uses this name (compared case-insensitively). |
| `skill_tool_not_authorable` | `422` | A custom skill selects a built-in-only tool, or a chat-drafted skill selects a tool that is not authorable. |
| `skill_invalid` | `422` | Invalid slug/name/description/instructions/steps (input not echoed). |
| `version_conflict` | `409` | Stale `expected_version` on concurrent update/delete. |

## 4. Documented Vertical Slice Example: `daily_briefing`

- **Skill identity:** `slug = "daily_briefing"` (`origin = "builtin"`).
- **Skill-specific backend tool:** `collect_daily_briefing`
  (`apps/api/src/api/skills/briefing.py`, `skill_only = True`).
- **Authorized domain behavior:**
  - Reads local calendar occurrences via `api.mail.calendar_tools.read_calendar`
    for `[date, date + 1 day)` in `ctx.calendar_timezone`.
  - Reads open due and overdue tasks via `api.tasks.chat.read` in
    `ctx.task_scope` and `ctx.account_ids`.
  - Reads cached priority/deadline inbox messages via `mail_message.list_page`
    and `mail_insight.get_for_messages` within `[ctx.account_id]` or
    `ctx.account_ids` (without calling providers or LLM insight generation).
  - Separately reports `coverage` (`excluded_domains`, `incomplete_domains`) so
    missing/paused/stale scopes are never hidden.
- **Multi-step workflow (`workflow_steps`):**
  1. `collect_agenda`: Run `collect_daily_briefing` for the target date.
  2. `inspect_details`: Optionally inspect specific items via
     `get_calendar_occurrence`, `get_task`, `get_message`, or `get_insights`.
  3. `summarize_and_propose`: Present separated domain sections with explicit
     coverage notices (`show_mails`, `suggest_followups`), and only propose
     task changes via `propose_task` when explicitly requested.
- **Intentional least-privilege exclusion:** `create_draft`, `propose_action`,
  and `propose_calendar_change` are omitted from `daily_briefing.allowed_tools`.

## 5. Drafting a Skill in the Chat (`#97`)

- **Creator skill:** the built-in `skill_creator` ("Skill erstellen") has only
  `list_authorable_tools`, `propose_skill` and `suggest_followups`. It reads no
  mail, calendar or task data. Both creator tools are `builtin_only` and check
  at dispatch that the turn runs the active built-in creator; otherwise they
  return `tool_not_permitted`. The web client offers the creator only as the
  "Skill erstellen" slash action, which always starts a new conversation and
  keeps the creator attached until the owner ends it.
- **Draft card:** `propose_skill` accepts exactly `slug`, `name`,
  `description`, `instructions`, `workflow_steps` and `allowed_tools`, validates
  them with the HTTP rules plus `authoring=True` (authorable tools only), and
  reports `skill_conflict`, `skill_name_conflict` or `skill_quota` to the model
  so it can suggest an alternative. It stores a pending proposal of kind
  `skill`; nothing is created yet. A changed tool selection or workflow needs a
  new card.
- **Confirmation:** `POST /api/v1/chat/proposals/{proposal_id}/confirm` accepts
  an optional `skill_edit` (`slug`, `name`, `description`, `instructions`) and
  no selection, body, task edit or preference decision. In one transaction it
  claims the card, rechecks that the bound context is live and belongs to the
  conversation, takes the catalog lock `380084`, rechecks that the context ran
  the built-in creator, validates the edited draft again (`authoring=True`),
  creates the custom skill and finishes the card with `result.skill_id`. Any
  refusal raises before the commit, so the card stays pending and can be
  corrected. A disabled creator yields `409 proposal_context_revoked`. As for
  the other card kinds, the claim also requires the turn's chat grant to be
  current; a refused claim or a second confirm yields
  `409 proposal_not_pending`, and the history shows a card from an older
  grant as cancel-only.
- **Uniqueness:** slugs are unique; names are unique case-insensitively
  (`lower(name)` under the catalog lock) for HTTP creation, updates and
  confirmed cards alike. `tools` and `skill_creator` are reserved slugs.
- **What a created skill can do:** it is an ordinary custom skill. Activated
  on a turn, it may use its authorable read tools within the turn's mail,
  calendar and task scopes, and its proposal tools still create confirmation
  cards.
- **HTTP path:** `POST`/`PATCH /api/v1/skills` keep validating against the
  whole registry except built-in-only tools; only chat-drafted skills are
  limited to authorable tools. `GET /api/v1/skills/tools` reports
  `authorable` per tool.

## 6. Adding and Versioning a New Skill or Tool

1. **Add a backend tool:**
   - Implement an async handler `(arguments: dict, ctx: TurnContext) -> ToolOutcome`
     that enforces `ctx` scopes and returns `untrusted_*` JSON.
   - Define a `RegisteredTool(..., skill_only=True)` and register it in
     `register_default_tools()` (`apps/api/src/api/skills/registry.py`) and
     dispatch it in `api.mail.agent_tools.execute_tool`.
   - Set `authorable=True` only when the tool is safe for skills drafted in
     the chat, and add its localized name to `skillProposal.toolNames` in both
     web catalogues.
2. **Add or version a built-in skill:**
   - Add or update a `BuiltinSkillSpec` in `BUILTIN_SKILLS`
     (`apps/api/src/api/skills/catalog.py`).
   - `ensure_builtin_skills()` hashes the canonical payload and automatically
     appends a new `SkillRevision` whenever code changes, preserving owner
     `status` and revoking older contexts if `allowed_tools` shrank.
3. **Add or version a custom skill via HTTP:**
   - `GET /api/v1/skills/tools` (`skill_tools_list`) lists trusted tools.
   - `GET /api/v1/skills` (`skills_list`), `POST /api/v1/skills` (`skill_create`),
     `GET /api/v1/skills/{skill_id}` (`skill_get`),
     `PATCH /api/v1/skills/{skill_id}` (`skill_update`), and
     `DELETE /api/v1/skills/{skill_id}?expected_version=...` (`skill_delete`).

## 7. Coordination Inventory: Shared Files Touched by `#84`

To prevent merge conflicts with `#91` (mail hotfix) and `#77` (`Heute fällig`
quick action), `#84` keeps all changes outside `#91`/`#77` UI/i18n paths:

- **Untouched by `#84`:**
  - `apps/api/tests/support/fake_store.py` (mail test helper used by `#91`)
  - `packages/py/db/tests/test_chat_settings_pg.py` (`#91`)
  - `apps/web/src/i18n/de.ts`, `apps/web/src/i18n/en.ts` (`#91` and `#77`)
  - `apps/web/src/routes/_app/index.tsx`, `apps/web/src/chat/session.tsx` (`#77`)
  - `apps/api/src/api/tasks/chat.py`, `apps/api/src/api/mail/calendar_tools.py` (`#77`)
- **Shared files requiring small, scoped sections in `#84`:**
  1. `packages/py/db/src/db/models/chat_context.py`: nullable `skill_id`,
     `skill_revision_id`, `skill_hash` columns.
  2. `packages/py/db/src/db/accessors/chat_context.py`: `lock_skill_revision`
     (locked before `ChatConversation`), skill resolution in `resolve(...)`, and
     skill active/non-removed checks in `is_live(...)`.
  3. `packages/py/db/src/db/accessors/chat_message.py`: check active/non-removed
     skill context inside `claim_proposal_in_session` (via shared helper or
     `chat_context.is_live_in_session`).
  4. `apps/api/src/api/mail/agent_tools.py`: `TurnContext.allowed_tools`,
     fail-closed tool allowlist check in `execute_tool`, `tools_for_context`, and
     dispatch of `collect_daily_briefing` via `TOOL_REGISTRY`.
  5. `apps/api/src/api/mail/context_resolver.py`: pass `skill_id` /
     `skill_revision_id` and map `SkillError` to HTTP `refuse(...)`.
  6. `apps/api/src/api/mail/agent_prompt.py`: append a separate skill-boundary
     rule (below the existing `Heute fällig` lines) and bump `APP_PROMPT_VERSION`.
  7. `apps/api/src/api/mail/agent.py`: accept `skill_id` / `skill_revision_id`,
     pass `tools_for_turn(active_skill)` to the model, inject
     `skill_prompt_message(active_skill)` in `role="user"`, and record
     content-free `skill` execution ref in `ChatExtrasOut`.
  8. `apps/api/src/api/mail/chat_schemas.py`: optional `skill_id`,
     `skill_revision_id`, `skill_hash` on `ContextOut` and optional `skill` on
     `ChatExtrasOut` (defaults `None`, keeping existing web clients compatible).
  9. `apps/api/src/api/routers/api/v1/chat/{route,stream}.py`: optional
     `skill_id` and `skill_revision_id` fields on `ChatTurnRequest` and
     `ChatStreamRequest`.
  10. `apps/api/tests/test_mcp_exposure.py`: register `skill_create`,
      `skill_update`, `skill_delete` in `MUTATING_OPERATIONS` and `skills_list`,
      `skill_get`, `skill_tools_list` in `BROWSER_ONLY_GET_OPERATIONS`.
  11. `apps/api/openapi.json` and `apps/web/src/api/generated/*`: regenerated via
      `bash tools/gen-types.sh`.
