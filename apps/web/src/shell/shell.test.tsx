import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider, QueryObserver } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import { ModelMenu } from '../chat/ModelMenu'
import { Shell, resetShellSyncForTest } from '../routes/_app'

let width = 1200
window.matchMedia = ((query: string) => ({ matches: query.includes('640') ? width >= 640 : query.includes('1024') ? width >= 1024 : query.includes('1440') ? width >= 1440 : false, media: query, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
const summary = { accounts: Array.from({ length: 4 }, () => ({ state: 'ok' })), unread_total: 47, last_sync_at: '2026-01-01T08:57:00' }
const conversations = [{ id: 'one', title: 'Erste Unterhaltung', updated_at: new Date().toISOString() }, { id: 'two', title: 'Zweite Unterhaltung', updated_at: new Date().toISOString() }]
function setup(path = '/', client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } }), showModel = false) {
  client.setQueryData(['summary'], summary)
  client.setQueryData(['conversations'], conversations)
  const root = createRootRoute({ component: Shell })
  const model = () => showModel ? <ModelMenu model="small" /> : null
  const index = createRoute({ getParentRoute: () => root, path: '/', component: model })
  const chat = createRoute({ getParentRoute: () => root, path: '/chat/$conversationId', component: model })
  const mail = createRoute({ getParentRoute: () => root, path: '/mail', component: () => null })
  const router = createRouter({ routeTree: root.addChildren([index, chat, mail]), history: createMemoryHistory({ initialEntries: [path] }) })
  return render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)
}
afterEach(() => { cleanup(); mock.restore(); resetShellSyncForTest() })
test('mobile header names the selected app folder', async () => {
  width = 375
  spyOn(sdk, 'mailSyncAll').mockResolvedValue({ data: {} } as never)
  spyOn(sdk, 'mailFoldersList').mockResolvedValue({ data: [{ id: 'folder', name: 'Projects', kind: 'folder' }] } as never)
  setup('/mail?app_folder=folder')
  expect(await screen.findByText('Ordner: Projects')).toBeTruthy()
})
test('desktop navigation shows summary and conversations and syncs once across mounts', async () => {
  width = 1200
  const sync = spyOn(sdk, 'mailSyncAll').mockImplementation((async () => ({ data: {} })) as never)
  spyOn(sdk, 'mailSummary').mockImplementation((async () => ({ data: summary })) as never)
  const first = setup()
  expect(await screen.findByText('47')).toBeTruthy()
  expect(screen.getByRole('link', { name: /Erste Unterhaltung/ }).getAttribute('href')).toBe('/chat/one')
  expect(screen.getByRole('link', { name: /Zweite Unterhaltung/ })).toBeTruthy()
  const status = screen.getByText('4 Konten · Stand 08:57')
  const dot = status.querySelector('.le-dot.le-dot--accent')
  expect(dot?.getAttribute('aria-hidden')).toBe('true')
  expect(status.firstElementChild).toBe(dot)
  first.unmount()
  setup()
  await screen.findByText('47')
  expect(sync).toHaveBeenCalledTimes(1)
  expect(sync).toHaveBeenCalledWith({ body: { only_if_older_than_minutes: 10 } })
})
test('refreshes active messages after sync finishes or the last sync advances', async () => {
  width = 1200
  spyOn(sdk, 'mailSyncAll').mockImplementation((async () => ({ error: {} })) as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const fetchMessages = mock(async () => [])
  const observer = new QueryObserver(client, { queryKey: ['messages', {}], queryFn: fetchMessages, staleTime: Infinity })
  const unsubscribe = observer.subscribe(() => {})
  setup('/', client)
  await screen.findByText('47')
  await waitFor(() => expect(fetchMessages).toHaveBeenCalledTimes(1))
  act(() => { client.setQueryData(['summary'], { ...summary, accounts: [{ state: 'syncing' }] }) })
  await screen.findByText('1 Konto · Stand 08:57')
  act(() => { client.setQueryData(['summary'], { ...summary, last_sync_at: '2026-01-01T09:00:00' }) })
  await waitFor(() => expect(fetchMessages).toHaveBeenCalledTimes(2))
  act(() => { client.setQueryData(['summary'], { ...summary, last_sync_at: '2026-01-01T09:00:00' }) })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
  expect(fetchMessages).toHaveBeenCalledTimes(2)
  act(() => { client.setQueryData(['summary'], { ...summary, last_sync_at: '2026-01-01T09:01:00' }) })
  await waitFor(() => expect(fetchMessages).toHaveBeenCalledTimes(3))
  unsubscribe()
})
test('all twelve conversations appear as links in the sidebar', async () => {
  width = 1200
  spyOn(sdk, 'mailSyncAll').mockImplementation((async () => ({ error: {} })) as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  setup('/', client)
  const entries = Array.from({ length: 12 }, (_, index) => ({ id: `conversation-${index}`, title: `Unterhaltung ${index + 1}`, updated_at: new Date().toISOString() }))
  act(() => { client.setQueryData(['conversations'], entries) })
  // Found by its title alone: the link's name also holds the time, and from 20:00 "Unterhaltung 1" plus "20:02" reads like "Unterhaltung 12".
  const last = (await screen.findByText('Unterhaltung 12')).closest('a')
  expect(last?.getAttribute('href')).toBe('/chat/conversation-11')
  const list = last?.closest('.le-shell-conversations')
  expect(list?.querySelectorAll('a')).toHaveLength(12)
  for (const entry of entries) expect(list?.querySelector(`a[href="/chat/${entry.id}"]`)).toBeTruthy()
})
test('only conversations are inside the sidebar scroll container', async () => {
  width = 1200
  spyOn(sdk, 'mailSyncAll').mockImplementation((async () => ({ data: {} })) as never)
  spyOn(sdk, 'mailSummary').mockImplementation((async () => ({ data: summary })) as never)
  setup()
  const conversation = await screen.findByRole('link', { name: /Erste Unterhaltung/ })
  const list = conversation.closest('.le-shell-conversations')
  expect(list).toBeTruthy()
  expect(list?.contains(screen.getByRole('link', { name: /Zweite Unterhaltung/ }))).toBe(true)
  expect(list?.textContent).toContain('Unterhaltungen')
  for (const name of ['Chat', /Posteingang/, 'Konten']) expect(list?.contains(screen.getByRole('link', { name }))).toBe(false)
  expect(list?.contains(screen.getByText('4 Konten · Stand 08:57'))).toBe(false)
})
test('chat navigation and the open conversation show their current state', async () => {
  width = 1200
  spyOn(sdk, 'mailSyncAll').mockImplementation((async () => ({ data: {} })) as never)
  spyOn(sdk, 'mailSummary').mockImplementation((async () => ({ data: summary })) as never)
  setup('/')
  expect((await screen.findByRole('link', { name: 'Chat' })).getAttribute('aria-current')).toBe('page')
  cleanup()
  setup('/chat/one')
  expect((await screen.findByRole('link', { name: 'Chat' })).getAttribute('aria-current')).toBe('page')
  expect(screen.getByRole('link', { name: /Erste Unterhaltung/ }).getAttribute('aria-current')).toBe('page')
  expect(screen.getByRole('link', { name: /Zweite Unterhaltung/ }).getAttribute('aria-current')).toBeNull()
  cleanup()
  setup('/mail')
  expect((await screen.findByRole('link', { name: 'Chat' })).getAttribute('aria-current')).toBeNull()
  const css = await Bun.file(new URL('../styles/ds.css', import.meta.url)).text()
  const selected = css.match(/\.le-conv\[aria-current="page"\] \{([^}]*)\}/u)?.[1]
  expect(selected).toContain('background: var(--accent-soft)')
  expect(selected).toContain('color: var(--accent)')
})
test.each([375, 800])('drawer close button restores focus at width %i', async (screenWidth) => {
  width = screenWidth
  spyOn(sdk, 'mailSyncAll').mockImplementation((async () => ({ data: {} })) as never)
  spyOn(sdk, 'mailSummary').mockImplementation((async () => ({ data: summary })) as never)
  setup()
  const trigger = await screen.findByRole('button', { name: screenWidth < 640 ? 'Menü öffnen' : 'Unterhaltungen' })
  act(() => { trigger.focus() })
  fireEvent.click(trigger)
  expect(screen.getByRole('dialog', { name: screenWidth < 640 ? 'Menü' : 'Unterhaltungen' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Menü schließen' }))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(document.activeElement).toBe(trigger)
})
test('drawer frame has inset padding, right radii and a coarse close target', async () => {
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  const rule = css.match(/\.le-drawer \{([^}]*)\}/u)?.[1]
  for (const property of ['width: min(308px, 100vw - 48px)', 'padding: var(--space-3)', 'border-radius: 0 var(--radius-lg) var(--radius-lg) 0', 'box-shadow: var(--shadow-overlay)']) expect(rule).toContain(property)
  expect(css).toContain('@media (pointer: coarse) { .le-drawer-brand .le-icon-btn, .le-shell-status .le-icon-btn { min-width: var(--control-height-lg); min-height: var(--control-height-lg); } }')
})
test('drawer stacks its content so the sidebar footer sits at the bottom', async () => {
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  const rule = css.match(/\.le-drawer \{([^}]*)\}/u)?.[1]
  for (const property of ['display: flex', 'flex-direction: column']) expect(rule).toContain(property)
  expect(css).toContain('.le-drawer .le-shell-sidebar { flex: 1 0 auto; }')
  expect(css).toMatch(/\.le-shell-status \{[^}]*margin-top: auto/u)
})
test('mobile menu opens and closes on Escape', async () => {
  width = 375
  spyOn(sdk, 'mailSyncAll').mockImplementation((async () => ({ data: {} })) as never)
  spyOn(sdk, 'mailSummary').mockImplementation((async () => ({ data: summary })) as never)
  setup()
  fireEvent.click(await screen.findByRole('button', { name: 'Menü öffnen' }))
  expect(screen.getByRole('dialog')).toBeTruthy()
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).toBeNull()
})
test('the tablet bar lists the conversations as a navigation entry with its own icon and a tooltip', async () => {
  width = 800
  spyOn(sdk, 'mailSyncAll').mockImplementation((async () => ({ data: {} })) as never)
  spyOn(sdk, 'mailSummary').mockImplementation((async () => ({ data: summary })) as never)
  setup()
  const button = await screen.findByRole('button', { name: 'Unterhaltungen' })
  expect(button.classList.contains('le-nav-item')).toBe(true)
  expect(button.classList.contains('le-icon-btn')).toBe(false)
  expect(button.querySelector('.lucide-messages-square')).toBeTruthy()
  expect(button.querySelector('.lucide-message-circle')).toBeNull()
  expect(screen.getByRole('link', { name: 'Chat' }).querySelector('.lucide-message-circle')).toBeTruthy()
  expect(button.closest('.le-shell-sidebar')?.querySelectorAll('.le-nav-item')).toHaveLength(7)
  expect(screen.getByRole('link', { name: 'Kalender' })).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Aufgaben' })).toBeTruthy()
  fireEvent.mouseEnter(button)
  expect((await screen.findByRole('tooltip', {}, { timeout: 2000 })).textContent).toBe('Öffnet die Liste deiner Unterhaltungen')
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
})
test('the tablet bar shows the unread count on the inbox entry and names it', async () => {
  width = 800
  spyOn(sdk, 'mailSyncAll').mockImplementation((async () => ({ data: {} })) as never)
  spyOn(sdk, 'mailSummary').mockImplementation((async () => ({ data: summary })) as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  setup('/', client)
  const inbox = await screen.findByRole('link', { name: 'Posteingang, 47 ungelesen' })
  const badge = inbox.querySelector('.le-nav-badge')
  expect(badge?.getAttribute('aria-hidden')).toBe('true')
  expect(badge?.textContent).toBe('47')
  act(() => { client.setQueryData(['summary'], { ...summary, unread_total: 1234 }) })
  expect((await screen.findByRole('link', { name: 'Posteingang, 1.234 ungelesen' })).querySelector('.le-nav-badge')?.textContent).toBe('999+')
  act(() => { client.setQueryData(['summary'], { ...summary, unread_total: 0 }) })
  expect((await screen.findByRole('link', { name: 'Posteingang' })).querySelector('.le-nav-badge')).toBeNull()
  const css = await Bun.file(new URL('../styles/ds.css', import.meta.url)).text()
  expect(css).toMatch(/\.le-nav-badge \{[^}]*position: absolute;/u)
  expect(css).toMatch(/\.le-nav-item \{[^}]*position: relative;/u)
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
})
test('the theme switch sits in the sidebar footer, the tablet bar and the phone drawer', async () => {
  spyOn(sdk, 'mailSyncAll').mockImplementation((async () => ({ data: {} })) as never)
  spyOn(sdk, 'mailSummary').mockImplementation((async () => ({ data: summary })) as never)
  width = 1200
  setup()
  const stand = await screen.findByText('4 Konten · Stand 08:57')
  const desktop = screen.getByRole('button', { name: 'Darstellung' })
  expect(stand.closest('.le-shell-status')?.contains(desktop)).toBe(true)
  fireEvent.click(desktop)
  expect(screen.getByRole('menu').classList.contains('le-menu--up-end')).toBe(true)
  cleanup()
  width = 800
  setup()
  fireEvent.click(await screen.findByRole('button', { name: 'Darstellung' }))
  expect(screen.getByRole('menu').classList.contains('le-menu--up-start')).toBe(true)
  expect(screen.getByRole('button', { name: 'Darstellung' }).closest('.le-shell-status--collapsed')).toBeTruthy()
  cleanup()
  width = 375
  setup()
  expect(screen.queryByRole('button', { name: 'Darstellung' })).toBeNull()
  fireEvent.click(await screen.findByRole('button', { name: 'Menü öffnen' }))
  expect(screen.getByRole('dialog').contains(screen.getByRole('button', { name: 'Darstellung' }))).toBe(true)
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
})
test.each(['/', '/chat/one'])('the mobile header on %s puts the model immediately before the new-conversation button', async path => {
  width = 375
  spyOn(sdk, 'mailSyncAll').mockImplementation((async () => ({ data: {} })) as never)
  spyOn(sdk, 'mailSummary').mockImplementation((async () => ({ data: summary })) as never)
  spyOn(sdk, 'chatModelsList').mockResolvedValue({ data: [{ id: 'small', label: 'Small', description: '', is_default: true }] } as never)
  setup(path, undefined, true)
  const menu = await screen.findByRole('button', { name: 'Menü öffnen' })
  const title = menu.closest('header')?.querySelector('.le-crumb')
  expect(title?.textContent).toBe('Chat')
  expect(title?.classList.contains('le-crumb--center')).toBe(true)
  expect(title?.previousElementSibling?.contains(menu)).toBe(true)
  const model = await screen.findByRole('button', { name: 'Small' })
  expect(title?.nextElementSibling?.contains(model)).toBe(true)
  expect(model.closest('.le-mobile-model')?.nextElementSibling?.contains(screen.getByRole('button', { name: 'Neue Unterhaltung' }))).toBe(true)
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  expect(css).toMatch(/\.le-crumb--center \{[^}]*justify-content: center/u)
})
