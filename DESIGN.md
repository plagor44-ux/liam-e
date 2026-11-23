# DESIGN.md — liam-e

> **Binding source for the UI.** Read before any work on `apps/web`.
> Exact values (colors, dimensions, shadows) are in the **tokens**; this document governs
> how they are used, how text sounds and how the app behaves on small screens.

**Status:** valid, as of 2026-11-22. The sources of values are `apps/web/src/styles/tokens.css`
(tokens) and `apps/web/src/ds/` (components). The „liam-e“ design system in Claude Design
(owner artifact, not public, style direction C „Weich“), from which they originated, is kept
aligned with them (§10).

## Conflict hierarchy

In a conflict, the higher item wins.

**Who decides:**
1. Owner in chat or the task.
2. `AGENTS.md` (repository rules).
3. **DESIGN.md** (this document).

**What applies in each case:**
- **Exact values:** the tokens. If a value here differs, DESIGN.md is corrected.
- **Rules and behavior** (text, states, responsive behavior): DESIGN.md. If components
  differ, a decision is made and either the code or this document is adjusted.
- Library defaults (Tailwind, TanStack) come last.

---

## 1. Essence and attitude

liam-e brings mail from all accounts into one place and takes care of what needs doing with you.
The UI is **light, soft and tidy**: floating surfaces on a greenish-gray ground, deep petrol as the
only color for actions, large rounded corners, light and dark themes. It recedes behind the content
until something needs doing; then the effect of a click is clear.

- **One goal per surface.** At most one primary action per view.
- **Nothing happens without confirmation.** The assistant proposes what it changes or sends;
  only a click executes it (§9.2).
- **Every button explains itself.** Icon buttons carry a name and tooltip; so do buttons with
  non-obvious consequences.
- **Calm before density.** Prefer one fewer line to one more border.
- **Motion explains; it does not decorate.**

## 2. Language and text

German examples in this section are UI text; keep their wording verbatim.

### 2.1 Write natural German and English

Write each locale as someone would speak to the reader. German is the reference for existing
wording. A caller chooses German, English, or the browser language in Settings → Language.
The app uses English when none of the browser’s preferred languages are supported. German
formatting follows §2.5. English uses month-first dates, 12-hour times, a decimal point, and
a thousands comma.

The owner's benchmark (2026-11-13): „Guten Morgen! Was kann ich für dich erledigen?“ sounds
slapdash. „Hallo, was kann ich für dich tun?“ sounds right. This is an example of tone, not an
individual rule.

### 2.2 Tone

- **Du**, lowercase (du, dein). Friendly, brief, factual; like someone thinking along, not a hotline.
- The assistant may speak in the **first person** („Hallo, was kann ich für dich tun?“), but
  without feelings or clichés: no „Gerne!“, no „Ich hoffe, das hilft“, no „Natürlich!“, no
  chains of apologies.
- **Verbs instead of noun chains.** „ob du das Angebot annimmst“, not „deine Bestätigung des Angebots“.
- **The common German word first.** Anglicisms only where they are the normal everyday word
  (Mail, Chat, Newsletter, Link, Spam) or a platform term (Secret).
- **No emojis, no exclamation marks** in the UI.

In English, use plain words and direct address. Prefer sentence case, short verbs, and natural
contractions. Use “email”, “account”, “inbox”, “thread”, “conversation”, “draft”, “archive”, “trash”,
and “unsubscribe” consistently. Buttons state what they do, for example “Connect an account” or
“Draft a reply”. A confirmation names the action and its consequences. Use curly quotation marks
and the same spaced ellipsis for progress. Assistant replies use the user’s current language and use
the app language when it is unclear.

### 2.3 Typical mistakes

| Instead | Better | Why |
|---|---|---|
| „Guten Morgen! Was kann ich für dich erledigen?“ | „Hallo, was kann ich für dich tun?“ | Time-of-day greeting and „erledigen“ sound like a hotline; nobody asks like that. |
| „Drei Mails brauchen eine Antwort.“ | „Auf drei Mails solltest du antworten.“ | „need a reply“, translated literally. |
| „Danach wartet die Meyer GmbH auf deine Bestätigung des Angebots.“ | „Die Meyer GmbH möchte bis Montag wissen, ob du ihr Angebot annimmst.“ | Nominal style; a verb carries the sentence. |
| „Bist du sicher?“ | „5 Newsletter abbestellen? Das lässt sich nicht rückgängig machen.“ | Name the consequence rather than sow doubt. |
| „Erfolgreich gespeichert!“ | „Gespeichert.“ | Success is the normal case. |
| „Es ist ein Fehler aufgetreten.“ | „Gmail antwortet nicht. Versuch es in ein paar Minuten noch mal.“ | Cause and next step. |
| „Keine Daten verfügbar“ | „Keine Mails in diesem Konto. Neue erscheinen nach dem nächsten Abruf.“ | Name the thing, then help further. |
| „Lade …“, „Loading …“ | „Wird geladen …“ | „Lade“ reads as a command. |
| „Klicke hier, um ein Konto zu verbinden.“ | Button „Konto verbinden“ | Link text is the action. |
| „Sync“, „Inbox“, „Draft“, „Thread“ | „Abrufen“/„Stand“, „Posteingang“, „Entwurf“, „Verlauf“ | A German everyday word exists. |
| „Gerne! Hier ist deine Zusammenfassung:“ | Start directly with content | An empty phrase without information. |
| „Möchtest du, dass ich Antworten entwerfe?“ | Suggestion as chip: „Antworten entwerfen“ | Offers are actions, not questions. |
| „Mail(s)“ | „1 Mail“, „2 Mails“, „keine Mails“ | Write out plural forms. |
| „11/21“, „Fri, Nov 21“ | „Fr., 21.11.“ | German date format. |

### 2.4 Terms

Same thing, same word, everywhere: navigation, buttons, tooltips, notices and assistant answers.
The first two columns contain German UI terms and alternatives.

| Term | Not | Meaning |
|---|---|---|
| Mail, Mails | E-Mail(s), Nachricht | a mail; „E-Mail“ only in fixed words such as „E-Mail-Adresse“ |
| Konto | Account, Postfach | a connected mail account (Gmail, Outlook, iCloud, IMAP) |
| Posteingang | Inbox | the combined list of all mail from all accounts |
| Verlauf | Thread, Konversation | related mails about one subject |
| Unterhaltung | Chat-Verlauf, Session, Thread | a conversation with the assistant |
| Entwurf | Draft | mail proposed by the assistant, not yet sent |
| Abrufen · Stand | Sync, Synchronisieren, Refresh | fetch new mail · time of last fetch („Stand 08:57“) |
| Archivieren | Ablegen, Verschieben | remove from inbox; mail stays in the account |
| In den Papierkorb | Löschen (bei Mails) | recoverable; „Löschen“ only where permanent |
| Abbestellen | Abmelden, Unsubscribe | end a newsletter through the unsubscribe link |
| Verbinden · Entfernen | Hinzufügen · Löschen (bei Konten) | add an account to or remove it from liam-e; everything remains at the provider |
| Anmelden | Einloggen, Login | sign in to Google or Microsoft |
| Bestätigen · Abbrechen | OK · Ja · Nein | decision on a proposal |
| Rückgängig | Undo, Zurücknehmen | reverse an action while possible |
| Modell | KI, LLM | the selected Mistral model |
| Darstellung | Theme, Modus, Design | light, dark or as in the system |
| Secret | Geheimnis, Token | workspace secret in Mistral Studio (platform term) |
| liam-e | Bot, KI, Agent | assistant's name where a name is needed |

### 2.5 Writing conventions

- **Punctuation:** quotation marks „…“ (nested ‚…‘). Ellipsis as one character „…“, preceded
  by a space when words are missing („Wird geladen …“). En dash „–“ with spaces; range dash
  without („9–17 Uhr“). Hyphen only inside words.
- **Numbers:** decimal comma, thousands dot from four digits („2.314 Mails“). Number and unit
  separated by a non-breaking space („184,20 €“, „3 Min.“). Digits in controls and counters;
  in assistant answers write out numbers up to twelve, except amounts, dates and times.
- **Date and time:** in lists „08:57“ for today, „Gestern“, for the remaining days of the
  current week from Monday the weekday („Di.“), before that „21.11.“; the year only when not
  the current one. An open mail shows day and time („Heute, 08:57“, „Di., 08:57“), a deadline
  appears as „Fr., 21.11.“. In sentences, thus in assistant answers, „Freitag, 21. November“ and
  „um 8:57 Uhr“; the UI itself does not yet need this form. The UI does not use relative values
  („vor 5 Min.“); if added, only up to „vor 59 Min.“.
- **Plural:** a separate text per form through `Intl.PluralRules` („keine Mails“, „1 Mail“,
  „2 Mails“). Never parenthesized plurals.
- **Capitalization** follows German spelling, with the first word of buttons and headings capitalized.
  No title case, no all caps.
- **Abbreviations** only in notices („z. B.“ with a space), never in buttons.

### 2.6 Types of text

- **Buttons:** infinitive verb with object, one to four words: „Konto verbinden“,
  „Antwort entwerfen“, „5 archivieren“. Never „OK“, „Ja“, „Absenden“.
- **Tooltips:** one sentence saying what happens, with a third-person verb („Holt neue Mails aus
  allen Konten“) or briefly in the infinitive („Archivieren (E)“). Keyboard shortcut in
  parentheses. Never repeat the visible name.
- **Notices after an action:** result with count, participle at the end („61 Mails archiviert.“),
  then „Rückgängig“ where possible.
- **Errors:** what failed, the cause if known, and the next step. No blame, no unexplained error codes.
- **Empty states:** name the thing and say what happens next or what you can do.
- **Confirmations:** question with count and object („5 Newsletter abbestellen?“), one sentence
  of justification, explicitly state irreversible consequences.
- **Greeting:** brief and without time of day („Hallo, was kann ich für dich tun?“), followed by
  one status line („47 ungelesene Mails in 4 Konten · Stand 08:57“).
- **Placeholders:** an invitation or example („Frag etwas zu deinen Mails …“), not instructions.
- **Assistant answers:** start with the result, without introduction or closing formula. Rules from
  §2.2 to §2.5 apply here too; the system prompt in `apps/api/src/api/mail/agent_prompt.py` contains them.

### 2.7 Checklist for every text

1. Read aloud: would you say this to someone?
2. Does an English sentence show through? Rephrase, do not rearrange.
3. Are the terms from §2.4 correct?
4. Are punctuation and formats from §2.5 correct?
5. Is the consequence of an action stated?
6. Are plural forms for 0, 1 and 2 checked?
7. Does the longest realistic value fit (account name, subject, sender)?

### 2.8 Where text lives

UI copy lives in `apps/web/src/i18n/de.ts` and `apps/web/src/i18n/en.ts`, grouped by view.
The German catalogue defines the required keys and argument types. `format.ts` formats dates,
times and sizes in the active locale. Counts use `Intl.PluralRules` and locale-specific number
formatting. Structured API errors are rendered from a catalogue by code, using the supplied
server message when a code is unknown. Technical literals in TSX, such as paths and class names,
carry `// i18n-exempt`. Stored content and provider text are shown in their original wording.

## 3. Color

Quoted German labels in this section are UI text.

- The app ground is `surface-ground`; `surface` surfaces with `shadow-sheet` float on it
  (sidebar, main surface). Within a surface, `surface-soft` tiles provide structure. Menus and
  popovers sit on `surface-overlay` with `shadow-overlay`.
- `accent` is the **only action color**: primary buttons, active navigation item, send, links,
  assistant mark, success. Text on it uses `on-accent`. `accent-line` is only the underline of
  links (`le-link`); on hover it becomes `accent`.
- **Hover** in three levels:
  - Rows and entries (mail row, navigation item, conversation, menu entry, confirmation card
    entry): `surface-soft`.
  - Tiles, chips, icon buttons and ghost buttons: `accent-soft` with `accent` text; chips also
    show their border in `accent`, quick actions lift with `shadow-lift`. The chat mail card
    gets only the surface.
  - Controls with their own color: primary buttons and send become `accent-hover` (primary
    buttons with `shadow-lift`), secondary buttons color only border and text `accent`, danger
    buttons get a `danger-soft` ring, fields an `ink-muted` border.
- `warm` means only **urgency**, never action or decoration: the dot of a marked mail (beside the
  time in the mail row, before the sender on the chat mail card) with name and tooltip „Dringend“,
  and the deadline badge.
- `danger` only for deletion and errors, always with a word or icon.
- `acc-1` through `acc-4` color **accounts** in connection order (avatar, account dot); from the
  fifth account the sequence starts again. They never signify status.
- Control borders are `control-border`; `line` and `line-strong` are purely decorative.
- **Dark theme** follows the system setting (`prefers-color-scheme`) until an appearance is
  selected. It is not inverted light: `accent` gets lighter and carries dark text, shadows get
  deeper rather than colored.
- **Choose appearance:** An icon button `sun-moon` („Darstellung“) at the bottom of the sidebar,
  to the right of the status line, opens a menu with „Hell“, „Dunkel“ and „Wie im System“; the
  selected row carries `check`. On the tablet toolbar it stands alone at the bottom, on mobile in
  the drawer. The menu opens upward. The choice applies only in this browser (`localStorage`,
  „Wie im System“ stores nothing) and is set as `data-theme` on `<html>` before first paint to
  prevent a flash.
- Drawer and dialogs dim the content behind them with `scrim` (`.le-scrim`; light
  `rgba(13, 19, 17, .45)`, dark `rgba(0, 0, 0, .6)`). Dialogs attach directly to `<body>`
  (`createPortal`): inside the page, the dimming would lie below the sidebar's sticky heading.
- Never hex values in components, always `var(--…)`.

### Contrast (WCAG AA, light and dark)

Text at least 4,5:1, edges, focus ring and other meaningful indicators at least 3:1. Measured
2026-11-13 against design-system values; the column names the weakest pair across both themes.

| Token | On | Target | Weakest pair |
|---|---|---|---|
| `ink` | `surface`, `surface-soft`, `surface-ground`, `surface-overlay`, `accent-soft` | 4,5:1 | 10,65:1 |
| `ink-muted` | `surface`, `surface-soft`, `surface-ground`, `accent-soft` | 4,5:1 | 5,43:1 |
| `accent` as text | `surface`, `surface-soft`, `accent-soft` | 4,5:1 | 5,36:1 |
| `on-accent` | `accent`, `accent-hover` | 4,5:1 | 6,40:1 |
| `warm` | `surface`, `surface-soft`, `warm-soft` | 4,5:1 | 4,94:1 |
| `danger` | `surface`, `surface-soft`, `danger-soft` | 4,5:1 | 5,38:1 |
| `on-danger` | `danger` | 4,5:1 | 6,54:1 |
| `on-acc` | `acc-1` through `acc-4` | 4,5:1 | 4,58:1 |
| `tip-ink` | `tip-bg` | 4,5:1 | 15,88:1 |
| `control-border` | `surface`, `surface-soft` | 3:1 | 3,19:1 |
| `focus-ring` | `surface`, `surface-soft`, `surface-ground`, `accent-soft` | 3:1 | 5,36:1 |
| `ink` | destination backgrounds | 4,5:1 | 9,68:1 |
| destination border | `surface`, `surface-soft` | 3:1 | 5,72:1 |

Destination colours use a filled tag with a border, distinct from the account's coloured dot and
the urgency and danger treatments. Folder and label icons and names remain visible in grayscale.
The fixed palette below gives background / border in each theme; text uses `ink`. `none` uses the
neutral on-surface tag. Settings show names and a rectangular swatch, with no free colour picker.

| Name | Light | Dark |
|---|---|---|
| Moss | `#e5f0e3` / `#496845` | `#263e2b` / `#9bbe9b` |
| Olive | `#eff1d8` / `#5c6631` | `#3b3c24` / `#b8bc7c` |
| Teal | `#dcefea` / `#2f6861` | `#193d39` / `#80bdad` |
| Lake | `#dbf0f3` / `#316771` | `#1b3a40` / `#83bac5` |
| Denim | `#e1ecf9` / `#425e88` | `#25374d` / `#95b5e0` |
| Indigo | `#e7e9fa` / `#555b8e` | `#30334b` / `#aaaee0` |
| Plum | `#f1e6f4` / `#76537e` | `#402e42` / `#c3a6c8` |
| Berry | `#f4e5ee` / `#80526c` | `#402b38` / `#c8a3ba` |

Anyone changing a color value remeasures this table. `apps/web/src/styles/contrast.test.ts`
checks each pair in both themes against the target, not the values in the last column.

## 4. Typography

- **Plus Jakarta Sans** (`font-sans`, followed by system fonts) in 400, 500, 600 and 700 for
  everything. Self-hosted in the app via Fontsource, exactly pinned; no transmission to Google.
  Only previews in Claude Design load it from Google Fonts.
- Styles from tokens: `display` (36 px, greeting only), `title` (24 px, page title and opened
  mail subject), `heading` (17 px), `body` (14,5 px), `body-strong`, `small` (13 px, metadata),
  `caption` (12 px, account tags, counters, times).
- Digits in counters, times and amounts are tabular (`font-variant-numeric:
  tabular-nums`, class `le-num`).
- Unread mails: sender and subject in 700, otherwise 500 and 400. No italics, no all caps.
- No arbitrary px font sizes: sizes are `--font-size-display` through `--font-size-caption` in
  `tokens.css`, and components use only these six. Buttons and inputs use `body`; small buttons,
  chips, notices, errors and subtitles `small`; tags, badges, times in lists and tooltips `caption`;
  confirmation card title and answer headings `heading`.

## 5. Space, shape, shadow, motion

Quoted German labels in this section are UI text.

- **4-px grid**, steps `space-1` through `space-8` (4 to 64 px). Every inner and outer spacing
  and gap (`padding`, `margin`, `gap`) lies on the grid and comes from these steps or their sum
  (20 px = `space-4` + `space-1`). Component sizes (heights, widths, icons) are exempt; control
  heights come from `control-height` (38 px) and `control-height-lg` (44 px). Icon and text are
  8 px apart, dot and text in tags 4 px. App frame and gap between floating surfaces `space-3`
  (from `tablet`; `mobile` has no frame, §7.3), main surface padding `space-5`, tiles and cards
  `space-4`, the „Konto verbinden“ panel 20 px (`space-4` + `space-1`).
- **Dimensions:** `sidebar-width` 236 px (desktop sidebar), `list-width` 400 px (mail list next
  to reading pane), `chat-width` 780 px (maximum history and composer width), `control-height`
  38 px (controls) and `control-height-lg` 44 px (large icon buttons, touch target, §7.2).
- **Radii** only from tokens: `radius-pill` for buttons, chips, navigation items, tags and icon
  buttons; `radius-sm` (12 px) for rows, fields and tooltips; `radius-md` (18 px) for tiles,
  cards and menus, including pressable tiles (quick actions, options in the „Konto verbinden“
  panel); `radius-lg` (24 px) for floating surfaces, composer and confirmation card. The own chat
  bubble has `radius-md` with a small bottom-right corner (`radius-sm` / 2).
- **Shadows** only from tokens: `shadow-sheet` for floating surfaces, `shadow-overlay` for menus,
  popovers and the action bar in lists, `shadow-lift` for hover on tiles and primary buttons.
  Cards within a surface have no shadow.
- **Lines sparingly:** areas are separated through surfaces and spacing; `line` only in long lists,
  outlines only on controls (`control-border`).
- **Motion:** 160 ms for hover and tooltips, 220 ms for state changes, 420 ms for appearing
  content (`le-rise`), curve `cubic-bezier(.2, .8, .2, 1)`. Tooltips after 350 ms dwell,
  immediately on keyboard focus. Loops: spinner 0,8 s per rotation, typing dots 1,2 s, caret 1 s.
  When the area changes (chat, inbox, accounts), the main surface content crossfades; not within
  an area, e.g. list to mail or between conversations. Sidebar and composer stay in place.
- **Floating elements (tooltip, menu) are always entirely on screen and on top.** Both attach to
  `<body>`, positioned only by `ds/floating.ts`: fixed screen coordinates, 8 px from trigger and
  edge. The preferred side is a preference; if there is no room it flips to the opposite side and
  shifts onto the screen. A menu scrolls within the space it has (at most 60% of screen height);
  a tooltip that fits on neither side shifts fully on screen, over its trigger if necessary, and
  is never taller than the screen. Thus no scroll area or `overflow` around the trigger clips it,
  and no ancestor stacking context (`z-index`, `transform`) overlays it. Order above the scrim
  (50): menu 55, tooltip 60. No CSS rule sets `top`, `left`, `right`, `bottom` or `inset` on
  `.le-menu` or `.le-tooltip` or styles them through an ancestor (`.le-chat-header .le-menu`
  never applies in `<body>`); `ds/floating.test.tsx` checks both.
- Always respect **`prefers-reduced-motion`**: then only crossfade (`le-rise` becomes `le-fade`,
  page transitions and drawer continue crossfading), no rising, no sliding in, streaming shows text
  immediately. Loops (typing dots, caret, spinner) stand still.

## 6. Icons and components

Quoted German labels and the German icon names in this section are UI text.

- **Icons: Lucide** (`lucide-react`, exactly pinned), 24 grid, stroke 1,8, rounded ends,
  `currentColor`. Sizes 19 px (standard) and 16 px (small): icons beside text in buttons, chips,
  tags and notices are 16 px; icon buttons, navigation items and quick actions use 19 px. No emojis,
  no provider logos.
- **Lexicon:** `message-circle` Chat · `messages-square` Unterhaltungen (Symbolleiste am Tablet) · `inbox` Posteingang · `at-sign` Konten · `archive`
  Archivieren · `trash-2` In den Papierkorb · `reply` Antworten · `pencil` Antwort
  entwerfen · `forward` Weiterleiten · `ban` Als Spam markieren · `file-code` Original anzeigen · `mail-open` Gelesen/Ungelesen · `refresh-cw` Abrufen · `more-horizontal`
  Weitere Aktionen · `external-link` Mail öffnen · `arrow-up` Senden · `square` (gefüllt)
  Anhalten · `sparkle` Assistent · `flag` Priorisieren · `newspaper` Newsletter · `clock`
  Frist · `paperclip` Anhang · `check`, `circle-alert`, `info` in notices · `menu` Menü
  öffnen · `x` Schließen · `arrow-left` Zurück · `plus` Neue Unterhaltung · `search` Suchen
  · `chevron-down` Auswahl · `list-checks` Auswählen · `link` Connector · `key-round` Anmeldung beim Anbieter ·
  `sun-moon` Darstellung; `check` also marks the selected row in a menu.
- **Brand mark:** the name „liam-e“ reads „e-mail“ backwards, and the mark is its „-e“: one
  stroke whose hyphen runs into the crossbar of a lowercase e. `BrandMark` (class `le-mark`)
  draws it in `on-accent` on the `accent` circle beside the name in the sidebar and the drawer.
  App icon and favicon (`apps/web/public/`) carry the same glyph on a petrol tile; the README
  header and social preview images are in `docs/assets/`. `sparkle` is no brand mark: it marks
  only the assistant (`le-ai-mark`).
- **Components** (classes `le-*`, description and preview in the design system): BrandMark, Button,
  IconButton, Chip (with tag, badge, dot), Tooltip, Notice, AppShell, NavItem, Menu, QuickAction,
  Composer, ChatMessage, MailReference, ConfirmCard, MailRow, Avatar, TextField, AccountCard,
  Drawer and NavButton (navigation item opening a drawer, „Unterhaltungen“ on tablet). In the app
  they live under `apps/web/src/ds/` with the same names. Screens under `src/routes/` and app-wide
  error and notice pages (`src/root-screens/de.tsx`: new version, error, page not found) do not
  build their own controls from raw `<button>` or `<input>` and do not use Tailwind sizes;
  `routes/no-raw-controls.test.ts` checks this.
- **Composed** from these, in their view's folder: ChatHeader and ModelMenu (`chat/`, §9.6),
  InsightBox („Kurz gefasst“, §9.4) and ActionToast (notice with „Rückgängig“, §9.2) in `mail/`,
  the „Konto entfernen“ dialog (`accounts/RemoveDialog.tsx`, class `le-dialog`, with `scrim`,
  focus stays in the dialog), the „Original der Mail“ dialog (`mail/OriginalDialog.tsx`, wider
  with `le-dialog--wide`, header with „Schließen“, content scrolls independently), and ThemeMenu
  (`shell/`, §3). Calendars connect through `CalendarSetup` (`calendar/FeedSetup.tsx`): „Kalender
  abonnieren“ opens `FeedSetup`, „Google Kalender verbinden“ opens `GoogleSetup`. Both dialogs use
  `le-dialog` and focus management. Google shows static placeholders while loading and real
  checkboxes from `TextField`; existing connections are checked, disabled and explained as „Schon
  verbunden“. New selections are never preselected. Only the selected count of new calendars counts
  for „N Kalender verbinden“. Long calendar names may wrap.
- **Variants:**
  - Button `primary`, `secondary`, `ghost`, `danger`; small with `le-btn--sm` (upper mail bar
    on desktop, §7.4).
  - IconButton `tone="danger"` (trash), `size="lg"` (44 px, bars on `mobile`); `shortcut` appends
    the keyboard shortcut in parentheses to the tooltip.
  - Chip pressed (`aria-pressed`, active filter) and `quiet` (without border on `surface-soft`,
    for selection chips such as account and model).
  - Notice `info`, `success`, `danger`, optionally with an action as a link.
  - Tag `default`, `warm` (deadline, with `clock`) and `on-surface` (account in lists, with account dot).
  - Avatar `md` and `lg` (account card).
  - Badge shows „999+“ from 1,000.
  - Tooltip in five positions: `top` (default), `top-end`, `bottom`, `bottom-end` and `right`
    (tablet toolbar). Position is a preference (§5, floating elements).
  - Menu: entries with icon, `tone: 'danger'`, `disabled` with `hint` (remains focusable and says
    why the entry is unavailable) and `checked` (selection, role `menuitemradio`, `check` on the
    selected row; focus starts there). Arrow keys move focus, Escape closes. `placement` specifies
    side and edge: `down-start` (default), `down-end`, `up-start` or `up-end`; this too is a
    preference (§5, floating elements). Focus moves inside once the menu is placed; Tab closes it
    and continues at the trigger, preserving the page focus order.
- **States without content are mandatory:** every list and view has empty, loading (placeholder
  rows, no spinner over the entire surface) and error with cause and next step. Placeholders are
  static bars on `surface-soft` in `radius-sm`, without shimmer, carrying `aria-hidden`; a status
  alongside them serves screen readers („Mails werden geladen …“). Inbox shows six rows with circle
  and bars, accounts show three cards, the open mail a header with circle and two rows and four bars below.

## 7. Responsive and mobile

Phone and tablet are not scaled-down desktop views; every new view plans for them from the start.
Quoted German labels in this section are UI text.

### 7.1 Breakpoints

| Name | Range | Target |
|---|---|---|
| `mobile` | below 640 px | phone portrait |
| `tablet` | 640–1023 px | tablet, small window, phone landscape |
| `desktop` | from 1024 px | desktop |
| `wide` | from 1440 px | wide monitor, limited content width |

- `wide` has no separate rule: only the main surface grows, content retains its maximum width.
  Chat and composer have `chat-width` (780 px), mail text 720 px, both centered; accounts are
  at most 1180 px wide and left-aligned; the mail list remains `list-width`.
- This table is the source of the boundaries. In code they appear in three places that must
  agree: `shell/useBreakpoint.tsx` (640, 1024 and 1440 px), media queries in `ds.css` and
  `ds-app.css` (`max-width: 639px` and `1023px`, `min-width: 640px`) and Tailwind names `tablet`,
  `desktop` and `wide` in `index.css` (40, 64 and 90 rem, unused so far). `styles/tokens.test.ts` checks this.

### 7.2 Touch

- With `pointer: coarse`, all touch targets are at least **44 × 44 px**, without overlapping.
  This applies generally to Button, IconButton, send, Chip, navigation item, conversation and
  menu entry components (`ds.css`); `ds-app.css` adds only targets that are not components,
  such as links, selection-field labels and the composer textarea.
- **Nothing hover-only.** A mail row action bar appears on desktop hover; on touch a visible
  „Weitere Aktionen“ button opens the same actions.
- The focus ring remains with touch too (keyboard on tablet).

### 7.3 Shell by breakpoint

- `desktop`: sidebar (`sidebar-width`) and main surface float side by side.
- `tablet`: sidebar as a narrow toolbar with tooltips to its right, as wide as a navigation item
  with icon and `space-3` per side. It shows the mark without name, chat, inbox, accounts and
  „Unterhaltungen“, with „Darstellung“ below; the status line with sync state is omitted.
  „Unterhaltungen“ opens a drawer over the content. The unread mail count sits as a badge on the
  inbox's upper-right corner (from 1,000 „999+“); the accessible name states it
  („Posteingang, 47 ungelesen“).
- `mobile`: the main surface fills the screen without frame, rounding or shadow. Header with menu
  button left, title and „Neue Unterhaltung“ right; in an open mail (`/mail/<id>`) it replaces
  the mail bar with „Zurück“. The menu opens a drawer with navigation and conversations, with
  dimmed background. No bottom navigation bar: the composer belongs at the bottom edge.
- The drawer is 308 px wide, at most `100vw - 48px`, with 12 px padding, 24 px rounding on the
  right and `shadow-overlay`; a close button sits in the brand row.
- The composer stays at the bottom, with spacing `max(12px, env(safe-area-inset-bottom))`.

### 7.4 Inbox by breakpoint

- List layout: on `desktop` and `tablet` a top bar contains „Posteingang“, status, select
  (`list-checks`), search (`search`) and fetch (`refresh-cw`). Below are filter chips: account
  (menu with „Alle Konten“), „Ungelesen“, „Wichtig“ and „Mit Anhang“. On `mobile` the header
  carries the title, and below the filters are „47 ungelesen · Stand 08:57“, a labeled „Auswählen“ button,
  search and fetch.
  Search opens a field „In allen Konten suchen …“. The list groups by day: „Heute“, „Gestern“,
  „Diese Woche“ (from Monday) and „Älter“.
- Selection mode replaces the top bar on `desktop` and `tablet`, the status-and-buttons row on
  `mobile`. It shows „Auswahl beenden“, the count, „Alle auswählen“ and archive, read/unread,
  trash and spam. The filing and provider actions occupy a second row so they remain visible
  within the mail list; they wrap further if needed. On `mobile` and
  with a coarse pointer targets are at least 44 px. Tapping a row selects it; the avatar directly
  selects even outside the mode. With a mouse it shows a checkbox on hover, shift-click selects the
  range. „Alle auswählen“ selects at most 100 loaded mails. Escape ends selection. Swiping and row
  actions are disabled during selection.
- `desktop`: list (`list-width`) and reading pane side by side. All mail actions are in the top
  bar of the reading pane: archive, read/unread, trash and „Weitere Aktionen“, with „Im Chat fragen“
  and „Antwort entwerfen“ as small buttons (`le-btn--sm`) to the right.
- `tablet` and `mobile`: list only; a mail opens as its own view with back. Archive, read/unread,
  trash and „Weitere Aktionen“ are in the top bar; „Im Chat fragen“ and „Antwort entwerfen“ are in
  a bar at the bottom.
- On `mobile` and with `pointer: coarse`, every mail row has a visible „Weitere Aktionen“ button;
  a chat button beside it opens the composer for that mail in one tap. With a mouse the action
  bar appears on hover on `tablet` and `desktop` (§7.2). It sits over the
  end of the text column so the time, urgency dot and attachment icon remain visible and their
  tooltip reachable.
- At all breakpoints a deadline appears as a badge in `warm-soft` below the snippet, on `mobile`
  beside the account tag.
- The opened mail's „Weitere Aktionen“ menu contains „Weiterleiten“, „Newsletter abbestellen“,
  „Als Spam markieren“ and „Original anzeigen“. The first two propose their action in chat (§9.2):
  „Weiterleiten“ asks for a draft and the assistant asks for the recipient there; „Newsletter
  abbestellen“ is disabled with a notice where the mail names no unsubscribe method. „Als Spam
  markieren“ is an individual action (§9.2): the mail immediately disappears from the list and the
  notice offers „Rückgängig“. „Original anzeigen“ opens a dialog with headers and source, read-only
  and in the normal font; it fetches both from the provider only on opening and stores nothing.
  It belongs to its mail: when the mail changes, e.g. through „Zurück“ in the browser, it closes.
  For accounts that do not supply the original (connector), the entry is disabled with a notice.
  On `desktop` the menu is right-aligned to its button (`down-end`), in the split view left-aligned
  (`down-start`); if space is insufficient it shifts onto the screen (§5, floating elements).
- On `mobile` filter chips sit in one independently horizontally scrolling row; the page never
  scrolls horizontally.

### 7.5 Forms and containers

- Fields fill the full width in forms.
- Children in flex and grid containers that grow (text, fields) carry `min-width: 0`.
- Long values (subject, address, account name) end with an ellipsis; full text is in the
  accessible name or tooltip.

### 7.6 Calendar by breakpoint

- Toolbar: „Heute“, previous and next as icon buttons, the period as title, „Neuer Termin“ (only
  with a writable calendar), the view switch „Monat | Woche | Tag | Liste“ as a segmented control
  with `aria-pressed`, and „Abrufen“. The toolbar wraps; the title never runs under the buttons.
  Below it the calendars as toggles with a coloured dot and the name. Colours repeat from the fifth
  calendar, so the name identifies it. Calendar notices collapse into one line „Kalenderstand
  anzeigen“. In „Liste“ the toolbar keeps only „Neuer Termin“ and the view switch: the list has its
  own range with „Heute“, „Von“, „Bis“ and „Abrufen“, and scrolls inside the page.
- `desktop` and `tablet` open in week, `mobile` in day with a seven-day strip on top. Week and day:
  an all-day row, then an hour grid in its own scroll area, scrolled to 07:00 or to the hour before
  now; a now-line in today's column; overlapping appointments share the column width, counting
  the drawn minimum height (20 px, with a coarse pointer 44 px) so short ones never cover the next.
  At narrow touch widths the header and time grid scroll together to keep lanes at least 44 px wide.
  Rows follow the wall clock like the hour labels, also on DST days: a spring-forward day leaves its
  missing hour empty, and the repeated hour of a fall-back day shares one row. The gutter
  label carries a soft hyphen („Ganz-tägig“), because browsers may lack German hyphenation. Month:
  six weeks from Monday, up to three entries per day and „+N weitere“, which opens that day; days
  outside the month are dimmed. With `pointer: coarse` a week grows with its 44 px entries and the
  month scrolls instead of letting targets overlap. On `mobile` the month shows dots and the
  selected day's appointments below.
- An appointment opens its detail: on `desktop` as a panel beside the grid, like the reading pane;
  on `tablet` and `mobile` as its own view with „Zum Kalender“. The detail shows the live state with
  „Bearbeiten“ and „Löschen“, or „Nur lesbar“ with the reason and „In Google Kalender öffnen“.
- Creating and editing use one dialog. While Google's answer is open it says „Wird geprüft …“ and
  offers „Prüfen“; a conflict shows the current values with „Übernehmen und neu bearbeiten“;
  „Erneut versuchen“ appears only after a definite failure. Deleting asks in a dialog and says
  that it cannot be guaranteed to be undone. A change whose result is still open stays as a notice
  with „Prüfen“ after its dialog closes; until then no new appointment, or no further change to
  that appointment, can start.
- Every appointment is a link whose accessible name holds title, day, time and calendar. With
  `pointer: coarse` targets are at least 44 px; nothing is hover-only.

## 8. Accessibility

The quoted key label is UI text.

- Contrast as in §3, in both themes.
- **Keyboard first:** visible focus ring (2 px `focus-ring`, 2 px offset) on `:focus-visible`,
  without exception for fields: the input shows it itself (plus `accent` border), the composer
  as a whole as soon as its text field has focus; there is no separate glow instead of the ring.
  Logical order, Escape closes menus and drawers, focus stays in open drawers and dialogs and
  returns to the trigger afterwards.
- **Inbox keyboard shortcuts**, only on `desktop` and `wide`, not in fields and menus and never
  while a dialog is open (even if focus is not currently inside it): j and k next and previous
  mail, Enter open, e archive, u read/unread, Entf or # trash, x select, Esc end selection,
  v choose a folder, l add a label and ? show the overview. The overview is also available from a
  visible touch-sized button and names the context of every action. The shared binding definition
  supplies handlers, overview, menu hints and tooltips; an already assigned label is not removed
  through the add-label shortcut. Composition and conflicting modifiers also block shortcuts.
- Icon buttons carry `aria-label`. Tooltips are `role="tooltip"` and attached to the element via
  `aria-describedby`.
- Never status by color alone: urgency has a dot **and** text or tooltip, errors an icon and sentence.
- Streamed answers: `aria-live="polite"` on the answer area; only the finished answer is announced,
  not each character.
- `prefers-reduced-motion` as in §5.

## 9. Chat

Quoted German labels and examples in this section are UI text.

### 9.1 Flow of an answer

1. The request appears on the right as a bubble in `accent`.
2. Below, `le-working` with three dots shows what the assistant is doing („Durchsuche 4 Konten …“).
3. Text streams in evenly (buffered, about three characters per 24 ms, never whole paragraphs at
   once), with a pulsing `accent` dot at the end.
4. Mail cards and confirmation cards appear only once their content is complete. A mail card shows
   sender, account and time, followed by subject and one line of preview text in `small` and
   `ink-muted`, ending in an ellipsis.
5. Then up to three follow-up suggestions as chips.

During the answer, send becomes stop. A stopped answer remains and gets the notice „Angehalten –
die Antwort ist unvollständig.“ The list follows only while you are at the bottom, at most 48 px
from the end; if you scroll further up it stays still and a „Zum Ende“ button appears.

### 9.2 Propose and confirm

- The assistant may search, read and summarize. Everything that changes mail or sends something
  (send, reply, forward, unsubscribe, archive, move to trash) it proposes in a **confirmation card**.
  Only a click executes it.
- The card names count and object, gives a one-sentence reason, preselects everything and lets
  individual entries be deselected. Irreversible consequences are a sentence in the card, not only
  in the tooltip.
- After the click a notice replaces the card, with result and „Rückgängig“ where possible. Never
  confirm automatically, never two open cards at once.
- **Calendar card:** „Termin anlegen“, „Termin ändern“ or „Termin löschen“ with the calendar,
  the reason, „Bisher“ and „Vorschlag“ side by side (one column on `mobile`) and the time zone.
  The card states that a concurrent change in Google can be overwritten. There is no „Rückgängig“;
  an open result offers „Prüfen“, and a changed appointment asks for a new proposal.
- **Actions initiated by the user in the inbox** on one mail or multiple selected mails take effect
  immediately and then show a notice for 10 seconds with a shared „Rückgängig“. Assistant proposals
  continue through the confirmation card.

### 9.3 Provenance and content from mail

- Assistant answers carry its mark (`le-ai-mark`) and selected model. What it proposes (drafts,
  selections in cards) is recognizable as a proposal until you confirm it.
- Answers are safe Markdown without raw HTML; links only `http`, `https` and `mailto`, external
  ones in a new tab with `rel="noopener noreferrer"`.
- Mail content is foreign data. It appears as text, never executed HTML, and instructions in it
  do not change the flow from §9.2.

### 9.4 Reading mail

- Every open mail starts with the assistant's **„Kurz gefasst“** box (`surface-soft`, mark
  `le-ai-mark`): at most three points, a deadline as a `warm-soft` badge with `clock`. Below it,
  up to two follow-up suggestions as chips.
- There is no direct „Antworten“. The primary action is „Antwort entwerfen“: the draft is created
  in chat and sent only after confirmation (§9.2). Beside it is „Im Chat fragen“, opening chat with
  this mail as context.
- Mail body appears as text (§9.3), attachments as chips with name and size.
- HTML mail follows the chosen appearance. In dark mode, readers can temporarily show its light
  original; image loading remains a separate per-view choice.

### 9.5 States

- **Empty:** greeting, status and quick actions (home page).
- **Loading:** `le-working` with sentence.
- **Error** with its own cause and next step, e.g. model unreachable („Das Modell antwortet gerade
  nicht. Versuch es gleich noch mal.“), interruption mid-answer („Die Antwort ist abgebrochen.“ with
  button „Noch mal versuchen“), account unreachable (notice names the account; other accounts continue).

### 9.6 Header, composer and mail context

- **Header:** on `desktop` and `tablet` a bar above history shows „Chat“ and, in a conversation,
  „/ Titel“. A long title ends in an ellipsis, the full title is in the tooltip. On the right is
  the model menu immediately beside „Neue Unterhaltung“ (`plus`).
  On `mobile` the chat uses that same model control beside the plus in the global header (§7.3).
  Long model names shorten in the header and wrap inside the menu.
- **Model menu:** a chip with model name and `chevron-down`, tooltip „Modell für diese Unterhaltung
  wählen“. The menu offers available general-purpose mail models and lists each verified version
  once. A previously selected specialist stays identifiable as the current selection. At most one verified
  general-purpose Mistral model is recommended for everyday mail. A checked row identifies the
  current choice. Choices appear as one row per name, with the recommendation first.
  Versions with identical display names also show their identifiers.
  „Preise und Details“ opens a dialog with model descriptions, the version behind each moving
  alias when known, separate input and output rates per million text tokens, dated sources and
  the workspace-rate caveat. An unresolved price reads „Preis unbekannt“. A saved ID missing
  from the catalogue stays visible. When a refresh fails, the menu offers a retry.
  In a conversation the selection is saved and applies
  to its next answers, on the home page to the first request. The chip is disabled while that
  conversation is waiting for an answer or streaming one. Each answer shows the model reported by
  its own turn. Older streams or messages without this information omit the model label.
- **Composer:** the text field grows to 200 px high. Enter sends, Shift+Enter starts a new line.
  Skills and saved personal templates live directly inside the composer rather than as a separate header
  template button: a clearly labeled `+ Favoriten` chip opens device-local favorite skills, and
  typing `/` searches active skills alongside personal templates and their stored revisions in a
  floating menu (`body` portal). Picking an entry attaches it visibly in the composer without sending;
  sending stays explicit. Skills default to a single turn (`Nur für diese Nachricht`) and can
  optionally be kept across clarifying follow-up turns until explicitly ended.
  Settings → Chat controls which mail accounts the assistant can use. Each active account has a
  checkbox. The initial selection is empty and Save applies it across devices. The composer’s
  task chip determines which tasks the assistant may read
  (default „Keine Aufgaben“). Its menu shows single selection with explanatory lines; in a
  conversation with history the other entries say „Braucht eine neue Unterhaltung.“ On desktop the
  bar wraps when needed and send stays at the end. On phones the text field, audio controls and
  send share a compact row; a labeled plus opens the favorite and task controls above it. Audio
  status text may use a separate row. The phone safe area remains clear of controls.
- **Mail context:** „Im Chat fragen“ opens the home page with the mail as context. Above the composer
  its mail card appears with the icon button „Bezug entfernen“ (`x`). The composer starts in the
  referenced account with focus in the question field when the account has chat access. An
  excluded reference shows the reason and can be removed. The reference shows its subject, sender
  and account and opens the full mail on tap. Each mail keeps its own unsent question.
  The calendar control shows the number selected and expands to edit the selection.

### 9.7 Templates and settings

- `/settings/folders` manages global app folders and labels. Explain that a folder hides mail from
  liam-e's inbox, labels do not, and neither changes the provider. Deletion names the consequence
  and needs a confirmation dialog; a destination used by a rule cannot be deleted yet.
  Search and the type filter apply to the complete inventory, with 20 rows per page and previous
  and next controls. Changing a search or filter returns to the first page; deletion cannot leave
  the user on an invalid page. Empty inventories and searches with no matches have distinct text.
- `/settings/rules` manages ordered, versioned rules with account, sender, domain, subject and
  recipient conditions. Preview shows a count; applying to existing mail needs a separate dialog and
  confirms the number queued for background processing. Rule rows summarize their conditions, targets
  and active state. Open run outcomes refresh while visible and identify skipped provider actions
  and folder assignments overridden by a higher-priority rule.
  Filing menus show shared assignments and identify partially assigned labels.
  Show unsupported provider actions as skipped and uncertain provider outcomes as unknown. Explain
  that a sleeping Studio instance delays rule execution until the next fetch. After an IMAP mailbox
  rebuild, the run notice explains that rules were not applied automatically, can be applied manually,
  and unread mail remains eligible for normal summaries. The same notice is shown for an account's
  initial import and its deferred historical messages, with the account identified. Mixed connector
  pages during initial history and its unfinished overlapping scan are treated as ambiguous and may include new messages; the notice
  offers manual rule application for them too.
- The inbox filters global folders and labels across accounts. A selected mail or bulk selection
  can be filed without requiring provider write capabilities. Chat filing and rule cards show the
  target and conditions and require explicit confirmation before local changes.
  Folder and label names carry their localized kind, with folder and tag icons in filing tags and
  choices. Tags wrap within a mail row rather than clipping the names on narrow screens; kind is
  never communicated by colour alone.
  Recent and frequent destinations are compact one-click actions in the hovered, focused or selected
  desktop row, the selection bar and the reader. Mobile rows offer them in the more menu; mobile
  controls retain 44 px touch targets. Already-assigned destinations are excluded. Names truncate
  with their full text available to assistive technology and in a tooltip. Show at most five on
  desktop, three on tablet and two on mobile; actions never trigger row navigation. The reader
  explains an empty history and the ordinary filing menu remains available.
  The opened mail's action menu can create a folder or label and assign that mail in one explicit
  save, or open a new rule with editable account, sender, domain and subject suggestions. The owner
  chooses which suggested conditions to use; the sender suggests a name and the form has one Save
  action. Leaving with edits asks for confirmation. A missing source
  or duplicate name is explained, and unsaved rule edits use the settings leave guard.
  Destination choices and tags use locale-aware name order within each kind, ignoring case and
  accents and comparing digit runs numerically; fixed commands stay first. Rule rows sort by name and display the stored execution
  priority, which is independent of their visual order.
- „Einstellungen“ is accessible in sidebar, toolbar and mobile drawer. `/settings` shows the template
  library, as does `/settings/prompts`. Further completed areas can be added to `settingsSections`;
  there are no empty landing pages. The current area is marked in section navigation like the current
  sidebar entry.
- Direct settings sections (`/settings/chat`, `/settings/calendar`, `/settings/controls`,
  `/settings/preferences` personalization toggles, `/settings/language` including speech speed) do not
  render per-setting save buttons. Instead, unsaved changes are detected across the active settings
  view and surfaced in a global sticky bar („Ungespeicherte Änderungen“ / “Unsaved changes”) with
  „Auswahl speichern“ (“Save choices”) and „Verwerfen“ (“Discard”). Existing endpoint revision and
  version checks remain authoritative; leaving a dirty settings view opens a confirmation dialog
  („Ungespeicherte Änderungen verwerfen?“ / “Discard unsaved changes?”) and sets `beforeunload`.
- The library shows name, short preview, version and, if known, modification date. Create and edit
  open separate views (`/settings/prompts/new`, `/settings/prompts/$promptId`), including on desktop.
  These dedicated create/edit views (and `/settings/preferences/new`,
  `/settings/preferences/$preferenceId`) retain their own form save, pause and delete actions.
- Name and instructions use TextField with counters (80 and 8,000 characters respectively). Saving
  is explicit; errors preserve inputs. A version conflict offers the current version without itself
  overwriting. Deletion requires a dialog with focus management and names consequences including
  stored versions.
- In chat, saved templates (including older revisions) are invoked via `/` in the composer, while
  `/settings/prompts` explains this entry point. When a template or older revision is active, the
  composer shows its tag, duration and a clear remove action (`x`); older pinned versions offer
  „Version übernehmen“. Selection in existing conversations becomes active only after the server
  response and is disabled while an answer is streaming.
- On a rejected account switch, „Neue Unterhaltung“ leads to the home page without prior history.
  Account selection and not-yet-accepted composer text remain.

### 9.8 Preferences

- `/settings/preferences` extends section navigation. Create and edit open separate views at all
  screen sizes; controls, spacing and dialogs follow templates.
- Manual saving confirms only the value. When usage is off, show „Gespeichert. Verwendung ist
  ausgeschaltet.“ with „Verwendung einschalten“. Only its dialog confirms: „Passende Präferenzen
  werden für Antworten an Mistral übermittelt.“
- The explicitly agreed action is called „Präferenz hinzufügen“; this is not an exception for
  connecting accounts. Empty lists do not name percentages. Source of manual values: „Von dir
  eingetragen“. Field and scope are chips with visible labels above. Confirmed chat values carry
  „Von dir im Chat bestätigt“. Under „Vorschläge“ are open remember candidates and enabled
  suggestions from new own messages, not yet active preferences. „Vorschlag prüfen“ opens a card
  only when the conversation slot is free; otherwise a notice explains the open card. An already
  associated card is linked with „Karte öffnen“, never duplicated. „Aus meinen Nachrichten
  Vorschläge machen“ turns automatic suggestions on or off independently of usage. Beside it,
  before enabling, state: selected new own messages are transmitted to Mistral and do not remain
  only in the app database. Earlier messages are not backfilled. State is also shown as text.
  Automatic suggestions carry „Aus deinem Chatbeitrag vorgeschlagen“, never open a card themselves
  and take effect only after confirmation and separately enabling usage.
- Preference cards load the value via their candidate reference. They show field, account and
  „Bisher“/„Vorschlag“, stacked on mobile. „Deine Bitte“ can be expanded. For an existing value,
  „Ersetzen“ and „Bisherige behalten“ decide; „Abbrechen“ discards the candidate without changing
  the preference. Equal values explicitly explain the no-op. Confirmation never changes the usage
  toggle; when usage is off, afterwards show „Gespeichert. Verwendung ist ausgeschaltet.“ Focus
  moves to the result.
- A version conflict shows „Inzwischen gespeichert“ and blocks the decision until „Aktuellen Stand
  prüfen“ loads the new comparison version. There is no automatic confirmation attempt. Afterwards
  focus is on the card heading. Removed accounts, sources or preferences no longer show value copies
  or confirmation actions. Loading, error with retry and an empty suggestion list have separate states.
- Deletion explains that chat messages and answers remain and already transmitted requests cannot
  be recalled. It promises neither deletion at the provider nor from backups and offers a new chat
  without prior context. Focus stays in the dialog and returns on closing.
- „N Präferenzen verwendet“ on an answer opens its stored revisions, not current replacement values.
  Removed revisions show „Präferenz entfernt“. Answers without used preferences show nothing, even
  when usage is off; a notice under every answer would be noise. A load error explicitly offers
  „Ohne Präferenzen fortfahren“.

## 10. Extending and maintaining

- Compose new components from existing ones first; never bypass tokens.
- A change to values or components is propagated together in three places: tokens or `src/ds/`,
  DESIGN.md and the design system in Claude Design.
- New views plan for §7 from the start (touch, drawer, separate view instead of reading pane).
- New text goes through the checklist in §2.7.

---

## Differences from the source

DESIGN.md follows the structure and rules of an older design brief from the owner. Deliberately different:

- **Assistant first person** is allowed (owner example 2026-11-13); the source forbids it.
  Clichés and feelings remain forbidden (§2.2).
- **Soft shape:** pill buttons, radii up to 24 px, colored bubble for own messages and shadows for
  floating surfaces; the source is flatter and separates without shadows.
- **No monospace font:** numbers are tabular rather than in a second family.
- **German and English UI catalogues.** Existing German wording is the reference.

## Decided (2026-11-13, owner)

Quoted German labels in this section are UI text.

1. **New design system**, style direction C „Weich“; no existing design system is adopted.
2. **Home page is chat** with the quick actions „Ungelesenes zusammenfassen“, „Priorisieren“,
   „Newsletter aufräumen“ and „Heute fällig“.
3. **Propose and confirm with one click**, nothing without confirmation (§9.2). Rules that act
   without asking are to be decided separately later.
4. **Design:** tidy, clearly recognizable buttons with tooltips, clean navigation, fluid transitions,
   clean streaming in chat.
5. **Text** sounds as though written in German (§2), benchmark „Hallo, was kann ich für dich tun?“.
6. **Design system „liam-e“** approved, with dark theme following system settings and immediate
   individual actions with „Rückgängig“.
7. **Mobile shell** with header and drawer instead of bottom navigation bar (§7.3).
8. **Font self-hosted** via Fontsource, no transmission to Google (§4).
9. **Text catalogue** `apps/web/src/i18n/de.ts` for all UI text (§2.8).
10. **Screens approved** on the design canvas „liam-e Oberfläche“, page „Screens“ (owner artifact,
    not public): chat start, answer with streaming, confirm proposal, inbox, accounts, plus phone with
    chat, drawer, inbox and mail. Decisions there: „Kurz gefasst“ above every mail, no direct reply
    (§9.4), immediate individual actions with „Rückgängig“ (§9.2), mobile action bars (§7.4).

## Decided (2026-11-14, owner)

Quoted German labels in this section are UI text.

1. **Borderless phone:** below 640 px the main surface fills the screen without frame, rounding or
   shadow. From 640 px it floats as before (§7.3).
2. **Four details adopted from the screens:** urgency dot in `warm` in the mail row with tooltip;
   icons in buttons with text 16 px, in icon buttons 19 px; one line of preview text on the chat
   mail card; 20 px padding in the „Konto verbinden“ panel.
3. **Unread count** on inbox also in the narrow tablet toolbar.
4. **More entries in the open mail's „Weitere Aktionen“ menu:** „Weiterleiten“ (as a proposal via
   chat), „Als Spam markieren“ (individual action with „Rückgängig“, another kind at the action
   endpoint) and „Original anzeigen“ (headers and source, read-only, separate endpoint). Implemented,
   see §7.4.
5. **Outlook:** the connector will be probed again read-only once the app has its `MISTRAL_API_KEY`
   in Studio. An own Microsoft login will be planned only if it continues to fail.
6. **Gmail probe** (replies through the connector, To and Cc) is done by the owner personally.
7. **CI:** a GitHub workflow runs the previously local-only checks.
8. **Design system „liam-e“** in Claude Design is kept aligned with DESIGN.md and code (§10).
9. **Theme switch** „Hell“, „Dunkel“, „System“, stored in browser, at the bottom of the sidebar
   beside the status line (§3).
10. **Focus:** input and composer show the focus ring from §8 like all other controls.
11. **Arbitrary values cleaned up:** font sizes only via tokens, spacing on the 4-px grid (§4, §5).

## Decided (2026-11-18, owner)

1. **Select multiple inbox mails and process immediately:** one shared undo without a confirmation
   dialog; assistant proposals still require confirmation (§7.4, §9.2).

## Decided (2026-11-22, owner)

1. **Brand mark „-e“** (direction B „Bindestrich-e“ on the design canvas „liam-e Branding“, owner
   artifact, not public) for app icon, favicon, README header, social preview and the brand row
   in the app; `sparkle` remains the assistant mark (§6).
