# UI routes and app language

Part of the agent instructions: [AGENTS.md](../../AGENTS.md) links here from its index, and its rules apply here too.

## UI routes

- `/` — chat start with greeting, account status, quick actions and composer; optionally with mail context.
- `/chat/$conversationId` — conversation with history, replies and proposal cards.
- `/mail` — inbox with list and filters; `/mail/$messageId` — opened mail with reading pane.
- `/accounts` — account overview, connect, sign in again and remove.
- `/tasks` and `/tasks/$taskId` — task list, manual creation and due dates with history.
- `/calendar` and `/calendar/$occurrenceId` — calendar with month, week, day and list views (`view` and `date` as search parameters) and an opened occurrence, shown beside the calendar on desktop and wide screens and in its place otherwise ([calendar.md](calendar.md)).
- `/settings` and `/settings/prompts` — settings with template library; `settingsSections` is the navigation entry point for further implemented areas.
- `/settings/prompts/new` and `/settings/prompts/$promptId` — create or edit a template in a separate view at all screen sizes.
- `/settings/skills` — built-in and custom skills with command, version, origin and status; switch them on or off and delete custom skills after a confirmation dialog, through `skills_list`, `skill_update` and `skill_delete` with `expected_version`.
- `/settings/preferences`, `/settings/preferences/new` and `/settings/preferences/$preferenceId` — preferences, usage toggle and separate create/edit views, including on mobile.
- `/settings/chat` — default model for new conversations and the mail accounts the chat may use ([chat.md](chat.md#chat-mail-access-80)).
- `/settings/calendar` — choose or clear the writable default calendar for new appointments ([calendar.md](calendar.md#calendar-in-chat)).
- `/settings/controls` — left and right swipe actions in the mail list (`none`, `archive`, `toggle_read` or `trash`), stored in `ui_settings`.
- `/settings/folders` and `/settings/rules` — app folders and labels, and ordered mail rules. `/settings/rules?source_mail=<message-id>` (at most 100 characters) prefills the rule editor from that mail and drops the parameter once used ([mail-filing-and-rules.md](mail-filing-and-rules.md); [DESIGN.md §9.7](../../DESIGN.md#97-templates-and-settings)).
- `/settings/language` — app language, speech pace and voice model; see [App language](#app-language).
- `/audio-device-test` — owner device check outside the app layout: record and transcribe, play a sample and run the paid smoke while `AUDIO_DEVICE_TEST_UNTIL` lies in the future, otherwise a closed notice ([audio.md](audio.md)).

The list covers every route under `apps/web/src/routes/`; the route files stay authoritative, and the linked notes describe each area. Add an entry here with every new route.

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
