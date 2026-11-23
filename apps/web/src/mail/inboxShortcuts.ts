import type { Locale } from '../i18n/locales'

// The handler, overview and action hints all use these definitions.
export const inboxShortcuts = {
  next: { keys: ['j'], context: 'inbox' }, previous: { keys: ['k'], context: 'inbox' }, open: { keys: ['Enter'], context: 'current' },
  archive: { keys: ['e'], context: 'selected' }, read: { keys: ['u'], context: 'selected' }, trash: { keys: ['Delete', '#'], context: 'selected' },
  select: { keys: ['x'], context: 'current' }, escape: { keys: ['Escape'], context: 'selection' },
  folder: { keys: ['v'], context: 'selected' }, label: { keys: ['l'], context: 'selected' }, overview: { keys: ['?'], context: 'inbox' },
} as const

export type InboxShortcut = keyof typeof inboxShortcuts
export const shortcutHint = (name: InboxShortcut, locale: Locale) => inboxShortcuts[name].keys.map(key =>
  key === 'Delete' && locale === 'de' ? 'Entf' : key === 'Escape' ? 'Esc' : key.length === 1 ? key.toLocaleUpperCase(locale) : key,
).join(' / ')
export function shortcutForKey(key: string): InboxShortcut | undefined {
  return (Object.keys(inboxShortcuts) as InboxShortcut[]).find(name => (inboxShortcuts[name].keys as readonly string[]).includes(key))
}
