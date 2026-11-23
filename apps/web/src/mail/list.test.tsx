import { mailCapabilities } from '../test/mailCapabilities'
import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from '@tanstack/react-router'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import { Route } from '../routes/_app/mail'
import { inboxSearchSchema } from './filters'

const originalMatchMedia = window.matchMedia
const row = { id: 'm1', account_id: 'a', color_index: 0, sender_name: 'Anna', sender_address: 'anna@example.com', subject: 'Termin', snippet: '', internal_date: null, deadline: null, has_attachments: false, is_flagged: false, is_read: false, needs_reply: false }
function setup(mode: 'empty' | 'error' | 'loading' | 'row' | 'rows' | 'many' = 'empty', path = '/mail', coarse = false, mobile = false, limited = false) {
  spyOn(sdk, 'mailFilingQuickActions').mockResolvedValue({ data: [] } as never)
  spyOn(sdk, 'mailFoldersList').mockImplementation((async () => ({ data: [{ id: 'folder', name: 'Projects', kind: 'folder' }, { id: 'label', name: 'Projects', kind: 'label' }] })) as never)
  window.matchMedia = ((query: string) => ({ matches: (!mobile && query.includes('1024px')) || (coarse && query.includes('pointer: coarse')), addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  spyOn(sdk, 'mailSummary').mockImplementation((async () => ({ data: { accounts: [{ id: 'a', display_name: '', email_address: 'ich@example.com', provider: 'gmail', color_index: 0, state: 'ok' }, ...(limited ? [{ id: 'limited', display_name: 'Studio', email_address: 'studio@example.test', provider: 'gmail', connection: 'connector', color_index: 1, state: 'ok', action_capabilities: mailCapabilities(false) }] : [])], unread_total: 1, last_sync_at: null } })) as never)
  const list = spyOn(sdk, 'mailMessagesList').mockImplementation((async () => {
    if (mode === 'loading') return await new Promise(() => {})
    if (mode === 'error') throw new Error('unavailable')
    return { data: { items: mode === 'row' ? [row] : mode === 'rows' ? [row, { ...row, id: 'm2', account_id: limited ? 'limited' : row.account_id, subject: 'Zweite' }, { ...row, id: 'm3', subject: 'Dritte' }] : mode === 'many' ? Array.from({ length: 101 }, (_, i) => ({ ...row, id: `m${i}`, subject: `Mail ${i}` })) : [], next_cursor: null } }
  }) as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({ component: () => <QueryClientProvider client={client}><Outlet /></QueryClientProvider> })
  const mail = createRoute({ getParentRoute: () => root, path: '/mail', validateSearch: inboxSearchSchema, component: Route.options.component })
  const index = createRoute({ getParentRoute: () => mail, path: '/', component: () => <div /> })
  const message = createRoute({ getParentRoute: () => mail, path: '$messageId', component: () => <div /> })
  const router = createRouter({ routeTree: root.addChildren([mail.addChildren([index, message])]), history: createMemoryHistory({ initialEntries: [path] }) })
  const view = render(<RouterProvider router={router} />)
  return { router, list, view, client }
}
afterEach(() => { cleanup(); mock.restore(); window.matchMedia = originalMatchMedia })
// Lets queries a view starts on mount settle inside act() before the test moves on or ends.
const settle = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
// Waits until a removal's follow-up refetches are done, then lets their renders settle.
const settleFetches = async (client: QueryClient) => { await waitFor(() => expect(client.isFetching()).toBe(0)); await settle() }

test('shortcut overview is visible by button and the question-mark key', async () => {
  setup('row')
  await screen.findByText('Termin')
  const button = screen.getByRole('button', { name: 'Tastenkürzel' })
  fireEvent.click(button)
  expect(screen.getByRole('dialog', { name: 'Tastenkürzel' })).toBeTruthy()
  fireEvent.keyDown(document, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  fireEvent.keyDown(document.body, { key: '?', shiftKey: true })
  expect(await screen.findByRole('dialog', { name: 'Tastenkürzel' })).toBeTruthy()
})

test('filing shortcuts open the matching choices for current and selected mail', async () => {
  const assign = spyOn(sdk, 'mailFilingAssign').mockResolvedValue({ data: { ok: true } } as never)
  setup('row')
  await screen.findByText('Termin')
  fireEvent.keyDown(document.body, { key: 'j' })
  fireEvent.keyDown(document.body, { key: 'v' })
  expect(await screen.findByRole('menuitemradio', { name: 'Ordner: Projects (V)' })).toBeTruthy()
  expect(screen.queryByRole('menuitemcheckbox', { name: 'Label: Projects (L)' })).toBeNull()
  fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
  fireEvent.click(screen.getByRole('button', { name: 'Auswählen: Termin' }))
  fireEvent.keyDown(document.body, { key: 'l' })
  fireEvent.click(await screen.findByRole('menuitemcheckbox', { name: 'Label: Projects (L)' }))
  await waitFor(() => expect(assign.mock.calls[0]?.[0]?.body).toEqual({ message_ids: ['m1'], add_labels: ['label'] }))
})

test('filing shortcut uses the opened mail even when it is outside the loaded list', async () => {
  const assign = spyOn(sdk, 'mailFilingAssign').mockResolvedValue({ data: { ok: true } } as never)
  spyOn(sdk, 'mailMessageGet').mockResolvedValue({ data: { ...row, id: 'canonical', body_text: '', attachments: [], to: [], cc: [], thread_id: '', can_show_original: false, can_unsubscribe: false } } as never)
  setup('empty', '/mail/alias')
  const trigger = await screen.findByRole('button', { name: 'Zuordnen' }) as HTMLButtonElement
  await waitFor(() => expect(trigger.disabled).toBe(false))
  fireEvent.keyDown(document.body, { key: 'v' })
  fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Ordner: Projects (V)' }))
  await waitFor(() => expect(assign.mock.calls[0]?.[0]?.body).toEqual({ message_ids: ['canonical'], folder_id: 'folder' }))
})

test('a quick action in a list row files without opening it', async () => {
  const assign = spyOn(sdk, 'mailFilingAssign').mockResolvedValue({ data: { ok: true } } as never)
  const { client, router } = setup('row')
  await screen.findByText('Termin')
  await waitFor(() => expect(client.getQueryState(['mail-quick-actions'])?.status).toBe('success'))
  client.setQueryData(['mail-quick-actions'], [{ destination: { id: 'folder', name: 'Projects', kind: 'folder' }, use_count: 2, last_used_at: '2026-11-23T00:00:00Z', score: 2 }])
  fireEvent.click(await screen.findByRole('button', { name: 'Ordner: Projects (V)' }))
  await waitFor(() => expect(assign.mock.calls[0]?.[0]?.body).toEqual({ message_ids: ['m1'], folder_id: 'folder' }))
  expect(await screen.findByText('Zuordnung gespeichert.')).toBeTruthy()
  expect(router.state.location.pathname).toBe('/mail')
})

test('inbox swipe uses the confirmed assignment and the shared action path', async () => {
  spyOn(sdk, 'settingsUiGet').mockImplementation((async () => ({ data: { swipe_left: 'archive', swipe_right: 'toggle_read', revision: 1 }, response: { ok: true } })) as never)
  const action = spyOn(sdk, 'mailMessageAction').mockImplementation((async () => ({ data: { action_id: 'act', message: row }, response: { status: 200 } })) as never)
  const { client } = setup('row', '/mail', true)
  const element = (await screen.findByText('Termin')).closest<HTMLElement>('.le-mailrow')!
  await waitFor(() => expect(client.getQueryState(['ui-settings'])?.status).toBe('success'))
  element.getBoundingClientRect = () => ({ width: 300, height: 100, left: 0, right: 300, top: 0, bottom: 100, x: 0, y: 0, toJSON() {} })
  const pointer = (type: string, x: number) => {
    const event = new MouseEvent(type, { bubbles: true, clientX: x, clientY: 100 })
    Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: 'touch' }, isPrimary: { value: true } })
    fireEvent(element, event)
  }
  pointer('pointerdown', 200); pointer('pointermove', 300); pointer('pointerup', 300)
  await waitFor(() => expect(action).toHaveBeenCalledTimes(1))
  expect(action.mock.calls[0]?.[0]).toEqual({ path: { message_id: 'm1' }, body: { kind: 'mark_read' } })
  expect(await screen.findByText('„Termin“ als gelesen markiert.')).toBeTruthy()
})

test('two disabled swipe directions do not capture a touch', async () => {
  spyOn(sdk, 'settingsUiGet').mockImplementation((async () => ({ data: { swipe_left: 'none', swipe_right: 'none', revision: 1 }, response: { ok: true } })) as never)
  const { client } = setup('row', '/mail', true)
  const element = (await screen.findByText('Termin')).closest<HTMLElement>('.le-mailrow')!
  await waitFor(() => expect(client.getQueryState(['ui-settings'])?.status).toBe('success'))
  await settle()
  const capture = spyOn(element, 'setPointerCapture')
  const event = new MouseEvent('pointerdown', { bubbles: true, clientX: 200, clientY: 100 })
  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: 'touch' }, isPrimary: { value: true } })
  fireEvent(element, event)
  expect(capture).not.toHaveBeenCalled()
})

test('coarse pointer inbox controls use the large touch target token', async () => {
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  const rule = css.match(/@media \(pointer: coarse\) \{ (\.le-inbox-filters[^{}]+)\{ ([^{}]+) \} \.le-inbox-toast \.le-link \{ ([^{}]+) \} \}/u)
  expect(rule).toBeTruthy()
  for (const selector of ['.le-inbox-filters .le-chip', '.le-inbox-control', '.le-inbox-toast .le-link']) expect(rule?.[1]).toContain(selector)
  expect(rule?.[2]).toContain('min-width: var(--control-height-lg)')
  expect(rule?.[2]).toContain('min-height: var(--control-height-lg)')
  for (const property of ['display: inline-flex', 'align-items: center', 'justify-content: center']) expect(rule?.[3]).toContain(property)
  expect(css).toMatch(/\.le-inbox-scroll \{[^}]*padding-bottom: calc\(var\(--control-height-lg\) \+ 2 \* var\(--space-4\)\)/u)
  setup('row', '/mail', true)
  await screen.findByText('Termin')
  for (const name of ['Suchen', 'Jetzt abrufen']) expect(screen.getByRole('button', { name }).classList.contains('le-icon-btn--lg')).toBe(true)
})

test('rows show provider tags and account filters fall back to the address', async () => {
  setup('row')
  const subject = await screen.findByText('Termin')
  expect(subject.closest('.le-mailrow')?.querySelector('.le-mailrow-top .le-tag')?.textContent).toBe('Gmail')
  const filter = screen.getByRole('button', { name: 'Alle Konten' })
  const chevron = filter.querySelector('.lucide-chevron-down')
  expect(chevron?.getAttribute('aria-hidden')).toBe('true')
  expect(filter.lastElementChild).toBe(chevron)
  fireEvent.click(filter)
  fireEvent.click(screen.getByRole('menuitem', { name: 'ich@example.com' }))
  expect(await screen.findByRole('button', { name: 'ich@example.com' })).toBeTruthy()
  await settle()
})

test('unread filter updates URL and query', async () => {
  const { router, list } = setup()
  fireEvent.click(await screen.findByRole('button', { name: 'Ungelesen' }))
  await waitFor(() => expect(router.state.location.searchStr).toContain('unread=true'))
  await waitFor(() => expect(list.mock.calls.some(([options]) => options?.query?.unread === true)).toBe(true))
})

test('shows the unfiltered empty state', async () => {
  setup(); expect(await screen.findByText('Keine Mails im Posteingang. Neue erscheinen nach dem nächsten Abruf.')).toBeTruthy()
})
test('shows the filtered empty state', async () => {
  setup('empty', '/mail?unread=true'); expect(await screen.findByText('Keine Mails passen zu diesen Filtern. Nimm einen Filter heraus, um mehr zu sehen.')).toBeTruthy()
})
test('error notice retries the query', async () => {
  const { list } = setup('error')
  expect(await screen.findByText('Die Mails konnten nicht geladen werden. Versuch es gleich noch mal.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Noch mal versuchen' }))
  await waitFor(() => expect(list.mock.calls.length).toBe(2))
})
test('archive toast replaces a failed refresh notice', async () => {
  spyOn(sdk, 'mailSyncAll').mockImplementation((async () => ({ error: {}, response: { ok: false } })) as never)
  spyOn(sdk, 'mailMessageAction').mockImplementation((async () => ({ data: { action_id: 'act', message: row }, response: { status: 200 } })) as never)
  setup('row'); await screen.findByText('Termin')
  fireEvent.click(screen.getByRole('button', { name: 'Jetzt abrufen' }))
  expect(await screen.findByText('Die Mails konnten nicht abgerufen werden. Versuch es gleich noch mal.')).toBeTruthy()
  fireEvent.keyDown(document.body, { key: 'j' }); fireEvent.keyDown(document.body, { key: 'e' })
  expect(await screen.findByText('„Termin“ archiviert.')).toBeTruthy()
  expect(screen.getByText('Rückgängig')).toBeTruthy()
  expect(screen.queryByText('Die Mails konnten nicht abgerufen werden. Versuch es gleich noch mal.')).toBeNull()
})

test('shows six placeholder rows while loading', async () => {
  const { view, client } = setup('loading')
  await screen.findByText('Mails werden geladen …')
  expect(view.container.querySelectorAll('.le-mailrow--placeholder').length).toBe(6)
  // The list stays pending on purpose; only the summary settles.
  await waitFor(() => expect(client.getQueryState(['summary'])?.status).toBe('success'))
  await settle()
})
test('archiving an open mail selects and opens the next mail with filters', async () => {
  const action = spyOn(sdk, 'mailMessageAction').mockImplementation((async () => ({ data: { action_id: 'act', message: row }, response: { status: 200 } })) as never)
  const { router } = setup('rows', '/mail/m1?unread=true')
  await screen.findByText('Termin')
  expect(screen.getByText('Termin').closest('.le-mailrow')?.getAttribute('aria-current')).toBe('true')
  fireEvent.keyDown(document.body, { key: 'e' })
  await waitFor(() => expect(router.state.location.pathname).toBe('/mail/m2'))
  expect(router.state.location.searchStr).toContain('unread=true')
  await waitFor(() => expect(action.mock.calls[0]?.[0]?.path).toEqual({ message_id: 'm1' }))
  expect(screen.getByText('Zweite').closest('.le-mailrow')?.getAttribute('aria-current')).toBe('true')
})

test('failed removal of an open mail returns to it if navigation has not changed', async () => {
  let fail!: (value: unknown) => void
  spyOn(sdk, 'mailMessageAction').mockImplementation((() => new Promise(resolve => { fail = resolve })) as never)
  const { router } = setup('rows', '/mail/m1?unread=true')
  await screen.findByText('Termin')
  fireEvent.keyDown(document.body, { key: 'e' })
  await waitFor(() => expect(router.state.location.pathname).toBe('/mail/m2'))
  await waitFor(() => expect(fail).toBeDefined())
  fail({ response: { status: 502 } })
  await waitFor(() => expect(router.state.location.pathname).toBe('/mail/m1'))
  expect(router.state.location.searchStr).toContain('unread=true')
})

test('archiving the only open mail returns to the filtered list', async () => {
  spyOn(sdk, 'mailMessageAction').mockImplementation((async () => ({ data: { action_id: 'act', message: row }, response: { status: 200 } })) as never)
  const { router } = setup('row', '/mail/m1?unread=true')
  await screen.findByText('Termin')
  fireEvent.keyDown(document.body, { key: 'e' })
  await waitFor(() => expect(router.state.location.pathname).toBe('/mail'))
  expect(router.state.location.searchStr).toContain('unread=true')
})

test('j and k open adjacent mails while preserving filters', async () => {
  const { router } = setup('rows', '/mail/m1?unread=true')
  await screen.findByText('Termin')
  fireEvent.keyDown(document.body, { key: 'j' })
  await waitFor(() => expect(router.state.location.pathname).toBe('/mail/m2'))
  expect(router.state.location.searchStr).toContain('unread=true')
  fireEvent.keyDown(document.body, { key: 'k' })
  await waitFor(() => expect(router.state.location.pathname).toBe('/mail/m1'))
})

test('trashing the last open mail by its row button opens the previous mail', async () => {
  const action = spyOn(sdk, 'mailMessageAction').mockImplementation((async () => ({ data: { action_id: 'act', message: row }, response: { status: 200 } })) as never)
  const { router } = setup('rows', '/mail/m3')
  const rowElement = (await screen.findByText('Dritte')).closest('.le-mailrow')!
  fireEvent.click(rowElement.querySelector('button[aria-label="In den Papierkorb"]')!)
  await waitFor(() => expect(router.state.location.pathname).toBe('/mail/m2'))
  await waitFor(() => expect(action.mock.calls[0]?.[0]?.path).toEqual({ message_id: 'm3' }))
})

test('failed archive restores the highlight and the next archive targets the restored row', async () => {
  let fail!: (value: unknown) => void
  const action = spyOn(sdk, 'mailMessageAction').mockImplementation((() => new Promise(resolve => { fail = resolve })) as never)
  setup('rows'); await screen.findByText('Termin')
  fireEvent.keyDown(document.body, { key: 'j' })
  expect(screen.getByText('Termin').closest('.le-mailrow')?.getAttribute('aria-current')).toBe('true')
  fireEvent.keyDown(document.body, { key: 'e' })
  await waitFor(() => expect(action).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(screen.getByText('Zweite').closest('.le-mailrow')?.getAttribute('aria-current')).toBe('true'))
  fail({ response: { status: 502 } })
  await screen.findByText('Das hat nicht geklappt. Lade den Posteingang neu, um zu sehen, wo die Mail jetzt liegt.')
  await waitFor(() => expect(screen.getByText('Termin').closest('.le-mailrow')?.getAttribute('aria-current')).toBe('true'))
  fireEvent.keyDown(document.body, { key: 'e' })
  await waitFor(() => expect(action).toHaveBeenCalledTimes(2))
  expect(action.mock.calls[1]?.[0]?.path).toEqual({ message_id: 'm1' })
})

test('failed removal does not replace a highlight moved by the user', async () => {
  let fail!: (value: unknown) => void
  spyOn(sdk, 'mailMessageAction').mockImplementation((() => new Promise(resolve => { fail = resolve })) as never)
  setup('rows'); await screen.findByText('Termin')
  fireEvent.keyDown(document.body, { key: 'j' })
  fireEvent.keyDown(document.body, { key: 'e' })
  await waitFor(() => expect(screen.getByText('Zweite').closest('.le-mailrow')?.getAttribute('aria-current')).toBe('true'))
  fireEvent.keyDown(document.body, { key: 'j' })
  expect(screen.getByText('Dritte').closest('.le-mailrow')?.getAttribute('aria-current')).toBe('true')
  fail({ response: { status: 502 } })
  await screen.findByText('Das hat nicht geklappt. Lade den Posteingang neu, um zu sehen, wo die Mail jetzt liegt.')
  expect(screen.getByText('Dritte').closest('.le-mailrow')?.getAttribute('aria-current')).toBe('true')
})

test('keyboard archives the selected row but not while typing search', async () => {
  const action = spyOn(sdk, 'mailMessageAction').mockImplementation((async () => ({ data: { action_id: 'act', message: row }, response: { status: 200 } })) as never)
  setup('row'); await screen.findByText('Termin')
  fireEvent.click(screen.getByRole('button', { name: 'Suchen' }))
  const input = screen.getByPlaceholderText('In allen Konten suchen …')
  fireEvent.keyDown(input, { key: 'e' }); expect(action).toHaveBeenCalledTimes(0)
  fireEvent.keyDown(document.body, { key: 'j' }); fireEvent.keyDown(document.body, { key: 'e' })
  await waitFor(() => expect(action).toHaveBeenCalledTimes(1))
  expect(action.mock.calls[0]?.[0]?.body).toEqual({ kind: 'archive' })
})

const rowOf = async (subject: string) => (await screen.findByText(subject)).closest('.le-mailrow') as HTMLElement
const linkOf = (subject: string) => (screen.getByText(subject).closest('.le-mailrow') as HTMLElement).querySelector('.le-mailrow-link')
const archiveFrom = (element: HTMLElement) => { const button = element.querySelector<HTMLButtonElement>('button[aria-label="Archivieren"]')!; act(() => { button.focus() }); fireEvent.click(button) }
// Like the server: an archived mail no longer comes back from the list.
function archivingServer(list: ReturnType<typeof setup>['list'], mode: 'row' | 'rows') {
  const removed = new Set<string>()
  const all = mode === 'row' ? [row] : [row, { ...row, id: 'm2', subject: 'Zweite' }, { ...row, id: 'm3', subject: 'Dritte' }]
  list.mockImplementation((async () => ({ data: { items: all.filter(item => !removed.has(item.id)), next_cursor: null } })) as never)
  return spyOn(sdk, 'mailMessageAction').mockImplementation((async (options: { path: { message_id: string } }) => { removed.add(options.path.message_id); return { data: { action_id: 'act', message: row }, response: { status: 200 } } }) as never)
}

test('removing a focused row moves the focus to the next row', async () => {
  const { list, client } = setup('rows'); archivingServer(list, 'rows')
  archiveFrom(await rowOf('Zweite'))
  await waitFor(() => expect(screen.queryByText('Zweite')).toBeNull())
  await waitFor(() => expect(document.activeElement).toBe(linkOf('Dritte')))
  await settleFetches(client)
})

test('removing the focused last row moves the focus to the previous row', async () => {
  const { list, client } = setup('rows'); archivingServer(list, 'rows')
  archiveFrom(await rowOf('Dritte'))
  await waitFor(() => expect(screen.queryByText('Dritte')).toBeNull())
  await waitFor(() => expect(document.activeElement).toBe(linkOf('Zweite')))
  await settleFetches(client)
})

test('removing the only focused row moves the focus to the list', async () => {
  const { list, client } = setup('row'); archivingServer(list, 'row')
  archiveFrom(await rowOf('Termin'))
  await waitFor(() => expect(screen.queryByText('Termin')).toBeNull())
  await waitFor(() => expect(document.activeElement?.classList.contains('le-inbox-scroll')).toBe(true))
  await settleFetches(client)
})

test('a keyboard removal without focus in the row leaves the focus alone', async () => {
  const { list } = setup('rows'); const action = archivingServer(list, 'rows')
  await rowOf('Termin')
  ;(document.activeElement as HTMLElement | null)?.blur()
  fireEvent.keyDown(document.body, { key: 'j' }); fireEvent.keyDown(document.body, { key: 'e' })
  await waitFor(() => expect(action).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(screen.queryByText('Termin')).toBeNull())
  expect(document.activeElement).toBe(document.body)
})

test('selection mode, row tap, avatar range and escape', async () => {
  const { router } = setup('rows')
  await screen.findByText('Termin')
  fireEvent.click(screen.getByRole('button', { name: 'Auswählen' }))
  expect(screen.getByText('Keine ausgewählt')).toBeTruthy()
  fireEvent.click(screen.getByText('Termin'))
  expect(screen.getByText('1 ausgewählt')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Auswählen: Dritte' }), { shiftKey: true })
  expect(screen.getByText('3 ausgewählt')).toBeTruthy()
  expect(router.state.location.pathname).toBe('/mail')
  fireEvent.keyDown(document.body, { key: 'Escape' })
  expect(screen.queryByText('3 ausgewählt')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Auswählen: Zweite' }))
  expect(screen.getByText('1 ausgewählt')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Auswählen: Zweite' }).getAttribute('aria-pressed')).toBe('true')
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl beenden' }))
  expect(screen.queryByText('1 ausgewählt')).toBeNull()
  // Outside the selection mode the list leaves Escape to the browser.
  expect(fireEvent.keyDown(document.body, { key: 'Escape' })).toBe(true)
})

test('select all caps at 100 and coarse controls are large', async () => {
  setup('many', '/mail', true)
  await screen.findByText('Mail 0')
  expect(screen.getByRole('button', { name: 'Auswählen' }).classList.contains('le-icon-btn--lg')).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Auswählen' }))
  for (const name of ['Auswahl beenden', 'Archivieren', 'Als gelesen markieren', 'In den Papierkorb', 'Als Spam markieren']) expect(screen.getByRole('button', { name }).classList.contains('le-icon-btn--lg')).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Alle auswählen' }))
  expect(screen.getByText('100 ausgewählt')).toBeTruthy()
  expect(screen.getByText('Höchstens 100 Mails auf einmal.')).toBeTruthy()
})

test('mobile selection bar uses large icon targets', async () => {
  setup('row', '/mail', false, true)
  await screen.findByText('Termin')
  fireEvent.click(screen.getByRole('button', { name: 'Auswählen' }))
  for (const name of ['Auswahl beenden', 'Archivieren', 'Als gelesen markieren', 'In den Papierkorb', 'Als Spam markieren']) expect(screen.getByRole('button', { name }).classList.contains('le-icon-btn--lg')).toBe(true)
})

test('keyboard selection and bulk archive remove optimistically and undo as a group', async () => {
  let finish!: (value: unknown) => void
  const action = spyOn(sdk, 'mailMessagesBulkAction').mockImplementation((() => new Promise(resolve => { finish = resolve })) as never)
  const undo = spyOn(sdk, 'mailActionsUndo').mockImplementation((async () => ({ data: { restored: ['a1', 'a2'], skipped: [], expired: [], failed: [] } })) as never)
  setup('rows')
  await screen.findByText('Termin')
  fireEvent.keyDown(document.body, { key: 'j' }); fireEvent.keyDown(document.body, { key: 'x' })
  fireEvent.keyDown(document.body, { key: 'j' }); fireEvent.keyDown(document.body, { key: 'x' })
  expect(screen.getByText('2 ausgewählt')).toBeTruthy()
  fireEvent.keyDown(document.body, { key: 'e' })
  await waitFor(() => expect(action).toHaveBeenCalledTimes(1))
  expect(screen.queryByText('Termin')).toBeNull()
  expect(screen.queryByText('Zweite')).toBeNull()
  expect(action.mock.calls[0]?.[0]?.body).toEqual({ message_ids: ['m1', 'm2'], kind: 'archive' })
  await act(async () => finish({ data: { results: [{ message_id: 'm1', status: 'done', action_id: 'a1', message: row }, { message_id: 'm2', status: 'done', action_id: 'a2', message: row }] }, response: { ok: true } }))
  expect(await screen.findByText('2 Mails archiviert.')).toBeTruthy()
  fireEvent.click(screen.getByText('Rückgängig'))
  await waitFor(() => expect(undo).toHaveBeenCalledTimes(1))
  expect(undo.mock.calls[0]?.[0]?.body).toEqual({ action_ids: ['a1', 'a2'] })
})

test('read action follows the selected read states', async () => {
  const action = spyOn(sdk, 'mailMessagesBulkAction').mockImplementation((async () => ({ data: { results: [{ message_id: 'm1', status: 'done', action_id: 'a1', message: { ...row, is_read: true } }] }, response: { ok: true } })) as never)
  const { client } = setup('row')
  await screen.findByText('Termin')
  fireEvent.click(screen.getByRole('button', { name: 'Auswählen: Termin' }))
  fireEvent.click(screen.getByRole('button', { name: 'Als gelesen markieren' }))
  await waitFor(() => expect(action.mock.calls[0]?.[0]?.body.kind).toBe('mark_read'))
  await screen.findByText('1 Mail als gelesen markiert.')
  // Set the current cache explicitly, independent of the mocked server's refetch.
  const matches = client.getQueriesData<{ pages: { items: (typeof row)[] }[] }>({ queryKey: ['messages'] })
  for (const [key, data] of matches) if (data) client.setQueryData(key, { ...data, pages: data.pages.map(page => ({ ...page, items: page.items.map(item => ({ ...item, is_read: true })) })) })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Auswählen: Termin' })).toBeTruthy())
  fireEvent.click(screen.getByRole('button', { name: 'Auswählen: Termin' }))
  fireEvent.click(screen.getByRole('button', { name: 'Als ungelesen markieren' }))
  await waitFor(() => expect(action.mock.calls[1]?.[0]?.body.kind).toBe('mark_unread'))
})

test('changing filters clears the selection', async () => {
  setup('row')
  await screen.findByText('Termin')
  fireEvent.click(screen.getByRole('button', { name: 'Auswählen: Termin' }))
  expect(screen.getByText('1 ausgewählt')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Ungelesen' }))
  await waitFor(() => expect(screen.queryByText('1 ausgewählt')).toBeNull())
  expect(screen.getByRole('button', { name: 'Auswählen' })).toBeTruthy()
})

test('selection mode disables swipe and hides row actions', async () => {
  spyOn(sdk, 'settingsUiGet').mockImplementation((async () => ({ data: { swipe_left: 'archive', swipe_right: 'toggle_read', revision: 1 }, response: { ok: true } })) as never)
  const action = spyOn(sdk, 'mailMessageAction')
  const { client } = setup('row', '/mail', true)
  const element = (await screen.findByText('Termin')).closest<HTMLElement>('.le-mailrow')!
  await waitFor(() => expect(client.getQueryState(['ui-settings'])?.status).toBe('success'))
  fireEvent.click(screen.getByRole('button', { name: 'Auswählen' }))
  expect(element.querySelector('.le-mailrow-more')).toBeNull()
  const event = new MouseEvent('pointerdown', { bubbles: true, clientX: 200, clientY: 100 })
  Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: 'touch' }, isPrimary: { value: true } })
  const capture = spyOn(element, 'setPointerCapture')
  fireEvent(element, event)
  expect(capture).not.toHaveBeenCalled()
  expect(action).not.toHaveBeenCalled()
})

test('a mixed selection keeps every message and explains why actions are blocked', async () => {
  const bulk = spyOn(sdk, 'mailMessagesBulkAction')
  setup('rows', '/mail', false, false, true)
  await screen.findByText('Zweite')
  fireEvent.click(screen.getByRole('button', { name: 'Auswählen: Termin' }))
  fireEvent.click(screen.getByRole('button', { name: 'Auswählen: Zweite' }))
  expect(await screen.findByText(/Wähle für diese Aktion nur Mails aus unterstützten Konten/)).toBeTruthy()
  const trash = screen.getByRole('button', { name: 'In den Papierkorb' })
  expect(trash.hasAttribute('disabled')).toBe(true)
  fireEvent.click(trash)
  fireEvent.keyDown(document.body, { key: 'Delete' })
  expect(bulk).not.toHaveBeenCalled()
  expect(screen.getByText('2 ausgewählt')).toBeTruthy()
})

test('a Gmail Studio row explains unavailable actions on hover and focus', async () => {
  const action = spyOn(sdk, 'mailMessageAction')
  setup('rows', '/mail', false, false, true)
  const row = (await screen.findByText('Zweite')).closest<HTMLElement>('.le-mailrow')!
  const hint = 'Gmail in Mistral Studio hat keine Berechtigung, Mails zu ändern.'
  expect(within(row).queryByText(hint)).toBeNull()
  const archive = within(row).getByRole('button', { name: 'Archivieren' })
  expect(archive.getAttribute('aria-disabled')).toBe('true')
  fireEvent.mouseEnter(archive)
  expect((await screen.findByRole('tooltip')).textContent).toBe(hint)
  fireEvent.mouseLeave(archive)
  expect(screen.queryByRole('tooltip')).toBeNull()
  fireEvent.keyDown(document.body, { key: 'Tab' })
  act(() => { archive.focus() })
  expect(document.activeElement).toBe(archive)
  const tooltip = screen.getByRole('tooltip')
  expect(tooltip.textContent).toBe(hint)
  expect(archive.getAttribute('aria-describedby')).toBe(tooltip.id)
  fireEvent.click(archive)
  expect(action).not.toHaveBeenCalled()
  expect(screen.getByText('Zweite')).toBeTruthy()
})

test('a swipe on Gmail Studio explains the restriction without an action', async () => {
  spyOn(sdk, 'settingsUiGet').mockImplementation((async () => ({ data: { swipe_left: 'trash', swipe_right: 'archive', revision: 1 }, response: { ok: true } })) as never)
  const action = spyOn(sdk, 'mailMessageAction')
  const { client } = setup('rows', '/mail', true, true, true)
  const element = (await screen.findByText('Zweite')).closest<HTMLElement>('.le-mailrow')!
  await waitFor(() => expect(client.getQueryState(['ui-settings'])?.status).toBe('success'))
  element.getBoundingClientRect = () => ({ width: 300, height: 100, left: 0, right: 300, top: 0, bottom: 100, x: 0, y: 0, toJSON() {} })
  const pointer = (type: string, x: number) => {
    const event = new MouseEvent(type, { bubbles: true, clientX: x, clientY: 100 })
    Object.defineProperties(event, { pointerId: { value: 1 }, pointerType: { value: 'touch' }, isPrimary: { value: true } })
    fireEvent(element, event)
  }
  pointer('pointerdown', 200); pointer('pointermove', 300); pointer('pointerup', 300)
  const hint = 'Gmail in Mistral Studio hat keine Berechtigung, Mails zu ändern.'
  expect((await screen.findByRole('alert')).textContent).toBe(hint)
  expect(screen.getAllByText(hint)).toHaveLength(1)
  expect(action).not.toHaveBeenCalled()
  expect(screen.getByText('Zweite')).toBeTruthy()
})

test('folder filter selects the global app-side folder', async () => {
  const { router, list } = setup('row')
  fireEvent.click(await screen.findByRole('button', { name: 'Ordner und Labels' }))
  expect(await screen.findByRole('menuitem', { name: 'Label: Projects (L)' })).toBeTruthy()
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Ordner: Projects (V)' }))
  await waitFor(() => expect(router.state.location.searchStr).toContain('app_folder='))
  expect(await screen.findByRole('heading', { name: 'Ordner: Projects (V)' })).toBeTruthy()
  await waitFor(() => expect(list.mock.calls.some(([options]) => options?.query?.app_folder_id === 'folder')).toBe(true))
})
