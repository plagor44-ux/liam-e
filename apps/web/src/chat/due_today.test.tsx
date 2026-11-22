// i18n-exempt: synthetic test fixtures and route paths.
import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from '@tanstack/react-router'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import type { ChatMessageOut, MailSummaryOut, SourceOut } from '../api/generated/types.gen'
import { Conversation } from './Conversation'
import { ChatSessionProvider } from './session'
import { de } from '../i18n/de'
import { en } from '../i18n/en'
import { I18nProvider } from '../i18n/I18n'
import { Route as StartRoute } from '../routes/_app/index'

const originalFetch = globalThis.fetch
const originalMatchMedia = window.matchMedia

type SetupOpts = {
  path?: string
  locale?: 'de' | 'en'
  accounts?: MailSummaryOut['accounts']
  mailScopeVersion?: number
  calendars?: Partial<SourceOut>[]
  defaultSourceId?: string | null
  history?: ChatMessageOut[]
}

function setup(opts: SetupOpts = {}) {
  const {
    path = '/',
    locale = 'de',
    accounts = [{
      id: 'a',
      display_name: 'Privat',
      email_address: 'me@example.org',
      provider: 'gmail',
      connection: 'connector',
      color_index: 0,
      unread: 2,
      state: 'ok',
      error_code: '',
      last_sync_at: '2026-11-21T08:57:00Z',
    }],
    mailScopeVersion = 7,
    calendars = [],
    defaultSourceId = null,
    history = [],
  } = opts
  let streamController: ReadableStreamDefaultController<Uint8Array> | undefined
  const requests: Record<string, unknown>[] = []
  window.matchMedia = ((query: string) => ({ matches: query !== '(max-width: 639px)', addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  globalThis.fetch = ((_url: unknown, init: RequestInit) => {
    requests.push(JSON.parse(String(init.body)) as Record<string, unknown>)
    return Promise.resolve(new Response(new ReadableStream({ start(controller) { streamController = controller } })))
  }) as typeof fetch

  const summarySpy = spyOn(sdk, 'mailSummary').mockImplementation((async () => ({
    data: {
      mail_scope_version: mailScopeVersion,
      accounts,
      unread_total: accounts.reduce((sum, item) => sum + item.unread, 0),
      newsletter_senders: 0,
      last_sync_at: accounts.find(item => item.last_sync_at)?.last_sync_at ?? null,
    },
  })) as never)
  const sourcesSpy = spyOn(sdk, 'calendarSourcesList').mockImplementation((async () => ({ data: calendars })) as never)
  const settingsSpy = spyOn(sdk, 'calendarSettingsGet').mockImplementation((async () => ({
    data: { version: 1, default_source_id: defaultSourceId },
  })) as never)
  spyOn(sdk, 'chatSettingsGet').mockResolvedValue({ data: { version: mailScopeVersion, account_ids: accounts.map(a => a.id) } } as never)
  spyOn(sdk, 'promptsList').mockResolvedValue({ data: [] } as never)
  spyOn(sdk, 'chatModelsList').mockResolvedValue({ data: [{ id: 'm1', label: 'Standardmodell', group: 'general', description: '', is_default: true }] } as never)
  spyOn(sdk, 'chatConversationsList').mockResolvedValue({ data: [{ id: 'c-due', title: 'Heute fällig', model: 'm1', version: 1, prompt_revision_id: null, account_scope_json: JSON.stringify({ account_ids: accounts.map(a => a.id), selection: null }), created_at: '', updated_at: '' }] } as never)
  spyOn(sdk, 'chatHistory').mockImplementation((async () => ({ data: history })) as never)
  spyOn(sdk, 'chatMailReferenceGet').mockResolvedValue({
    data: { id: 'mail-1', account_id: 'a', color_index: 0, sender_name: 'Buchhaltung', subject: 'Rechnung fällig', internal_date: null, deadline: '2026-11-21', is_flagged: true },
  } as never)
  spyOn(sdk, 'calendarOccurrenceGet').mockResolvedValue({
    data: { id: 'occ-1', source_id: 'cal-work', event_id: 'evt-1', title: 'Abgabetermin', start_date: '2026-11-21', end_date: '2026-11-22', start_at: null, end_at: null, timezone: null },
  } as never)

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({
    component: () => (
      <I18nProvider locale={locale}>
        <QueryClientProvider client={client}>
          <ChatSessionProvider>
            <Outlet />
          </ChatSessionProvider>
        </QueryClientProvider>
      </I18nProvider>
    ),
  })
  const start = createRoute({ getParentRoute: () => root, path: '/', validateSearch: StartRoute.options.validateSearch, component: StartRoute.options.component })
  const chat = createRoute({
    getParentRoute: () => root,
    path: '/chat/$conversationId',
    component: function ChatRoute() {
      const { conversationId } = chat.useParams()
      return <Conversation conversationId={conversationId} />
    },
  })
  const settingsChat = createRoute({ getParentRoute: () => root, path: '/settings/chat', component: () => <p>Chat-Einstellungen</p> })
  const settingsCal = createRoute({ getParentRoute: () => root, path: '/settings/calendar', component: () => <p>Standardkalender</p> })
  const accountsRoute = createRoute({ getParentRoute: () => root, path: '/accounts', component: () => <p>Konten</p> })
  const calendarDetail = createRoute({ getParentRoute: () => root, path: '/calendar/$occurrenceId', component: () => <p>Termin</p> })
  const mailDetail = createRoute({ getParentRoute: () => root, path: '/mail/$messageId', component: () => <p>Mail</p> })
  const router = createRouter({
    routeTree: root.addChildren([start, chat, settingsChat, settingsCal, accountsRoute, calendarDetail, mailDetail]),
    history: createMemoryHistory({ initialEntries: [path] }),
  })
  render(<RouterProvider router={router} />)
  const frame = (value: object) => streamController!.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(value)}\n\n`))
  return { client, router, requests, frame, summarySpy, sourcesSpy, settingsSpy }
}

afterEach(() => {
  cleanup()
  mock.restore()
  globalThis.fetch = originalFetch
  window.matchMedia = originalMatchMedia
})

test('clicking Due today streams from start page to separated conversation reply with mail, calendar and task scopes', async () => {
  const { router, requests, frame } = setup({
    calendars: [{ id: 'cal-work', name: 'Arbeit', provider: 'google', capabilities: ['read', 'write'], is_active: true, chat_access: 'writable', stale: false, incomplete: false, last_success_at: new Date().toISOString() }],
    defaultSourceId: 'cal-work',
  })

  expect(await screen.findByText(de.taskChat.excludedHint)).toBeTruthy()
  const dueBtnBefore = await screen.findByRole('button', { name: /Heute fällig/ })
  expect(dueBtnBefore.textContent).toContain('Mailfristen · Termine')
  expect(dueBtnBefore.textContent).not.toContain('Aufgaben')

  // Select tasks in the composer before clicking Due today.
  fireEvent.click(screen.getByRole('button', { name: de.taskChat.none }))
  fireEvent.click(await screen.findByRole('menuitemradio', { name: de.tasks.allSources }))
  expect(screen.queryByText(de.taskChat.excludedHint)).toBeNull()
  const dueBtnAfter = screen.getByRole('button', { name: /Heute fällig/ })
  expect(dueBtnAfter.textContent).toContain('Mailfristen · Termine · Aufgaben')

  fireEvent.click(dueBtnAfter)
  expect(await screen.findByRole('status')).toBeTruthy()
  await waitFor(() => expect(requests).toHaveLength(1))
  expect(requests[0]).toMatchObject({
    text: de.quickActions.dueToday.prompt,
    task_source: 'all',
    expected_mail_scope_version: 7,
  })
  expect(requests[0]!.calendar_source_ids).toBeUndefined()

  await act(async () => {
    frame({ event: 'start', conversation_id: 'c-due', user_message_id: 'u-1', model: 'm1' })
    frame({ event: 'status', code: 'checking_deadlines' })
    frame({ event: 'delta', text: 'Mailfristen: Rechnung fällig.\n\nKalendertermine: Abgabetermin.\n\nAufgaben: Belege abheften.' })
    frame({ event: 'mails', mails: [{ id: 'mail-1', account_id: 'a', color_index: 0, subject: 'Rechnung fällig', sender_name: 'Buchhaltung', snippet: 'Heute fällig', internal_date: null, deadline: '2026-11-21', is_flagged: true }] })
    frame({ event: 'calendars', calendars: [{ source_id: 'cal-work', occurrence_id: 'occ-1' }] })
    frame({ event: 'suggestions', items: ['Antwort entwerfen'] })
    frame({ event: 'done', conversation_id: 'c-due', message_id: 'a-1', stopped: false })
  })

  await waitFor(() => expect(router.state.location.pathname).toBe('/chat/c-due'))
  expect((await screen.findAllByText(/Mailfristen: Rechnung fällig/)).length).toBeGreaterThanOrEqual(1)
  expect(screen.getByRole('link', { name: 'Abgabetermin' })).toBeTruthy()
  expect(screen.getByText('Rechnung fällig', { selector: 'p' })).toBeTruthy()
  expect(screen.getByText('Antwort entwerfen', { selector: 'button.le-chip' })).toBeTruthy()
})

test('start page surfaces paused, unreachable, incomplete sources and missing write default in de and en', async () => {
  for (const locale of ['de', 'en'] as const) {
    const t = locale === 'de' ? de : en
    setup({
      locale,
      accounts: [
        { id: 'a', display_name: 'Privat', email_address: 'a@example.org', provider: 'gmail', connection: 'connector', color_index: 0, unread: 1, state: 'ok', error_code: '', last_sync_at: '2026-11-21T08:00:00Z' },
        { id: 'b', display_name: 'Arbeit', email_address: 'b@example.org', provider: 'imap', connection: 'imap', color_index: 1, unread: 0, state: 'auth_expired', error_code: 'auth_expired', last_sync_at: null },
        { id: 'c', display_name: 'Familie', email_address: 'c@example.org', provider: 'imap', connection: 'imap', color_index: 2, unread: 0, state: 'never', error_code: '', last_sync_at: null },
      ],
      calendars: [
        { id: 'cal-w', name: 'Team', provider: 'google', capabilities: ['read', 'write'], is_active: true, chat_access: 'writable', stale: false, incomplete: false, last_error: 'provider_unavailable', last_success_at: new Date().toISOString() },
        { id: 'cal-p', name: 'Privatkalender', provider: 'ics', capabilities: ['read'], is_active: false, chat_access: 'paused', stale: true, incomplete: true },
        { id: 'cal-d', name: 'Vereinskalender', provider: 'ics', capabilities: ['read'], is_active: true, chat_access: 'disconnected', stale: true, incomplete: true },
      ],
      defaultSourceId: null,
    })

    expect(await screen.findByText(t.errors.accountUnavailable('Arbeit'))).toBeTruthy()
    expect(screen.getByText(t.chat.mailIncomplete('Familie', 1))).toBeTruthy()
    expect(screen.getByText(t.calendarSettings.pausedNames('Privatkalender'))).toBeTruthy()
    expect(screen.getByText(t.calendarSettings.unreachable('Vereinskalender'))).toBeTruthy()
    expect(screen.getByText(t.calendarSettings.refreshFailed('Team'))).toBeTruthy()
    expect(screen.getByText(t.calendarSettings.noDefault)).toBeTruthy()
    expect(screen.getByText(t.taskChat.excludedHint)).toBeTruthy()
    cleanup()
  }
})

test('all mail accounts unreachable or unavailable write default show dedicated states', async () => {
  setup({
    accounts: [{ id: 'a', display_name: 'Arbeit', email_address: 'a@example.org', provider: 'imap', connection: 'imap', color_index: 0, unread: 0, state: 'error', error_code: 'provider_error', last_sync_at: null }],
    calendars: [{ id: 'cal-ro', name: 'Verein', provider: 'ics', capabilities: ['read'], is_active: true, chat_access: 'read_only', stale: false, incomplete: false, last_success_at: new Date().toISOString() }],
    defaultSourceId: 'cal-ro',
  })

  expect(await screen.findByText(de.chat.allAccountsUnreachable('Arbeit', 1))).toBeTruthy()
  expect(screen.getByText(de.calendarSettings.defaultUnavailable('Verein', de.calendarSettings.reason.read_only))).toBeTruthy()
})

test('revoking mail grant on settings change clears stale new-chat task account and invalid chatAccount focus', async () => {
  const { client, summarySpy, requests } = setup({
    path: '/?chatAccount=a',
    calendars: [{ id: 'cal-w', name: 'Arbeit', provider: 'google', capabilities: ['read', 'write'], is_active: true, chat_access: 'writable', stale: false, incomplete: false, last_success_at: new Date().toISOString() }],
    defaultSourceId: 'cal-w',
  })

  // User picks tasks for account 'a' on the start page.
  fireEvent.click(await screen.findByRole('button', { name: de.taskChat.none }))
  fireEvent.click(await screen.findByRole('menuitemradio', { name: de.taskChat.thisAccount }))
  const btn = await screen.findByRole('button', { name: /Heute fällig/ })
  expect(btn.textContent).toContain('Mailfristen · Termine · Aufgaben')

  // Simulate revoking account 'a' in /settings/chat.
  summarySpy.mockResolvedValue({ data: { mail_scope_version: 8, accounts: [], unread_total: 0, newsletter_senders: 0, last_sync_at: null } } as never)
  await act(() => client.invalidateQueries({ queryKey: ['summary', 'chat'] }))

  expect(await screen.findByText(de.errors.chat_account_not_granted)).toBeTruthy()
  expect(await screen.findByRole('button', { name: de.taskChat.none })).toBeTruthy()
  const updatedBtn = await screen.findByRole('button', { name: /Heute fällig/ })
  expect(updatedBtn.textContent).toBe('Heute fälligTermine')

  fireEvent.click(updatedBtn)
  await waitFor(() => expect(requests).toHaveLength(1))
  expect(requests[0]!.task_source).toBe('none')
  expect(requests[0]!.task_source_account_id).toBeUndefined()
  expect(requests[0]!.account_id).toBeUndefined()
})

test('chat_settings_changed on Due today reloads grants and succeeds on retry', async () => {
  const { requests, summarySpy } = setup({
    calendars: [{ id: 'cal-w', name: 'Arbeit', provider: 'google', capabilities: ['read', 'write'], is_active: true, chat_access: 'writable', stale: false, incomplete: false, last_success_at: new Date().toISOString() }],
    defaultSourceId: 'cal-w',
  })

  let attempt = 0
  let currentScopeVersion = 7
  summarySpy.mockImplementation((async () => ({
    data: {
      mail_scope_version: currentScopeVersion,
      accounts: [{ id: 'a', display_name: 'Privat', email_address: 'me@example.org', provider: 'gmail', connection: 'connector', color_index: 0, unread: 1, state: 'ok', error_code: '', last_sync_at: '2026-11-21T08:57:00Z' }],
      unread_total: 1,
      newsletter_senders: 0,
      last_sync_at: '2026-11-21T08:57:00Z',
    },
  })) as never)
  spyOn(sdk, 'chatSettingsGet').mockImplementation((async () => ({ data: { version: currentScopeVersion, account_ids: ['a'] } })) as never)

  globalThis.fetch = ((_url: unknown, init: RequestInit) => {
    attempt += 1
    requests.push(JSON.parse(String(init.body)) as Record<string, unknown>)
    if (attempt === 1) {
      currentScopeVersion = 9
      return Promise.resolve(Response.json({ detail: { code: 'chat_settings_changed' } }, { status: 409 }))
    }
    return Promise.resolve(new Response(new ReadableStream({ start() {} })))
  }) as typeof fetch

  fireEvent.click(await screen.findByRole('button', { name: /Heute fällig/ }))
  expect(await screen.findByText(de.errors.chat_settings_changed)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: de.chat.retry }))
  await waitFor(() => expect(requests).toHaveLength(2))
  expect(requests[0]!.expected_mail_scope_version).toBe(7)
  expect(requests[1]!.expected_mail_scope_version).toBe(9)
})
