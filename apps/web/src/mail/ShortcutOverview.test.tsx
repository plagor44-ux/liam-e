import { afterEach, expect, test } from 'bun:test'
import { cleanup, render, screen, within } from '@testing-library/react'
import { inboxShortcuts, shortcutForKey, shortcutHint, type InboxShortcut } from './inboxShortcuts'
import { ShortcutOverview } from './ShortcutOverview'

afterEach(cleanup)

test('overview lists every binding with its defined hint and an inbox context', () => {
  render(<ShortcutOverview onClose={() => {}} />)
  const dialog = screen.getByRole('dialog', { name: 'Tastenkürzel' })
  for (const id of Object.keys(inboxShortcuts) as InboxShortcut[]) {
    const entry = within(dialog).getByTestId(`shortcut-${id}`)
    expect(entry.textContent).toContain(shortcutHint(id, 'de'))
    expect(entry.textContent).toContain('Posteingang')
    for (const key of inboxShortcuts[id].keys) expect(shortcutForKey(key)).toBe(id)
  }
  const keys = Object.values(inboxShortcuts).flatMap(binding => [...binding.keys])
  expect(new Set(keys).size).toBe(keys.length)
})
