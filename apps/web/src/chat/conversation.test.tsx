import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from '@tanstack/react-router'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import type { ChatMessageOut, ConversationOut, MailSummaryOut } from '../api/generated/types.gen'
import { Route as StartRoute } from '../routes/_app/index'
import { de } from '../i18n/de'
import { Conversation, turnOrder } from './Conversation'
import { ChatSessionProvider, useChatSession } from './session'

const currentContext = {
  id: 'context', app_prompt_version: '10', mail_scope_version: 4, account_scope_json: '{"selection":null,"account_ids":["a"]}',
  prompt_revision_id: null, prompt_hash: null, preference_revision_id: null, revoked: false,
}
const originalFetch = globalThis.fetch
const originalMatchMedia = window.matchMedia
let stream: ReadableStreamDefaultController<Uint8Array>
let signal: AbortSignal
let history: ChatMessageOut[] = []
let requestBody: Record<string, unknown>
let summary: MailSummaryOut = { accounts: [], unread_total: 0, newsletter_senders: 0, last_sync_at: null }
// A test can hold the account summary back to see the state while it loads.
let summaryGate: Promise<void> | null = null
function setup(path = '/', historyGone = false, selection: string | null = null, voiceTrigger = false) {
  history = []
  summary = { mail_scope_version: 4, accounts: [{ id: 'a', display_name: 'Privat', email_address: 'ich@example.com', provider: 'gmail', color_index: 0, state: 'ok' } as MailSummaryOut['accounts'][number]], unread_total: 0, newsletter_senders: 0, last_sync_at: null }
  window.matchMedia = ((query: string) => ({ matches: query !== '(max-width: 639px)', addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  globalThis.fetch = ((_url: unknown, init: RequestInit) => { signal = init.signal!; requestBody = JSON.parse(String(init.body)) as Record<string, unknown>; return Promise.resolve(new Response(new ReadableStream({ start(c) { stream = c } }))) }) as typeof fetch
  spyOn(sdk, 'calendarSourcesList').mockResolvedValue({ data: [] } as never)
  spyOn(sdk, 'calendarSettingsGet').mockResolvedValue({ data: { version: 1, default_source_id: null } } as never)
  spyOn(sdk, 'promptsList').mockImplementation((async () => ({ data: [] })) as never)
  spyOn(sdk, 'skillsList').mockImplementation((async () => ({ data: [] })) as never)
  spyOn(sdk, 'chatHistory').mockImplementation((async () => { if (historyGone) throw { detail: { code: 'conversation_not_found' } }; return { data: history } }) as never)
  spyOn(sdk, 'mailSummary').mockImplementation((async () => { await summaryGate; return { data: summary } }) as never)
  spyOn(sdk, 'chatModelsList').mockImplementation((async () => ({ data: [] })) as never)
  spyOn(sdk, 'chatConversationsList').mockImplementation((async () => ({ data: [{ id: 'c', title: 'Test', model: 'm1', version: 5, prompt_revision_id: null, account_scope_json: JSON.stringify({ account_ids: selection ? [selection] : ['a'], selection }), created_at: '', updated_at: '' }] })) as never)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  function VoiceTrigger() {
    const { send } = useChatSession()
    return <button onClick={() => void send({ text: 'Hallo', conversation_id: 'c', voice_response: true }, { voiceAudio: { push() {}, end: async () => {}, fail() {} } })}>Test voice turn</button>
  }
  const root = createRootRoute({ component: () => <QueryClientProvider client={client}><ChatSessionProvider>{voiceTrigger && <VoiceTrigger />}<Outlet /></ChatSessionProvider></QueryClientProvider> })
  const start = createRoute({ getParentRoute: () => root, path: '/', validateSearch: StartRoute.options.validateSearch, component: StartRoute.options.component })
  const chat = createRoute({ getParentRoute: () => root, path: '/chat/$conversationId', component: function ChatRoute() { const { conversationId } = chat.useParams(); return <Conversation conversationId={conversationId} /> } })
  const mail = createRoute({ getParentRoute: () => root, path: '/mail/$messageId', component: () => <p>Mail</p> })
  const router = createRouter({ routeTree: root.addChildren([start, chat, mail]), history: createMemoryHistory({ initialEntries: [path] }) })
  render(<RouterProvider router={router} />)
  return { client, router }
}
function frame(value: object) { stream.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(value)}\n\n`)) }
async function send() {
  const input = await screen.findByPlaceholderText('Frag etwas zu deinen Mails …')
  fireEvent.change(input, { target: { value: 'Meine Frage' } })
  fireEvent.click(screen.getByRole('button', { name: 'Senden' }))
}
afterEach(() => { cleanup(); mock.restore(); summaryGate = null; globalThis.fetch = originalFetch; window.matchMedia = originalMatchMedia })

// The task chip opens its menu; the choices are radio items, the chip names the current one.
const taskChoice = async (chip: string, choice: string) => {
  const trigger = await screen.findByRole('button', { name: chip })
  await waitFor(() => expect((trigger as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(trigger)
  fireEvent.click(await screen.findByRole('menuitemradio', { name: new RegExp(`^${choice}`) }))
}

test('account chat explicitly includes personal tasks without granting other accounts', async () => {
  setup('/chat/c', false, 'a')
  const trigger = await screen.findByRole('button', { name: 'Keine Aufgaben' })
  await waitFor(() => expect((trigger as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(trigger)
  const items = await screen.findAllByRole('menuitemradio')
  expect(items.map(item => item.querySelector('.le-menu-title')?.textContent)).toEqual(['Keine Aufgaben', 'Alle Aufgaben', 'Aufgaben dieses Kontos'])
  expect(items[1]!.textContent).toContain('persönliche Aufgaben ohne Konto- und Mailbezug')
  expect(items[0]!.getAttribute('aria-checked')).toBe('true')
  fireEvent.click(items[1]!)
  expect(await screen.findByRole('button', { name: 'Alle Aufgaben' })).toBeTruthy()
  await send()
  expect(requestBody.account_id).toBe('a')
  expect(requestBody.task_source).toBe('account_and_unlinked')
  expect(requestBody.task_source_account_id).toBe('a')
})

test('a general chat offers only granted task accounts and has no mail account selector', async () => {
  setup()
  summary.accounts.push({ ...summary.accounts[0]!, id: 'b', display_name: 'Arbeit' })
  fireEvent.click(await screen.findByRole('button', { name: 'Keine Aufgaben' }))
  expect(screen.queryByRole('button', { name: 'Alle Konten' })).toBeNull()
  const choice = await screen.findByRole('menuitemradio', { name: /Arbeit/ })
  fireEvent.click(choice)
  await send()
  expect(requestBody).toMatchObject({ account_id: null, task_source: 'account', task_source_account_id: 'b', expected_mail_scope_version: 4 })
})

test('shows status, streams text and stops the answer', async () => {
  setup('/chat/c'); await send()
  await act(async () => { frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' }); frame({ event: 'status', code: 'searching', count: 4 }) })
  expect((await screen.findByRole('status')).textContent).toBe('Durchsuche 4 Konten …')
  expect(screen.getByRole('button', { name: 'Anhalten' })).toBeTruthy()
  await act(async () => { frame({ event: 'delta', text: 'Eine Antwort' }) })
  expect(await screen.findByText('Eine Antwort')).toBeTruthy()
  expect(document.querySelector('.le-sr[aria-live="polite"]')?.textContent).toBe('')
  fireEvent.click(screen.getByRole('button', { name: 'Anhalten' }))
  expect(signal.aborted).toBe(true)
  expect(await screen.findByText('Angehalten – die Antwort ist unvollständig.')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Noch mal fragen' }).closest('.le-chat-retry')).toBeTruthy()
})

for (const phase of ['stopped', 'error'] as const) test(`saved partial answer is shown once after ${phase}`, async () => {
  const { client } = setup('/chat/c'); await send()
  await act(async () => { frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' }); frame({ event: 'delta', text: 'Teilantwort' }) })
  expect(await screen.findByText('Teilantwort')).toBeTruthy()
  const row = (id: string, role: 'user' | 'assistant', content: string): ChatMessageOut => ({ id, conversation_id: 'c', role, content, created_at: '', extras: {}, proposal: null, proposal_status: '', stopped: role === 'assistant', context: currentContext, tool_name: '' })
  history = [row('u', 'user', 'Meine Frage'), row('a', 'assistant', 'Teilantwort')]
  if (phase === 'stopped') fireEvent.click(screen.getByRole('button', { name: 'Anhalten' }))
  else await act(async () => { frame({ event: 'error', code: 'aborted' }) })
  await waitFor(() => expect(client.getQueryData(['history', 'c'])).toEqual(history))
  await waitFor(() => expect(screen.getAllByText('Teilantwort')).toHaveLength(1))
  expect(screen.getAllByText('Meine Frage')).toHaveLength(1)
  expect(screen.getByRole('button', { name: phase === 'stopped' ? 'Noch mal fragen' : 'Noch mal versuchen' })).toBeTruthy()
})

test('coarse pointer chat controls use the large touch target token', async () => {
  const css = await Bun.file(new URL('../styles/ds-app.css', import.meta.url)).text()
  const rule = css.match(/@media \(pointer: coarse\) \{ ([^{}]+)\{ ([^{}]+) \} \}/u)
  expect(rule).toBeTruthy()
  for (const selector of ['.le-chat-suggestions .le-chip', '.le-chat-header .le-menu-anchor .le-chip', '.le-chat-end .le-btn', '.le-chat-retry .le-btn', '.le-chat-reference .le-icon-btn']) expect(rule?.[1]).toContain(selector)
  expect(rule?.[2]).toContain('min-width: var(--control-height-lg)')
  expect(rule?.[2]).toContain('min-height: var(--control-height-lg)')
})

test('navigation after start preserves the stream through done', async () => {
  const { router } = setup(); await send()
  await act(async () => { frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' }) })
  await waitFor(() => expect(router.state.location.pathname).toBe('/chat/c'))
  expect(signal.aborted).toBe(false)
  await act(async () => { frame({ event: 'delta', text: 'Kommt noch' }); frame({ event: 'done', conversation_id: 'c', message_id: 'm', stopped: false }) })
  expect(await screen.findByText('Kommt noch', { selector: 'p' })).toBeTruthy()
  const announcements = [...document.querySelectorAll('.le-chat-messages [aria-live]')].filter(element => element.textContent?.includes('Kommt noch'))
  expect(announcements).toHaveLength(1)
  expect(announcements[0]?.classList.contains('le-sr')).toBe(true)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Senden' })).toBeTruthy())
  expect(signal.aborted).toBe(false)
})

test('account scope and composer focus survive navigation to the conversation', async () => {
  const { router } = setup()
  await send()
  expect(requestBody.account_id).toBeNull()
  expect(requestBody.expected_mail_scope_version).toBe(4)
  await act(async () => { frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' }) })
  await waitFor(() => expect(router.state.location.pathname).toBe('/chat/c'))
  const input = await screen.findByPlaceholderText('Frag etwas zu deinen Mails …')
  expect(document.activeElement).toBe(input)
  await act(async () => { frame({ event: 'done', conversation_id: 'c', message_id: 'm', stopped: false }) })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Senden' })).toBeTruthy())
  fireEvent.change(input, { target: { value: 'Noch eine Frage' } })
  fireEvent.click(screen.getByRole('button', { name: 'Senden' }))
  expect(requestBody.text).toBe('Noch eine Frage')
  expect(requestBody.account_id).toBeNull()
  expect(requestBody.expected_mail_scope_version).toBe(4)
  expect(requestBody.conversation_id).toBe('c')
})

test('a live follow-up keeps the central mail grant and calendars after navigation', async () => {
  setup()
  spyOn(sdk, 'calendarSourcesList').mockResolvedValue({ data: [{ id: 'work', name: 'Arbeit', is_active: true, chat_access: 'read_only' }] } as never)
  await send()
  await act(async () => { frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' }); frame({ event: 'suggestions', items: ['Weiter fragen'] }); frame({ event: 'done', conversation_id: 'c', message_id: 'm', stopped: false }) })
  fireEvent.click(await screen.findByRole('button', { name: 'Weiter fragen' }))
  expect(requestBody.text).toBe('Weiter fragen')
  expect(requestBody.calendar_source_ids).toBeUndefined()
  expect(requestBody.account_id).toBeNull()
  expect(requestBody.expected_mail_scope_version).toBe(4)
  expect(requestBody.conversation_id).toBe('c')
})

test('a history suggestion after reload uses the saved account', async () => {
  const { client } = setup('/chat/c', false, 'a')
  await screen.findByRole('button', { name: 'Senden' })
  act(() => { client.setQueryData(['history', 'c'], [historyRow('answer', 'assistant', { suggestions: ['Weiter fragen'] })]) })
  fireEvent.click(await screen.findByRole('button', { name: 'Weiter fragen' }))
  expect(requestBody.account_id).toBe('a')
})

test('reload restores the saved account for the composer', async () => {
  setup('/chat/c', false, 'a')
  await screen.findByRole('button', { name: 'Senden' })
  await send()
  expect(requestBody.account_id).toBe('a')
})

test('the pencil on a mail card keeps the live calendar scope after navigation', async () => {
  setup()
  spyOn(sdk, 'calendarSourcesList').mockResolvedValue({ data: [{ id: 'work', name: 'Arbeit', is_active: true, chat_access: 'read_only' }] } as never)
  await send()
  await act(async () => { frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' }); frame({ event: 'mails', mails: [{ id: 'mail1', account_id: 'a', color_index: 0, subject: 'Frist', sender_name: 'Anna', snippet: 'Einwände bis Freitag möglich', internal_date: null, deadline: null, is_flagged: true }] }); frame({ event: 'done', conversation_id: 'c', message_id: 'm', stopped: false }) })
  expect(await screen.findByRole('img', { name: 'Dringend' })).toBeTruthy()
  expect(screen.getByText('Einwände bis Freitag möglich').classList.contains('le-mailref-snippet')).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Antwort entwerfen' }))
  expect(requestBody).toMatchObject({ text: 'Entwirf eine Antwort auf diese Mail.', conversation_id: 'c', context_message_id: 'mail1' })
  expect(requestBody.calendar_source_ids).toBeUndefined()
  expect(requestBody.expected_mail_scope_version).toBe(4)
})

test('an answer shows at most three follow-up suggestions', async () => {
  setup('/chat/c'); await send()
  await act(async () => { frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' }); frame({ event: 'suggestions', items: ['Eins', 'Zwei', 'Drei', 'Vier'] }); frame({ event: 'done', conversation_id: 'c', message_id: 'm', stopped: false }) })
  expect(await screen.findByRole('button', { name: 'Drei' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Vier' })).toBeNull()
  expect(document.querySelectorAll('.le-chat-suggestions .le-chip')).toHaveLength(3)
})

test('a follow-up suggestion without a selected account uses the central grant', async () => {
  setup('/chat/c'); await send()
  await act(async () => { frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' }); frame({ event: 'suggestions', items: ['Weiter fragen'] }); frame({ event: 'done', conversation_id: 'c', message_id: 'm', stopped: false }) })
  fireEvent.click(await screen.findByRole('button', { name: 'Weiter fragen' }))
  expect(requestBody.text).toBe('Weiter fragen')
  expect(requestBody.account_id).toBeNull()
})

test('history user message is not duplicated while streaming after navigation', async () => {
  const { client, router } = setup(); await send()
  history = [{ id: 'u', conversation_id: 'c', role: 'user', content: 'Meine Frage', created_at: '', extras: {}, proposal: null, proposal_status: '', stopped: false, context: currentContext, tool_name: '' }]
  await act(async () => { frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' }); frame({ event: 'delta', text: 'Antwort' }) })
  await waitFor(() => expect(router.state.location.pathname).toBe('/chat/c'))
  expect(await screen.findByText('Antwort')).toBeTruthy()
  await waitFor(() => expect(client.getQueryData(['history', 'c'])).toEqual(history))
  expect(screen.getAllByText('Meine Frage')).toHaveLength(1)
})

test('history replaces the live turn when the answer is persisted', async () => {
  const { client } = setup('/chat/c'); await send()
  await act(async () => { frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' }); frame({ event: 'delta', text: 'Gespeichert' }); frame({ event: 'done', conversation_id: 'c', message_id: 'm', stopped: false }) })
  expect(await screen.findByText('Gespeichert', { selector: 'p' })).toBeTruthy()
  const message: ChatMessageOut = { id: 'm', conversation_id: 'c', role: 'assistant', content: 'Gespeichert', created_at: '', extras: {}, proposal: null, proposal_status: '', stopped: false, context: currentContext, tool_name: '' }
  history = [message]
  act(() => { client.setQueryData(['history', 'c'], history) })
  await waitFor(() => expect(screen.queryByText('Meine Frage')).toBeNull())
  expect(screen.getAllByText('Gespeichert', { selector: 'p' })).toHaveLength(1)
})

test('selection survives the live proposal being replaced by history', async () => {
  const proposal: ChatMessageOut = { id: 'p', conversation_id: 'c', role: 'proposal', content: '', created_at: '', extras: {}, proposal_status: 'pending', stopped: false, context: currentContext, tool_name: '', proposal: { kind: 'unsubscribe', reason: 'Nicht mehr gelesen.', items: [{ id: 'a', kind: 'sender', title: 'Alpha', subtitle: '', method: 'post' }, { id: 'b', kind: 'sender', title: 'Beta', subtitle: '', method: 'post' }] } }
  const answer: ChatMessageOut = { id: 'm', conversation_id: 'c', role: 'assistant', content: 'Fertig', created_at: '', extras: {}, proposal: null, proposal_status: '', stopped: false, context: currentContext, tool_name: '' }
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockImplementation((async () => ({ data: { ...proposal, proposal_status: 'done' } })) as never)
  const { client } = setup('/chat/c')
  await waitFor(() => expect(client.getQueryData(['history', 'c'])).toEqual([]))
  let resolveHistory!: (value: { data: ChatMessageOut[] }) => void
  spyOn(sdk, 'chatHistory').mockImplementation((() => new Promise(resolve => { resolveHistory = resolve })) as never)
  await send()
  await act(async () => { frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' }); frame({ event: 'proposal', message: proposal }); frame({ event: 'done', conversation_id: 'c', message_id: 'm', stopped: false }) })
  fireEvent.click(await screen.findByRole('checkbox', { name: /Beta/ }))
  await waitFor(() => expect(resolveHistory).toBeDefined())
  await act(async () => { resolveHistory({ data: [answer, proposal] }) })
  await waitFor(() => expect(client.getQueryData(['history', 'c'])).toEqual([answer, proposal]))
  const beta = await screen.findByRole('checkbox', { name: /Beta/ })
  expect((beta as HTMLInputElement).checked).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: '1 abbestellen und archivieren' }))
  await waitFor(() => expect(confirm).toHaveBeenCalledWith({ path: { proposal_id: 'p' }, body: { selected_ids: ['a'], body: undefined }, throwOnError: true }))
  // The confirmed card replaces its button with the result; without waiting for that the update lands after the test.
  await waitFor(() => expect(screen.queryByRole('button', { name: '1 abbestellen und archivieren' })).toBeNull())
})

test('composer sends the focused account and grant revision and keeps text until acceptance', async () => {
  setup('/chat/c', false, 'a')
  await send()
  expect(requestBody.account_id).toBe('a')
  expect(requestBody.conversation_id).toBe('c')
  expect((screen.getByPlaceholderText('Frag etwas zu deinen Mails …') as HTMLTextAreaElement).value).toBe('Meine Frage')
  await act(async () => { frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' }) })
  expect((screen.getByPlaceholderText('Frag etwas zu deinen Mails …') as HTMLTextAreaElement).value).toBe('')
})

test('does not follow new text after scrolling up; button resumes following', async () => {
  setup('/chat/c'); await send()
  await act(async () => { frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' }) })
  const container = document.querySelector('.le-chat-messages') as HTMLElement
  Object.defineProperty(container, 'scrollHeight', { configurable: true, value: 1000 })
  Object.defineProperty(container, 'clientHeight', { configurable: true, value: 200 })
  Object.defineProperty(container, 'scrollTop', { configurable: true, writable: true, value: 100 })
  fireEvent.scroll(container)
  await act(async () => { frame({ event: 'delta', text: 'Neuer Text' }) })
  expect(container.scrollTop).toBe(100)
  fireEvent.click(screen.getByRole('button', { name: 'Zum Ende' }))
  expect(container.scrollTop).toBe(1000)
  expect(screen.queryByRole('button', { name: 'Zum Ende' })).toBeNull()
})

test('missing conversation in history offers a new conversation', async () => {
  const { router } = setup('/chat/c', true)
  const notice = (await screen.findByText('Diese Unterhaltung gibt es nicht mehr. Starte eine neue Unterhaltung.')).closest('[role="alert"]') as HTMLElement
  expect(within(notice).queryByRole('button', { name: 'Noch mal versuchen' })).toBeNull()
  fireEvent.click(within(notice).getByRole('button', { name: 'Neue Unterhaltung' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/'))
})

test('missing conversation in a stream offers a new conversation', async () => {
  const { router } = setup('/chat/c')
  globalThis.fetch = (() => Promise.resolve(new Response(JSON.stringify({ detail: { code: 'conversation_not_found' } }), { status: 404 }))) as unknown as typeof fetch
  await send()
  const notice = (await screen.findByText('Diese Unterhaltung gibt es nicht mehr. Starte eine neue Unterhaltung.')).closest('[role="alert"]') as HTMLElement
  expect(within(notice).queryByRole('button', { name: 'Noch mal versuchen' })).toBeNull()
  fireEvent.click(within(notice).getByRole('button', { name: 'Neue Unterhaltung' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/'))
})

for (const path of ['/', '/chat/c']) test(`invalid context names both account and calendar selection on ${path}`, async () => {
  setup(path)
  globalThis.fetch = (() => Promise.resolve(Response.json({ detail: { code: 'context_invalid' } }, { status: 422 }))) as unknown as typeof fetch
  await send()
  const notice = (await screen.findByText('Ein Konto oder Kalender aus deiner Auswahl ist nicht verfügbar. Prüfe deine Auswahl und versuch es noch mal.')).closest('[role="alert"]') as HTMLElement
  expect(within(notice).getByRole('button', { name: 'Noch mal versuchen' })).toBeTruthy()
  expect(within(notice).queryByRole('button', { name: 'Neue Unterhaltung' })).toBeNull()
})

for (const [code, action] of [
  ['preferences_unavailable', de.preferences.continueWithout],
  ['conversation_scope_change_requires_new', de.shell.newConversation],
] as const) test(`voice refusal ${code} offers the existing recovery action`, async () => {
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
    setup('/chat/c', false, null, true)
    await screen.findByPlaceholderText('Frag etwas zu deinen Mails …')
    fireEvent.click(screen.getByRole('button', { name: 'Test voice turn' }))
    await waitFor(() => expect(socket).toBeDefined())
    await act(async () => { socket.emit({ event: 'refusal', code }) })
    const notice = await screen.findByRole('alert')
    expect(within(notice).getByRole('button', { name: action })).toBeTruthy()
  } finally { globalThis.WebSocket = originalSocket }
})

test('account change in a conversation offers a new conversation instead of a retry', async () => {
  const { router } = setup('/chat/c', false, 'a')
  globalThis.fetch = (() => Promise.resolve(new Response(JSON.stringify({ detail: { code: 'conversation_scope_change_requires_new' } }), { status: 409 }))) as unknown as typeof fetch
  await send()
  const notice = (await screen.findByText(/Für andere Konten, Kalender oder Aufgaben/)).closest('[role="alert"]') as HTMLElement
  expect(within(notice).queryByRole('button', { name: 'Noch mal versuchen' })).toBeNull()
  fireEvent.click(within(notice).getByRole('button', { name: 'Neue Unterhaltung' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/'))
  expect((await screen.findByPlaceholderText('Frag etwas zu deinen Mails …') as HTMLTextAreaElement).value).toBe('Meine Frage')
  expect(screen.queryByRole('button', { name: 'Alle Konten' })).toBeNull()
})

test('in a conversation the other task choices say they need a new conversation', async () => {
  setup('/chat/c')
  const trigger = await screen.findByRole('button', { name: 'Keine Aufgaben' })
  await waitFor(() => expect((trigger as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(trigger)
  const items = await screen.findAllByRole('menuitemradio')
  expect(items[0]!.textContent).not.toContain('Braucht eine neue Unterhaltung.')
  for (const item of items.slice(1)) expect(item.textContent).toContain('Braucht eine neue Unterhaltung.')
})

test('a restored task choice the list cannot offer still names itself', async () => {
  const { client } = setup('/chat/c')
  const trigger = await screen.findByRole('button', { name: 'Keine Aufgaben' })
  await waitFor(() => expect((trigger as HTMLButtonElement).disabled).toBe(false))
  const context = { id: 'ctx', mail_scope_version: 4, account_scope_json: '{}', app_prompt_version: '1', preference_revision_id: null, prompt_hash: null, prompt_revision_id: null, revoked: false, task_scope_json: JSON.stringify({ source: 'account', account_id: 'gone' }) } as ChatMessageOut['context']
  act(() => { client.setQueryData(['history', 'c'], [{ ...historyRow('a1', 'assistant'), context }]) })
  expect(await screen.findByRole('button', { name: 'Aufgaben: Gespeichertes Konto nicht verfügbar' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Keine Aufgaben' })).toBeNull()
})

const restoredContext = (scope: object) => ({ id: 'ctx', mail_scope_version: 4, account_scope_json: '{}', app_prompt_version: '1', preference_revision_id: null, prompt_hash: null, prompt_revision_id: null, revoked: false, task_scope_json: JSON.stringify(scope) }) as ChatMessageOut['context']

test('a restored task choice never repeats an offered label', async () => {
  const { client } = setup('/chat/c', false, 'a')
  const trigger = await screen.findByRole('button', { name: 'Keine Aufgaben' })
  await waitFor(() => expect((trigger as HTMLButtonElement).disabled).toBe(false))
  act(() => { client.setQueryData(['history', 'c'], [{ ...historyRow('a1', 'assistant'), context: restoredContext({ source: 'all' }) }]) })
  fireEvent.click(await screen.findByRole('button', { name: 'Gespeichert: Alle Aufgaben' }))
  const names = (await screen.findAllByRole('menuitemradio')).map(item => item.querySelector('.le-menu-title')?.textContent)
  expect(names).toEqual(['Keine Aufgaben', 'Alle Aufgaben', 'Aufgaben dieses Kontos', 'Gespeichert: Alle Aufgaben'])
})

test('a restored account-and-personal choice without a mail account is marked as saved', async () => {
  const { client } = setup('/chat/c')
  const trigger = await screen.findByRole('button', { name: 'Keine Aufgaben' })
  await waitFor(() => expect((trigger as HTMLButtonElement).disabled).toBe(false))
  act(() => { client.setQueryData(['history', 'c'], [{ ...historyRow('a1', 'assistant'), context: restoredContext({ source: 'account_and_unlinked', account_id: 'a' }) }]) })
  fireEvent.click(await screen.findByRole('button', { name: 'Gespeichert: Alle Aufgaben' }))
  const names = (await screen.findAllByRole('menuitemradio')).map(item => item.querySelector('.le-menu-title')?.textContent)
  expect(new Set(names).size).toBe(names.length)
})

test('a restored account the menu does not offer shows its listed name', async () => {
  const { client } = setup('/chat/c', false, 'a')
  summary.accounts.push({ ...summary.accounts[0]!, id: 'b', display_name: 'Arbeit', email_address: 'work@example.org' })
  const trigger = await screen.findByRole('button', { name: 'Keine Aufgaben' })
  await waitFor(() => expect((trigger as HTMLButtonElement).disabled).toBe(false))
  act(() => { client.setQueryData(['history', 'c'], [{ ...historyRow('a1', 'assistant'), context: restoredContext({ source: 'account', account_id: 'b' }) }]) })
  expect(await screen.findByRole('button', { name: 'Aufgaben: Arbeit' })).toBeTruthy()
})

test('a restored account choice waits for the account list before calling the account unavailable', async () => {
  let release = () => {}
  summaryGate = new Promise<void>(resolve => { release = resolve })
  const { client } = setup('/chat/c')
  const trigger = await screen.findByRole('button', { name: 'Keine Aufgaben' })
  await waitFor(() => expect((trigger as HTMLButtonElement).disabled).toBe(false))
  act(() => { client.setQueryData(['history', 'c'], [{ ...historyRow('a1', 'assistant'), context: restoredContext({ source: 'account', account_id: 'gone' }) }]) })
  expect(await screen.findByRole('button', { name: 'Aufgaben: …' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: /nicht verfügbar/ })).toBeNull()
  await act(async () => { release() })
  expect(await screen.findByRole('button', { name: 'Aufgaben: Gespeichertes Konto nicht verfügbar' })).toBeTruthy()
})

test.each([null, 'a'])('a restarted chat preserves the permitted mail and task focus %s', async selection => {
  const { router } = setup('/chat/c', false, selection)
  await taskChoice('Keine Aufgaben', 'Alle Aufgaben')
  globalThis.fetch = ((_url: unknown, init: RequestInit) => {
    requestBody = JSON.parse(String(init.body))
    return Promise.resolve(Response.json({ detail: { code: 'conversation_scope_change_requires_new' } }, { status: 409 }))
  }) as typeof fetch
  await send()
  const notice = (await screen.findByText(/Für andere Konten, Kalender oder Aufgaben/)).closest('[role="alert"]') as HTMLElement
  expect(requestBody.task_source).toBe(selection ? 'account_and_unlinked' : 'all')
  fireEvent.click(within(notice).getByRole('button', { name: 'Neue Unterhaltung' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/'))
  expect(await screen.findByRole('button', { name: 'Alle Aufgaben' })).toBeTruthy()
  await send()
  await waitFor(() => expect(requestBody.conversation_id).toBeUndefined())
  expect(requestBody.task_source).toBe(selection ? 'account_and_unlinked' : 'all')
  expect(requestBody.account_id).toBe(selection)
  if (selection) expect(requestBody.task_source_account_id).toBe(selection)
  expect(requestBody.text).toBe('Meine Frage')
  expect(requestBody.history).toBeUndefined()
})

test('a newly readable calendar requires a fresh chat and keeps the previous turn restricted', async () => {
  const { client, router } = setup('/chat/c', false, 'a')
  const source = { id: 'work', name: 'Arbeitskalender', is_active: true, chat_access: 'read_only' }
  const prior = { ...historyRow('saved', 'assistant'), context: { ...currentContext, calendar_scope_json: '[]' } }
  await screen.findByRole('button', { name: 'Senden' })
  act(() => client.setQueryData(['history', 'c'], [prior]))
  expect(await screen.findByRole('button', { name: 'Senden' })).toBeTruthy()
  spyOn(sdk, 'calendarSourcesList').mockResolvedValue({ data: [source] } as never)
  act(() => client.setQueryData(['calendar', 'sources'], [source]))
  const notice = await screen.findByText(de.calendarSettings.restart)
  expect(screen.queryByRole('button', { name: 'Senden' })).toBeNull()
  fireEvent.click(within(notice.closest('[role="status"], [role="alert"]') ?? notice.parentElement!).getByRole('button', { name: de.prompts.restart }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/'))
  await send()
  expect(requestBody.conversation_id).toBeUndefined()
  expect(requestBody.calendar_source_ids).toBeUndefined()
  expect(requestBody.account_id).toBe('a')
  expect(requestBody.history).toBeUndefined()
  expect(prior.context.calendar_scope_json).toBe('[]')
})

test('revoked context during an answer offers a fresh conversation', async () => {
  setup('/chat/c')
  const frames = ['event: start\ndata: {"event":"start","conversation_id":"c","user_message_id":"u"}\n\n', 'event: error\ndata: {"event":"error","code":"context_revoked"}\n\n']
  globalThis.fetch = (() => Promise.resolve(new Response(frames.join(''), { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))) as unknown as typeof fetch
  await send()
  const notice = (await screen.findByText(/Die Vorlage, Präferenzen oder die Auswahl von Konten, Kalendern oder Aufgaben sind nicht mehr gültig/)).closest('[role="alert"]') as HTMLElement
  expect(within(notice).getByRole('button', { name: 'Neue Unterhaltung' })).toBeTruthy()
})

test('a conversation renders both header menus without a duplicate key', async () => {
  const error = spyOn(console, 'error').mockImplementation(() => {})
  setup('/chat/c')
  await screen.findByPlaceholderText('Frag etwas zu deinen Mails …')
  const duplicate = error.mock.calls.filter(call => call.some(part => typeof part === 'string' && part.includes('same key')))
  expect(duplicate).toEqual([])
})

test('model selection errors do not follow a conversation switch', async () => {
  spyOn(sdk, 'chatConversationUpdate').mockImplementation((async () => { throw { detail: { code: 'model_unknown' } } }) as never)
  const { client, router } = setup('/chat/c')
  await screen.findByPlaceholderText('Frag etwas zu deinen Mails …')
  act(() => { client.setQueryData(['models'], [{ id: 'm1', label: 'Modell Eins', group: 'general', is_default: true }, { id: 'm2', label: 'Modell Zwei', group: 'general', is_default: false }]) })
  fireEvent.click(await screen.findByRole('button', { name: /Modell Eins/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: /Modell Zwei/ }))
  expect(await screen.findByText('Dieses Modell gibt es nicht mehr. Wähle ein anderes.')).toBeTruthy()
  await act(async () => { await router.navigate({ to: '/chat/$conversationId', params: { conversationId: 'b' } }) })
  expect(screen.queryByText('Dieses Modell gibt es nicht mehr. Wähle ein anderes.')).toBeNull()
})

test('template selection errors do not follow a conversation switch', async () => {
  spyOn(sdk, 'chatConversationUpdate').mockImplementation((async () => { throw { detail: { code: 'version_conflict' } } }) as never)
  const { client, router } = setup('/chat/c')
  await waitFor(() => expect(client.getQueryData(['conversations'])).toBeTruthy())
  const saved = client.getQueryData<Record<string, unknown>[]>(['conversations'])![0]!
  act(() => {
    client.setQueryData(['conversations'], [saved, { ...saved, id: 'b' }])
    client.setQueryData(['prompts'], [{ id: 't', name: 'Fristen', text: 'Kurz', version: 1, revision_id: 'r1', content_hash: 'h1', updated_at: null }])
  })
  const input = await screen.findByPlaceholderText('Frag etwas zu deinen Mails …')
  fireEvent.change(input, { target: { value: '/', selectionStart: 1 } })
  fireEvent.click(await screen.findByRole('option', { name: /Fristen/ }))
  await screen.findByRole('button', { name: 'Aktuelle Fassung laden' })
  await act(async () => { await router.navigate({ to: '/chat/$conversationId', params: { conversationId: 'b' } }) })
  expect(screen.queryByRole('button', { name: 'Aktuelle Fassung laden' })).toBeNull()
})

test('restart after template removal keeps the unsent text', async () => {
  const { client, router } = setup('/chat/c', false, 'a')
  await screen.findByRole('button', { name: 'Senden' })
  const saved = client.getQueryData<Record<string, unknown>[]>(['conversations'])![0]!
  act(() => { client.setQueryData(['conversations'], [{ ...saved, prompt_revision_id: 'removed' }]) })
  const input = screen.getByPlaceholderText('Frag etwas zu deinen Mails …')
  fireEvent.change(input, { target: { value: 'Noch offen' } })
  fireEvent.click(await screen.findByRole('button', { name: 'Neue Unterhaltung ohne bisherigen Kontext' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/'))
  expect(screen.queryByRole('button', { name: 'Privat' })).toBeNull()
  expect((screen.getByPlaceholderText('Frag etwas zu deinen Mails …') as HTMLTextAreaElement).value).toBe('Noch offen')
})

test('restart from a revoked answer keeps the unsent text', async () => {
  const { client, router } = setup('/chat/c', false, 'a')
  await screen.findByRole('button', { name: 'Senden' })
  await waitFor(() => expect(client.getQueryData(['history', 'c'])).toEqual([]))
  act(() => { client.setQueryData(['history', 'c'], [{ ...historyRow('revoked', 'assistant'), context: { id: 'ctx', mail_scope_version: 4, account_scope_json: '{}', app_prompt_version: '1', preference_revision_id: null, prompt_hash: null, prompt_revision_id: null, revoked: true } }]) })
  fireEvent.change(screen.getByPlaceholderText('Frag etwas zu deinen Mails …'), { target: { value: 'Noch offen' } })
  fireEvent.click(await screen.findByRole('button', { name: 'Neue Unterhaltung ohne bisherigen Kontext' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/'))
  expect(screen.queryByRole('button', { name: 'Privat' })).toBeNull()
  expect((screen.getByPlaceholderText('Frag etwas zu deinen Mails …') as HTMLTextAreaElement).value).toBe('Noch offen')
})

test('unknown model refusal renders its explanation', async () => {
  spyOn(sdk, 'chatConversationUpdate').mockImplementation((async () => { throw { detail: { code: 'model_unknown' } } }) as never)
  const { client } = setup('/chat/c')
  spyOn(sdk, 'chatModelsList').mockResolvedValue({ data: [{ id: 'm1', label: 'Modell Eins', group: 'general', is_default: true }] } as never)
  await screen.findByPlaceholderText('Frag etwas zu deinen Mails …')
  act(() => { client.setQueryData(['models'], [{ id: 'm1', label: 'Modell Eins', group: 'general', is_default: true }, { id: 'm2', label: 'Modell Zwei', group: 'general', is_default: false }]) })
  fireEvent.click(await screen.findByRole('button', { name: /Modell Eins/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: /Modell Zwei/ }))
  expect(await screen.findByText('Dieses Modell gibt es nicht mehr. Wähle ein anderes.')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Modell Eins' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Modell Eins' }))
  expect(screen.queryByRole('menuitemradio', { name: /Modell Zwei/ })).toBeNull()
})

const modelChoices = [
  { id: 'm1', label: 'Modell Eins', group: 'general', description: 'Schnell', is_default: true },
  { id: 'm2', label: 'Modell Zwei', group: 'general', description: 'Genau', is_default: false },
]

test('the saved model survives switching conversations and a fresh page with an older answer', async () => {
  let saved: ConversationOut = { id: 'c', title: 'Test', model: 'm1', version: 5, prompt_revision_id: null, account_scope_json: null, created_at: '', updated_at: '' }
  function openPage() {
    const { router } = setup('/chat/c')
    history = [historyRow('old', 'assistant', { model: 'm1' })]
    spyOn(sdk, 'chatHistory').mockImplementation(async options => ({ data: options.query.conversation_id === 'c' ? history : [] }) as never)
    spyOn(sdk, 'chatModelsList').mockResolvedValue({ data: modelChoices } as never)
    spyOn(sdk, 'chatConversationsList').mockImplementation(async () => ({ data: [saved, { ...saved, id: 'other', model: 'retired-model' }] }) as never)
    return router
  }
  openPage()
  const update = spyOn(sdk, 'chatConversationUpdate').mockImplementation(async () => {
    saved = { ...saved, model: 'm2', version: 6 }
    return { data: saved } as never
  })
  fireEvent.click(await screen.findByRole('button', { name: /Modell Eins/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Modell Zwei' }))
  await waitFor(() => expect(update).toHaveBeenCalledWith({ path: { conversation_id: 'c' }, body: { model: 'm2', expected_version: 5 }, throwOnError: true }))
  expect(await screen.findByRole('button', { name: 'Modell Zwei' })).toBeTruthy()
  cleanup()
  const router = openPage()
  expect(await screen.findByRole('button', { name: 'Modell Zwei' })).toBeTruthy()
  await screen.findByText('Antwort old')
  expect(document.querySelector('.le-msg-ai small')?.textContent).toBe('Modell Eins')
  await act(async () => { await router.navigate({ to: '/chat/$conversationId', params: { conversationId: 'other' } }) })
  expect(await screen.findByRole('button', { name: 'retired-model' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'retired-model' }))
  const missing = screen.getByRole('menuitemradio', { name: /retired-model/, checked: true })
  expect(missing.getAttribute('aria-disabled')).toBe('true')
  expect(missing.textContent).toContain('In der aktuellen Modellliste nicht verfügbar.')
  fireEvent.keyDown(missing, { key: 'Escape' })
  await act(async () => { await router.navigate({ to: '/chat/$conversationId', params: { conversationId: 'c' } }) })
  expect(await screen.findByRole('button', { name: 'Modell Zwei' })).toBeTruthy()
})

test('a live answer keeps the model announced by the server when the saved choice changes', async () => {
  const { client } = setup('/chat/c')
  spyOn(sdk, 'chatModelsList').mockResolvedValue({ data: modelChoices } as never)
  await send()
  await act(async () => {
    frame({ event: 'start', conversation_id: 'c', user_message_id: 'u', model: 'm2' })
    frame({ event: 'delta', text: 'Laufende Antwort' })
  })
  await screen.findByText('Laufende Antwort')
  expect(document.querySelector('.le-msg-ai small')?.textContent).toBe('Modell Zwei')
  act(() => { client.setQueryData<ConversationOut[]>(['conversations'], rows => rows?.map(row => ({ ...row, model: 'next-model', version: row.version + 1 }))) })
  const chip = await screen.findByRole('button', { name: 'next-model' })
  expect((chip as HTMLButtonElement).disabled).toBe(true)
  expect(document.querySelector('.le-msg-ai small')?.textContent).toBe('Modell Zwei')
})

test('a stream without model metadata shows the answer without guessing a model', async () => {
  setup('/chat/c')
  spyOn(sdk, 'chatModelsList').mockResolvedValue({ data: modelChoices } as never)
  await send()
  await act(async () => {
    frame({ event: 'start', conversation_id: 'c', user_message_id: 'u' })
    frame({ event: 'delta', text: 'Text aus einem älteren Stream' })
  })
  await screen.findByText('Text aus einem älteren Stream')
  expect(document.querySelector('.le-msg-ai small')?.textContent).toBeUndefined()
})

test('a model conflict reloads the conversation and only an explicit retry uses its new version', async () => {
  const { client } = setup('/chat/c')
  let reads = 0
  const refreshed = { id: 'c', title: 'Test', model: 'm3', version: 6, prompt_revision_id: null, account_scope_json: null, created_at: '', updated_at: '' }
  let saved = { ...refreshed, model: 'm1', version: 5 }
  spyOn(sdk, 'chatConversationsList').mockImplementation(async () => { reads++; return { data: [saved] } as never })
  const update = spyOn(sdk, 'chatConversationUpdate').mockImplementationOnce(async () => { saved = refreshed; throw { detail: { code: 'version_conflict' }, status: 409 } })
    .mockImplementation(async () => { saved = { ...refreshed, model: 'm2', version: 7 }; return { data: saved } as never })
  await screen.findByPlaceholderText('Frag etwas zu deinen Mails …')
  act(() => { client.setQueryData(['models'], [{ id: 'm1', label: 'Modell Eins', group: 'general', is_default: true }, { id: 'm2', label: 'Modell Zwei', group: 'general' }, { id: 'm3', label: 'Modell Drei', group: 'general' }]) })
  fireEvent.click(await screen.findByRole('button', { name: /Modell Eins/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: /Modell Zwei/ }))
  expect(await screen.findByText('Die Unterhaltung wurde inzwischen geändert. Der aktuelle Stand ist geladen. Prüfe das Modell und wähle bei Bedarf erneut.')).toBeTruthy()
  expect(client.getQueryData(['conversations'])).toEqual([refreshed])
  expect(update).toHaveBeenCalledTimes(1)
  expect(reads).toBeGreaterThanOrEqual(2)
  expect(update).toHaveBeenCalledWith({ path: { conversation_id: 'c' }, body: { model: 'm2', expected_version: 5 }, throwOnError: true })
  fireEvent.click(await screen.findByRole('button', { name: /Modell Drei/ }))
  fireEvent.click(screen.getByRole('menuitemradio', { name: /Modell Zwei/ }))
  await waitFor(() => expect(update).toHaveBeenLastCalledWith({ path: { conversation_id: 'c' }, body: { model: 'm2', expected_version: 6 }, throwOnError: true }))
  expect(await screen.findByRole('button', { name: /Modell Zwei/ })).toBeTruthy()
})

test('older mail references keep their stored answer without opening a live preview', async () => {
  const { client } = setup('/chat/c')
  const scoped = spyOn(sdk, 'chatMailReferenceGet')
  const inbox = spyOn(sdk, 'mailMessageGet')
  await waitFor(() => expect(client.getQueryData(['history', 'c'])).toEqual([]))
  const row = historyRow('previous', 'assistant', { mails: ['old-mail'] })
  act(() => { client.setQueryData(['history', 'c'], [{ ...row, context: { ...currentContext, mail_scope_version: 3 } }]) })
  expect(await screen.findByText('Antwort previous')).toBeTruthy()
  expect(scoped).not.toHaveBeenCalled()
  expect(inbox).not.toHaveBeenCalled()
})

test('a missing history mail reference is requested only once', async () => {
  const get = spyOn(sdk, 'chatMailReferenceGet').mockImplementation((async () => ({ response: new Response(null, { status: 404 }) })) as never)
  const { client } = setup('/chat/c')
  await waitFor(() => expect(client.getQueryData(['history', 'c'])).toEqual([]))
  const row: ChatMessageOut = { id: 'm', conversation_id: 'c', role: 'assistant', content: 'Antwort', created_at: '', extras: { mails: ['missing'] }, proposal: null, proposal_status: '', stopped: false, context: currentContext, tool_name: '' }
  act(() => { client.setQueryData(['history', 'c'], [row]) })
  await waitFor(() => expect(client.getQueryState(['chat-mail-reference', 'missing', 4, currentContext.id])?.status).toBe('error'))
  expect(client.getQueryState(['chat-mail-reference', 'missing', 4, currentContext.id])?.fetchFailureCount).toBe(1)
  expect(get).toHaveBeenCalledTimes(1)
})

test('history renders safe text, model, mail account and only the latest suggestions', async () => {
  const { client, router } = setup('/chat/c')
  spyOn(sdk, 'chatMailReferenceGet').mockImplementation((async () => ({ data: { id: 'mail', account_id: 'a', color_index: 0, sender_name: 'Absender', subject: '<b>Betreff</b>', internal_date: null, deadline: null, is_flagged: true } })) as never)
  const row =(id: string, content: string, extras: ChatMessageOut['extras']): ChatMessageOut => ({ id, conversation_id: 'c', role: 'assistant', content, created_at: '', extras, proposal: null, proposal_status: '', stopped: false, context: currentContext, tool_name: '' })
  await screen.findByPlaceholderText('Frag etwas zu deinen Mails …')
  await waitFor(() => expect(screen.queryByText('Unterhaltung wird geladen …')).toBeNull())
  act(() => {
    client.setQueryData(['summary', 'chat'], { ...summary, accounts: [{ id: 'a', display_name: '', email_address: 'ich@example.com', provider: 'gmail' }] })
    client.setQueryData(['models'], [{ id: 'model', label: 'Modellname', group: 'general', description: '', is_default: true }])
    client.setQueryData(['history', 'c'], [row('old', 'Alt', { suggestions: ['Alte Frage'] }), row('new', 'Neu', { model: 'model', mails: ['mail'], suggestions: ['Weiter fragen'] })])
  })
  expect(await screen.findByText('<b>Betreff</b>')).toBeTruthy()
  expect(screen.getByText('Gmail')).toBeTruthy()
  expect(screen.getByRole('img', { name: 'Dringend' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Antwort entwerfen' })).toBeTruthy()
  expect(document.querySelector('.le-msg-ai small')?.textContent).toBe('Modellname')
  expect(screen.getByRole('button', { name: 'm1' })).toBeTruthy()
  expect(screen.queryByText('Alte Frage')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Weiter fragen' }))
  expect(requestBody.text).toBe('Weiter fragen')
  expect(requestBody.conversation_id).toBe('c')
  fireEvent.click(screen.getByRole('button', { name: 'Mail öffnen' }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/mail/mail'))
})

const historyRow = (id: string, role: string, extras: ChatMessageOut['extras'] = {}): ChatMessageOut => ({ id, conversation_id: 'c', role, content: role === 'assistant' ? `Antwort ${id}` : role === 'user' ? `Frage ${id}` : '', created_at: '', extras, proposal: role === 'proposal' ? { kind: 'archive', reason: 'Erledigt.', items: [{ id: 'm', kind: 'mail', title: 'Rechnung', subtitle: 'Anna' }] } : null, proposal_status: role === 'proposal' ? 'pending' : '', stopped: false, context: currentContext, tool_name: '' })

test('turn order puts a proposal after the answer of its turn', () => {
  const rows = ['u1:user', 'p1:proposal', 'a1:assistant', 'u2:user', 'a2:assistant', 'u3:user', 'p3:proposal'].map(entry => { const [id, role] = entry.split(':') as [string, string]; return historyRow(id, role) })
  expect(turnOrder(rows).map(row => row.id)).toEqual(['u1', 'a1', 'p1', 'u2', 'a2', 'u3', 'p3'])
  expect(turnOrder([])).toEqual([])
})

test('history shows answer, card and follow-ups in the same order as live', async () => {
  const { client } = setup('/chat/c')
  await screen.findByPlaceholderText('Frag etwas zu deinen Mails …')
  await waitFor(() => expect(screen.queryByText('Unterhaltung wird geladen …')).toBeNull())
  act(() => { client.setQueryData(['history', 'c'], [historyRow('u', 'user'), historyRow('p', 'proposal'), historyRow('a', 'assistant', { suggestions: ['Weiter fragen'] })]) })
  const answer = await screen.findByText('Antwort a')
  const card = screen.getByText('1 Mail archivieren?')
  const chip = screen.getByRole('button', { name: 'Weiter fragen' })
  expect(answer.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(card.compareDocumentPosition(chip) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(screen.getAllByRole('button', { name: 'Weiter fragen' })).toHaveLength(1)
})


test.each([null, { ...currentContext, mail_scope_version: 3 }])('an earlier or unverified conversation is readable and offers a fresh start', async stale => {
  const { client, router } = setup('/chat/c')
  await screen.findByPlaceholderText(de.chat.placeholder)
  const rows = [historyRow('old', 'assistant', { suggestions: ['Old suggestion'] }), historyRow('card', 'proposal')]
  act(() => { client.setQueryData(['history', 'c'], rows.map(row => ({ ...row, context: stale }))) })
  expect(await screen.findByText('Antwort old')).toBeTruthy()
  expect(await screen.findByText(de.chatAccounts.restart)).toBeTruthy()
  expect(screen.getByText(de.chatAccounts.cancelOnly)).toBeTruthy()
  expect(screen.getByRole('button', { name: de.controls.cancel })).toBeTruthy()
  expect(screen.queryByPlaceholderText(de.chat.placeholder)).toBeNull()
  expect(screen.queryByText('Old suggestion')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: de.prompts.restart }))
  await waitFor(() => expect(router.state.location.pathname).toBe('/'))
  expect((await screen.findByPlaceholderText(de.chat.placeholder) as HTMLTextAreaElement).value).toBe('')
})


test.each(['loading', 'playing'])('changing a grant stops %s speech', async phase => {
  const previousAudio = Object.getOwnPropertyDescriptor(globalThis, 'Audio')
  const pause = mock(() => {})
  const play = mock(async () => {})
  class FakeAudio extends EventTarget {
    play = play
    pause = pause
    load() {}
    removeAttribute() {}
  }
  Object.defineProperty(globalThis, 'Audio', { value: FakeAudio, configurable: true })
  try {
    let finish!: () => void
    const download = new Promise<void>(resolve => { finish = resolve })
    let requestSignal: AbortSignal | undefined
    const speech = spyOn(sdk, 'audioSpeech').mockImplementation((async ({ signal }: { signal: AbortSignal }) => {
      requestSignal = signal
      await download
      return { data: new Blob(['audio'], { type: 'audio/mpeg' }) }
    }) as never)
    spyOn(sdk, 'audioCapabilitiesGet').mockResolvedValue({ data: { tts: { status: 'available', formats: ['audio/mpeg'] } } } as never)
    const { client } = setup('/chat/c')
    await screen.findByPlaceholderText(de.chat.placeholder)
    act(() => { client.setQueryData(['history', 'c'], [historyRow('answer', 'assistant')]) })
    fireEvent.click(await screen.findByRole('button', { name: de.audio.speech }))
    await waitFor(() => expect(speech).toHaveBeenCalledTimes(1))
    if (phase === 'playing') {
      await act(async () => { finish() })
      await screen.findByText(de.audio.speechPlaying)
    }
    act(() => { client.setQueryData(['summary', 'chat'], { ...summary, mail_scope_version: 5 }) })
    await screen.findByText(de.chatAccounts.restart)
    expect(requestSignal?.aborted).toBe(true)
    expect(screen.queryByRole('button', { name: de.audio.speechStop })).toBeNull()
    await act(async () => { finish() })
    expect(play).toHaveBeenCalledTimes(phase === 'playing' ? 1 : 0)
    expect(pause).toHaveBeenCalledTimes(phase === 'playing' ? 1 : 0)
  } finally {
    if (previousAudio) Object.defineProperty(globalThis, 'Audio', previousAudio)
    else Reflect.deleteProperty(globalThis, 'Audio')
  }
})

test('adding another readable calendar still allows confirmation of the original named target', async () => {
  const { client } = setup('/chat/c')
  await screen.findByPlaceholderText('Frag etwas zu deinen Mails …')
  const sources = ['work', 'second-team'].map(id => ({ id, name: 'Team', is_active: true, chat_access: 'writable' }))
  const card: ChatMessageOut = {
    ...historyRow('bound', 'proposal'), context: { ...currentContext, calendar_scope_json: '["second-team"]' },
    proposal: { kind: 'calendar', reason: 'Test', items: [], calendar: {
      action: 'create', source_id: 'second-team', timezone: 'UTC', after: { title: 'Review', start_date: '2026-11-21', end_date: '2026-11-22' },
    } },
  }
  history = [card]
  spyOn(sdk, 'calendarSourcesList').mockResolvedValue({ data: sources } as never)
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockResolvedValue({ data: { ...card, proposal_status: 'done' }, response: { status: 200 } } as never)
  act(() => {
    client.setQueryData(['calendar', 'sources'], sources)
    client.setQueryData(['history', 'c'], history)
  })
  await screen.findByText(de.calendarSettings.restart)
  expect(screen.queryByPlaceholderText('Frag etwas zu deinen Mails …')).toBeNull()
  await screen.findByText('Team · second-team')
  expect(screen.queryByText('Team · work')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: de.calendar.cardConfirm }))
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
  expect(confirm.mock.calls[0]?.[0]).toEqual({ path: { proposal_id: 'bound' }, body: { selected_ids: [] } })
})

test('an unavailable calendar list can be retried before confirming the pending card', async () => {
  const { client } = setup('/chat/c')
  await screen.findByPlaceholderText('Frag etwas zu deinen Mails …')
  const card: ChatMessageOut = {
    ...historyRow('pending', 'proposal'), context: { ...currentContext, calendar_scope_json: '["work"]' },
    proposal: { kind: 'calendar', reason: 'Test', items: [], calendar: { action: 'create', source_id: 'work', timezone: 'UTC' } },
  }
  const sources = [{ id: 'work', name: 'Work', is_active: true, chat_access: 'writable' }]
  const list = spyOn(sdk, 'calendarSourcesList').mockRejectedValue(new Error('synthetic failure'))
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockResolvedValue({ data: { ...card, proposal_status: 'done' }, response: { status: 200 } } as never)
  history = [card]
  act(() => {
    client.setQueryData(['calendar', 'sources'], sources)
    client.setQueryData(['history', 'c'], history)
  })
  await act(() => client.refetchQueries({ queryKey: ['calendar', 'sources'] }))
  await screen.findByText(de.calendar.error)
  expect(screen.getByRole<HTMLButtonElement>('button', { name: de.calendar.cardConfirm }).disabled).toBe(true)
  expect(screen.queryByText(de.calendarSettings.cancelOnly)).toBeNull()
  list.mockResolvedValue({ data: sources } as never)
  fireEvent.click(screen.getByRole('button', { name: de.calendar.retry }))
  await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: de.calendar.cardConfirm }).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: de.calendar.cardConfirm }))
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1))
})

test('the original pending card becomes confirmable when a failed mail grant lookup recovers', async () => {
  const { client } = setup('/chat/c')
  const mail = spyOn(sdk, 'mailSummary').mockRejectedValue(new Error('synthetic summary failure'))
  const card = { ...historyRow('original', 'proposal'), context: { ...currentContext, calendar_scope_json: '[]' } }
  const confirm = spyOn(sdk, 'chatProposalConfirm').mockResolvedValue({ data: { ...card, proposal_status: 'done' } } as never)
  history = [card]
  act(() => client.setQueryData(['history', 'c'], [card]))
  await act(() => client.refetchQueries({ queryKey: ['summary'] }))
  const failure = await screen.findByText(de.chat.summaryError)
  expect(screen.queryByText(de.chatAccounts.cancelOnly)).toBeNull()
  expect(screen.queryByRole('button', { name: de.proposal.confirm.archive(1) })).toBeNull()
  mail.mockResolvedValue({ data: summary } as never)
  fireEvent.click(within(failure.closest('[role="alert"]') as HTMLElement).getByRole('button', { name: de.chat.retry }))
  const recovered = await screen.findByRole<HTMLButtonElement>('button', { name: de.proposal.confirm.archive(1) })
  expect(recovered.disabled).toBe(false)
  expect(recovered.closest('.le-chat-entry')?.id).toBe('message-original')
  fireEvent.click(recovered)
  await waitFor(() => expect(confirm.mock.calls[0]?.[0]?.path).toEqual({ proposal_id: 'original' }))
})
