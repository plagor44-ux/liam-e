// i18n-exempt: synthetic API fixtures and route paths.
import { afterEach, expect, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from '@tanstack/react-router'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { client as api } from '../api/generated/client.gen'
import { Settings } from '../prompts/Settings'
import { de } from '../i18n/de'
import { CalendarSettings } from './CalendarSettings'
import { ChatAccountSettings } from './ChatAccountSettings'
import { SwipeSettings } from './SwipeSettings'
import { useSwipeSettings } from './useSwipeSettings'

const originalFetch = globalThis.fetch
const originalConfig = api.getConfig()
const confirmed = { swipe_left: 'archive', swipe_right: 'toggle_read', revision: 1, updated_at: '2026-01-01T00:00:00Z' }
let requests: { path: string; method: string; body: Record<string, unknown> }[]
function setup(path: string, respond?: (request: Request) => Promise<Response | undefined>) {
  requests = []
  api.setConfig({ baseUrl: 'https://api.example.test' })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init)
    const url = new URL(request.url)
    requests.push({ path: url.pathname, method: request.method, body: request.method === 'PATCH' ? await request.clone().json() as Record<string, unknown> : {} })
    const custom = await respond?.(request)
    if (custom) return custom
    return Response.json(url.pathname.endsWith('/settings/ui') ? confirmed : {})
  }) as typeof fetch
  const query = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const root = createRootRoute({ component: () => <QueryClientProvider client={query}><Outlet /></QueryClientProvider> })
  const settings = createRoute({ getParentRoute: () => root, path: '/settings', component: Settings })
  const chat = createRoute({ getParentRoute: () => settings, path: '/chat', component: ChatAccountSettings })
  const calendar = createRoute({ getParentRoute: () => settings, path: '/calendar', component: CalendarSettings })
  const controls = createRoute({ getParentRoute: () => settings, path: '/controls', component: SwipeSettings })
  const router = createRouter({ routeTree: root.addChildren([settings.addChildren([controls, chat, calendar])]), history: createMemoryHistory({ initialEntries: [path] }) })
  render(<RouterProvider router={router} />)
  return { query, router }
}
afterEach(() => { cleanup(); globalThis.fetch = originalFetch; api.setConfig(originalConfig) })

test('the controls section link is marked as the current page', async () => {
  setup('/settings/controls')
  expect((await screen.findByRole('link', { name: 'Bedienung' })).getAttribute('aria-current')).toBe('page')
})

test('the loading state precedes the confirmed assignment', async () => {
  let answer!: (value: Response) => void
  setup('/settings/controls', async () => await new Promise<Response>(resolve => { answer = resolve }))
  expect((await screen.findByRole('status')).textContent).toBe('Einstellungen werden geladen …')
  expect(requests).toHaveLength(1)
  await act(async () => { answer(Response.json(confirmed)) })
  expect((await screen.findByRole('button', { name: 'Nach links wischen' })).textContent).toContain('Archivieren')
})

test('the confirmed assignment shows its consequence for each direction', async () => {
  setup('/settings/controls')
  expect((await screen.findByRole('button', { name: 'Nach links wischen' })).textContent).toContain('Archivieren')
  expect(screen.getByRole('button', { name: 'Nach rechts wischen' }).textContent).toContain('Gelesen/Ungelesen')
  expect(screen.getByText('Die Mail verlässt den Posteingang und bleibt im Konto.')).toBeTruthy()
  expect(screen.getByText('Die Mail wechselt zwischen gelesen und ungelesen.')).toBeTruthy()
})

test('saving reports success only after the server accepted the choice', async () => {
  let answer!: (value: Response) => void
  setup('/settings/controls', async request => request.method === 'PATCH' ? await new Promise<Response>(resolve => { answer = resolve }) : undefined)
  fireEvent.click(await screen.findByRole('button', { name: 'Nach links wischen' }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'In den Papierkorb' }))
  expect((await screen.findByRole('button', { name: 'Nach links wischen' })).textContent).toContain('In den Papierkorb')
  expect(screen.getByText('Die Mail kommt in den Papierkorb und lässt sich von dort wiederherstellen.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  await waitFor(() => expect(requests.find(item => item.method === 'PATCH')?.body).toEqual({ expected_revision: 1, swipe_left: 'trash', swipe_right: 'toggle_read' }))
  expect(screen.queryByText('Gespeichert.')).toBeNull()
  await act(async () => { answer(Response.json({ ...confirmed, swipe_left: 'trash', revision: 2 })) })
  expect(await screen.findByText('Gespeichert.')).toBeTruthy()
})

test('a conflict keeps the inputs, names the competing change and never retries on its own', async () => {
  let stored = confirmed
  setup('/settings/controls', async request => {
    if (request.method === 'PATCH') {
      stored = { swipe_left: 'none', swipe_right: 'trash', revision: 2, updated_at: '2026-01-02T00:00:00Z' }
      return Response.json({ detail: { code: 'ui_settings_conflict', message: 'Konflikt' } }, { status: 409 })
    }
    return request.url.endsWith('/settings/ui') ? Response.json(stored) : undefined
  })
  fireEvent.click(await screen.findByRole('button', { name: 'Nach links wischen' }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Keine Aktion' }))
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  const notice = await screen.findByRole('alert')
  expect(notice.textContent).toContain('„Keine Aktion“ nach links und „In den Papierkorb“ nach rechts')
  expect((screen.getByRole('button', { name: 'Nach links wischen' })).textContent).toContain('Keine Aktion')
  expect(requests.filter(item => item.method === 'PATCH')).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Neu laden' }))
  await waitFor(() => expect((screen.getByRole('button', { name: 'Nach links wischen' })).textContent).toContain('Keine Aktion'))
  expect(screen.queryByRole('button', { name: 'Auswahl speichern' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Nach links wischen' }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Archivieren' }))
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  await waitFor(() => expect(requests.filter(item => item.method === 'PATCH')).toHaveLength(2))
  expect(requests.filter(item => item.method === 'PATCH')[1]?.body).toEqual({ expected_revision: 2, swipe_left: 'archive', swipe_right: 'trash' })
})

test('saving again after a conflict consciously replaces the assignment the notice named', async () => {
  let stored = confirmed
  setup('/settings/controls', async request => {
    if (request.method === 'PATCH') {
      const body = await request.clone().json() as { expected_revision: number }
      if (body.expected_revision !== stored.revision) return Response.json({ detail: { code: 'ui_settings_conflict', message: 'Konflikt' } }, { status: 409 })
      return Response.json({ ...stored, swipe_left: 'none', revision: stored.revision + 1 })
    }
    if (request.url.endsWith('/settings/ui')) {
      const current = stored
      stored = { swipe_left: 'trash', swipe_right: 'none', revision: 2, updated_at: '2026-01-02T00:00:00Z' }
      return Response.json(current)
    }
    return undefined
  })
  fireEvent.click(await screen.findByRole('button', { name: 'Nach links wischen' }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Keine Aktion' }))
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  expect((await screen.findByRole('alert')).textContent).toContain('„In den Papierkorb“ nach links und „Keine Aktion“ nach rechts')
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  await waitFor(() => expect(requests.filter(item => item.method === 'PATCH')).toHaveLength(2))
  expect(requests.filter(item => item.method === 'PATCH')[1]?.body).toEqual({ expected_revision: 2, swipe_left: 'none', swipe_right: 'toggle_read' })
  expect(await screen.findByText('Gespeichert.')).toBeTruthy()
})

test('a further change while a conflict is shown ends in another conflict, never an unseen overwrite', async () => {
  let stored = confirmed
  const { query } = setup('/settings/controls', async request => {
    if (request.method === 'PATCH') {
      // Another device saved just before the first attempt.
      if (stored.revision === 1) stored = { ...confirmed, swipe_right: 'none', revision: 2 }
      const body = await request.clone().json() as { expected_revision: number }
      if (body.expected_revision === stored.revision) return Response.json({ ...stored, revision: stored.revision + 1 })
      return Response.json({ detail: { code: 'ui_settings_conflict', message: 'Konflikt' } }, { status: 409 })
    }
    return request.url.endsWith('/settings/ui') ? Response.json(stored) : undefined
  })
  fireEvent.click(await screen.findByRole('button', { name: 'Nach links wischen' }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'In den Papierkorb' }))
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  expect((await screen.findByRole('alert')).textContent).toContain('„Archivieren“ nach links und „Keine Aktion“ nach rechts')
  stored = { ...confirmed, swipe_left: 'none', swipe_right: 'trash', revision: 3 }
  await act(async () => { await query.refetchQueries({ queryKey: ['ui-settings'] }) })
  expect(screen.getByRole('alert').textContent).toContain('„Archivieren“ nach links und „Keine Aktion“ nach rechts')
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('„Keine Aktion“ nach links und „In den Papierkorb“ nach rechts'))
  expect(requests.filter(item => item.method === 'PATCH').map(item => item.body.expected_revision)).toEqual([1, 2])
})

test('a background refetch never moves the revision an edit was based on', async () => {
  let stored = confirmed
  const { query } = setup('/settings/controls', async request => {
    if (request.method === 'PATCH') {
      const body = await request.clone().json() as { expected_revision: number }
      return body.expected_revision === stored.revision ? Response.json({ ...stored, revision: stored.revision + 1 })
        : Response.json({ detail: { code: 'ui_settings_conflict', message: 'Konflikt' } }, { status: 409 })
    }
    return request.url.endsWith('/settings/ui') ? Response.json(stored) : undefined
  })
  fireEvent.click(await screen.findByRole('button', { name: 'Nach links wischen' }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'In den Papierkorb' }))
  stored = { ...confirmed, swipe_right: 'none', revision: 2 }
  await act(async () => { await query.refetchQueries({ queryKey: ['ui-settings'] }) })
  expect(screen.getByRole('button', { name: 'Nach links wischen' }).textContent).toContain('In den Papierkorb')
  expect(screen.getByRole('button', { name: 'Nach rechts wischen' }).textContent).toContain('Gelesen/Ungelesen')
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  expect((await screen.findByRole('alert')).textContent).toContain('„Archivieren“ nach links und „Keine Aktion“ nach rechts')
  expect(requests.filter(item => item.method === 'PATCH').map(item => item.body.expected_revision)).toEqual([1])
})

test('an untouched form follows a newer assignment from another device', async () => {
  let stored = confirmed
  const { query } = setup('/settings/controls', async request => request.method === 'GET' && request.url.endsWith('/settings/ui') ? Response.json(stored) : undefined)
  expect((await screen.findByRole('button', { name: 'Nach rechts wischen' })).textContent).toContain('Gelesen/Ungelesen')
  stored = { ...confirmed, swipe_right: 'none', revision: 2 }
  await act(async () => { await query.refetchQueries({ queryKey: ['ui-settings'] }) })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Nach rechts wischen' }).textContent).toContain('Keine Aktion'))
  fireEvent.click(screen.getByRole('button', { name: 'Standard wiederherstellen' }))
  await waitFor(() => expect(requests.find(item => item.method === 'PATCH')?.body).toEqual({ expected_revision: 2, swipe_left: 'archive', swipe_right: 'toggle_read' }))
})

test('the choices stay locked while a save is pending', async () => {
  let answer!: (value: Response) => void
  setup('/settings/controls', async request => request.method === 'PATCH' ? await new Promise<Response>(resolve => { answer = resolve }) : undefined)
  fireEvent.click(await screen.findByRole('button', { name: 'Nach links wischen' }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'In den Papierkorb' }))
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  await waitFor(() => expect((screen.getByRole('button', { name: 'Nach rechts wischen' }) as HTMLButtonElement).disabled).toBe(true))
  expect((screen.getByRole('button', { name: 'Nach links wischen' }) as HTMLButtonElement).disabled).toBe(true)
  await act(async () => { answer(Response.json({ ...confirmed, swipe_left: 'trash', revision: 2 })) })
  expect(await screen.findByText('Gespeichert.')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Nach links wischen' }) as HTMLButtonElement).disabled).toBe(false)
  expect(screen.getByRole('button', { name: 'Nach links wischen' }).textContent).toContain('In den Papierkorb')
})

test('a failed save keeps the selection editable', async () => {
  setup('/settings/controls', async request => request.method === 'PATCH'
    ? Response.json({ detail: { code: 'settings_unavailable', message: 'Nicht erreichbar' } }, { status: 503 }) : undefined)
  fireEvent.click(await screen.findByRole('button', { name: 'Nach rechts wischen' }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Keine Aktion' }))
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  expect((await screen.findByRole('alert')).textContent).toContain('Die Änderung wurde nicht gespeichert. Deine Auswahl bleibt erhalten.')
  expect((screen.getByRole('button', { name: 'Nach rechts wischen' })).textContent).toContain('Keine Aktion')
})

test('restoring the defaults saves both directions with the current revision', async () => {
  const stored = { ...confirmed, swipe_left: 'trash', swipe_right: 'none', revision: 4 }
  setup('/settings/controls', async request => request.url.endsWith('/settings/ui') ? Response.json(stored) : undefined)
  fireEvent.click(await screen.findByRole('button', { name: 'Standard wiederherstellen' }))
  await waitFor(() => expect(requests.find(item => item.method === 'PATCH')?.body).toEqual({ expected_revision: 4, swipe_left: 'archive', swipe_right: 'toggle_read' }))
})

function Probe() {
  const state = useSwipeSettings()
  return <>
    <p data-testid="state">{`${state.status}|${state.assignment?.left ?? ''}|${state.assignment?.right ?? ''}|${state.revision ?? ''}`}</p>
    <button type="button" onClick={() => state.retry()}>Wiederholen</button>
  </>
}

function setupHook(respond: (request: Request) => Promise<Response | undefined>) {
  const query = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  api.setConfig({ baseUrl: 'https://api.example.test' })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init)
    const custom = await respond(request)
    return custom ?? Response.json(confirmed)
  }) as typeof fetch
  render(<QueryClientProvider client={query}><Probe /></QueryClientProvider>)
  return query
}

test('the hook keeps gestures off until the server confirmed an assignment', async () => {
  let answer!: (value: Response) => void
  setupHook(async () => await new Promise<Response>(resolve => { answer = resolve }))
  expect((await screen.findByTestId('state')).textContent).toBe('loading|||')
  await act(async () => { answer(Response.json(confirmed)) })
  expect(await screen.findByText('ready|archive|toggle_read|1')).toBeTruthy()
})

test('the hook reports a load failure with a retry that recovers', async () => {
  let failing = true
  setupHook(async () => failing ? Response.json({}, { status: 503 }) : undefined)
  expect(await screen.findByText('error|||')).toBeTruthy()
  failing = false
  fireEvent.click(screen.getByRole('button', { name: 'Wiederholen' }))
  expect(await screen.findByText('ready|archive|toggle_read|1')).toBeTruthy()
})

test('a failed refetch after a confirmed assignment turns gestures off until a retry succeeds', async () => {
  let failing = false
  const query = setupHook(async () => failing ? Response.json({}, { status: 503 }) : undefined)
  expect(await screen.findByText('ready|archive|toggle_read|1')).toBeTruthy()
  failing = true
  await act(async () => { await query.refetchQueries({ queryKey: ['ui-settings'] }) })
  expect(await screen.findByText('error|||')).toBeTruthy()
  failing = false
  fireEvent.click(screen.getByRole('button', { name: 'Wiederholen' }))
  expect(await screen.findByText('ready|archive|toggle_read|1')).toBeTruthy()
})


test('a delayed swipe save cannot replace a newer language already read from the server', async () => {
  const defaults = { ...confirmed, locale: 'browser' }
  let stored = defaults
  let acceptSwipe!: (value: Response) => void
  const { query } = setup('/settings/controls', async request => {
    if (request.method === 'PATCH') return new Promise(resolve => { acceptSwipe = resolve })
    return request.url.endsWith('/settings/ui') ? Response.json(stored) : undefined
  })

  fireEvent.click(await screen.findByRole('button', { name: 'Nach links wischen' }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'In den Papierkorb' }))
  fireEvent.click(screen.getByRole('button', { name: 'Auswahl speichern' }))
  await waitFor(() => expect(acceptSwipe).toBeDefined())
  stored = { ...defaults, swipe_left: 'trash', revision: 3, locale: 'en' }
  await act(async () => { await query.refetchQueries({ queryKey: ['ui-settings'] }) })
  await act(async () => { acceptSwipe(Response.json({ ...defaults, swipe_left: 'trash', revision: 2 })) })

  expect(await screen.findByText('Gespeichert.')).toBeTruthy()
  expect(query.getQueryData(['ui-settings'])).toEqual(stored)
})


const chatAccounts = [
  { id: 'work', display_name: 'Work', email_address: 'work@example.test', is_active: true, chat_eligibility: 'eligible' },
  { id: 'home', display_name: '', email_address: 'home@example.test', is_active: true, chat_eligibility: 'eligible' },
  { id: 'inactive', display_name: 'Inactive', email_address: 'inactive@example.test', is_active: false },
  { id: 'studio', display_name: 'Studio inbox', email_address: 'gmail@connector.mistral', is_active: true, chat_eligibility: 'inbox_only' },
  { id: 'pending', display_name: 'Pending', email_address: 'pending@example.test', is_active: true, chat_eligibility: 'awaiting_verification' },
]

test('chat grants persist across page loads and the last checkbox can be cleared', async () => {
  let grant = { version: 1, account_ids: [] as string[] }
  async function respond(request: Request) {
    if (new URL(request.url).pathname.endsWith('/mail/accounts')) return Response.json(chatAccounts)
    if (request.method === 'PATCH') {
      const body = await request.json() as { expected_version: number; account_ids: string[] }
      expect(body.expected_version).toBe(grant.version)
      grant = { version: grant.version + 1, account_ids: body.account_ids }
    }
    return Response.json(grant)
  }
  setup('/settings/chat', respond)
  expect((await screen.findByRole('checkbox', { name: /Work/ }) as HTMLInputElement).checked).toBe(false)
  expect((screen.getByRole('checkbox', { name: /Studio inbox/ }) as HTMLInputElement).disabled).toBe(true)
  expect(screen.getByText(de.chatAccounts.inboxOnly)).toBeTruthy()
  expect((screen.getByRole('checkbox', { name: /Pending/ }) as HTMLInputElement).disabled).toBe(true)
  expect(screen.getByText(de.chatAccounts.awaitingVerification)).toBeTruthy()
  expect(screen.queryByRole('checkbox', { name: /Inactive/ })).toBeNull()
  fireEvent.click(screen.getByRole('checkbox', { name: /Work/ }))
  fireEvent.click(screen.getByRole('checkbox', { name: /home@example/ }))
  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.save }))
  await screen.findByText(de.settingsUi.saved)
  expect(grant.account_ids).toEqual(['work', 'home'])
  cleanup()
  setup('/settings/chat', respond)
  for (const name of [/Work/, /home@example/]) {
    const checkbox = await screen.findByRole('checkbox', { name }) as HTMLInputElement
    expect(checkbox.checked).toBe(true)
    fireEvent.click(checkbox)
  }
  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.save }))
  await screen.findByText(de.settingsUi.saved)
  expect(grant).toEqual({ version: 3, account_ids: [] })
})

test('a stale chat grant reloads the other device’s selection before allowing another edit', async () => {
  let changed = false
  setup('/settings/chat', async request => {
    if (new URL(request.url).pathname.endsWith('/mail/accounts')) return Response.json(chatAccounts)
    if (request.method === 'PATCH') {
      changed = true
      return Response.json({ detail: { code: 'version_conflict' } }, { status: 409 })
    }
    return Response.json(changed ? { version: 2, account_ids: ['home'] } : { version: 1, account_ids: [] })
  })
  fireEvent.click(await screen.findByRole('checkbox', { name: /Work/ }))
  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.save }))
  await screen.findByText(de.chatAccounts.conflict)
  expect((screen.getByRole('button', { name: de.settingsUi.save }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.reload }))
  await waitFor(() => expect((screen.getByRole('checkbox', { name: /home@example/ }) as HTMLInputElement).checked).toBe(true))
  expect((screen.getByRole('checkbox', { name: /Work/ }) as HTMLInputElement).checked).toBe(false)
  expect(requests.filter(item => item.method === 'PATCH')).toHaveLength(1)
})

test('a previous API response without eligibility cannot clear a stored chat grant', async () => {
  setup('/settings/chat', async request => new URL(request.url).pathname.endsWith('/mail/accounts')
    ? Response.json([{ id: 'work', display_name: 'Work', is_active: true }])
    : Response.json({ version: 3, account_ids: ['work'] }))
  expect(await screen.findByText(de.chatAccounts.eligibilityUnavailable)).toBeTruthy()
  fireEvent.click(screen.getByRole('checkbox', { name: /Work/ }))
  expect((screen.getByRole('button', { name: de.settingsUi.save }) as HTMLButtonElement).disabled).toBe(true)
  expect(requests.filter(item => item.method === 'PATCH')).toHaveLength(0)
})

test('editing a visible account preserves a newly granted account omitted from a cached list', async () => {
  setup('/settings/chat', async request => {
    if (new URL(request.url).pathname.endsWith('/mail/accounts')) return Response.json([chatAccounts[0]])
    if (request.method === 'PATCH') return Response.json({ version: 8, account_ids: ['elsewhere', 'work'] })
    return Response.json({ version: 7, account_ids: ['elsewhere'] })
  })
  const visible = await screen.findByRole('checkbox', { name: /Work/ })
  expect(screen.queryByRole('button', { name: de.settingsUi.save })).toBeNull()
  fireEvent.click(visible)
  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.save }))
  await screen.findByText(de.settingsUi.saved)
  expect(requests.find(item => item.method === 'PATCH')?.body).toEqual({ expected_version: 7, account_ids: ['elsewhere', 'work'] })
})

const calendarOptions = [
  { id: 'work', name: 'Team', is_active: true, chat_access: 'writable' },
  { id: 'personal', name: 'Privat', is_active: true, chat_access: 'writable' },
  { id: 'feed', name: 'Ferien', is_active: true, chat_access: 'read_only' },
  { id: 'old', name: 'Veraltet', is_active: false, chat_access: 'paused' },
]

function calendarForm(initial: string | null = null, sources = calendarOptions) {
  let stored = { version: 1, default_source_id: initial }
  const view = setup('/settings/calendar', async request => {
    if (request.url.endsWith('/calendar/sources')) return Response.json(sources)
    if (!request.url.endsWith('/settings/calendar')) return undefined
    if (request.method === 'PATCH') {
      const body = await request.json() as { expected_version: number; default_source_id: string | null }
      if (body.expected_version !== stored.version) return Response.json({ detail: { code: 'version_conflict' } }, { status: 409 })
      stored = { version: stored.version + 1, default_source_id: body.default_source_id }
    }
    return Response.json(stored)
  })
  return { ...view, changeElsewhere: () => { stored = { version: stored.version + 1, default_source_id: 'personal' } } }
}

test('the calendar default can be set and cleared while read-only and paused choices explain their status', async () => {
  const { query } = calendarForm()
  const select = await screen.findByRole<HTMLSelectElement>('combobox', { name: de.calendarSettings.title })
  expect(select.value).toBe('')
  for (const label of ['Ferien · Nur lesbar', 'Veraltet · Pausiert']) {
    expect((screen.getByRole('option', { name: label }) as HTMLOptionElement).disabled).toBe(true)
  }
  fireEvent.change(select, { target: { value: 'work' } })
  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.save }))
  await screen.findByText(de.settingsUi.saved)
  await act(() => query.refetchQueries({ queryKey: ['calendar-settings'] }))
  expect(select.value).toBe('work')
  fireEvent.change(select, { target: { value: '' } })
  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.save }))
  await screen.findByText(de.settingsUi.saved)
  expect(requests.filter(r => r.method === 'PATCH').map(r => r.body)).toEqual([
    { default_source_id: 'work', expected_version: 1 }, { default_source_id: null, expected_version: 2 },
  ])
})

test('a conflicting calendar edit can reload the other device’s default', async () => {
  const { changeElsewhere } = calendarForm()
  const select = await screen.findByRole<HTMLSelectElement>('combobox', { name: de.calendarSettings.title })
  fireEvent.change(select, { target: { value: 'work' } })
  changeElsewhere()
  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.save }))
  expect(await screen.findByText(de.calendarSettings.conflict)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.reload }))
  await waitFor(() => expect(select.value).toBe('personal'))
  expect(requests.filter(r => r.method === 'PATCH')).toHaveLength(1)
})

test('a removed default stays visible until the owner changes it', async () => {
  calendarForm('removed')
  const select = await screen.findByRole<HTMLSelectElement>('combobox', { name: de.calendarSettings.title })
  expect(select.selectedOptions[0]?.textContent).toBe(de.calendarSettings.missing)
  expect(screen.queryByRole('button', { name: de.settingsUi.save })).toBeNull()
  expect(requests.filter(r => r.method === 'PATCH')).toHaveLength(0)
})

test('leaving a settings page with unsaved changes warns and lets the owner stay or discard', async () => {
  const { router } = setup('/settings/controls')
  fireEvent.click(await screen.findByRole('button', { name: 'Nach links wischen' }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'In den Papierkorb' }))
  expect(screen.getByRole('region', { name: de.settingsUi.unsaved })).toBeTruthy()

  fireEvent.click(screen.getByRole('link', { name: 'Chat' }))
  const dialog = await screen.findByRole('dialog', { name: de.settingsUi.leaveTitle })
  expect(dialog.textContent).toContain(de.settingsUi.leaveDescription)
  expect(router.state.location.pathname).toBe('/settings/controls')

  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.keepEditing }))
  await waitFor(() => expect(screen.queryByRole('dialog', { name: de.settingsUi.leaveTitle })).toBeNull())
  expect(router.state.location.pathname).toBe('/settings/controls')
  expect(screen.getByRole('button', { name: 'Nach links wischen' }).textContent).toContain('In den Papierkorb')

  fireEvent.click(screen.getByRole('link', { name: 'Chat' }))
  await screen.findByRole('dialog', { name: de.settingsUi.leaveTitle })
  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.discardAndLeave }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/settings/chat'))
  expect(screen.queryByRole('region', { name: de.settingsUi.unsaved })).toBeNull()
})

test('a paused or read-only saved default displays an explanatory notice', async () => {
  calendarForm('old')
  expect(await screen.findByText(de.calendarSettings.defaultUnavailable('Veraltet', de.calendarSettings.reason.paused))).toBeTruthy()
})

test('the owner can select the second calendar with the same name by its stable identifier', async () => {
  const twin = { ...calendarOptions[0]!, id: 'second-team' }
  calendarForm(null, [calendarOptions[0]!, twin])
  const select = await screen.findByRole<HTMLSelectElement>('combobox', { name: de.calendarSettings.title })
  expect(screen.getByRole<HTMLOptionElement>('option', { name: 'Team · work' }).value).toBe('work')
  const target = screen.getByRole<HTMLOptionElement>('option', { name: 'Team · second-team' })
  fireEvent.change(select, { target: { value: target.value } })
  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.save }))
  await screen.findByText(de.settingsUi.saved)
  expect(select.selectedOptions[0]?.textContent).toBe('Team · second-team')
  expect(requests.filter(r => r.method === 'PATCH').map(r => r.body)).toEqual([{ default_source_id: 'second-team', expected_version: 1 }])
})
