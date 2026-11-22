// i18n-exempt: synthetic API fixtures and route paths.
import { afterEach, expect, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from '@tanstack/react-router'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { client as api } from '../api/generated/client.gen'
import type { ChatModelOut, ChatModelSettingsOut } from '../api/generated/types.gen'
import { de } from '../i18n/de'
import { Settings } from '../prompts/Settings'
import { ChatAccountSettings } from './ChatAccountSettings'
import { ChatModelSettings } from './ChatModelSettings'

const originalFetch = globalThis.fetch
const originalConfig = api.getConfig()

const modelsFixture: ChatModelOut[] = [
  {
    id: 'mistral-medium-latest',
    label: 'Mistral Medium',
    description: 'Allrounder',
    is_default: true,
    is_app_default: true,
    availability: 'available',
    group: 'general',
    resolved_id: 'mistral-medium-3-5',
    price: { input_per_million: 1.5, output_per_million: 7.5, currency: 'USD', source: 'https://docs.mistral.ai/inference/pricing', checked_on: '2026-11-19' },
  },
  {
    id: 'mistral-small-latest',
    label: 'Mistral Small',
    description: 'Schnell',
    is_default: false,
    is_app_default: false,
    availability: 'available',
    group: 'general',
    resolved_id: 'mistral-small-2603',
    recommended: true,
    price: { input_per_million: 0.15, output_per_million: 0.6, currency: 'USD', source: 'https://docs.mistral.ai/inference/pricing', checked_on: '2026-11-19' },
  },
]

let requests: { path: string; method: string; body: Record<string, unknown> }[]

function setup(respond?: (request: Request) => Promise<Response | undefined>) {
  requests = []
  api.setConfig({ baseUrl: 'https://api.example.test' })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init)
    const url = new URL(request.url)
    requests.push({
      path: url.pathname,
      method: request.method,
      body: request.method === 'PATCH' ? await request.clone().json() as Record<string, unknown> : {},
    })
    const custom = await respond?.(request)
    if (custom) return custom
    if (url.pathname.endsWith('/settings/chat/model')) {
      return Response.json({
        version: 1,
        default_model: null,
        effective_default_model: 'mistral-medium-latest',
        app_default_model: 'mistral-medium-latest',
        status: 'app_default',
      } satisfies ChatModelSettingsOut)
    }
    if (url.pathname.endsWith('/chat/models')) return Response.json(modelsFixture)
    if (url.pathname.endsWith('/settings/chat')) return Response.json({ version: 1, account_ids: [] })
    if (url.pathname.endsWith('/mail/accounts')) return Response.json([])
    return Response.json({})
  }) as typeof fetch

  const query = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const root = createRootRoute({ component: () => <QueryClientProvider client={query}><Outlet /></QueryClientProvider> })
  const settings = createRoute({ getParentRoute: () => root, path: '/settings', component: Settings })
  const chat = createRoute({
    getParentRoute: () => settings,
    path: '/chat',
    component: () => <>
      <ChatModelSettings />
      <ChatAccountSettings />
    </>,
  })
  const router = createRouter({ routeTree: root.addChildren([settings.addChildren([chat])]), history: createMemoryHistory({ initialEntries: ['/settings/chat'] }) })
  render(<RouterProvider router={router} />)
  return { query, router }
}

afterEach(() => { cleanup(); globalThis.fetch = originalFetch; api.setConfig(originalConfig) })

test('selecting, saving and resetting default chat model refreshes models cache and navigates via Chat tab', async () => {
  let state: ChatModelSettingsOut = {
    version: 1,
    default_model: null,
    effective_default_model: 'mistral-medium-latest',
    app_default_model: 'mistral-medium-latest',
    status: 'app_default',
  }
  const { query } = setup(async request => {
    const url = new URL(request.url)
    if (url.pathname.endsWith('/settings/chat/model')) {
      if (request.method === 'PATCH') {
        const body = await request.json() as { expected_version: number; default_model: string | null }
        state = {
          version: state.version + 1,
          default_model: body.default_model,
          effective_default_model: body.default_model ?? 'mistral-medium-latest',
          app_default_model: 'mistral-medium-latest',
          status: body.default_model ? 'available' : 'app_default',
        }
      }
      return Response.json(state)
    }
    if (url.pathname.endsWith('/chat/models')) {
      return Response.json(modelsFixture.map(m => ({ ...m, is_default: m.id === state.effective_default_model })))
    }
    return undefined
  })

  expect((await screen.findByRole('link', { name: 'Chat' })).getAttribute('aria-current')).toBe('page')
  const picker = await screen.findByRole('button', { name: de.chatModel.choiceLabel })
  expect(picker.textContent).toContain('App-Vorgabe (Mistral Medium)')
  const reset = screen.getByRole('button', { name: de.chatModel.reset }) as HTMLButtonElement
  expect(reset.disabled).toBe(true)

  fireEvent.click(picker)
  fireEvent.click(screen.getByRole('menuitemradio', { name: /Mistral Small/ }))
  expect(picker.textContent).toContain('Mistral Small')
  fireEvent.click(screen.getAllByRole('button', { name: de.settingsUi.save })[0]!)
  await screen.findByText(de.settingsUi.saved)
  expect(requests.filter(r => r.path.endsWith('/settings/chat/model') && r.method === 'PATCH').map(r => r.body)).toEqual([
    { expected_version: 1, default_model: 'mistral-small-latest' },
  ])
  await waitFor(() => expect(query.getQueryData<ChatModelOut[]>(['models'])?.find(m => m.id === 'mistral-small-latest')?.is_default).toBe(true))

  fireEvent.click(screen.getByRole('button', { name: de.chatModel.reset }))
  await screen.findByText(de.settingsUi.saved)
  expect(picker.textContent).toContain('App-Vorgabe (Mistral Medium)')
  expect(requests.filter(r => r.path.endsWith('/settings/chat/model') && r.method === 'PATCH').map(r => r.body)[1]).toEqual({
    expected_version: 2,
    default_model: null,
  })
})

test('opens model details dialog from default model selector', async () => {
  setup()
  fireEvent.click(await screen.findByRole('button', { name: de.chatModel.choiceLabel }))
  fireEvent.click(screen.getByRole('menuitem', { name: de.chat.modelDetails }))
  const dialog = screen.getByRole('dialog')
  expect(dialog.textContent).toContain('USD')
  fireEvent.click(within(dialog).getByRole('button', { name: de.shell.close }))
  expect(screen.queryByRole('dialog')).toBeNull()
})

test('version conflict disables edits, avoids auto-retry and loads competing change on reload or discard', async () => {
  let state: ChatModelSettingsOut = {
    version: 1,
    default_model: null,
    effective_default_model: 'mistral-medium-latest',
    app_default_model: 'mistral-medium-latest',
    status: 'app_default',
  }
  setup(async request => {
    const url = new URL(request.url)
    if (url.pathname.endsWith('/settings/chat/model')) {
      if (request.method === 'PATCH') {
        const body = await request.json() as { expected_version: number; default_model: string | null }
        if (body.expected_version !== state.version) {
          return Response.json({ detail: { code: 'version_conflict' } }, { status: 409 })
        }
        state = {
          ...state,
          version: state.version + 1,
          default_model: body.default_model,
          effective_default_model: body.default_model ?? 'mistral-medium-latest',
          status: body.default_model ? 'available' : 'app_default',
        }
      }
      return Response.json(state)
    }
    return undefined
  })

  const picker = await screen.findByRole('button', { name: de.chatModel.choiceLabel })
  fireEvent.click(picker)
  fireEvent.click(screen.getByRole('menuitemradio', { name: /Mistral Small/ }))
  state = {
    version: 2,
    default_model: 'mistral-medium-latest',
    effective_default_model: 'mistral-medium-latest',
    app_default_model: 'mistral-medium-latest',
    status: 'available',
  }
  fireEvent.click(screen.getAllByRole('button', { name: de.settingsUi.save })[0]!)
  await screen.findByText(de.chatModel.conflict)
  expect(requests.filter(r => r.method === 'PATCH')).toHaveLength(1)
  expect((picker as HTMLButtonElement).disabled).toBe(true)

  fireEvent.click(screen.getByRole('button', { name: de.settingsUi.discard }))
  await waitFor(() => expect(picker.textContent).toContain('Mistral Medium'))
  expect((picker as HTMLButtonElement).disabled).toBe(false)

  fireEvent.click(picker)
  fireEvent.click(screen.getByRole('menuitemradio', { name: /Mistral Small/ }))
  fireEvent.click(screen.getAllByRole('button', { name: de.settingsUi.save })[0]!)
  await screen.findByText(de.settingsUi.saved)
  expect(requests.filter(r => r.method === 'PATCH').map(r => r.body)).toEqual([
    { expected_version: 1, default_model: 'mistral-small-latest' },
    { expected_version: 2, default_model: 'mistral-small-latest' },
  ])
})

test('retired default explains fallback and can still be reset when workspace catalog is empty or offline', async () => {
  let state: ChatModelSettingsOut = {
    version: 3,
    default_model: 'retired-model',
    effective_default_model: 'mistral-medium-latest',
    app_default_model: 'mistral-medium-latest',
    status: 'unlisted',
  }
  let offline = false
  setup(async request => {
    const url = new URL(request.url)
    if (url.pathname.endsWith('/settings/chat/model')) {
      if (request.method === 'PATCH') {
        const body = await request.json() as { expected_version: number; default_model: string | null }
        state = {
          version: state.version + 1,
          default_model: body.default_model,
          effective_default_model: 'mistral-medium-latest',
          app_default_model: 'mistral-medium-latest',
          status: 'app_default',
        }
      }
      return Response.json(state)
    }
    if (url.pathname.endsWith('/chat/models')) {
      if (offline) return Response.json({ detail: { code: 'model_catalog_unavailable' } }, { status: 503 })
      return Response.json([])
    }
    return undefined
  })

  expect(await screen.findByText(de.chatModel.unlistedNotice('retired-model', 'mistral-medium-latest'))).toBeTruthy()
  offline = true
  fireEvent.click(screen.getByRole('button', { name: de.chatModel.reset }))
  await screen.findByText(de.settingsUi.saved)
  expect(requests.filter(r => r.method === 'PATCH').map(r => r.body)).toEqual([
    { expected_version: 3, default_model: null },
  ])
})

test('handles catalog unavailability, specialist notice, and rejected save codes cleanly', async () => {
  let state: ChatModelSettingsOut = {
    version: 2,
    default_model: 'codestral-2508',
    effective_default_model: 'codestral-2508',
    app_default_model: 'mistral-medium-latest',
    status: 'available',
  }
  let rejectCode: string | undefined
  setup(async request => {
    const url = new URL(request.url)
    if (url.pathname.endsWith('/settings/chat/model')) {
      if (request.method === 'PATCH') {
        if (rejectCode) return Response.json({ detail: { code: rejectCode } }, { status: rejectCode === 'model_unknown' ? 422 : 503 })
        const body = await request.json() as { default_model: string | null }
        state = { ...state, version: state.version + 1, default_model: body.default_model }
      }
      return Response.json(state)
    }
    if (url.pathname.endsWith('/chat/models')) {
      return Response.json([
        ...modelsFixture,
        { id: 'codestral-2508', label: 'Codestral', description: '', is_default: true, is_app_default: false, availability: 'available', group: 'specialist' },
      ])
    }
    return undefined
  })

  expect(await screen.findByText(de.chatModel.outsideMailNotice)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: de.chatModel.choiceLabel }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: /Mistral Small/ }))
  rejectCode = 'model_unknown'
  fireEvent.click(screen.getAllByRole('button', { name: de.settingsUi.save })[0]!)
  expect(await screen.findByText(de.errors.model_unknown)).toBeTruthy()

  rejectCode = 'model_catalog_unavailable'
  fireEvent.click(screen.getAllByRole('button', { name: de.settingsUi.save })[0]!)
  expect(await screen.findByText(de.errors.model_catalog_unavailable)).toBeTruthy()
})

test('refreshing after catalog recovery updates same-version status and clears stale warnings', async () => {
  let recovered = false
  setup(async request => {
    const url = new URL(request.url)
    if (url.pathname.endsWith('/settings/chat/model')) {
      return Response.json({
        version: 4,
        default_model: 'mistral-small-latest',
        effective_default_model: recovered ? 'mistral-small-latest' : 'mistral-medium-latest',
        app_default_model: 'mistral-medium-latest',
        status: recovered ? 'available' : 'unlisted',
      } satisfies ChatModelSettingsOut)
    }
    if (url.pathname.endsWith('/chat/models')) {
      return Response.json(
        recovered
          ? modelsFixture.map(m => ({ ...m, is_default: m.id === 'mistral-small-latest' }))
          : [modelsFixture[0]!],
      )
    }
    return undefined
  })

  expect(await screen.findByText(de.chatModel.unlistedNotice('mistral-small-latest', 'Mistral Medium'))).toBeTruthy()
  recovered = true
  fireEvent.click(screen.getByRole('button', { name: de.chat.modelRefresh }))
  await waitFor(() => expect(screen.queryByText(de.chatModel.unlistedNotice('mistral-small-latest', 'Mistral Medium'))).toBeNull())
  expect((screen.getByRole('button', { name: de.chatModel.choiceLabel })).textContent).toContain('Mistral Small')
})

test('same-version status recovery preserves an edited draft while clearing unverified notice', async () => {
  let online = false
  let state: ChatModelSettingsOut = {
    version: 5,
    default_model: 'mistral-small-latest',
    effective_default_model: 'mistral-small-latest',
    app_default_model: 'mistral-medium-latest',
    status: 'unverified',
  }
  const { query } = setup(async request => {
    const url = new URL(request.url)
    if (url.pathname.endsWith('/settings/chat/model')) {
      if (request.method === 'PATCH') {
        const body = await request.json() as { expected_version: number; default_model: string | null }
        state = {
          version: state.version + 1,
          default_model: body.default_model,
          effective_default_model: body.default_model ?? 'mistral-medium-latest',
          app_default_model: 'mistral-medium-latest',
          status: 'available',
        }
      } else if (online) {
        state = { ...state, status: 'available' }
      }
      return Response.json(state)
    }
    if (url.pathname.endsWith('/chat/models')) {
      if (!online) return Response.json({ detail: { code: 'model_catalog_unavailable' } }, { status: 503 })
      return Response.json([
        ...modelsFixture,
        { id: 'mistral-large-latest', label: 'Mistral Large', description: 'Komplex', is_default: false, is_app_default: false, availability: 'available', group: 'general' },
      ])
    }
    return undefined
  })

  expect(await screen.findByText(de.chatModel.unverifiedNotice)).toBeTruthy()
  online = true
  fireEvent.click(screen.getByRole('button', { name: de.chat.modelRefresh }))
  await waitFor(() => expect(screen.queryByText(de.chatModel.unverifiedNotice)).toBeNull())

  // Simulate another outage followed by user editing draft before same-version status recovery.
  const picker = screen.getByRole('button', { name: de.chatModel.choiceLabel })
  fireEvent.click(picker)
  fireEvent.click(screen.getByRole('menuitemradio', { name: /Mistral Large/ }))
  expect(picker.textContent).toContain('Mistral Large')

  online = false
  await act(async () => {
    await query.refetchQueries({ queryKey: ['chat-model-settings'] })
    await query.refetchQueries({ queryKey: ['models'] })
  })
  online = true
  await act(async () => {
    await query.refetchQueries({ queryKey: ['chat-model-settings'] })
    await query.refetchQueries({ queryKey: ['models'] })
  })

  await waitFor(() => expect(picker.textContent).toContain('Mistral Large'))
  fireEvent.click(screen.getAllByRole('button', { name: de.settingsUi.save })[0]!)
  await screen.findByText(de.settingsUi.saved)
  expect(requests.filter(r => r.method === 'PATCH').map(r => r.body)).toEqual([
    { expected_version: 5, default_model: 'mistral-large-latest' },
  ])
})
