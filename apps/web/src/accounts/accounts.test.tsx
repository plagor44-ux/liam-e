import { mailCapabilities } from '../test/mailCapabilities'
import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from '@tanstack/react-router'
import { act, render, screen, cleanup, fireEvent, waitFor, within } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import type { AccountOptionsOut, AccountOut, SourceOut } from '../api/generated/types.gen'
import { AccountCard } from '../ds/AccountCard'
import { AccountsPage } from './AccountsPage'
import { redirect } from './redirect'

const account: AccountOut = { action_capabilities: mailCapabilities(), id: 'a1', email_address: 'x@yahoo.com', display_name: 'Privat', provider: 'imap', auth_type: 'imap_password', connection: 'imap', state: 'never', error_code: '', last_sync_at: null, message_count: 0, unread: 0, color_index: 0, is_active: true, created_at: '', updated_at: '' }
const originalMatchMedia = window.matchMedia
afterEach(() => { cleanup(); mock.restore(); window.matchMedia = originalMatchMedia })
// Lets queries the page starts on mount settle inside act() before the test ends.
const settle = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
const props = { onSync: () => {}, onReconnect: () => {}, onRename: () => {}, onRemove: () => {} }
const options: AccountOptionsOut = { gmail_connector: false, outlook_connector: false, google_oauth: true, microsoft_oauth: false, imap_presets: [{ domain: 'yahoo.com', imap_host: 'imap.mail.yahoo.com', smtp_host: 'smtp.mail.yahoo.com' }] }
function setup(mode: 'row' | 'empty' | 'error' | 'loading' = 'row', path = '/accounts', item = account, extra: AccountOut[] = [], available = options, connectorNames: string[] = [], optionsMode: 'ready' | 'error' | 'loading' = 'ready') {
  window.matchMedia = (() => ({ matches: true, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  spyOn(sdk, 'calendarSourcesList').mockResolvedValue({ data: [] } as never)
  const list = spyOn(sdk, 'mailAccountsList').mockImplementation((async () => { if (mode === 'loading') return await new Promise(() => {}); if (mode === 'error') throw new Error('offline'); return { data: mode === 'empty' ? [] : [item, ...extra] } }) as never)
  const optionList = spyOn(sdk, 'mailAccountOptions').mockImplementation((async () => { if (optionsMode === 'loading') return await new Promise(() => {}); if (optionsMode === 'error') throw new Error('offline'); return { data: available } }) as never)
  spyOn(sdk, 'mailConnectorsList').mockImplementation((async () => ({ data: connectorNames.map(name => ({ id: name, name })) })) as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => <QueryClientProvider client={client}><Outlet /></QueryClientProvider> })
  const page = createRoute({ getParentRoute: () => root, path: '/accounts', validateSearch: (search: Record<string, unknown>) => search, component: AccountsPage })
  const router = createRouter({ routeTree: root.addChildren([page]), history: createMemoryHistory({ initialEntries: [path] }) })
  const view = render(<RouterProvider router={router} />)
  return { router, list, view, client, optionList }
}

const calendarSource: SourceOut = { id: 'test-only', provider: 'ics', name: 'Verein', timezone: 'UTC', is_active: false, capabilities: ['read'], last_success_at: null, coverage_start: null, coverage_end: null, stale: true, incomplete: true }

test('calendar management keeps setup unavailable and activates a paused source explicitly', async () => {
  const { client } = setup('empty')
  await waitFor(() => expect(client.getQueryData(['calendar', 'sources'])).toEqual([]))
  act(() => client.setQueryData(['calendar', 'sources'], [calendarSource]))
  await screen.findByText('Verein')
  const update = spyOn(sdk, 'calendarSourceUpdate').mockResolvedValue({ data: { ...calendarSource, is_active: true }, response: new Response() } as never)
  expect(screen.getByText('Abonnierter Kalender · Nur lesen')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Kalender verbinden' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Aktivieren' }))
  await settle()
  expect(update.mock.calls[0]?.[0]).toEqual({ path: { source_id: 'test-only' }, body: { is_active: true } })
})

test('calendar removal names historical consequences and Escape cancels without a request', async () => {
  const { client } = setup('empty')
  await waitFor(() => expect(client.getQueryData(['calendar', 'sources'])).toEqual([]))
  act(() => client.setQueryData(['calendar', 'sources'], [calendarSource]))
  await screen.findByText('Verein')
  const remove = spyOn(sdk, 'calendarSourceDelete')
  fireEvent.click(screen.getByRole('button', { name: 'Entfernen' }))
  const dialog = screen.getByRole('dialog')
  expect(dialog.textContent).toContain('Frühere Chatnachrichten und der Kalender beim Anbieter bleiben erhalten.')
  fireEvent.keyDown(dialog, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(remove).not.toHaveBeenCalled()
})

test('coarse pointer account actions and notice links use large touch targets', async () => {
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  const rule = css.match(/@media \(pointer: coarse\) \{ (\.le-accounts-refresh[^{}]+)\{ ([^{}]+) \} \.le-notice \.le-link \{ ([^{}]+) \} \}/u)
  expect(rule).toBeTruthy()
  expect(rule?.[1]).toContain('.le-account-actions .le-btn')
  expect(rule?.[2]).toContain('min-height: var(--control-height-lg)')
  for (const property of ['display: inline-flex', 'align-items: center', 'min-width: var(--control-height-lg)', 'min-height: var(--control-height-lg)']) expect(rule?.[3]).toContain(property)
})

test('the provider subtitles may wrap inside their option, although buttons do not wrap', async () => {
  // At 13 px "iCloud, Yahoo und andere über IMAP mit App-Passwort" needs 343 px; beside an account list the option is 316 px wide.
  const [ds, app] = await Promise.all(['ds.css', 'ds-app.css'].map(name => Bun.file(new URL(`../styles/${name}`, import.meta.url)).text()))
  expect(ds).toMatch(/^\.le-btn \{[^}]*white-space: nowrap;/mu)
  expect(app).toMatch(/^\.le-connect-option-text \{[^}]*white-space: normal;/mu)
})

test('accounts scroll area is bounded by the viewport', async () => {
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  const app = css.match(/\.le-app:has\(\.le-accounts-scroll\)[^{]*\{([^}]*)\}/u)?.[1]
  const sheet = css.match(/\.le-sheet:has\(\.le-accounts-scroll\) \{([^}]*)\}/u)?.[1]
  const scroll = css.match(/\.le-accounts-scroll \{([^}]*)\}/u)?.[1]
  for (const property of ['height: 100dvh', 'overflow: hidden']) expect(app).toContain(property)
  expect(sheet).toContain('min-height: 0')
  for (const property of ['flex: 1', 'min-height: 0', 'overflow-y: auto']) expect(scroll).toContain(property)
})

test('the footnote is a small block paragraph below the list and the connect panel is as tall as its content', async () => {
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  expect(css).toMatch(/\.le-accounts-layout \{[^}]*display: flex;[^}]*align-items: flex-start;/u)
  expect(css).toMatch(/\.le-accounts-note \{[^}]*margin: var\(--space-4\) var\(--space-1\) 0;/u)
  setup('row')
  const note = await screen.findByText('Entfernst du ein Konto, löscht liam-e nur seine Kopie der Mails. Beim Anbieter bleibt alles, wie es ist.')
  expect(note.tagName).toBe('P')
  expect(note.classList.contains('small')).toBe(true)
  expect(note.classList.contains('le-muted')).toBe(true)
  expect(note.previousElementSibling?.tagName).toBe('UL')
  await settle()
})

test('the connect panel has 20 px inner padding on the 4 px grid', async () => {
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  expect(css).toMatch(/\.le-accounts-connect \{[^}]*padding: calc\(var\(--space-4\) \+ var\(--space-1\)\);/u)
})

test('account cards have spaced lists and mobile actions below the content', async () => {
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  expect(css).toMatch(/\.le-accounts-list > h2 \{[^}]*margin: 0 0 var\(--space-5\);/u)
  expect(css).toMatch(/\.le-accounts-list ul \{[^}]*display: flex;[^}]*flex-direction: column;[^}]*gap: var\(--space-3\);/u)
  expect(css).toMatch(/@media \(max-width: 639px\) \{ \.le-account \{ grid-template-columns: auto minmax\(0, 1fr\); \} \.le-account-actions \{ grid-column: 2; flex-direction: row; \} \}/u)
  const { container } = render(<AccountCard account={{ ...account, unread: 7, message_count: 23 }} {...props} />)
  expect(container.querySelector('.le-account-meta .le-badge')).toBeNull()
  expect(screen.getByText('23 Mails')).toBeTruthy()
  expect(screen.getByText('IMAP')).toBeTruthy()
})

test('card shows each state and connection', () => {
  const { rerender } = render(<AccountCard account={account} {...props} />)
  expect(screen.getByText('Yahoo')).toBeTruthy(); expect(screen.getByText('IMAP')).toBeTruthy(); expect(screen.getByText('Noch nicht abgerufen')).toBeTruthy()
  rerender(<AccountCard account={{ ...account, state: 'syncing', connection: 'connector' }} {...props} />)
  expect(screen.getByText('Wird abgerufen …')).toBeTruthy(); expect(screen.getByText('über Connector')).toBeTruthy(); expect((screen.getByRole('button', { name: 'Wird gerade abgerufen' }) as HTMLButtonElement).disabled).toBe(true)
  rerender(<AccountCard account={{ ...account, state: 'auth_expired', connection: 'oauth', provider: 'microsoft' }} {...props} />)
  expect(screen.getByText('Anmeldung abgelaufen')).toBeTruthy(); expect(screen.getByText('Outlook')).toBeTruthy(); expect(screen.getByText('mit Anmeldung')).toBeTruthy()
  rerender(<AccountCard account={{ ...account, state: 'error', error_code: 'login_failed', provider: 'gmail' }} {...props} />)
  expect(screen.getByText('Gmail')).toBeTruthy(); expect(screen.getByText('Die Anmeldung beim Mailserver hat nicht geklappt. Prüfe die Serveradresse und das App-Passwort.')).toBeTruthy()
  rerender(<AccountCard account={{ ...account, state: 'error', error_code: 'unknown', email_address: 'a@icloud.com' }} {...props} />)
  expect(screen.getByText('iCloud')).toBeTruthy(); expect(screen.getByText('Beim letzten Abruf ist etwas schiefgegangen. Versuch es gleich noch mal.')).toBeTruthy()
  rerender(<AccountCard account={{ ...account, state: 'ok', last_sync_at: '2026-01-01T12:00:00Z', email_address: 'a@example.com' }} {...props} />)
  expect(screen.getByText('example.com')).toBeTruthy(); expect(screen.getByText(/^Stand /)).toBeTruthy()
})
test('return error is shown and removed from URL', async () => {
  const { router } = setup('empty', '/accounts?error=oauth_denied')
  expect(await screen.findByText('Die Anmeldung wurde abgebrochen, es ist kein Konto verbunden. Du kannst sie jederzeit neu starten.')).toBeTruthy()
  await waitFor(() => expect(router.state.location.searchStr).toBe(''))
})
test('connected return syncs once and clears URL', async () => {
  const sync = spyOn(sdk, 'mailAccountSync').mockImplementation((async () => ({ response: { ok: true } })) as never)
  const { router } = setup('empty', '/accounts?connected=a1')
  expect(await screen.findByText('Konto verbunden.')).toBeTruthy()
  await waitFor(() => expect(router.state.location.searchStr).toBe(''))
  expect(sync).toHaveBeenCalledTimes(1); expect(sync.mock.calls[0]?.[0]?.path).toEqual({ account_id: 'a1' })
})
test('sync one and all', async () => {
  const sync = spyOn(sdk, 'mailAccountSync').mockImplementation((async () => ({ response: { ok: true } })) as never)
  const all = spyOn(sdk, 'mailSyncAll').mockImplementation((async () => ({ response: { ok: true } })) as never)
  setup(); fireEvent.click(await screen.findByRole('button', { name: 'x@yahoo.com jetzt abrufen' })); await waitFor(() => expect(sync).toHaveBeenCalledTimes(1))
  expect(sync.mock.calls[0]?.[0]?.path).toEqual({ account_id: 'a1' }); fireEvent.click(screen.getByRole('button', { name: 'Alle abrufen' })); await waitFor(() => expect(all).toHaveBeenCalledTimes(1))
})
test('oauth reconnect redirects and connector reconnect shows Studio notice', async () => {
  const authorize = spyOn(sdk, 'mailAccountOauthAuthorize').mockImplementation((async () => ({ data: { authorization_url: 'https://example.com' } })) as never)
  const go = spyOn(redirect, 'to').mockImplementation(() => {})
  setup('row', '/accounts', { ...account, provider: 'google', connection: 'oauth', state: 'auth_expired' })
  fireEvent.click(await screen.findByRole('button', { name: 'Erneut anmelden' })); await waitFor(() => expect(go).toHaveBeenCalledWith('https://example.com'))
  expect(authorize.mock.calls[0]?.[0]?.query).toEqual({ account_id: 'a1' }); cleanup(); mock.restore()
  const noAuthorize = spyOn(sdk, 'mailAccountOauthAuthorize')
  setup('row', '/accounts', { ...account, provider: 'gmail', connection: 'connector', state: 'auth_expired' })
  fireEvent.click(await screen.findByRole('button', { name: 'Erneut anmelden' })); expect(screen.getByText(/Die Anmeldung für diesen Connector liegt/)).toBeTruthy(); expect(noAuthorize).not.toHaveBeenCalled()
})
async function openAction(action: 'Umbenennen' | 'Entfernen') {
  const trigger = await screen.findByRole('button', { name: 'Weitere Aktionen zu Privat' })
  act(() => { trigger.focus() }); fireEvent.click(trigger); fireEvent.click(screen.getByRole('menuitem', { name: action }))
  return trigger
}

test('rename selects the current name and saves trimmed input on Enter', async () => {
  const update = spyOn(sdk, 'mailAccountUpdate').mockImplementation((async () => ({ data: { ...account, display_name: 'Arbeit' }, response: { ok: true } })) as never)
  const { client } = setup(); const invalidate = spyOn(client, 'invalidateQueries'); await openAction('Umbenennen')
  const field = screen.getByRole('textbox', { name: 'Name des Kontos' }) as HTMLInputElement
  expect(document.activeElement).toBe(field); expect(field.selectionStart).toBe(0); expect(field.selectionEnd).toBe(6)
  fireEvent.change(field, { target: { value: '  Arbeit  ' } }); fireEvent.submit(field.closest('form')!)
  await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
  expect(update.mock.calls[0]?.[0]).toMatchObject({ path: { account_id: 'a1' }, body: { display_name: 'Arbeit' } })
  await waitFor(() => expect(screen.getByText('Arbeit')).toBeTruthy())
  expect(client.getQueryData<AccountOut[]>(['accounts'])?.[0]?.display_name).toBe('Arbeit'); expect(invalidate.mock.calls.map(call => call[0]?.queryKey)).toContainEqual(['summary'])
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Weitere Aktionen zu Arbeit' }))
})

test('rename Escape cancels and returns focus without a request', async () => {
  const update = spyOn(sdk, 'mailAccountUpdate')
  setup(); const trigger = await openAction('Umbenennen')
  fireEvent.keyDown(screen.getByRole('textbox', { name: 'Name des Kontos' }), { key: 'Escape' })
  expect(screen.queryByRole('textbox', { name: 'Name des Kontos' })).toBeNull(); expect(update).not.toHaveBeenCalled(); expect(document.activeElement).toBe(trigger)
})

test('rename rejects empty and overly long names', async () => {
  const update = spyOn(sdk, 'mailAccountUpdate')
  setup(); await openAction('Umbenennen')
  const field = screen.getByRole('textbox', { name: 'Name des Kontos' }) as HTMLInputElement
  for (const value of ['  ', 'x'.repeat(81)]) {
    fireEvent.change(field, { target: { value } }); fireEvent.submit(field.closest('form')!)
    expect(screen.getByText('Gib einen Namen mit bis zu 80 Zeichen ein.')).toBeTruthy()
  }
  expect(update).not.toHaveBeenCalled()
})

test('rename keeps the form and shows an API refusal', async () => {
  spyOn(sdk, 'mailAccountUpdate').mockImplementation((async () => ({ error: { detail: { code: 'account_not_found' } } })) as never)
  setup(); await openAction('Umbenennen'); fireEvent.submit(screen.getByRole('textbox', { name: 'Name des Kontos' }).closest('form')!)
  expect(await screen.findByText('Das Konto gibt es nicht mehr.')).toBeTruthy(); expect(screen.getByRole('textbox', { name: 'Name des Kontos' })).toBeTruthy()
})

test('remove traps focus and Escape returns it without deleting', async () => {
  const remove = spyOn(sdk, 'mailAccountDelete')
  setup(); const trigger = await openAction('Entfernen')
  const dialog = screen.getByRole('dialog', { name: 'Privat entfernen?' })
  // The scrim sits in the body: inside the page it would stack below the sticky label of the sidebar.
  expect(dialog.parentElement?.className).toBe('le-scrim'); expect(dialog.parentElement?.parentElement).toBe(document.body)
  expect(screen.getAllByText('liam-e löscht nur die eigene Kopie der Mails. Beim Anbieter bleibt alles, wie es ist.')).toHaveLength(1)
  expect(screen.getByText('Entfernst du ein Konto, löscht liam-e nur seine Kopie der Mails. Beim Anbieter bleibt alles, wie es ist.')).toBeTruthy()
  expect(within(dialog).getByText(/liam-e löscht nur die eigene Kopie der Mails/)).toBeTruthy()
  const cancel = within(dialog).getByRole('button', { name: 'Abbrechen' }), confirm = within(dialog).getByRole('button', { name: 'Konto entfernen' })
  expect(document.activeElement).toBe(cancel)
  fireEvent.keyDown(document, { key: 'Tab', shiftKey: true }); expect(document.activeElement).toBe(confirm)
  fireEvent.keyDown(document, { key: 'Tab' }); expect(document.activeElement).toBe(cancel)
  fireEvent.keyDown(document, { key: 'Escape' }); expect(screen.queryByRole('dialog')).toBeNull(); expect(document.activeElement).toBe(trigger); expect(remove).not.toHaveBeenCalled()
})

test('remove confirms once, removes the card and focuses the connect heading', async () => {
  const remove = spyOn(sdk, 'mailAccountDelete').mockImplementation((async () => ({ response: { ok: true, status: 204 } })) as never)
  setup(); await openAction('Entfernen'); fireEvent.click(screen.getByRole('button', { name: 'Konto entfernen' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull()); expect(remove).toHaveBeenCalledTimes(1)
  expect(remove.mock.calls[0]?.[0]?.path).toEqual({ account_id: 'a1' }); expect(screen.queryByText('Privat')).toBeNull(); expect(screen.getByText('Privat entfernt.')).toBeTruthy()
  expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Konto verbinden' }))
})

test('remove waits for confirmation and prevents duplicate requests while pending', async () => {
  let resolve!: (result: unknown) => void
  const remove = spyOn(sdk, 'mailAccountDelete').mockImplementation((() => new Promise(done => { resolve = done })) as never)
  setup(); await openAction('Entfernen'); expect(remove).not.toHaveBeenCalled()
  const button = screen.getByRole('button', { name: 'Konto entfernen' }) as HTMLButtonElement
  fireEvent.click(button); expect(button.disabled).toBe(true); fireEvent.click(button); expect(remove).toHaveBeenCalledTimes(1)
  resolve({ response: { ok: true, status: 204 } }); await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
})

test('remove focuses the remaining accounts heading and invalidates related queries', async () => {
  spyOn(sdk, 'mailAccountDelete').mockImplementation((async () => ({ response: { ok: true, status: 204 } })) as never)
  const { client } = setup('row', '/accounts', account, [{ ...account, id: 'a2', display_name: 'Arbeit' }])
  const invalidate = spyOn(client, 'invalidateQueries')
  await openAction('Entfernen'); fireEvent.click(screen.getByRole('button', { name: 'Konto entfernen' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Verbundene Konten' }))
  expect(screen.getByText('Arbeit')).toBeTruthy(); expect(invalidate.mock.calls.map(call => call[0]?.queryKey)).toContainEqual(['summary']); expect(invalidate.mock.calls.map(call => call[0]?.queryKey)).toContainEqual(['messages'])
})

test('remove failure keeps the dialog open with a notice', async () => {
  spyOn(sdk, 'mailAccountDelete').mockImplementation((async () => ({ error: { detail: { code: 'account_not_found' } } })) as never)
  setup(); await openAction('Entfernen'); fireEvent.click(screen.getByRole('button', { name: 'Konto entfernen' }))
  const dialog = screen.getByRole('dialog'); expect(await within(dialog).findByText('Das Konto gibt es nicht mehr.')).toBeTruthy()
})

async function openImap() {
  fireEvent.click(await screen.findByRole('button', { name: /Anderer Anbieter/ }))
  await screen.findByLabelText('E-Mail-Adresse')
}
function fillImap() {
  fireEvent.change(screen.getByLabelText('E-Mail-Adresse'), { target: { value: 'x@yahoo.com' } })
  fireEvent.change(screen.getByLabelText('Secret mit dem App-Passwort'), { target: { value: 'yahoo-app-password' } })
}
test('gmail connector suppresses own Google login', async () => {
  setup('empty', '/accounts', account, [], { ...options, gmail_connector: true, google_oauth: true })
  fireEvent.click(await screen.findByRole('button', { name: /Gmail/ }))
  expect(screen.getByText('Melde Gmail in Mistral Studio an. Hier musst du keine Zugangsdaten eingeben.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Bei Google anmelden' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Gmail verbinden' })).toBeTruthy()
})
test('Google login authorizes without account id and redirects', async () => {
  const authorize = spyOn(sdk, 'mailAccountOauthAuthorize').mockImplementation((async () => ({ data: { authorization_url: 'https://example.com' } })) as never)
  const go = spyOn(redirect, 'to').mockImplementation(() => {})
  setup('empty'); fireEvent.click(await screen.findByRole('button', { name: /Gmail/ })); fireEvent.click(screen.getByRole('button', { name: 'Bei Google anmelden' }))
  await waitFor(() => expect(go).toHaveBeenCalledWith('https://example.com'))
  expect(authorize.mock.calls[0]?.[0]).toEqual({ path: { provider: 'google' } })
})
test('IMAP presets populate both servers without overwriting typed server fields', async () => {
  setup('empty'); await openImap()
  fireEvent.change(screen.getByLabelText('IMAP-Server'), { target: { value: 'custom.example' } }); fillImap()
  expect((screen.getByLabelText('IMAP-Server') as HTMLInputElement).value).toBe('custom.example')
  expect((screen.getByLabelText('SMTP-Server') as HTMLInputElement).value).toBe('smtp.mail.yahoo.com')
  cleanup(); mock.restore(); setup('empty'); await openImap(); fillImap()
  expect((screen.getByLabelText('IMAP-Server') as HTMLInputElement).value).toBe('imap.mail.yahoo.com')
  expect((screen.getByLabelText('SMTP-Server') as HTMLInputElement).value).toBe('smtp.mail.yahoo.com')
})
test('IMAP secret name field disables autofill', async () => {
  setup('empty'); await openImap()
  expect(screen.getByLabelText('Secret mit dem App-Passwort').getAttribute('autocomplete')).toBe('off')
})
test('IMAP submits secret reference, syncs new account and shows success', async () => {
  const connect = spyOn(sdk, 'mailAccountConnectImap').mockImplementation((async () => ({ data: account })) as never)
  const sync = spyOn(sdk, 'mailAccountSync').mockImplementation((async () => ({ response: { ok: true } })) as never)
  setup('empty'); await openImap(); fillImap(); fireEvent.click(screen.getByRole('button', { name: 'Konto verbinden' }))
  await waitFor(() => expect(sync).toHaveBeenCalledTimes(1))
  expect(connect.mock.calls[0]?.[0]?.body).toEqual({ email_address: 'x@yahoo.com', password_secret: 'yahoo-app-password' })
  expect(sync.mock.calls[0]?.[0]?.path).toEqual({ account_id: 'a1' }); expect(await screen.findByText('Konto verbunden.')).toBeTruthy()
})
test('IMAP submits only a server changed from its preset', async () => {
  const connect = spyOn(sdk, 'mailAccountConnectImap').mockImplementation((async () => ({ data: account })) as never)
  spyOn(sdk, 'mailAccountSync').mockImplementation((async () => ({ response: { ok: true } })) as never)
  setup('empty'); await openImap(); fillImap()
  fireEvent.change(screen.getByLabelText('IMAP-Server'), { target: { value: 'imap.example.net' } })
  fireEvent.change(screen.getByLabelText('SMTP-Server'), { target: { value: ' SMTP.MAIL.YAHOO.COM ' } })
  fireEvent.click(screen.getByRole('button', { name: 'Konto verbinden' }))
  await waitFor(() => expect(connect).toHaveBeenCalledTimes(1))
  expect(connect.mock.calls[0]?.[0]?.body).toEqual({ email_address: 'x@yahoo.com', imap_host: 'imap.example.net', password_secret: 'yahoo-app-password' })
})
test('IMAP submits both servers for a domain without a preset', async () => {
  const connect = spyOn(sdk, 'mailAccountConnectImap').mockImplementation((async () => ({ data: account })) as never)
  spyOn(sdk, 'mailAccountSync').mockImplementation((async () => ({ response: { ok: true } })) as never)
  setup('empty'); await openImap()
  fireEvent.change(screen.getByLabelText('E-Mail-Adresse'), { target: { value: 'x@example.org' } })
  fireEvent.change(screen.getByLabelText('IMAP-Server'), { target: { value: 'imap.example.org' } })
  fireEvent.change(screen.getByLabelText('SMTP-Server'), { target: { value: 'smtp.example.org' } })
  fireEvent.change(screen.getByLabelText('Secret mit dem App-Passwort'), { target: { value: 'yahoo-app-password' } })
  fireEvent.click(screen.getByRole('button', { name: 'Konto verbinden' }))
  await waitFor(() => expect(connect).toHaveBeenCalledTimes(1))
  expect(connect.mock.calls[0]?.[0]?.body).toEqual({ email_address: 'x@example.org', imap_host: 'imap.example.org', smtp_host: 'smtp.example.org', password_secret: 'yahoo-app-password' })
})
for (const [code, text, field] of [
  ['host_not_allowed', 'liam-e darf diesen Server nicht ansprechen. Prüfe die Serveradresse oder trag den Server in Mistral Studio unter MAIL_IMAP_ALLOWED_HOSTS ein.', ''],
  ['secret_not_found', 'Das Secret wurde nicht gefunden oder ist nicht lesbar. Prüfe den Namen und die Freigabe in Mistral Studio.', 'Secret mit dem App-Passwort'],
  ['login_failed', 'Die Anmeldung beim Mailserver hat nicht geklappt. Prüfe die Serveradresse und das App-Passwort.', ''],
  ['mistral_not_configured', 'liam-e hat keinen Zugang zu Mistral Studio. Hinterleg für die App einen MISTRAL_API_KEY aus diesem Workspace, starte sie neu und versuch es dann noch mal.', ''],
  ['secret_access_denied', 'Mistral Studio lässt liam-e die Secrets nicht lesen. Prüfe, ob der MISTRAL_API_KEY der App zu diesem Workspace gehört und noch gültig ist.', ''],
  ['secret_unavailable', 'Mistral Studio antwortet gerade nicht, das Secret ließ sich nicht lesen. Versuch es gleich noch mal.', ''],
  ['encryption_not_configured', 'liam-e kann das Konto nicht verschlüsselt speichern, weil der Schlüssel fehlt. Leg in Mistral Studio das Secret „mail_token_encryption_key“ an, falls es fehlt, starte die App neu und verbinde das Konto dann noch einmal.', ''],
] as const) test(`IMAP displays ${code} refusal`, async () => {
  spyOn(sdk, 'mailAccountConnectImap').mockImplementation((async () => ({ error: { detail: { code } } })) as never)
  setup('empty'); await openImap(); fillImap(); fireEvent.click(screen.getByRole('button', { name: 'Konto verbinden' }))
  expect(await screen.findByText(text)).toBeTruthy()
  if (field) expect(screen.getByLabelText(field).getAttribute('aria-invalid')).toBe('true')
  else expect(screen.getByRole('alert').textContent).toContain(text)
})
test('Gmail connector uses listed google_mail name', async () => {
  const connect = spyOn(sdk, 'mailAccountConnectConnector').mockImplementation((async () => ({ data: account })) as never)
  spyOn(sdk, 'mailAccountSync').mockImplementation((async () => ({ response: { ok: true } })) as never)
  setup('empty', '/accounts', account, [], { ...options, gmail_connector: true }, ['google_mail'])
  fireEvent.click(await screen.findByRole('button', { name: /Gmail/ })); fireEvent.click(screen.getByRole('button', { name: 'Gmail verbinden' }))
  await waitFor(() => expect(connect).toHaveBeenCalledTimes(1)); expect(connect.mock.calls[0]?.[0]?.path).toEqual({ name: 'google_mail' }); expect(await screen.findByText('Konto verbunden.')).toBeTruthy()
})
test('provider loading and failure offer retry and IMAP fallback', async () => {
  const loading = setup('empty', '/accounts', account, [], options, [], 'loading')
  await screen.findByText('Anbieter werden geladen …'); expect(loading.view.container.querySelectorAll('.le-connect-placeholder')).toHaveLength(3)
  cleanup(); mock.restore()
  const failed = setup('empty', '/accounts', account, [], options, [], 'error')
  expect(await screen.findByText('Die Anbieter konnten nicht geladen werden. Versuch es gleich noch mal.')).toBeTruthy()
  expect(screen.getByRole('button', { name: /Anderer Anbieter/ })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Noch mal versuchen' })); await waitFor(() => expect(failed.optionList).toHaveBeenCalledTimes(2))
  await openImap(); expect((screen.getByLabelText('IMAP-Server') as HTMLInputElement).value).toBe('')
})
test('IMAP validates required fields before connecting', async () => {
  const connect = spyOn(sdk, 'mailAccountConnectImap')
  setup('empty'); await openImap(); fireEvent.click(screen.getByRole('button', { name: 'Konto verbinden' }))
  expect(screen.getByText('Gib eine E-Mail-Adresse ein.')).toBeTruthy(); expect(screen.getAllByText('Gib den Server ein.')).toHaveLength(2); expect(screen.getByText('Gib den Namen des Secrets ein.')).toBeTruthy()
  fireEvent.change(screen.getByLabelText('E-Mail-Adresse'), { target: { value: 'x@@y' } }); fireEvent.click(screen.getByRole('button', { name: 'Konto verbinden' }))
  expect(screen.getByText('Gib eine E-Mail-Adresse ein.')).toBeTruthy(); expect(connect).not.toHaveBeenCalled()
})
test('cancel returns focus to provider option', async () => {
  setup('empty'); await openImap()
  const option = screen.getByRole('button', { name: /Anderer Anbieter/ })
  expect(option.getAttribute('aria-pressed')).toBe('true'); fireEvent.click(screen.getByRole('button', { name: 'Abbrechen' }))
  expect(option.getAttribute('aria-pressed')).toBe('false'); expect(document.activeElement).toBe(option); expect(screen.queryByLabelText('E-Mail-Adresse')).toBeNull()
})
test('connector refusal remains in panel and pending requests cannot be repeated', async () => {
  let finish!: (result: unknown) => void
  const connect = spyOn(sdk, 'mailAccountConnectConnector').mockImplementation((() => new Promise(resolve => { finish = resolve })) as never)
  setup('empty', '/accounts', account, [], { ...options, gmail_connector: true })
  fireEvent.click(await screen.findByRole('button', { name: /Gmail/ }))
  const button = screen.getByRole('button', { name: 'Gmail verbinden' }) as HTMLButtonElement
  fireEvent.click(button); expect(button.disabled).toBe(true); expect(button.getAttribute('aria-busy')).toBe('true'); fireEvent.click(button); expect(connect).toHaveBeenCalledTimes(1)
  finish({ error: { detail: { code: 'connector_auth_required' } } }); expect(await screen.findByText('Der Connector ist in Mistral Studio nicht angemeldet. Melde ihn dort unter Connector → Auth an und versuch es dann noch mal.')).toBeTruthy()
})
test('IMAP reconnect selects provider and focuses the secret field', async () => {
  setup('row', '/accounts', { ...account, state: 'auth_expired' })
  fireEvent.click(await screen.findByRole('button', { name: 'Erneut anmelden' }))
  expect((await screen.findByLabelText('E-Mail-Adresse') as HTMLInputElement).value).toBe('x@yahoo.com')
  await waitFor(() => expect((screen.getByLabelText('IMAP-Server') as HTMLInputElement).value).toBe('imap.mail.yahoo.com'))
  expect(document.activeElement).toBe(screen.getByLabelText('Secret mit dem App-Passwort'))
})

test('the calendar link in calendar management uses the touch-sized link style', async () => {
  setup()
  const link = await screen.findByRole('link', { name: 'Zum Kalender' })
  expect(link.classList.contains('le-calendar-back')).toBe(true)
})

test('loading, retry and empty states', async () => {
  const loading = setup('loading'); await screen.findByText('Konten werden geladen …'); expect(loading.view.container.querySelectorAll('.le-account--placeholder')).toHaveLength(3); cleanup(); mock.restore()
  const failed = setup('error'); await screen.findByText('Die Konten konnten nicht geladen werden. Versuch es gleich noch mal.'); fireEvent.click(screen.getByRole('button', { name: 'Noch mal versuchen' })); await waitFor(() => expect(failed.list).toHaveBeenCalledTimes(2)); cleanup(); mock.restore()
  setup('empty'); await screen.findByText('Noch kein Konto verbunden. Wähle einen Anbieter, dann holt liam-e deine Mails.'); expect(screen.queryByRole('list', { name: 'Verbundene Konten' })).toBeNull()
})

test('Gmail connector limitations are visible on the account and before connecting', async () => {
  const gmail = { ...account, provider: 'gmail', connection: 'connector' as const, auth_type: 'mistral_connector', action_capabilities: mailCapabilities(false) }
  setup('row', '/accounts', gmail, [], { ...options, gmail_connector: true }, ['gmail'])
  expect(await screen.findByText(/Lesen, suchen, zusammenfassen und nach Bestätigung senden/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: /Gmail/ }))
  expect(await screen.findAllByText(/Lesen, suchen, zusammenfassen und nach Bestätigung senden/)).toHaveLength(2)
  expect(screen.getAllByText(/Papierkorb, Archivieren, Spam/)).toHaveLength(2)
})
