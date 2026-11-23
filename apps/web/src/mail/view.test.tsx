import { mailCapabilities } from '../test/mailCapabilities'
import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider, useParams } from '@tanstack/react-router'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import type { InsightOut } from '../api/generated/types.gen'
import { ChatSessionProvider, useChatSession, type TaskScope } from '../chat/session'
import { Route as MailRoute } from '../routes/_app/mail'
import { inboxSearchSchema } from './filters'
import { MailView } from './MailView'
import { useEffect } from 'react'
import { de } from '../i18n/de'

const originalMatchMedia = window.matchMedia, originalFetch = globalThis.fetch
const mail = { id: 'm1', account_id: 'a', color_index: 0, sender_name: 'Anna', sender_address: 'anna@example.com', to: ['ich@example.com'], cc: [], subject: 'Termin', snippet: '', body_text: '<b>fett</b>', internal_date: null, deadline: null, has_attachments: true, attachments: [{ name: 'Abrechnung_2025.pdf', size: 217088, content_type: 'application/pdf' }], is_flagged: false, is_read: true, needs_reply: false, can_unsubscribe: true, can_show_original: true, thread_id: null }
// Stands for a task choice left on the start page before the mail was opened.
function StartPageTasks({ scope }: { scope?: TaskScope }) {
  const { setTaskScope } = useChatSession()
  useEffect(() => { if (scope) setTaskScope('new', scope) }, [])
  return null
}
function setup({ mobile = false, unread = false, referenceOnly = false, missing = false, loading = false, failed = false, path = '/mail/m1', stream = false, insight, startTasks, preferencesFail = false, limited = false }: { mobile?: boolean; unread?: boolean; referenceOnly?: boolean; missing?: boolean; loading?: boolean; failed?: boolean; path?: string; stream?: boolean; insight?: () => Promise<{ data: InsightOut }>; startTasks?: TaskScope; preferencesFail?: boolean; limited?: boolean } = {}) {
  window.matchMedia = ((q: string) => ({ matches: !mobile && (q.includes('1024px') || q.includes('640px')), addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  let request: Record<string, unknown> | undefined
  const requests: Record<string, unknown>[] = []
  globalThis.fetch = ((_url: unknown, init: RequestInit) => {
    request = JSON.parse(String(init.body)) as Record<string, unknown>
    requests.push(request)
    if (preferencesFail && requests.length === 1) return Promise.resolve(Response.json({ detail: { code: 'preferences_unavailable' } }, { status: 503 }))
    return Promise.resolve(new Response(new ReadableStream({ start(controller) { if (stream) controller.enqueue(new TextEncoder().encode('event: start\ndata: {"event":"start","conversation_id":"c1","user_message_id":"u1"}\n\n')) } })))
  }) as typeof fetch
  const rows = [{ ...mail, is_read: !unread }, { ...mail, id: 'm2', subject: 'Zweite', is_read: !unread }]
  const getInsight = spyOn(sdk, 'mailMessageInsight').mockImplementation((insight ?? (async () => ({ data: { points: [], deadline: null, followups: [], needs_reply: false, model: '', created_at: '' } }))) as never)
  spyOn(sdk, 'chatSettingsGet').mockResolvedValue({ data: { version: 3, account_ids: ['a'] } } as never)
  spyOn(sdk, 'mailSummary').mockImplementation((async () => ({ data: { accounts: [{ id: 'a', display_name: '', email_address: 'ich@example.com', provider: 'gmail', action_capabilities: mailCapabilities(!limited), color_index: 0, state: 'ok' }], unread_total: unread ? 1 : 0, last_sync_at: null } })) as never)
  spyOn(sdk, 'mailMessagesList').mockImplementation((async () => ({ data: { items: rows, next_cursor: null } })) as never)
  const get = spyOn(sdk, 'mailMessageGet').mockImplementation((async (options: { path: { message_id: string } }) => { if (missing) return { error: { detail: 'Mail nicht gefunden.' }, response: new Response(null, { status: 404 }) }; if (failed) return { error: { detail: 'down' }, response: new Response(null, { status: 500 }) }; if (loading) return await new Promise(() => {}); if (referenceOnly) return { data: { ...mail, id: options.path.message_id, is_read: false, local_reference_only: true, can_unsubscribe: false, can_show_original: false } }; return { data: rows.find(row => row.id === options.path.message_id) ?? mail } }) as never)
  const action = spyOn(sdk, 'mailMessageAction').mockImplementation((async (options: { path: { message_id: string } }) => ({ data: { action_id: 'act', message: rows.find(row => row.id === options.path.message_id) }, response: { status: 200 } })) as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  const root = createRootRoute({ component: () => <QueryClientProvider client={client}><ChatSessionProvider><StartPageTasks scope={startTasks} /><Outlet /></ChatSessionProvider></QueryClientProvider> })
  const inbox = createRoute({ getParentRoute: () => root, path: '/mail', validateSearch: inboxSearchSchema, component: MailRoute.options.component })
  const index = createRoute({ getParentRoute: () => inbox, path: '/', component: () => <div>Inbox index</div> })
  const detail = createRoute({ getParentRoute: () => inbox, path: '$messageId', component: () => { const { messageId } = useParams({ strict: false }) as { messageId: string }; return <MailView id={messageId} /> } })
  const home = createRoute({ getParentRoute: () => root, path: '/', validateSearch: (s: Record<string, unknown>) => s, component: () => <div>Home</div> })
  const chat = createRoute({ getParentRoute: () => root, path: '/chat/$conversationId', component: () => <div>Chat</div> })
  const router = createRouter({ routeTree: root.addChildren([home, inbox.addChildren([index, detail]), chat]), history: createMemoryHistory({ initialEntries: [path] }) })
  const view = render(<RouterProvider router={router} />)
  return { client, router, view, action, get, getInsight, request: () => request, requests }
}
afterEach(() => { cleanup(); mock.restore(); window.matchMedia = originalMatchMedia; globalThis.fetch = originalFetch })
// Lets queries a view starts on mount settle inside act() before the test moves on or ends.
const settle = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

test('task proposal starts a fresh chat with explicit mail and task account scope', async () => {
  const { request, action, router } = setup({ stream: true })
  const confirm = spyOn(sdk, 'chatProposalConfirm')
  const more = await screen.findByRole('button', { name: 'Weitere Aktionen' })
  fireEvent.click(more)
  const item = await screen.findByRole('menuitem', { name: 'Aufgabe vorschlagen' })
  await waitFor(() => expect(document.activeElement).toBe(item))
  fireEvent.keyDown(item, { key: 'Escape' })
  expect(document.activeElement).toBe(more)
  fireEvent.click(more)
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Aufgabe vorschlagen' }))
  await waitFor(() => expect(request()?.context_message_id).toBe('m1'))
  expect(request()?.account_id).toBe('a')
  expect(request()?.task_source).toBe('account')
  expect(request()?.task_source_account_id).toBe('a')
  expect(request()?.conversation_id).toBeUndefined()
  expect(request()?.calendar_source_ids).toBeUndefined()
  expect(confirm).not.toHaveBeenCalled()
  expect(action).not.toHaveBeenCalled()
  await waitFor(() => expect(router.state.location.pathname).toBe('/chat/c1'))
})

test('a task source retained locally opens as read-only and makes no provider requests', async () => {
  const { action, getInsight } = setup({ referenceOnly: true, path: '/mail/earlier' })
  await screen.findByText(de.mail.localReference)
  expect(document.querySelector('.le-mail-text')?.textContent).toBe(mail.body_text)
  expect(screen.queryByRole('button', { name: de.mail.askInChat })).toBeNull()
  expect(screen.queryByRole('button', { name: de.mail.more })).toBeNull()
  fireEvent.keyDown(document, { key: 'e' })
  await settle()
  expect(action).not.toHaveBeenCalled()
  expect(getInsight).not.toHaveBeenCalled()
})

test('open mail shows the provider when the account has no display name', async () => {
  setup()
  await screen.findByRole('heading', { name: 'Termin' })
  await waitFor(() => expect(document.querySelector('.le-mail-header .le-tag')?.textContent).toBe('Gmail'))
})

test('insight points use disc markers, indentation and item spacing', async () => {
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  expect(css).toMatch(/\.le-insight-points \{[^}]*padding-left: calc\(var\(--space-4\) \+ var\(--space-1\)\);[^}]*list-style: disc;/u)
  expect(css).toMatch(/\.le-insight-points li \+ li \{[^}]*margin-top: var\(--space-1\);/u)
})

test('mail text becomes one paragraph per block, spaced by the paragraph gap and not by blank lines', async () => {
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  expect(css).toMatch(/\.le-mail-text \{[^}]*display: flex; flex-direction: column; gap: var\(--space-3\);/u)
  expect(css).toMatch(/\.le-mail-text p \{[^}]*margin: 0; white-space: pre-wrap;/u)
  const original = mail.body_text
  mail.body_text = 'Sehr geehrte Damen und Herren,\n\nanbei die Abrechnung.\n\n\nMit freundlichen Grüßen\nIhre Hausverwaltung'
  try {
    setup()
    await screen.findByRole('heading', { name: 'Termin' })
    const text = document.querySelector('.le-mail-text')
    expect(Array.from(text?.querySelectorAll('p') ?? []).map(p => p.textContent)).toEqual(['Sehr geehrte Damen und Herren,', 'anbei die Abrechnung.', 'Mit freundlichen Grüßen\nIhre Hausverwaltung'])
    expect(text?.textContent).not.toContain('\n\n')
    await settle()
  } finally { mail.body_text = original }
})

test('opening unread mail marks it read once across rerender and refetch, updating list', async () => {
  const { action, client, view, router } = setup({ unread: true })
  await waitFor(() => expect(action.mock.calls.length).toBe(1))
  await waitFor(() => expect(client.getQueriesData<{ pages: { items: { id: string; is_read: boolean }[] }[] }>({ queryKey: ['messages'] }).some(([, d]) => d?.pages.some(page => page.items.some(row => row.id === 'm1' && row.is_read)))).toBe(true))
  expect(action.mock.calls[0]?.[0]?.body).toEqual({ kind: 'mark_read' })
  view.rerender(<RouterProvider router={router} />)
  await act(async () => { await client.refetchQueries({ queryKey: ['message', 'm1'] }) })
  expect(action.mock.calls.length).toBe(1)
})

test('read mail is not automatically marked and manual unread is not reversed', async () => {
  const { action } = setup(); await screen.findByRole('heading', { name: 'Termin' })
  expect(action).not.toHaveBeenCalled()
  fireEvent.click(within(document.querySelector('.le-mail-view') as HTMLElement).getByRole('button', { name: 'Als ungelesen markieren' }))
  await waitFor(() => expect(action.mock.calls.length).toBe(1))
  await new Promise(resolve => setTimeout(resolve, 20))
  expect(action.mock.calls.length).toBe(1)
})

test('manual unread after auto read is not marked read again', async () => {
  const { action, client } = setup({ unread: true })
  await waitFor(() => expect(action.mock.calls.length).toBe(1))
  await waitFor(() => expect(client.isFetching({ queryKey: ['message', 'm1'] })).toBe(0))
  await act(async () => { client.setQueryData(['message', 'm1'], { ...mail, is_read: true }) })
  const view = document.querySelector('.le-mail-view') as HTMLElement
  fireEvent.click(await within(view).findByRole('button', { name: 'Als ungelesen markieren' }))
  await waitFor(() => expect(action.mock.calls.length).toBe(2))
  await act(async () => { await client.refetchQueries({ queryKey: ['message', 'm1'] }) })
  expect(action.mock.calls.map(([options]) => options?.body?.kind)).toEqual(['mark_read', 'mark_unread'])
})

test('manual unread waits for pending automatic read and remains unread in both caches', async () => {
  const { action, get, client } = setup({ unread: true })
  let serverRead = false
  let finishRead!: (value: unknown) => void
  get.mockImplementation((async () => ({ data: { ...mail, is_read: serverRead } })) as never)
  action.mockImplementation(((options: { body: { kind: string } }) => {
    if (options.body.kind === 'mark_read') return new Promise(resolve => { finishRead = resolve })
    serverRead = false
    return Promise.resolve({ data: { action_id: 'unread', message: { ...mail, is_read: false } }, response: { status: 200 } })
  }) as never)
  await waitFor(() => expect(action.mock.calls.length).toBe(1))
  const view = document.querySelector('.le-mail-view') as HTMLElement
  fireEvent.click(await within(view).findByRole('button', { name: 'Als ungelesen markieren' }))
  await act(async () => { await Promise.resolve() })
  expect(action.mock.calls.length).toBe(1)
  await act(async () => {
    serverRead = true
    finishRead({ data: { action_id: 'read', message: { ...mail, is_read: true } }, response: { status: 200 } })
  })
  await waitFor(() => expect(action.mock.calls.map(([options]) => options?.body?.kind)).toEqual(['mark_read', 'mark_unread']))
  await waitFor(() => expect(client.getQueryData<{ is_read: boolean }>(['message', 'm1'])?.is_read).toBe(false))
  expect(client.getQueriesData<{ pages: { items: { id: string; is_read: boolean }[] }[] }>({ queryKey: ['messages'] }).some(([, data]) => data?.pages.some(page => page.items.some(row => row.id === 'm1' && !row.is_read)))).toBe(true)
})

test('mobile bars contain the requested actions; desktop has no back button', async () => {
  setup({ mobile: true }); await screen.findByRole('heading', { name: 'Termin' })
  for (const name of ['Zurück zum Posteingang', 'Archivieren', 'Als ungelesen markieren', 'In den Papierkorb', 'Weitere Aktionen', 'Im Chat fragen', 'Antwort entwerfen']) expect(screen.getByRole('button', { name })).toBeTruthy()
  await settle(); cleanup(); mock.restore(); setup(); await screen.findByRole('heading', { name: 'Termin' })
  expect(screen.queryByRole('button', { name: 'Zurück zum Posteingang' })).toBeNull()
  await settle()
})

test.each([{ mobile: false }, { mobile: true }])('"Im Chat fragen" and "Antwort entwerfen" carry their icons in the split view and on mobile (%o)', async ({ mobile }) => {
  setup({ mobile }); await screen.findByRole('heading', { name: 'Termin' })
  expect(screen.getByRole('button', { name: 'Im Chat fragen' }).querySelector('.lucide-message-circle')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Antwort entwerfen' }).querySelector('.lucide-pencil')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Antwort entwerfen' }).classList.contains('le-btn--sm')).toBe(!mobile)
  await settle()
})

test('mail body is literal text and attachment is non-interactive', async () => {
  const { view } = setup(); await screen.findByRole('heading', { name: 'Termin' })
  expect(screen.getByText('<b>fett</b>')).toBeTruthy()
  expect(view.container.querySelector('b')).toBeNull()
  const chip = screen.getByText('Abrechnung_2025.pdf').closest('.le-mail-attachment') as HTMLElement
  expect(chip).toBeTruthy()
  expect(within(chip).getByText('212 KB')).toBeTruthy()
  expect(chip.querySelector('.lucide-paperclip')).toBeTruthy()
  expect(chip.querySelector('button, a')).toBeNull()
  await settle()
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  const rule = css.match(/\.le-mail-attachment \{([^}]+)\}/u)?.[1]
  for (const property of ['min-height: 36px', 'border: 1px solid var(--control-border)', 'border-radius: var(--radius-pill)']) expect(rule).toContain(property)
  expect(css).toMatch(/\.le-mail-attachment-name \{[^}]*font-weight: 600;/u)
  expect(css).toMatch(/\.le-mail-attachment-size \{[^}]*color: var\(--ink-muted\);/u)
})

test('not found offers return to inbox', async () => {
  const { router, get } = setup({ mobile: true, missing: true, path: '/mail/unknown' })
  expect(await screen.findByText('Die Mail ist nicht mehr da.')).toBeTruthy()
  expect(get.mock.calls.length).toBe(1)
  fireEvent.click(screen.getByRole('button', { name: 'Zum Posteingang' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/mail'))
})

test('draft reply starts a turn with context and opens conversation', async () => {
  const { router, request } = setup({ stream: true }); await screen.findByRole('heading', { name: 'Termin' })
  fireEvent.click(screen.getByRole('button', { name: 'Antwort entwerfen' }))
  await waitFor(() => expect(request()).toMatchObject({ text: 'Entwirf eine Antwort auf diese Mail.', context_message_id: 'm1' }))
  expect(request()?.account_id).toBe('a')
  await waitFor(() => expect(router.state.location.pathname).toBe('/chat/c1'))
})

test('mail reader turns never inherit the task choice left on the start page', async () => {
  const { request } = setup({ stream: true, startTasks: { source: 'account_and_unlinked', account_id: 'a' } }); await screen.findByRole('heading', { name: 'Termin' })
  fireEvent.click(screen.getByRole('button', { name: 'Antwort entwerfen' }))
  await waitFor(() => expect(request()?.context_message_id).toBe('m1'))
  expect(request()?.task_source).toBe('none')
  expect(request()?.task_source_account_id).toBeUndefined()
})

test('a preference failure in the mail reader offers an explicit continuation without preferences', async () => {
  const { requests, router } = setup({ stream: true, preferencesFail: true }); await screen.findByRole('heading', { name: 'Termin' })
  fireEvent.click(screen.getByRole('button', { name: 'Antwort entwerfen' }))
  fireEvent.click(await screen.findByRole('button', { name: de.preferences.continueWithout }))
  await waitFor(() => expect(requests).toHaveLength(2))
  expect(requests[0]?.without_preferences).toBeUndefined()
  expect(requests[1]).toMatchObject({ context_message_id: 'm1', without_preferences: true })
  await waitFor(() => expect(router.state.location.pathname).toBe('/chat/c1'))
})

test('ask in chat opens start page with mail reference', async () => {
  const { router } = setup(); await screen.findByRole('heading', { name: 'Termin' })
  fireEvent.click(screen.getByRole('button', { name: 'Im Chat fragen' }))
  await waitFor(() => expect(router.state.location.searchStr).toContain('about=m1'))
})

for (const mobile of [false, true]) test(`${mobile ? 'mobile' : 'desktop'} archive navigates and shows undo`, async () => {
  const { router } = setup({ mobile, path: '/mail/m1?unread=true' }); await screen.findByRole('heading', { name: 'Termin' })
  const actions = mobile ? screen.getByRole('button', { name: 'Archivieren' }) : within(document.querySelector('.le-mail-view') as HTMLElement).getByRole('button', { name: 'Archivieren' })
  fireEvent.click(actions)
  await waitFor(() => expect(router.state.location.pathname).toBe(mobile ? '/mail' : '/mail/m2'))
  expect(router.state.location.searchStr).toContain('unread=true')
  expect(await screen.findByText('Rückgängig')).toBeTruthy()
})

for (const mobile of [false, true]) test(`${mobile ? 'mobile' : 'desktop'} reading view shows a failed read change once`, async () => {
  const { action } = setup({ mobile })
  await screen.findByRole('heading', { name: 'Termin' })
  action.mockImplementation((async () => ({ error: { detail: { code: 'provider_failed' } }, response: { status: 502 } })) as never)
  const view = document.querySelector('.le-mail-view') as HTMLElement
  fireEvent.click(within(view).getByRole('button', { name: 'Als ungelesen markieren' }))
  await waitFor(() => expect(document.querySelectorAll('.le-inbox-toast')).toHaveLength(1))
  const toast = document.querySelector('.le-inbox-toast') as HTMLElement
  expect(toast.classList.contains('le-inbox-toast--inline')).toBe(mobile)
  expect(view.contains(toast)).toBe(mobile)
  expect(toast.textContent).toContain('Der Anbieter hat die Änderung nicht bestätigt. Prüfe die Mail nach dem nächsten Abruf.')
  expect(within(view).getByRole('button', { name: 'Als ungelesen markieren' })).toBeTruthy()
})

test('silent auto read preserves an existing undo toast', async () => {
  const { router, action } = setup({ unread: true, path: '/mail' })
  await screen.findByText('Termin')
  fireEvent.keyDown(document.body, { key: 'j' }); fireEvent.keyDown(document.body, { key: 'e' })
  expect(await screen.findByText('Rückgängig')).toBeTruthy()
  await act(async () => { await router.navigate({ to: '/mail/$messageId', params: { messageId: 'm2' } }) })
  await waitFor(() => expect(action.mock.calls.some(([options]) => options?.path?.message_id === 'm2' && options?.body?.kind === 'mark_read')).toBe(true))
  expect(screen.getByText('Rückgängig')).toBeTruthy()
})


test('loading displays placeholders and mobile back button', async () => {
  const { view } = setup({ mobile: true, loading: true })
  expect(await screen.findByRole('status', { name: '' })).toBeTruthy()
  expect(screen.getByText('Mail wird geladen …')).toBeTruthy()
  expect(view.container.querySelector('.le-mail-placeholder[aria-hidden="true"]')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Zurück zum Posteingang' })).toBeTruthy()
  await settle()
})

test('forward menu entry asks the assistant to forward this mail', async () => {
  const { request } = setup({ mobile: true }); await screen.findByRole('heading', { name: 'Termin' })
  fireEvent.click(screen.getByRole('button', { name: 'Weitere Aktionen' }))
  fireEvent.click(screen.getByRole('menuitem', { name: 'Weiterleiten' }))
  await waitFor(() => expect(request()).toMatchObject({ text: 'Leite diese Mail weiter.', context_message_id: 'm1' }))
  expect(request()?.account_id).toBe('a')
})

test('spam menu entry marks the mail as spam and leaves the opened mail', async () => {
  const { action, router } = setup({ mobile: true }); await screen.findByRole('heading', { name: 'Termin' })
  fireEvent.click(screen.getByRole('button', { name: 'Weitere Aktionen' }))
  fireEvent.click(screen.getByRole('menuitem', { name: 'Als Spam markieren' }))
  await waitFor(() => expect(action).toHaveBeenCalledWith({ path: { message_id: 'm1' }, body: { kind: 'spam' } }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/mail'))
  expect(await screen.findByText('Mail als Spam markiert.')).toBeTruthy()
})

test('newsletter menu starts the contextual confirmation turn', async () => {
  const { request } = setup({ mobile: true }); await screen.findByRole('heading', { name: 'Termin' })
  fireEvent.click(screen.getByRole('button', { name: 'Weitere Aktionen' }))
  fireEvent.click(screen.getByRole('menuitem', { name: 'Newsletter abbestellen' }))
  await waitFor(() => expect(request()).toMatchObject({ text: 'Bestelle diesen Newsletter ab.', context_message_id: 'm1' }))
  expect(request()?.account_id).toBe('a')
  expect((screen.getByRole('button', { name: 'Antwort entwerfen' }) as HTMLButtonElement).disabled).toBe(true)
})

test.each([{ mobile: false }, { mobile: true }])('the more-actions button is always there; without an unsubscribe route that entry is disabled and explains why (%o)', async ({ mobile }) => {
  const original = mail.can_unsubscribe
  mail.can_unsubscribe = false
  try {
    const { request } = setup({ mobile }); await screen.findByRole('heading', { name: 'Termin' })
    const more = screen.getByRole('button', { name: 'Weitere Aktionen' })
    expect(more.getAttribute('aria-haspopup')).toBe('menu')
    fireEvent.click(more)
    const item = screen.getByRole('menuitem', { name: /Newsletter abbestellen/ })
    expect(item.getAttribute('aria-disabled')).toBe('true')
    expect(within(item).getByText('In dieser Mail steht kein Weg zum Abbestellen.')).toBeTruthy()
    fireEvent.click(item)
    expect(screen.getByRole('menuitem', { name: /Newsletter abbestellen/ })).toBeTruthy()
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expect(request()).toBeUndefined()
    expect(screen.getByRole('button', { name: 'Antwort entwerfen' }).hasAttribute('disabled')).toBe(false)
    expect(await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()).toMatch(/\.le-menu-item\[aria-disabled="true"\][^{]*\{[^}]*cursor: not-allowed/u)
  } finally { mail.can_unsubscribe = original }
})

// Placement comes from floating.ts, which keeps the menu inside the viewport; no CSS rule positions it per context.
test('the "Weitere Aktionen" menu hangs from the start of its trigger in the split view', async () => {
  setup(); await screen.findByRole('heading', { name: 'Termin' })
  fireEvent.click(screen.getByRole('button', { name: 'Weitere Aktionen' }))
  expect(screen.getByRole('menu').classList.contains('le-menu--down-start')).toBe(true)
  expect(await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()).not.toMatch(/\.le-mail-topbar[^{]*\.le-menu/u)
})

test('the "Weitere Aktionen" menu hangs from the end of its trigger below desktop, where the actions end the bar', async () => {
  setup({ mobile: true }); await screen.findByRole('heading', { name: 'Termin' })
  fireEvent.click(screen.getByRole('button', { name: 'Weitere Aktionen' }))
  expect(screen.getByRole('menu').classList.contains('le-menu--down-end')).toBe(true)
})

test('with an unsubscribe route the entry is enabled and has no hint', async () => {
  setup(); await screen.findByRole('heading', { name: 'Termin' })
  fireEvent.click(screen.getByRole('button', { name: 'Weitere Aktionen' }))
  const item = screen.getByRole('menuitem', { name: 'Newsletter abbestellen' })
  expect(item.hasAttribute('aria-disabled')).toBe(false)
  expect(item.querySelector('.le-menu-sub')).toBeNull()
  await settle()
})

const source = 'Received: from a\r\nSubject: Termin\r\n\r\n<b>fett</b>'
const originalData = { headers: [{ name: 'Received', value: 'from a' }, { name: 'Subject', value: 'Termin' }], source, truncated: false }
async function openOriginal() {
  await screen.findByRole('heading', { name: 'Termin' })
  const more = screen.getByRole('button', { name: 'Weitere Aktionen' })
  fireEvent.click(more)
  fireEvent.click(screen.getByRole('menuitem', { name: 'Original anzeigen' }))
  return { more, dialog: await screen.findByRole('dialog', { name: 'Original der Mail' }) }
}

test('original shows loading, then headers and literal source, fetched once; Escape closes and returns the focus', async () => {
  let resolve!: (value: unknown) => void
  const fetchOriginal = spyOn(sdk, 'mailMessageOriginal').mockImplementation((() => new Promise(done => { resolve = done })) as never)
  setup()
  const { more, dialog } = await openOriginal()
  // The scrim sits in the body: inside the page it would stack below the sticky label of the sidebar.
  expect(dialog.parentElement?.className).toBe('le-scrim'); expect(dialog.parentElement?.parentElement).toBe(document.body)
  expect(within(dialog).getByRole('status').textContent).toBe('Original wird abgerufen …')
  expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Schließen' }))
  await act(async () => { resolve({ data: originalData, response: { status: 200 } }) })
  expect(await within(dialog).findByText('Received')).toBeTruthy()
  expect(within(dialog).getByText('from a')).toBeTruthy()
  expect(within(dialog).getByRole('heading', { name: 'Kopfzeilen' })).toBeTruthy()
  expect(within(dialog).getByRole('heading', { name: 'Quelltext' })).toBeTruthy()
  expect(dialog.querySelector('pre')?.textContent).toBe(source)
  expect(dialog.querySelector('pre b')).toBeNull()
  expect(within(dialog).queryByRole('status')).toBeNull()
  expect(fetchOriginal.mock.calls).toEqual([[{ path: { message_id: 'm1' } }]])
  fireEvent.keyDown(dialog, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(document.activeElement).toBe(more)
})

test('a truncated original says so, an empty one too, and the close button closes', async () => {
  spyOn(sdk, 'mailMessageOriginal').mockImplementation((async () => ({ data: { headers: [], source: '', truncated: true }, response: { status: 200 } })) as never)
  setup({ mobile: true })
  const { dialog } = await openOriginal()
  expect(await within(dialog).findByText('Die Mail ist groß, deshalb siehst du hier nur den Anfang des Quelltexts.')).toBeTruthy()
  expect(within(dialog).getByText('Der Anbieter hat zu dieser Mail keinen Quelltext geliefert.')).toBeTruthy()
  expect(within(dialog).queryByRole('heading', { name: 'Kopfzeilen' })).toBeNull()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Schließen' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
})

test.each([[409, 'Bei dieser Mail lässt sich das Original nicht abrufen.'], [404, 'Die Mail ist nicht mehr da.']])('a refused original (%i) explains itself and offers no retry', async (status, text) => {
  const fetchOriginal = spyOn(sdk, 'mailMessageOriginal').mockImplementation((async () => ({ error: { detail: { code: 'original_unavailable' } }, response: { status } })) as never)
  setup()
  const { dialog } = await openOriginal()
  expect((await within(dialog).findByRole('alert')).textContent).toBe(text)
  expect(within(dialog).queryByRole('button', { name: 'Noch mal versuchen' })).toBeNull()
  expect(fetchOriginal).toHaveBeenCalledTimes(1)
})

test('a provider failure offers a retry that fetches the original again', async () => {
  let fail = true
  const fetchOriginal = spyOn(sdk, 'mailMessageOriginal').mockImplementation((async () => fail ? { error: { detail: { code: 'provider_failed' } }, response: { status: 502 } } : { data: originalData, response: { status: 200 } }) as never)
  setup()
  const { dialog } = await openOriginal()
  expect(await within(dialog).findByText('Das Original ließ sich beim Anbieter nicht abrufen. Versuch es gleich noch mal.')).toBeTruthy()
  fail = false
  fireEvent.click(within(dialog).getByRole('button', { name: 'Noch mal versuchen' }))
  await waitFor(() => expect(dialog.querySelector('pre')?.textContent).toBe(source))
  expect(within(dialog).queryByRole('alert')).toBeNull()
  expect(fetchOriginal).toHaveBeenCalledTimes(2)
})

test('retrying keeps the focus in the dialog, and list shortcuts do nothing behind it', async () => {
  let answer!: (value: unknown) => void
  let calls = 0
  spyOn(sdk, 'mailMessageOriginal').mockImplementation((() => ++calls === 1 ? Promise.resolve({ error: { detail: { code: 'provider_failed' } }, response: { status: 502 } }) : new Promise(done => { answer = done })) as never)
  const { action } = setup()
  const { dialog } = await openOriginal()
  const retry = await within(dialog).findByRole('button', { name: 'Noch mal versuchen' })
  retry.focus()
  fireEvent.click(retry)
  // The button is gone while the original loads again.
  expect(await within(dialog).findByRole('status')).toBeTruthy()
  expect(within(dialog).queryByRole('button', { name: 'Noch mal versuchen' })).toBeNull()
  expect(document.activeElement).toBe(dialog)
  for (const target of [dialog, document.body]) for (const key of ['e', 'Delete', '#', 'u']) fireEvent.keyDown(target, { key })
  await settle()
  expect(action).not.toHaveBeenCalled()
  await act(async () => { answer({ data: originalData, response: { status: 200 } }) })
  await waitFor(() => expect(dialog.querySelector('pre')?.textContent).toBe(source))
  expect(dialog.contains(document.activeElement)).toBe(true)
})

test('closing drops the original: nothing stays in the cache, and opening again fetches it again', async () => {
  const fetchOriginal = spyOn(sdk, 'mailMessageOriginal').mockImplementation((async () => ({ data: originalData, response: { status: 200 } })) as never)
  const { client } = setup()
  const cached = () => client.getQueryCache().findAll({ queryKey: ['original'] }).length
  const first = await openOriginal()
  await waitFor(() => expect(first.dialog.querySelector('pre')?.textContent).toBe(source))
  expect(cached()).toBe(1)
  fireEvent.keyDown(first.dialog, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  await waitFor(() => expect(cached()).toBe(0))
  const second = await openOriginal()
  await waitFor(() => expect(second.dialog.querySelector('pre')?.textContent).toBe(source))
  expect(fetchOriginal).toHaveBeenCalledTimes(2)
})

test('an answer that arrives after closing is dropped as well', async () => {
  let answer!: (value: unknown) => void
  spyOn(sdk, 'mailMessageOriginal').mockImplementation((() => new Promise(done => { answer = done })) as never)
  const { client } = setup()
  const { dialog } = await openOriginal()
  expect(within(dialog).getByRole('status')).toBeTruthy()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Schließen' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  await act(async () => { answer({ data: originalData, response: { status: 200 } }) })
  await waitFor(() => expect(client.getQueryCache().findAll({ queryKey: ['original'] })).toHaveLength(0))
  expect(screen.queryByRole('dialog')).toBeNull()
})

test('each mail gets its own original', async () => {
  const fetchOriginal = spyOn(sdk, 'mailMessageOriginal').mockImplementation((async (options: { path: { message_id: string } }) => ({ data: { headers: [], source: `Quelle ${options.path.message_id}`, truncated: false }, response: { status: 200 } })) as never)
  const { router } = setup()
  const first = await openOriginal()
  await waitFor(() => expect(first.dialog.querySelector('pre')?.textContent).toBe('Quelle m1'))
  fireEvent.keyDown(first.dialog, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  await act(async () => { await router.navigate({ to: '/mail/$messageId', params: { messageId: 'm2' } }) })
  await screen.findByRole('heading', { name: 'Zweite' })
  fireEvent.click(screen.getByRole('button', { name: 'Weitere Aktionen' }))
  fireEvent.click(screen.getByRole('menuitem', { name: 'Original anzeigen' }))
  const second = await screen.findByRole('dialog', { name: 'Original der Mail' })
  await waitFor(() => expect(second.querySelector('pre')?.textContent).toBe('Quelle m2'))
  expect(fetchOriginal.mock.calls.map(([options]) => (options as { path: { message_id: string } }).path.message_id)).toEqual(['m1', 'm2'])
})

test('moving to another mail closes the dialog, fetches nothing for that mail and does not reopen on the way back', async () => {
  const fetchOriginal = spyOn(sdk, 'mailMessageOriginal').mockImplementation((async () => ({ error: { detail: { code: 'provider_failed' } }, response: { status: 502 } })) as never)
  const { router } = setup()
  const { dialog } = await openOriginal()
  ;(await within(dialog).findByRole('button', { name: 'Noch mal versuchen' })).focus()
  await act(async () => { await router.navigate({ to: '/mail/$messageId', params: { messageId: 'm2' } }) })
  await screen.findByRole('heading', { name: 'Zweite' })
  expect(screen.queryByRole('dialog')).toBeNull()
  await act(async () => { await router.navigate({ to: '/mail/$messageId', params: { messageId: 'm1' } }) })
  await screen.findByRole('heading', { name: 'Termin' })
  await settle()
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(fetchOriginal.mock.calls.map(([options]) => (options as { path: { message_id: string } }).path.message_id)).toEqual(['m1'])
})

test('where the original cannot be fetched the entry is disabled, explains why and opens nothing', async () => {
  const fetchOriginal = spyOn(sdk, 'mailMessageOriginal')
  mail.can_show_original = false
  try {
    setup(); await screen.findByRole('heading', { name: 'Termin' })
    fireEvent.click(screen.getByRole('button', { name: 'Weitere Aktionen' }))
    const item = screen.getByRole('menuitem', { name: /Original anzeigen/ })
    expect(item.getAttribute('aria-disabled')).toBe('true')
    expect(within(item).getByText('Bei diesem Konto lässt sich das Original nicht abrufen.')).toBeTruthy()
    fireEvent.click(item)
    await settle()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(fetchOriginal).not.toHaveBeenCalled()
  } finally { mail.can_show_original = true }
})

test('other load errors can retry or return to the filtered inbox', async () => {
  const { router, get } = setup({ mobile: true, failed: true, path: '/mail/m1?unread=true' })
  expect(await screen.findByText('Die Mail konnte nicht geladen werden. Versuch es gleich noch mal.')).toBeTruthy()
  expect(get.mock.calls.length).toBe(3)
  fireEvent.click(screen.getByRole('button', { name: 'Noch mal versuchen' }))
  await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(3))
  fireEvent.click(await screen.findByRole('button', { name: 'Zum Posteingang' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/mail'))
  expect(router.state.location.searchStr).toContain('unread=true')
})


const insightData: InsightOut = { points: ['Erster Punkt', '**fett**', 'Dritter Punkt', 'Vierter Punkt'], deadline: '2026-11-20', followups: ['Was ist wichtig?', 'Termin prüfen', 'Dritter Vorschlag'], needs_reply: false, model: 'm', created_at: '' }

test('insight shows loading then up to three literal points and a deadline', async () => {
  let finish!: (value: { data: InsightOut }) => void
  const { view } = setup({ insight: () => new Promise(resolve => { finish = resolve }) })
  const section = await screen.findByRole('region', { name: 'Zusammenfassung von liam-e' })
  expect(section.getAttribute('aria-busy')).toBe('true')
  expect(within(section).getByText('Zusammenfassung wird erstellt …')).toBeTruthy()
  expect(section.querySelectorAll('[aria-hidden="true"] .le-insight-placeholder-line')).toHaveLength(3)
  await act(async () => { finish({ data: insightData }) })
  expect(await within(section).findByText('Erster Punkt')).toBeTruthy()
  expect(within(section).getByText('**fett**')).toBeTruthy()
  expect(section.querySelector('strong')).toBeNull()
  expect(within(section).getByText('Frist Fr., 20.11.')).toBeTruthy()
  expect(within(section).queryByText('Vierter Punkt')).toBeNull()
  expect(within(section).queryByText('Dritter Vorschlag')).toBeNull()
  expect(view.container.querySelector('.le-mail-header')?.nextElementSibling).toBe(section)
  expect(section.nextElementSibling?.classList.contains('le-mail-text')).toBe(true)
})

test('insight error shows quiet fallback without retry', async () => {
  const { getInsight } = setup({ insight: async () => { throw { detail: { code: 'insight_unavailable' }, status: 503 } } })
  expect(await screen.findByText('Das Modell antwortet gerade nicht, deshalb fehlt die Zusammenfassung. Öffne die Mail gleich noch mal.')).toBeTruthy()
  expect(getInsight.mock.calls.length).toBe(1)
})

test('insight followup sends context and disables chips while working', async () => {
  const { request } = setup({ insight: async () => ({ data: insightData }) })
  fireEvent.click(await screen.findByRole('button', { name: 'Was ist wichtig?' }))
  await waitFor(() => expect(request()).toMatchObject({ text: 'Was ist wichtig?', context_message_id: 'm1' }))
  expect(request()?.account_id).toBe('a')
  expect((screen.getByRole('button', { name: 'Termin prüfen' }) as HTMLButtonElement).disabled).toBe(true)
})

test('empty insight renders no box', async () => {
  const { getInsight } = setup()
  await screen.findByRole('heading', { name: 'Termin' })
  await waitFor(() => expect(getInsight.mock.calls.length).toBe(1))
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Zusammenfassung von liam-e' })).toBeNull())
})


test('insight followup opens the conversation when the turn starts', async () => {
  const { router, request } = setup({ stream: true, insight: async () => ({ data: insightData }) })
  fireEvent.click(await screen.findByRole('button', { name: 'Was ist wichtig?' }))
  await waitFor(() => expect(request()).toMatchObject({ text: 'Was ist wichtig?', context_message_id: 'm1' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/chat/c1'))
})


test('an excluded mail cannot start a reply from the reader', async () => {
  const { requests } = setup()
  spyOn(sdk, 'chatSettingsGet').mockResolvedValue({ data: { version: 4, account_ids: [] } } as never)
  fireEvent.click(await screen.findByRole('button', { name: 'Antwort entwerfen' }))
  expect(await screen.findByText(de.errors.chat_account_not_granted)).toBeTruthy()
  expect(requests).toHaveLength(0)
})

test('reading Gmail Studio mail leaves UNREAD untouched and disables label actions', async () => {
  const { action } = setup({ unread: true, limited: true })
  const notice = 'Bei Gmail in Mistral Studio kannst du Mails nicht archivieren, in den Papierkorb legen, als Spam markieren oder als gelesen oder ungelesen markieren.'
  await screen.findByText(notice)
  expect(screen.queryByText('Gmail in Mistral Studio unterstützt diese Mail-Aktion nicht.')).toBeNull()
  await settle()
  expect(action).not.toHaveBeenCalled()
  const controls = screen.getByRole('button', { name: 'Weitere Aktionen' })
  fireEvent.click(controls)
  expect((await screen.findByRole('menuitem', { name: /Als Spam markieren/ })).getAttribute('aria-disabled')).toBe('true')
})
