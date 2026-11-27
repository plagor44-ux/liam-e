// i18n-exempt: synthetic API fixtures and test labels.
import { afterEach, expect, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { client as api } from '../api/generated/client.gen'
import type { AgentAccessOut } from '../api/generated/types.gen'
import { de } from '../i18n/de'
import { en } from '../i18n/en'
import { formatListDate } from '../i18n/format'
import { I18nProvider } from '../i18n/I18n'
import { settingsSections } from '../prompts/Settings'
import { AgentAccess } from './AgentAccess'
import { AGENT_ACCESS_POLL_MS, agentAccessQuery } from './agentAccessApi'
import { SettingsSaveBar, SettingsSaveProvider } from './SettingsSave'

const original = globalThis.fetch
const config = api.getConfig()

const defaultState = (over: Partial<AgentAccessOut> = {}): AgentAccessOut => ({
  enabled: false,
  revision: 1,
  changed_at: null,
  verifier_configured: false,
  owner_bound: true,
  ...over,
})

type RecordedRequest = { method: string; path: string; body: unknown }
let requests: RecordedRequest[]

function setup({
  initial = defaultState(),
  respond,
  locale = 'de',
  pollIntervalMs = AGENT_ACCESS_POLL_MS,
  seedCache,
}: {
  initial?: AgentAccessOut
  respond?: (request: RecordedRequest) => Response | Promise<Response> | undefined
  locale?: 'de' | 'en'
  pollIntervalMs?: number | false
  seedCache?: AgentAccessOut
} = {}) {
  requests = []
  let current = { ...initial }
  api.setConfig({ baseUrl: 'https://api.example.test' })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const url = new URL(req.url)
    const rawText = req.method === 'POST' ? await req.clone().text() : ''
    const request: RecordedRequest = {
      method: req.method,
      path: url.pathname,
      body: rawText ? (JSON.parse(rawText) as unknown) : undefined,
    }
    requests.push(request)
    const custom = await respond?.(request)
    if (custom) return custom
    if (req.method === 'POST' && url.pathname.endsWith('/disable')) {
      // Like the server: every turn-off advances the revision, so a pending turn-on goes stale.
      current = {
        ...current,
        enabled: false,
        revision: current.revision + 1,
        changed_at: current.enabled ? new Date().toISOString() : current.changed_at,
      }
      return Response.json(current)
    }
    if (req.method === 'POST' && url.pathname.endsWith('/enable')) {
      current = {
        ...current,
        enabled: true,
        revision: current.revision + 1,
        changed_at: new Date().toISOString(),
      }
      return Response.json(current)
    }
    return Response.json(current)
  }) as typeof fetch

  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: seedCache ? 60_000 : 0 } },
  })
  if (seedCache) client.setQueryData(agentAccessQuery().queryKey, seedCache)
  render(
    <I18nProvider locale={locale}>
      <QueryClientProvider client={client}>
        <SettingsSaveProvider>
          <AgentAccess pollIntervalMs={pollIntervalMs} />
          <SettingsSaveBar />
        </SettingsSaveProvider>
      </QueryClientProvider>
    </I18nProvider>,
  )
  return { client, setState: (next: AgentAccessOut) => { current = { ...next } } }
}

const refused = (status: number, code: string) => Response.json({ detail: { code, message: 'test-only' } }, { status })

afterEach(() => {
  cleanup()
  globalThis.fetch = original
  api.setConfig(config)
})

test('settingsSections lists /settings/access directly after /settings/controls', () => {
  const deSections = settingsSections(de)
  const controlsIndex = deSections.findIndex(section => section.to === '/settings/controls')
  expect(controlsIndex).toBeGreaterThanOrEqual(0)
  expect(deSections[controlsIndex + 1]).toEqual({ to: '/settings/access', label: de.agentAccess.section })

  const enSections = settingsSections(en)
  expect(enSections[controlsIndex + 1]).toEqual({ to: '/settings/access', label: en.agentAccess.section })
})

test('shows "Aus" by default with explanation, limits note, always-reachable off button, and no save bar', async () => {
  setup()
  expect(await screen.findByText(de.agentAccess.statusOff)).toBeTruthy()
  expect(screen.getByText(de.agentAccess.description)).toBeTruthy()
  expect(screen.getByText(de.agentAccess.limits)).toBeTruthy()
  const turnOn = screen.getByRole('button', { name: de.agentAccess.turnOn })
  const turnOff = screen.getByRole('button', { name: de.agentAccess.turnOff })
  expect(turnOn.className).toContain('le-btn--primary')
  expect(turnOff.className).toContain('le-btn--secondary')
  // Negative cases: no connection notice while off, no shared save bar.
  expect(screen.queryByText(de.agentAccess.noConnection)).toBeNull()
  expect(screen.queryByText(de.settingsUi.unsaved)).toBeNull()
})

test('off action stays reachable and works in the off state without confirmation, advancing revision', async () => {
  setup({ initial: defaultState({ enabled: false, revision: 3 }) })
  const turnOff = await screen.findByRole('button', { name: de.agentAccess.turnOff })
  expect((turnOff as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(turnOff)

  expect(screen.queryByRole('dialog')).toBeNull()
  expect(await screen.findByText(de.agentAccess.turnedOff)).toBeTruthy()
  expect(screen.getByText(de.agentAccess.statusOff)).toBeTruthy()
  expect(requests.filter(r => r.method === 'POST')).toEqual([
    { method: 'POST', path: '/api/v1/settings/agent-access/disable', body: undefined },
  ])

  // A subsequent turn-on sends the advanced revision returned by disable.
  fireEvent.click(screen.getByRole('button', { name: de.agentAccess.turnOn }))
  fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: de.agentAccess.turnOn }))
  expect(await screen.findByText(de.agentAccess.turnedOn)).toBeTruthy()
  expect(requests.filter(r => r.method === 'POST').at(-1)).toEqual({
    method: 'POST',
    path: '/api/v1/settings/agent-access/enable',
    body: { expected_revision: 4 },
  })
})

test('query refetches on mount and polls while visible so another device turning access on is shown', async () => {
  const defaults = agentAccessQuery()
  expect(defaults.refetchOnMount).toBe('always')
  expect(defaults.refetchOnWindowFocus).toBe('always')
  expect(defaults.refetchInterval).toBe(AGENT_ACCESS_POLL_MS)
  expect(defaults.refetchIntervalInBackground).toBe(false)

  // Poll picks up a server flip from off to on without user interaction.
  const changedAt = new Date().toISOString()
  const expectedWhen = formatListDate(new Date(changedAt), new Date(), undefined, 'de')
  const { setState } = setup({ initial: defaultState({ enabled: false, revision: 1 }), pollIntervalMs: 25 })
  expect(await screen.findByText(de.agentAccess.statusOff)).toBeTruthy()
  setState(defaultState({ enabled: true, revision: 2, changed_at: changedAt, verifier_configured: false }))
  expect(await screen.findByText(de.agentAccess.statusOnSince(expectedWhen))).toBeTruthy()
  expect(screen.getByRole('button', { name: de.agentAccess.turnOff }).className).toContain('le-btn--primary')
  expect(screen.queryByRole('button', { name: de.agentAccess.turnOn })).toBeNull()

  // Mounting over stale cached "Aus" data immediately refetches the server's "An" state.
  cleanup()
  setup({
    initial: defaultState({ enabled: true, revision: 5, changed_at: changedAt }),
    seedCache: defaultState({ enabled: false, revision: 4 }),
    pollIntervalMs: false,
  })
  expect(await screen.findByText(de.agentAccess.statusOnSince(expectedWhen))).toBeTruthy()
})

test('shows loading placeholder and unavailable error with retry on GET failure', async () => {
  let release = () => {}
  setup({
    respond: request =>
      request.method === 'GET'
        ? new Promise(resolve => { release = () => resolve(Response.json(defaultState())) })
        : undefined,
  })
  expect((await screen.findByRole('status')).textContent).toBe(de.agentAccess.loading)
  await act(async () => { release() })
  expect(await screen.findByText(de.agentAccess.statusOff)).toBeTruthy()

  cleanup()
  let fail = true
  setup({
    respond: request =>
      request.method === 'GET' && fail
        ? refused(503, 'agent_access_unavailable')
        : undefined,
  })
  expect((await screen.findByRole('alert')).textContent).toContain(de.agentAccess.errorUnavailable)
  fail = false
  fireEvent.click(screen.getByRole('button', { name: de.chat.retry }))
  expect(await screen.findByText(de.agentAccess.statusOff)).toBeTruthy()
})

test('when on, shows formatted "An seit …" date and noConnection notice if verifier is not configured', async () => {
  const changedAt = new Date().toISOString()
  const expectedWhen = formatListDate(new Date(changedAt), new Date(), undefined, 'de')
  setup({ initial: defaultState({ enabled: true, revision: 2, changed_at: changedAt, verifier_configured: false }) })
  expect(await screen.findByText(de.agentAccess.statusOnSince(expectedWhen))).toBeTruthy()
  expect(screen.getByText(de.agentAccess.noConnection)).toBeTruthy()
  expect(screen.getByRole('button', { name: de.agentAccess.turnOff })).toBeTruthy()

  cleanup()
  setup({ initial: defaultState({ enabled: true, revision: 2, changed_at: changedAt, verifier_configured: true }) })
  expect(await screen.findByText(de.agentAccess.statusOnSince(expectedWhen))).toBeTruthy()
  expect(screen.queryByText(de.agentAccess.noConnection)).toBeNull()
})

test('turning off acts with one click, no dialog, and shows the notice even when owner is unbound', async () => {
  setup({ initial: defaultState({ enabled: true, revision: 3, changed_at: new Date().toISOString(), owner_bound: false }) })
  const turnOff = await screen.findByRole('button', { name: de.agentAccess.turnOff })
  expect((turnOff as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(turnOff)

  expect(screen.queryByRole('dialog')).toBeNull()
  expect(await screen.findByText(de.agentAccess.turnedOff)).toBeTruthy()
  expect(screen.getByText(de.agentAccess.statusOff)).toBeTruthy()
  expect(requests.filter(r => r.method === 'POST')).toEqual([
    { method: 'POST', path: '/api/v1/settings/agent-access/disable', body: undefined },
  ])
  expect(screen.queryByText(de.settingsUi.unsaved)).toBeNull()
})

test('turning on after turning off sends the revision the turn-off returned', async () => {
  setup({ initial: defaultState({ enabled: true, revision: 6, changed_at: new Date().toISOString() }) })
  fireEvent.click(await screen.findByRole('button', { name: de.agentAccess.turnOff }))
  expect(await screen.findByText(de.agentAccess.turnedOff)).toBeTruthy()

  fireEvent.click(await screen.findByRole('button', { name: de.agentAccess.turnOn }))
  fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: de.agentAccess.turnOn }))
  expect(await screen.findByText(de.agentAccess.turnedOn)).toBeTruthy()
  expect(requests.filter(r => r.method === 'POST')).toEqual([
    { method: 'POST', path: '/api/v1/settings/agent-access/disable', body: undefined },
    { method: 'POST', path: '/api/v1/settings/agent-access/enable', body: { expected_revision: 7 } },
  ])
})

test('turning on opens confirmation dialog; "Abbrechen" and Escape send nothing and return focus', async () => {
  setup()
  const turnOn = await screen.findByRole('button', { name: de.agentAccess.turnOn })
  act(() => turnOn.focus())
  fireEvent.click(turnOn)

  const dialog = await screen.findByRole('dialog', { name: de.agentAccess.confirmTitle })
  expect(dialog.textContent).toContain(de.agentAccess.confirmBody)
  expect(dialog.contains(document.activeElement)).toBe(true)

  // Negative case: Cancel sends no POST.
  fireEvent.click(within(dialog).getByRole('button', { name: de.agentAccess.confirmCancel }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(document.activeElement).toBe(turnOn)
  expect(requests.some(r => r.method === 'POST')).toBe(false)

  // Negative case: Escape sends no POST.
  fireEvent.click(turnOn)
  await screen.findByRole('dialog', { name: de.agentAccess.confirmTitle })
  fireEvent.keyDown(document, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(document.activeElement).toBe(turnOn)
  expect(requests.some(r => r.method === 'POST')).toBe(false)
})

test('confirming turn-on sends expected_revision, updates status, and shows notice', async () => {
  setup({ initial: defaultState({ enabled: false, revision: 4 }) })
  fireEvent.click(await screen.findByRole('button', { name: de.agentAccess.turnOn }))
  const dialog = await screen.findByRole('dialog', { name: de.agentAccess.confirmTitle })
  fireEvent.click(within(dialog).getByRole('button', { name: de.agentAccess.turnOn }))

  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(await screen.findByText(de.agentAccess.turnedOn)).toBeTruthy()
  expect(screen.getByRole('button', { name: de.agentAccess.turnOff })).toBeTruthy()
  expect(requests.filter(r => r.method === 'POST')).toEqual([
    { method: 'POST', path: '/api/v1/settings/agent-access/enable', body: { expected_revision: 4 } },
  ])
})

test('maps 409 conflict, 409 owner_unbound, 403 owner error, and 503 unavailable to catalogue texts', async () => {
  // 409 agent_access_conflict (with reload)
  const { setState } = setup({
    respond: request =>
      request.method === 'POST'
        ? refused(409, 'agent_access_conflict')
        : undefined,
  })
  fireEvent.click(await screen.findByRole('button', { name: de.agentAccess.turnOn }))
  fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: de.agentAccess.turnOn }))
  expect((await screen.findByRole('alert')).textContent).toContain(de.agentAccess.errorConflict)
  setState(defaultState({ enabled: true, revision: 5, changed_at: new Date().toISOString() }))
  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.reload }))
  await waitFor(() => expect(screen.queryByRole('button', { name: de.agentAccess.turnOn })).toBeNull())
  expect(screen.getByRole('button', { name: de.agentAccess.turnOff }).className).toContain('le-btn--primary')

  // 409 agent_access_owner_unbound
  cleanup()
  setup({
    respond: request =>
      request.method === 'POST'
        ? refused(409, 'agent_access_owner_unbound')
        : undefined,
  })
  fireEvent.click(await screen.findByRole('button', { name: de.agentAccess.turnOn }))
  fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: de.agentAccess.turnOn }))
  expect((await screen.findByRole('alert')).textContent).toContain(de.agentAccess.errorUnbound)

  // 403 non-owner / wrong origin / x-api-key
  cleanup()
  setup({
    respond: request =>
      request.method === 'POST'
        ? refused(403, 'agent_access_owner_required')
        : undefined,
  })
  fireEvent.click(await screen.findByRole('button', { name: de.agentAccess.turnOn }))
  fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: de.agentAccess.turnOn }))
  expect((await screen.findByRole('alert')).textContent).toContain(de.agentAccess.errorOwner)

  // 503 unavailable on disable
  cleanup()
  setup({
    initial: defaultState({ enabled: true, revision: 2, changed_at: new Date().toISOString() }),
    respond: request =>
      request.method === 'POST'
        ? refused(503, 'agent_access_unavailable')
        : undefined,
  })
  fireEvent.click(await screen.findByRole('button', { name: de.agentAccess.turnOff }))
  expect((await screen.findByRole('alert')).textContent).toContain(de.agentAccess.errorUnavailable)
})

test('when owner_bound is false and switch is off, disables turn-on with explanation while keeping turn-off enabled', async () => {
  setup({ initial: defaultState({ enabled: false, owner_bound: false }) })
  const turnOn = await screen.findByRole('button', { name: de.agentAccess.turnOn })
  const turnOff = screen.getByRole('button', { name: de.agentAccess.turnOff })
  expect((turnOn as HTMLButtonElement).disabled).toBe(true)
  expect((turnOff as HTMLButtonElement).disabled).toBe(false)
  expect(screen.getByText(de.agentAccess.errorUnbound)).toBeTruthy()
})

test('English locale renders all texts and confirmation dialog', async () => {
  const changedAt = new Date().toISOString()
  const expectedWhen = formatListDate(new Date(changedAt), new Date(), undefined, 'en')
  setup({ locale: 'en', initial: defaultState({ enabled: true, changed_at: changedAt, verifier_configured: false }) })
  expect(await screen.findByText(en.agentAccess.statusOnSince(expectedWhen))).toBeTruthy()
  expect(screen.getByText(en.agentAccess.noConnection)).toBeTruthy()
  expect(screen.getByText(en.agentAccess.limits)).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: en.agentAccess.turnOff }))
  expect(await screen.findByText(en.agentAccess.turnedOff)).toBeTruthy()
  expect(screen.getByText(en.agentAccess.statusOff)).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: en.agentAccess.turnOn }))
  const dialog = await screen.findByRole('dialog', { name: en.agentAccess.confirmTitle })
  expect(dialog.textContent).toContain(en.agentAccess.confirmBody)
  expect(within(dialog).getByRole('button', { name: en.agentAccess.confirmCancel })).toBeTruthy()
})

test('access card wraps on narrow screens and keeps 44 px actions on touch screens', () => {
  const css = readFileSync(new URL('./settings.css', import.meta.url), 'utf8')
  expect(css).toMatch(/\.le-access-card \{[^}]*flex-wrap: wrap/u)
  expect(css).toMatch(/\.le-access-status \{[^}]*min-width: 0/u)
  expect(css).toMatch(/@media \(pointer: coarse\) \{ \.le-access-actions \.le-btn \{ min-height: var\(--control-height-lg\); \} \}/u)
})
