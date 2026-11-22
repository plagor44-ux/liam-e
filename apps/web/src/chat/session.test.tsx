import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from '@tanstack/react-router'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import { de } from '../i18n/de'
import { MobileHeader } from '../shell/MobileHeader'
import { ChatSessionProvider, useChatSession, useRestartConversation } from './session'

const originalFetch = globalThis.fetch, originalMatchMedia = window.matchMedia
let controller: ReadableStreamDefaultController<Uint8Array>, signal: AbortSignal
function clientWithGrants() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(['summary', 'chat'], { accounts: [{ id: 'a' }], mail_scope_version: 3 })
  return client
}
function setup() {
  window.matchMedia = (() => ({ matches: true })) as unknown as typeof window.matchMedia
  globalThis.fetch = ((_url: unknown, init: RequestInit) => { signal = init.signal!; return Promise.resolve(new Response(new ReadableStream({ start(c) { controller = c } }))) }) as typeof fetch
  function Consumer() {
    const { state, send, stop } = useChatSession()
    return <><button onClick={() => void send({ text: 'Hi' })}>send</button><button onClick={stop}>stop</button><div data-testid="phase">{state.phase}</div><div data-testid="text">{state.text}</div><div data-testid="mails">{state.mails.length}</div>{state.phase === 'stopped' && de.chat.stopped}{state.errorCode && de.errors[state.errorCode]}</>
  }
  render(<QueryClientProvider client={clientWithGrants()}><ChatSessionProvider><Consumer /></ChatSessionProvider></QueryClientProvider>)
  fireEvent.click(screen.getByText('send'))
}
function frame(value: object) { controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(value)}\n\n`)) }
afterEach(() => { cleanup(); mock.restore(); globalThis.fetch = originalFetch; window.matchMedia = originalMatchMedia })
test('buffers cards until done and text', async () => {
  setup(); await act(async () => { frame({ event: 'start', conversation_id: 'c' }); frame({ event: 'delta', text: 'Hallo' }); frame({ event: 'mails', mails: [{}] }) })
  expect(screen.getByTestId('text').textContent).toBe('Hallo'); expect(screen.getByTestId('mails').textContent).toBe('0')
  await act(async () => { frame({ event: 'done', conversation_id: 'c', message_id: 'm', stopped: false }) })
  await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('done'))
  expect(screen.getByTestId('mails').textContent).toBe('1')
})
test('voice turn leaves an identical unsent composer draft intact', async () => {
  window.matchMedia = (() => ({ matches: true })) as unknown as typeof window.matchMedia
  globalThis.fetch = ((_url: unknown, init: RequestInit) => { signal = init.signal!; return Promise.resolve(new Response(new ReadableStream({ start(c) { controller = c } }))) }) as typeof fetch
  function Consumer() {
    const session = useChatSession()
    return <><button onClick={() => session.setComposerText('c', 'Hallo')}>draft</button><button onClick={() => void session.send({ conversation_id: 'c', text: 'Hallo' }, { preserveDraft: true })}>voice</button><div data-testid="draft">{session.composerTexts.c}</div></>
  }
  render(<QueryClientProvider client={clientWithGrants()}><ChatSessionProvider><Consumer /></ChatSessionProvider></QueryClientProvider>)
  fireEvent.click(screen.getByText('draft')); fireEvent.click(screen.getByText('voice'))
  await act(async () => { frame({ event: 'start', conversation_id: 'c' }) })
  expect(screen.getByTestId('draft').textContent).toBe('Hallo')
})
test('stop aborts and displays notice', async () => { setup(); await act(async () => { frame({ event: 'delta', text: 'Hallo' }) }); fireEvent.click(screen.getByText('stop')); expect(signal.aborted).toBe(true); expect(screen.getByText(de.chat.stopped)).toBeTruthy(); expect(screen.getByTestId('phase').textContent).toBe('stopped') })
test('network break reports aborted', async () => { setup(); await act(async () => { frame({ event: 'delta', text: 'Hallo' }) }); await act(async () => { controller.error(new TypeError('network')) }); await waitFor(() => expect(screen.getByText(de.errors.aborted)).toBeTruthy()); expect(screen.getByTestId('phase').textContent).toBe('error') })

test('preference failure never silently retries; explicit continuation sets only this request flag', async () => {
  const bodies: Record<string, unknown>[] = []
  globalThis.fetch = (async (_url: unknown, init: RequestInit) => {
    bodies.push(JSON.parse(init.body as string))
    return Response.json({ detail: { code: 'preferences_unavailable' } }, { status: 503 })
  }) as typeof fetch
  function Consumer() {
    const { state, send, retryWithoutPreferences } = useChatSession()
    return <><button onClick={() => void send({ text: 'test-only', account_id: 'a' })}>send</button>{state.errorCode === 'preferences_unavailable' && <button onClick={() => void retryWithoutPreferences()}>{de.preferences.continueWithout}</button>}</>
  }
  render(<QueryClientProvider client={clientWithGrants()}><ChatSessionProvider><Consumer /></ChatSessionProvider></QueryClientProvider>)
  fireEvent.click(screen.getByText('send'))
  const continueButton = await screen.findByRole('button', { name: de.preferences.continueWithout })
  expect(bodies).toHaveLength(1)
  expect(bodies[0]?.without_preferences).toBeUndefined()
  fireEvent.click(continueButton)
  await waitFor(() => expect(bodies).toHaveLength(2))
  expect(bodies[1]).toMatchObject({ text: 'test-only', account_id: 'a', without_preferences: true })
})

// Restarting is the only place that hands a conversation's unsent text to the start page.
function restartHarness(id?: string, seed = true) {
  const sources = spyOn(sdk, 'calendarSourcesList').mockResolvedValue({ data: [] } as never)
  const client = clientWithGrants()
  if (seed) client.setQueryData(['conversations'], [{ id: 'c', title: 'Test', account_scope_json: JSON.stringify({ account_ids: ['b'], selection: 'b' }) }])
  function Start() {
    const { composerTexts, setComposerText } = useChatSession()
    return <><div data-testid="start" data-draft={composerTexts.new ?? ''} /><button onClick={() => { setComposerText('new', 'Bleibt') }}>draft</button></>
  }
  function Chat() {
    const session = useChatSession()
    const restart = useRestartConversation(id)
    return <><button onClick={() => void session.send({ text: 'Test', conversation_id: id })}>send-calendar</button><span>{session.state.errorCode}</span><button onClick={restart}>restart</button><button onClick={() => session.setComposerText(id ?? 'new', 'Noch offen')}>type</button>{id && <MobileHeader title="Chat" conversationId={id} onMenu={() => {}} />}</>
  }
  function Away() { return <div data-testid="away" /> }
  const root = createRootRoute({ component: () => <QueryClientProvider client={client}><ChatSessionProvider><Outlet /></ChatSessionProvider></QueryClientProvider> })
  const index = createRoute({ getParentRoute: () => root, path: '/', component: Start })
  const chat = createRoute({ getParentRoute: () => root, path: '/chat/$conversationId', component: Chat })
  const accounts = createRoute({ getParentRoute: () => root, path: '/accounts', component: Away })
  const router = createRouter({ routeTree: root.addChildren([index, chat, accounts]), history: createMemoryHistory({ initialEntries: ['/'] }) })
  render(<RouterProvider router={router} />)
  return { router, client, sources }
}

async function restart(router: ReturnType<typeof restartHarness>['router']) {
  fireEvent.click(await screen.findByText('draft'))
  await act(async () => { await router.navigate({ to: '/chat/$conversationId', params: { conversationId: 'c' } }) })
  fireEvent.click(await screen.findByText('restart'))
  await waitFor(() => expect(router.state.location.pathname).toBe('/'))
}

test('restart after 409 needs no calendar selection or catalogue read', async () => {
  const { router, client, sources } = restartHarness('c')
  await openChat(router)
  globalThis.fetch = (async () => new Response(JSON.stringify({ detail: { code: 'conversation_scope_change_requires_new' } }), { status: 409 })) as unknown as typeof fetch
  sources.mockRejectedValue(new Error('test-only'))
  expect(client.getQueryData(['calendar', 'sources'])).toBeUndefined()
  fireEvent.click(screen.getByText('send-calendar'))
  await screen.findByText('conversation_scope_change_requires_new')
  fireEvent.click(screen.getByText('restart'))
  await waitFor(() => expect(router.state.location.pathname).toBe('/'))
  expect(sources).not.toHaveBeenCalled()
})

test('restart without unsent text keeps the start draft', async () => {
  const { router } = restartHarness('c')
  await restart(router)
  expect(screen.getByTestId('start').dataset.draft).toBe('Bleibt')
})

test('restart without a conversation only navigates', async () => {
  const { router } = restartHarness()
  await restart(router)
  expect(screen.getByTestId('start').dataset.draft).toBe('Bleibt')
})

async function openChat(router: ReturnType<typeof restartHarness>['router']) {
  fireEvent.click(await screen.findByText('draft'))
  await act(async () => { await router.navigate({ to: '/chat/$conversationId', params: { conversationId: 'c' } }) })
}

test('a fresh start never waits for the old account selection', async () => {
  const list = spyOn(sdk, 'chatConversationsList')
  const { router } = restartHarness('c', false)
  await restart(router)
  expect(list).not.toHaveBeenCalled()
})

test('a fresh start transfers unsent text immediately when the catalogue is unavailable', async () => {
  const { router, sources } = restartHarness('c', false)
  sources.mockImplementation(() => new Promise<never>(() => {}))
  await openChat(router)
  fireEvent.click(await screen.findByRole('button', { name: 'type' }))
  fireEvent.click(screen.getByRole('button', { name: 'Neue Unterhaltung' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/'))
  expect(screen.getByTestId('start').dataset.draft).toBe('Noch offen')
  expect(sources).not.toHaveBeenCalled()
})

test('voice returns the current saved reply without waiting for text animation', async () => {
  window.matchMedia = (() => ({ matches: false })) as unknown as typeof window.matchMedia
  globalThis.fetch = ((_url: unknown, init: RequestInit) => { signal = init.signal!; return Promise.resolve(new Response(new ReadableStream({ start(c) { controller = c } }))) }) as typeof fetch
  function Consumer() {
    const { state, send } = useChatSession()
    return <><button onClick={() => void send({ conversation_id: 'c', text: 'Zweite Frage', voice_response: true })}>voice</button><div data-testid="phase">{state.phase}</div><div data-testid="text">{state.text}</div><div data-testid="message">{state.messageId}</div></>
  }
  render(<QueryClientProvider client={clientWithGrants()}><ChatSessionProvider><Consumer /></ChatSessionProvider></QueryClientProvider>)
  fireEvent.click(screen.getByText('voice'))
  const reply = 'Antwort '.repeat(100)
  await act(async () => { frame({ event: 'start', conversation_id: 'c' }); frame({ event: 'delta', text: reply }); frame({ event: 'done', conversation_id: 'c', message_id: 'newest', stopped: false }) })
  await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('done'), { timeout: 300 })
  expect(screen.getByTestId('text').textContent).toBe(reply)
  expect(screen.getByTestId('message').textContent).toBe('newest')
})

test('interrupting playback after text completion preserves the saved reply state', async () => {
  const originalSocket = globalThis.WebSocket
  let socket!: FakeSocket
  class FakeSocket {
    static OPEN = 1
    readyState = 1
    onopen?: () => void; onmessage?: (event: { data: string }) => void; onclose?: () => void; onerror?: () => void
    constructor() { socket = this; queueMicrotask(() => this.onopen?.()) }
    send() {} close() { this.readyState = 3; this.onclose?.() }
    emit(value: object) { this.onmessage?.({ data: JSON.stringify(value) }) }
  }
  globalThis.WebSocket = FakeSocket as unknown as typeof WebSocket
  try {
    window.matchMedia = (() => ({ matches: true })) as unknown as typeof window.matchMedia
    function Consumer() {
      const { state, send, stop } = useChatSession()
      return <><button onClick={() => void send({ text: 'Hallo', conversation_id: 'c', voice_response: true }, { voiceAudio: { push() {}, end: async () => {}, fail() {} } })}>voice</button><button onClick={stop}>interrupt</button><div data-testid="voice-phase">{state.phase}</div><div data-testid="voice-reply">{state.text}</div><div data-testid="voice-id">{state.messageId}</div></>
    }
    render(<QueryClientProvider client={clientWithGrants()}><ChatSessionProvider><Consumer /></ChatSessionProvider></QueryClientProvider>)
    fireEvent.click(screen.getByText('voice'))
    await waitFor(() => expect(socket).toBeDefined())
    await act(async () => { socket.emit({ event: 'start', conversation_id: 'c', user_message_id: 'u', model: 'm' }); socket.emit({ event: 'delta', text: 'Gespeicherte Antwort.' }); socket.emit({ event: 'voice_text_done', conversation_id: 'c', message_id: 'new' }) })
    await waitFor(() => expect(screen.getByTestId('voice-phase').textContent).toBe('done'))
    fireEvent.click(screen.getByText('interrupt'))
    expect(screen.getByTestId('voice-phase').textContent).toBe('done')
    expect(screen.getByTestId('voice-reply').textContent).toBe('Gespeicherte Antwort.')
    expect(screen.getByTestId('voice-id').textContent).toBe('new')
  } finally { globalThis.WebSocket = originalSocket }
})
