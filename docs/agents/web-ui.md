# UI routes and app language

Part of the agent instructions: [AGENTS.md](../../AGENTS.md) links here from its index, and its rules apply here too.

## UI routes

- `/` — chat start with greeting, account status, quick actions and composer; optionally with mail context.
- `/chat/$conversationId` — conversation with history, replies and proposal cards.
- `/mail` — inbox with list and filters; `/mail/$messageId` — opened mail with reading pane.
- `/accounts` — account overview, connect, sign in again and remove.
- `/tasks` and `/tasks/$taskId` — task list, manual creation and due dates with history.
- `/settings` and `/settings/prompts` — settings with template library; `settingsSections` is the navigation entry point for further implemented areas.
- `/settings/prompts/new` and `/settings/prompts/$promptId` — create or edit a template in a separate view at all screen sizes.
- `/settings/skills` — built-in and custom skills with command, version, origin and status; switch them on or off and delete custom skills after a confirmation dialog, through `skills_list`, `skill_update` and `skill_delete` with `expected_version`.
- `/settings/preferences`, `/settings/preferences/new` and `/settings/preferences/$preferenceId` — preferences, usage toggle and separate create/edit views, including on mobile.

This list is not complete. The route files under `apps/web/src/routes/` are authoritative; calendar, chat, language, folder and rule settings are described in their domain notes.

## App language

`/settings/language` saves the single owner’s language in the shared `ui_settings` row with the
same revision check as swipe settings. Existing rows default to `browser`. In automatic mode
the web server uses the browser’s `Accept-Language` list, with English as the fallback when
neither German nor English is accepted. A cookie mirrors an explicit setting after the browser
confirms it with the API. It lets subsequent documents start in that language during hydration.
A new device starts with its browser preference until its first settings read. Re-focusing a tab
reloads the setting from the server.

Chat and insight requests send the browser language. The saved override always takes precedence;
in browser mode requests use their browser hint, and background jobs use the last confirmed
browser hint or English if none exists. Chat replies and new drafts default to that language; an explicit language request takes precedence.
Replies and forwards default to the original mail language. Automatically detected input language alone
does not override the app language. The current user message includes a transient, clearly delimited instruction in the effective
language for each model round. It is never stored in chat history and does not change message roles. Speech recognition and
voice follow the effective app language. Insights store their
language and are regenerated when it differs, while seed accounts never call the model. Changes to the
mail body continue to invalidate its insight. Direct preference requests support both languages
(for example “Remember this”). Automatic suggestions retain the existing German phrase and
evidence checks.
