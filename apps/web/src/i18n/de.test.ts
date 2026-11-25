import { expect, test } from 'bun:test'
import { de } from './de'

// Match complete words so that ordinary German words are not rejected as substrings.
const forbidden = /(?:\b(?:OK|Absenden|Account|Postfach|Inbox|Thread|Konversation|Session|Draft|Sync|Synchronisieren|Refresh|Ablegen|Verschieben|Abmelden|Unsubscribe|Hinzufügen|Einloggen|Login|Undo|Zurücknehmen|KI|LLM|Geheimnis|Token|Bot|Agent|Nachricht)\b|Chat-Verlauf|E-Mail\(s\)|Mail\(s\)|\.\.\.|!)/i // i18n-exempt

// „Nachricht“ names the owner's own chat message (a turn), never a mail (DESIGN §2.4, owner decision D9 in #83).
const turnCopy = new Set<unknown>([de.prompts.next, de.prompts.durationNext, de.skills.activeDuration, de.skills.removeSelectionTip, de.errors.chat_invalid])

test('catalogue text follows the language guide', () => {
  function visit(value: unknown): void {
    // Issue #38 explicitly requires this action label; it does not refer to connecting accounts.
    if (value === de.preferences.add) return
    // Turn copy may say „Nachricht“ but follows every other rule.
    if (typeof value === 'string' && turnCopy.has(value)) expect(value.replace(/\bNachricht\b/g, '')).not.toMatch(forbidden)
    else if (typeof value === 'string') expect(value).not.toMatch(forbidden)
    else if (typeof value === 'function') visit(value(4))
    else if (value && typeof value === 'object') Object.values(value).forEach(visit)
  }
  visit(de)
})

test('only chat-turn copy may say „Nachricht“', () => {
  for (const value of turnCopy) expect(String(value)).toMatch(/\bNachricht\b/)
  expect('Neue Nachricht von Anna').toMatch(forbidden)
})
