// i18n-exempt: synthetic API fixtures and test labels.
import { afterEach, expect, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { client as api } from '../api/generated/client.gen'
import type { AccountOut, McpSettingsOut } from '../api/generated/types.gen'
import { de } from '../i18n/de'
import { en } from '../i18n/en'
import { I18nProvider } from '../i18n/I18n'
import { settingsSections } from '../prompts/Settings'
import { McpAccountSettings } from './McpAccountSettings'
import { SettingsSaveBar, SettingsSaveProvider } from './SettingsSave'

const originalFetch = globalThis.fetch
const originalConfig = api.getConfig()

const accountFixture = (id: string, name: string, email: string, active = true): AccountOut => ({
  id,
  provider: 'imap',
  auth_type: 'imap_password',
  connection: 'imap',
  email_address: email,
  display_name: name,
  is_active: active,
  chat_eligibility: 'eligible',
  color_index: 0,
  error_code: '',
  last_sync_at: '2026-01-01T00:00:00Z',
  message_count: 10,
  unread: 2,
  state: 'ok',
  action_capabilities: {
    archive: true,
    label: true,
    mark_read: true,
    mark_unread: true,
    move: true,
    restore: true,
    spam: true,
    trash: true,
  },
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
})

type RecordedRequest = { method: string; path: string; body: unknown }
let requests: RecordedRequest[]

function renderMcpSettings({
  initial = { account_ids: [], version: 1 },
  accounts = [accountFixture('acc-1', 'Work Example', 'work@example.org'), accountFixture('acc-2', 'Private Example', 'private@example.org')],
  respond,
  locale = 'de',
}: {
  initial?: McpSettingsOut
  accounts?: AccountOut[]
  respond?: (request: RecordedRequest) => Response | Promise<Response> | undefined
  locale?: 'de' | 'en'
} = {}) {
  requests = []
  let current = { ...initial, account_ids: [...initial.account_ids] }
  api.setConfig({ baseUrl: 'https://api.example.test' })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init)
    const url = new URL(req.url)
    const raw = req.method === 'PATCH' ? await req.clone().text() : ''
    const rec: RecordedRequest = { method: req.method, path: url.pathname, body: raw ? (JSON.parse(raw) as unknown) : undefined }
    requests.push(rec)
    const custom = await respond?.(rec)
    if (custom) return custom
    if (url.pathname.endsWith('/mail/accounts')) return Response.json(accounts)
    if (req.method === 'PATCH' && url.pathname.endsWith('/settings/mcp')) {
      const body = rec.body as { expected_version: number; account_ids: string[] }
      if (body.expected_version !== current.version) {
        return Response.json({ detail: { code: 'version_conflict', message: 'conflict' } }, { status: 409 })
      }
      current = { account_ids: [...body.account_ids].sort(), version: current.version + 1 }
      return Response.json(current)
    }
    return Response.json(current)
  }) as typeof fetch

  const rootRoute = createRootRoute({
    component: () => (
      <I18nProvider locale={locale}>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <SettingsSaveProvider>
            <McpAccountSettings />
            <SettingsSaveBar />
          </SettingsSaveProvider>
        </QueryClientProvider>
      </I18nProvider>
    ),
  })
  const mcpRoute = createRoute({ getParentRoute: () => rootRoute, path: '/settings/mcp' })
  const accessRoute = createRoute({ getParentRoute: () => rootRoute, path: '/settings/access' })
  const accountsRoute = createRoute({ getParentRoute: () => rootRoute, path: '/accounts' })
  const router = createRouter({
    routeTree: rootRoute.addChildren([mcpRoute, accessRoute, accountsRoute]),
    history: createMemoryHistory({ initialEntries: ['/settings/mcp'] }),
  })
  return render(<RouterProvider router={router} />)
}

afterEach(() => {
  cleanup()
  globalThis.fetch = originalFetch
  api.setConfig(originalConfig)
})

test('settings navigation registers /settings/mcp in both languages', () => {
  expect(settingsSections(de).some(section => section.to === '/settings/mcp' && section.label === de.mcpAccounts.section)).toBe(true)
  expect(settingsSections(en).some(section => section.to === '/settings/mcp' && section.label === en.mcpAccounts.section)).toBe(true)
})

test('explains the four read-only tools, inactive status, data leaving liam-e, and revocation', async () => {
  renderMcpSettings({ locale: 'en' })
  await screen.findByLabelText(/Work Example/)
  expect(screen.getByText(en.mcpAccounts.inactiveNotice)).toBeTruthy()
  expect(screen.getByText(en.mcpAccounts.privacyNotice)).toBeTruthy()
  expect(screen.getByText(en.mcpAccounts.revokeBody)).toBeTruthy()
  for (const name of ['liam_mail_accounts', 'liam_mail_search', 'liam_mail_read', 'liam_mail_summary']) {
    expect(screen.getByText(new RegExp(name))).toBeTruthy()
  }
  expect(screen.getByRole('link', { name: en.mcpAccounts.openAccessSwitch })).toBeTruthy()
})

test('toggling an account uses the sticky save bar and sends expected_version', async () => {
  renderMcpSettings({ locale: 'de' })
  const checkbox = await screen.findByRole('checkbox', { name: /Work Example/ })
  expect((checkbox as HTMLInputElement).checked).toBe(false)

  fireEvent.click(checkbox)
  const saveButton = await screen.findByRole('button', { name: de.settingsUi.save })
  fireEvent.click(saveButton)

  await screen.findByText(de.settingsUi.saved)
  const patch = requests.find(r => r.method === 'PATCH' && r.path.endsWith('/settings/mcp'))
  expect(patch?.body).toEqual({ expected_version: 1, account_ids: ['acc-1'] })
})

test('version conflict surfaces reload action without losing conflict message', async () => {
  renderMcpSettings({
    locale: 'en',
    respond: req => {
      if (req.method === 'PATCH') {
        return Response.json({ detail: { code: 'version_conflict', message: 'conflict' } }, { status: 409 })
      }
      return undefined
    },
  })
  const checkbox = await screen.findByRole('checkbox', { name: /Work Example/ })
  fireEvent.click(checkbox)
  fireEvent.click(await screen.findByRole('button', { name: en.settingsUi.save }))

  await waitFor(() => {
    expect(screen.getByText(en.mcpAccounts.conflict)).toBeTruthy()
  })
})

test('saving drops hidden inactive account IDs so the server does not reject with 422', async () => {
  renderMcpSettings({
    locale: 'en',
    initial: { account_ids: ['acc-inactive'], version: 2 },
    accounts: [
      accountFixture('acc-1', 'Work Example', 'work@example.org', true),
      accountFixture('acc-inactive', 'Inactive Example', 'inactive@example.org', false),
    ],
  })
  const checkbox = await screen.findByRole('checkbox', { name: /Work Example/ })
  fireEvent.click(checkbox)
  fireEvent.click(await screen.findByRole('button', { name: en.settingsUi.save }))

  await screen.findByText(en.settingsUi.saved)
  const patch = requests.find(r => r.method === 'PATCH' && r.path.endsWith('/settings/mcp'))
  expect(patch?.body).toEqual({ expected_version: 2, account_ids: ['acc-1'] })
})
