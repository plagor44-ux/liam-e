// i18n-exempt: synthetic fixtures and route tests.
import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from '@tanstack/react-router'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import type { ChatMessageOut, ChatModelOut, ConversationOut, PromptDetailOut, PromptOut, SkillOut } from '../api/generated/types.gen'
import { Route as StartRoute } from '../routes/_app/index'
import { de } from '../i18n/de'
import { en } from '../i18n/en'
import { I18nProvider } from '../i18n/I18n'
import { Conversation } from './Conversation'
import { ChatSessionProvider } from './session'
import { SKILL_FAVORITES_STORAGE_KEY } from './skill-favorites'
import { buildSlashOptions, detectSlashQuery, stripSlashToken } from './skill-selection'

const originalFetch = globalThis.fetch
const originalMatchMedia = window.matchMedia
const originalAudio = { AudioContext: globalThis.AudioContext, WebSocket: globalThis.WebSocket, secure: globalThis.isSecureContext, media: navigator.mediaDevices }

const dailySkill: SkillOut = {
  id: 'sk-daily',
  slug: 'daily_briefing',
  origin: 'builtin',
  status: 'active',
  name: 'Tagesbriefing',
  description: 'Fasst Termine, Fristen und wichtige Mails für heute zusammen.',
  version: 2,
  revision_id: 'skrev-daily-2',
  instructions: 'Erstelle einen kompakten Tagesüberblick.',
  workflow_steps: [
    { id: 'collect', title: 'Tagesdaten sammeln', description: 'Kalender, Fristen und Aufgaben prüfen.', tool_names: ['collect_daily_briefing'] },
  ],
  allowed_tools: ['collect_daily_briefing', 'get_task'],
  content_hash: 'h-daily-2',
  updated_at: '2026-11-21T08:00:00Z',
}

const digestSkill: SkillOut = {
  id: 'sk-digest',
  slug: 'postfach_digest',
  origin: 'custom',
  status: 'active',
  name: 'Postfach-Digest',
  description: 'Ordnet ungelesene Nachrichten nach Dringlichkeit.',
  version: 1,
  revision_id: 'skrev-digest-1',
  instructions: 'Gruppiere ungelesene Nachrichten.',
  workflow_steps: [],
  allowed_tools: ['search_messages'],
  content_hash: 'h-digest-1',
  updated_at: '2026-11-21T08:00:00Z',
}

const disabledSkill: SkillOut = {
  id: 'sk-old',
  slug: 'archiv_helfer',
  origin: 'custom',
  status: 'disabled',
  name: 'Archivhelfer',
  description: 'Deaktivierter Helfer.',
  version: 1,
  revision_id: 'skrev-old-1',
  instructions: 'Alt',
  workflow_steps: [],
  allowed_tools: [],
  content_hash: 'h-old-1',
  updated_at: '2026-11-21T08:00:00Z',
}

const promptTemplate: PromptOut = {
  id: 'pr-1',
  name: 'Fristen',
  text: 'Fasse offene Fristen mit Datum zusammen.',
  version: 2,
  revision_id: 'pr-rev-2',
  content_hash: 'p-hash-2',
  updated_at: '2026-11-20T12:00:00Z',
}

const promptDetail: PromptDetailOut = {
  ...promptTemplate,
  revisions: [
    { id: 'pr-rev-2', number: 2, content: promptTemplate.text, content_hash: 'p-hash-2' },
    { id: 'pr-rev-1', number: 1, content: 'Nenne alle Fristen kurz.', content_hash: 'p-hash-1' },
  ],
}

const defaultModels: ChatModelOut[] = [
  { id: 'm1', label: 'Small', description: '', is_default: true, group: 'general' },
]

let streamController: ReadableStreamDefaultController<Uint8Array> | null = null
let sentBodies: Record<string, unknown>[] = []
let storedHistory: ChatMessageOut[] = []
let storedConversations: ConversationOut[] = []

function emitFrame(frame: object) {
  streamController?.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(frame)}\n\n`))
}

function setup({
  path = '/',
  locale = 'de',
  skills = [dailySkill, digestSkill],
  prompts = [promptTemplate],
  details = [promptDetail],
  conversations,
  history = [],
}: {
  path?: string
  locale?: 'de' | 'en'
  skills?: SkillOut[]
  prompts?: PromptOut[]
  details?: PromptDetailOut[]
  conversations?: ConversationOut[]
  history?: ChatMessageOut[]
} = {}) {
  sentBodies = []
  streamController = null
  storedHistory = history
  storedConversations = conversations ?? [{
    id: 'c1',
    title: 'Bestehende Unterhaltung',
    model: 'm1',
    version: 3,
    prompt_revision_id: null,
    account_scope_json: JSON.stringify({ selection: null, account_ids: ['a'] }),
    created_at: '2026-11-21T08:00:00Z',
    updated_at: '2026-11-21T08:00:00Z',
  }]

  window.matchMedia = ((query: string) => ({ matches: query !== '(max-width: 639px)', addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  globalThis.fetch = ((_url: unknown, init?: RequestInit) => {
    sentBodies.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>)
    return Promise.resolve(new Response(new ReadableStream({ start(c) { streamController = c } })))
  }) as typeof fetch

  spyOn(sdk, 'skillsList').mockImplementation((async () => ({ data: skills })) as never)
  spyOn(sdk, 'promptsList').mockImplementation((async () => ({ data: prompts })) as never)
  spyOn(sdk, 'promptGet').mockImplementation((async ({ path }: { path: { id: string } }) => {
    const found = details.find(item => item.id === path.id)
    if (!found) throw { detail: { code: 'prompt_unavailable' } }
    return { data: found }
  }) as never)
  spyOn(sdk, 'calendarSourcesList').mockResolvedValue({ data: [{ id: 'cal-1', name: 'Arbeit', is_active: true, chat_access: 'read_only' }] } as never)
  spyOn(sdk, 'calendarSettingsGet').mockResolvedValue({ data: { version: 1, default_source_id: null } } as never)
  spyOn(sdk, 'mailSummary').mockImplementation((async () => ({
    data: {
      mail_scope_version: 7,
      accounts: [{ id: 'a', display_name: 'Privat', provider: 'gmail', email_address: 'ich@example.org', color_index: 0, state: 'ok' }],
      unread_total: 2,
      newsletter_senders: 1,
      last_sync_at: null,
    },
  })) as never)
  spyOn(sdk, 'chatSettingsGet').mockResolvedValue({ data: { version: 7, account_ids: ['a'] } } as never)
  spyOn(sdk, 'chatModelsList').mockResolvedValue({ data: defaultModels } as never)
  spyOn(sdk, 'chatHistory').mockImplementation((async () => ({ data: storedHistory })) as never)
  spyOn(sdk, 'chatConversationsList').mockImplementation((async () => ({ data: storedConversations })) as never)
  spyOn(sdk, 'chatConversationUpdate').mockImplementation((async ({ path, body }: { path: { conversation_id: string }; body: { prompt_revision_id?: string | null } }) => {
    const updated: ConversationOut = {
      ...(storedConversations.find(c => c.id === path.conversation_id) ?? storedConversations[0]!),
      version: 4,
      prompt_revision_id: body.prompt_revision_id ?? null,
    }
    storedConversations = [updated]
    return { data: updated }
  }) as never)
  spyOn(sdk, 'chatMailReferenceGet').mockImplementation((async () => ({
    data: { id: 'mail1', account_id: 'a', color_index: 0, sender_name: 'Anna', subject: 'Quartalsplan', internal_date: null, deadline: null },
  })) as never)
  spyOn(sdk, 'audioCapabilitiesGet').mockResolvedValue({ data: { stt: null, tts: null } } as never)

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
  const promptsRoute = createRoute({ getParentRoute: () => root, path: '/settings/prompts', component: () => <p>Vorlagenliste</p> })
  const mailRoute = createRoute({ getParentRoute: () => root, path: '/mail/$messageId', component: () => <p>Mail</p> })
  const router = createRouter({
    routeTree: root.addChildren([start, chat, promptsRoute, mailRoute]),
    history: createMemoryHistory({ initialEntries: [path] }),
  })
  const rendered = render(<RouterProvider router={router} />)
  return { client, router, unmount: rendered.unmount }
}

afterEach(() => {
  cleanup()
  mock.restore()
  window.localStorage.clear()
  globalThis.fetch = originalFetch
  window.matchMedia = originalMatchMedia
  Object.assign(globalThis, { AudioContext: originalAudio.AudioContext, WebSocket: originalAudio.WebSocket, isSecureContext: originalAudio.secure })
  Object.defineProperty(navigator, 'mediaDevices', { value: originalAudio.media, configurable: true })
})

test('voice survives automatic one-turn skill consumption but stops on an explicit skill change', async () => {
  let socket!: { onmessage?: (event: { data: string }) => void }, voiceSocket!: FakeSocket, stopped = 0, played = 0
  class FakeSocket {
    static OPEN = 1
    readyState = 1; bufferedAmount = 0; binaryType = ''
    onopen?: () => void; onclose?: () => void; onmessage?: (event: { data: string }) => void
    constructor(url: string) { if (new URL(url).pathname.endsWith('/transcription')) socket = this; else voiceSocket = this; queueMicrotask(() => this.onopen?.()) }
    send(data: string | ArrayBuffer) { if (typeof data === 'string') { const value = JSON.parse(data); if (value.text) sentBodies.push(value) } } close() { this.readyState = 3; this.onclose?.() }
  }
  class FakeContext {
    sampleRate = 24000; currentTime = 0; destination = {}
    async resume() {} async close() {}
    createMediaStreamSource() { return { connect() {} } }
    createScriptProcessor() { return { connect() {}, disconnect() {}, onaudioprocess: null } }
    createBuffer(_channels: number, length: number) { return { duration: 0, getChannelData: () => new Float32Array(length) } }
    createBufferSource() { return { connect() {}, start() { played++; queueMicrotask(() => this.onended?.()) }, stop() {}, onended: null as (() => void) | null, buffer: null } }
  }
  Object.assign(globalThis, { AudioContext: FakeContext, WebSocket: FakeSocket, isSecureContext: true })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: async () => ({ getTracks: () => [{ stop: () => { stopped++ } }] }) } })
  const { client } = setup({ path: '/chat/c1' })
  spyOn(sdk, 'audioLiveStatus').mockResolvedValue({ data: { available: true } } as never)
  const field = await screen.findByPlaceholderText(de.chat.placeholder)
  fireEvent.change(field, { target: { value: '/daily', selectionStart: 6 } })
  await screen.findByRole('listbox')
  fireEvent.keyDown(field, { key: 'Enter' })
  await screen.findByText(de.skills.activeSkill(dailySkill.name))
  fireEvent.click(await screen.findByLabelText('Sprachmodus starten'))
  await screen.findByText('Zuhören')
  await act(async () => {
    for (const event of [{ type: 'turn.start' }, { type: 'transcription.text.delta', text: 'Hallo' }, { type: 'turn.end' }]) socket.onmessage?.({ data: JSON.stringify(event) })
    await new Promise(resolve => setTimeout(resolve, 510))
  })
  expect(sentBodies[0]?.skill_id).toBe(dailySkill.id)
  await act(async () => { voiceSocket.onmessage?.({ data: JSON.stringify({ event: 'start', conversation_id: 'c1', user_message_id: 'u1', model: 'm1' }) }) })
  await waitFor(() => expect(screen.queryByText(de.skills.activeSkill(dailySkill.name))).toBeNull())
  expect(stopped).toBe(0)
  await act(async () => { for (const event of [{ event: 'voice_audio', audio: 'AAAAPw==' }, { event: 'voice_audio_end' }, { event: 'done', conversation_id: 'c1', message_id: 'a1', stopped: false }]) voiceSocket.onmessage?.({ data: JSON.stringify(event) }) })
  await waitFor(() => expect(played).toBe(1))
  expect(screen.getByLabelText('Sprachmodus beenden')).toBeTruthy()
  fireEvent.change(field, { target: { value: '/daily', selectionStart: 6 } })
  await screen.findByRole('listbox')
  fireEvent.keyDown(field, { key: 'Enter' })
  await waitFor(() => expect(stopped).toBe(1))
  expect(screen.getByLabelText('Sprachmodus starten')).toBeTruthy()
  for (const change of [{ model: 'm2' }, { prompt_revision_id: 'pr-rev-2' }]) {
    fireEvent.click(screen.getByLabelText('Sprachmodus starten'))
    await screen.findByText('Zuhören')
    await act(async () => {
      for (const event of [{ type: 'turn.start' }, { type: 'transcription.text.delta', text: 'Alter Kontext' }, { type: 'turn.end' }]) socket.onmessage?.({ data: JSON.stringify(event) })
      client.setQueryData<ConversationOut[]>(['conversations'], previous => previous?.map(item => ({ ...item, ...change })))
    })
    await waitFor(() => expect(screen.getByLabelText('Sprachmodus starten')).toBeTruthy())
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 510)) })
    expect(sentBodies).toHaveLength(1)
  }
  expect(stopped).toBe(3)
})

test('detectSlashQuery ignores ordinary slash text and strips only the active slash command token', () => {
  for (const plain of ['21/11', 'und/oder', 'https://example.org/a', '/usr/bin', '/ ', 'Termin am 21/11 um 10:00']) {
    expect(detectSlashQuery(plain, plain.length)).toBeNull()
  }

  const startSlash = detectSlashQuery('/', 1)
  expect(startSlash).toEqual({ start: 0, end: 1, query: '', bare: true })

  const inline = detectSlashQuery('Für heute /daily prüfen', 16)
  expect(inline).toEqual({ start: 10, end: 16, query: 'daily', bare: false })
  expect(stripSlashToken('Für heute /daily prüfen', inline!)).toEqual({ nextText: 'Für heute prüfen', nextCursor: 10 })

  const onlyToken = detectSlashQuery('/brief', 6)
  expect(stripSlashToken('/brief', onlyToken!)).toEqual({ nextText: '', nextCursor: 0 })

  const options = buildSlashOptions({
    query: '',
    skills: [disabledSkill, digestSkill, dailySkill],
    prompts: [promptTemplate],
    promptDetails: [promptDetail],
    favoriteIds: [digestSkill.id],
    selectedSkillId: null,
    selectedPromptRevisionId: null,
    busy: false,
    texts: de,
  })
  expect(options.map(item => item.key)).toEqual([
    `skill:${digestSkill.id}`,
    `skill:${dailySkill.id}`,
    `template:${promptTemplate.revision_id}`,
    'template-rev:pr-rev-1',
    'action:manage-templates',
  ])
})

test('header has no TemplateMenu button while + Favoriten persists favorites locally and selects without auto-sending', async () => {
  const { unmount } = setup()
  const favTrigger = await screen.findByRole('button', { name: de.skills.favoritesChip })
  expect(screen.queryByRole('button', { name: /Vorlage:/ })).toBeNull()

  fireEvent.click(favTrigger)
  expect(await screen.findByText(de.skills.favoritesEmpty)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: de.skills.manageFavorites }))
  fireEvent.click(screen.getByRole('button', { name: de.skills.favoriteAdd(dailySkill.name) }))
  expect(JSON.parse(window.localStorage.getItem(SKILL_FAVORITES_STORAGE_KEY) ?? '[]')).toEqual([dailySkill.id])

  fireEvent.click(screen.getByRole('button', { name: de.skills.manageFavorites }))
  expect(await screen.findByRole('button', { name: new RegExp(`/${dailySkill.slug}`) })).toBeTruthy()
  unmount()

  setup()
  fireEvent.click(await screen.findByRole('button', { name: de.skills.favoritesChip }))
  const favoriteOption = await screen.findByRole('button', { name: new RegExp(`/${dailySkill.slug}`) })
  fireEvent.click(favoriteOption)

  expect(await screen.findByText(de.skills.activeSkill(dailySkill.name))).toBeTruthy()
  expect(screen.getByText(de.skills.activeDuration)).toBeTruthy()
  expect(sentBodies).toHaveLength(0)

  fireEvent.click(screen.getByRole('button', { name: de.skills.favoritesChip }))
  fireEvent.click(await screen.findByRole('button', { name: de.skills.favoriteRemove(dailySkill.name) }))
  expect(JSON.parse(window.localStorage.getItem(SKILL_FAVORITES_STORAGE_KEY) ?? '[]')).toEqual([])
})

test('disabled favorite cannot be activated and error state offers retry in + Favoriten', async () => {
  window.localStorage.setItem(SKILL_FAVORITES_STORAGE_KEY, JSON.stringify([disabledSkill.id]))
  setup({ skills: [disabledSkill] })
  fireEvent.click(await screen.findByRole('button', { name: de.skills.favoritesChip }))
  const disabledRow = await screen.findByRole('button', { name: new RegExp(`/${disabledSkill.slug}`) })
  expect(disabledRow.getAttribute('aria-disabled')).toBe('true')
  expect(screen.getByText(de.skills.disabledHint)).toBeTruthy()
  fireEvent.click(disabledRow)
  expect(screen.queryByText(de.skills.activeSkill(disabledSkill.name))).toBeNull()
  expect(sentBodies).toHaveLength(0)

  cleanup()
  mock.restore()
  window.localStorage.clear()
  setup({ skills: [] })
  let failOnce = true
  spyOn(sdk, 'skillsList').mockImplementation((async () => {
    if (failOnce) {
      failOnce = false
      throw new Error('temporary failure')
    }
    return { data: [dailySkill] }
  }) as never)
  fireEvent.click(await screen.findByRole('button', { name: de.skills.favoritesChip }))
  expect(await screen.findByText(de.skills.loadError)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: de.chat.retry }))
  await waitFor(() => expect(screen.queryByText(de.skills.loadError)).toBeNull())
})

test('slash picker dismisses via Close button and outside pointer tap on touch/mouse', async () => {
  setup()
  const field = await screen.findByPlaceholderText(de.chat.placeholder)
  fireEvent.change(field, { target: { value: '/', selectionStart: 1 } })
  expect(await screen.findByRole('listbox')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: de.shell.close }))
  expect(screen.queryByRole('listbox')).toBeNull()

  fireEvent.change(field, { target: { value: '/daily', selectionStart: 6 } })
  expect(await screen.findByRole('listbox')).toBeTruthy()
  fireEvent.pointerDown(document.body)
  await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
})

test('arrow navigation reveals the active option in a long picker and keeps focus in the composer', async () => {
  const skills = Array.from({ length: 24 }, (_, i) => ({
    ...dailySkill, id: `skill-${i}`, slug: `long_${i}`, name: `Long ${String(i).padStart(2, '0')}`,
  }))
  setup({ skills })
  const field = await screen.findByPlaceholderText(de.chat.placeholder)
  field.focus()
  fireEvent.change(field, { target: { value: '/long', selectionStart: 5 } })
  const options = await screen.findAllByRole('option')
  expect(options).toHaveLength(skills.length)
  const first = options[0]!, last = options.at(-1)!
  first.scrollIntoView = mock()
  last.scrollIntoView = mock()
  fireEvent.keyDown(field, { key: 'ArrowUp' })
  expect(field.getAttribute('aria-activedescendant')).toBe(last.id)
  expect(last.scrollIntoView).toHaveBeenCalledWith(expect.objectContaining({ block: 'nearest' }))
  fireEvent.keyDown(field, { key: 'ArrowDown' })
  expect(field.getAttribute('aria-activedescendant')).toBe(first.id)
  expect(first.scrollIntoView).toHaveBeenCalledWith(expect.objectContaining({ block: 'nearest' }))
  expect(document.activeElement).toBe(field)
  fireEvent.keyDown(field, { key: 'Enter' })
  expect(await screen.findByText(de.skills.activeSkill(skills[0]!.name))).toBeTruthy()
  expect(sentBodies).toHaveLength(0)
})

test.each(['skills', 'templates', 'revision'])('the slash picker keeps loaded options usable and retries a failed %s request', async failed => {
  setup()
  const skillError = failed === 'skills'
  const request = failed === 'skills' ? spyOn(sdk, 'skillsList') : failed === 'templates' ? spyOn(sdk, 'promptsList') : spyOn(sdk, 'promptGet')
  request.mockRejectedValue(new Error('synthetic catalogue failure'))
  const errorText = skillError ? de.skills.loadError : de.prompts.error
  const openPicker = () => fireEvent.change(screen.getByRole('combobox'), { target: { value: '/', selectionStart: 1 } })
  await screen.findByPlaceholderText(de.chat.placeholder)
  openPicker()
  const currentTemplate = (name: string) => name.startsWith(promptTemplate.name) && !name.includes(de.prompts.version(1))
  const available = await screen.findByRole('option', { name: skillError ? currentTemplate : new RegExp(dailySkill.name) })
  expect(await screen.findByText(errorText)).toBeTruthy()
  if (failed === 'revision') expect(screen.getByRole('option', { name: currentTemplate })).toBeTruthy()
  expect(screen.queryByRole('option', { name: skillError ? new RegExp(dailySkill.name) : /Version 1/ })).toBeNull()
  fireEvent.click(available)
  expect(await screen.findByText(skillError ? de.prompts.chip(promptTemplate.name) : de.skills.activeSkill(dailySkill.name))).toBeTruthy()

  openPicker()
  const retry = await screen.findByRole('button', { name: de.chat.retry })
  const calls = request.mock.calls.length
  request.mockResolvedValue({ data: skillError ? [dailySkill] : failed === 'templates' ? [promptTemplate] : promptDetail } as never)
  fireEvent.click(retry)
  await waitFor(() => expect(screen.queryByText(errorText)).toBeNull())
  expect(request.mock.calls.length).toBeGreaterThan(calls)
  const restored = await screen.findByRole('option', { name: skillError ? new RegExp(dailySkill.name) : /Version 1/ })
  fireEvent.click(restored)
  expect(await screen.findByText(skillError ? de.skills.activeSkill(dailySkill.name) : /Vorlage: Fristen · Version 1/)).toBeTruthy()
  expect(sentBodies).toHaveLength(0)
})

test('slash picker supports keyboard navigation, empty/unknown handling, dismiss and explicit send', async () => {
  setup()
  const field = await screen.findByPlaceholderText(de.chat.placeholder)

  // Ordinary slash text does not open the menu
  fireEvent.change(field, { target: { value: 'Bitte 21/11 und/oder https://example.org/a prüfen', selectionStart: 47 } })
  expect(screen.queryByRole('listbox')).toBeNull()

  // Unknown /command never sends implicitly and can be dismissed to send plain text
  fireEvent.change(field, { target: { value: '/unknown', selectionStart: 8 } })
  expect(await screen.findByText(de.skills.slashNoMatch('unknown'))).toBeTruthy()
  fireEvent.keyDown(field, { key: 'Enter' })
  expect(sentBodies).toHaveLength(0)
  fireEvent.keyDown(field, { key: 'Escape' })
  expect(screen.queryByRole('listbox')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: de.controls.send }))
  expect(sentBodies).toHaveLength(1)
  expect(sentBodies[0]!.skill_id).toBeUndefined()
  expect(sentBodies[0]!.text).toBe('/unknown')
})

test('slash keyboard selection picks second skill, keeps typed prefix text, expands steps, and clears after single turn', async () => {
  setup()
  const field = await screen.findByPlaceholderText(de.chat.placeholder)
  fireEvent.change(field, { target: { value: 'Bitte prüfen /', selectionStart: 14 } })
  expect(await screen.findByRole('listbox')).toBeTruthy()

  // Move down to template/second skill and back up to Tagesbriefing (index 1) and select with Enter
  fireEvent.keyDown(field, { key: 'ArrowDown' })
  fireEvent.keyDown(field, { key: 'ArrowDown' })
  fireEvent.keyDown(field, { key: 'ArrowUp' })
  fireEvent.keyDown(field, { key: 'Enter' })

  expect(sentBodies).toHaveLength(0)
  expect(await screen.findByText(de.skills.activeSkill(dailySkill.name))).toBeTruthy()
  expect((field as HTMLTextAreaElement).value).toBe('Bitte prüfen ')

  fireEvent.click(screen.getByRole('button', { name: de.skills.showSteps(1) }))
  expect(screen.getByText('Tagesdaten sammeln')).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: de.controls.send }))
  expect(sentBodies[0]!.skill_id).toBe(dailySkill.id)
  expect(sentBodies[0]!.skill_revision_id).toBe(dailySkill.revision_id)

  await act(async () => {
    emitFrame({ event: 'start', conversation_id: 'c1', user_message_id: 'u1', model: 'm1' })
    emitFrame({ event: 'done', conversation_id: 'c1', message_id: 'a1' })
  })
  // Single-turn default resets immediately upon turn start, leaving conversation without implicit skill carryover
  await waitFor(() => expect(screen.queryByText(de.skills.activeSkill(dailySkill.name))).toBeNull())
})

test('sticky persistence keeps active skill across follow-up clarifying turns until explicitly ended', async () => {
  setup()
  const field = await screen.findByPlaceholderText(de.chat.placeholder)
  fireEvent.change(field, { target: { value: '/daily', selectionStart: 6 } })
  fireEvent.click(await screen.findByRole('option', { name: new RegExp(dailySkill.name) }))

  const stickyToggle = await screen.findByRole('button', { name: de.skills.keepForFollowUps })
  fireEvent.click(stickyToggle)
  expect(stickyToggle.getAttribute('aria-pressed')).toBe('true')
  expect(screen.getByText(de.skills.durationUntilStopped)).toBeTruthy()

  const activeField = screen.getByPlaceholderText(de.skills.placeholderWithSkill(dailySkill.name))
  fireEvent.change(activeField, { target: { value: 'Gib mir das Briefing', selectionStart: 20 } })
  fireEvent.click(screen.getByRole('button', { name: de.controls.send }))

  await act(async () => {
    emitFrame({ event: 'start', conversation_id: 'c1', user_message_id: 'u1', model: 'm1' })
    emitFrame({ event: 'done', conversation_id: 'c1', message_id: 'a1' })
  })

  // Navigated to /chat/c1 and sticky skill stays visible and endable for clarifying follow-up questions
  expect(await screen.findByText(de.skills.activeSkill(dailySkill.name))).toBeTruthy()
  expect(screen.getByText(de.skills.durationUntilStopped)).toBeTruthy()

  const followupField = screen.getByPlaceholderText(de.skills.placeholderWithSkill(dailySkill.name))
  fireEvent.change(followupField, { target: { value: 'Ja, inklusive Kalender', selectionStart: 22 } })
  fireEvent.click(screen.getByRole('button', { name: de.controls.send }))
  expect(sentBodies).toHaveLength(2)
  expect(sentBodies[1]!.skill_id).toBe(dailySkill.id)
  expect(sentBodies[1]!.skill_revision_id).toBe(dailySkill.revision_id)

  await act(async () => {
    emitFrame({ event: 'start', conversation_id: 'c1', user_message_id: 'u2', model: 'm1' })
    emitFrame({ event: 'done', conversation_id: 'c1', message_id: 'a2' })
  })

  // Explicit end resets cleanly so next turn carries no skill_id
  fireEvent.click(await screen.findByRole('button', { name: de.skills.endSkill }))
  expect(screen.queryByText(de.skills.activeSkill(dailySkill.name))).toBeNull()

  const plainField = screen.getByPlaceholderText(de.chat.placeholder)
  fireEvent.change(plainField, { target: { value: 'Normale Frage danach', selectionStart: 20 } })
  fireEvent.click(screen.getByRole('button', { name: de.controls.send }))
  expect(sentBodies).toHaveLength(3)
  expect(sentBodies[2]!.skill_id).toBeUndefined()
})

test.each(['suggestion', 'mail reference'])('a %s sends the conversation’s exact skill until the owner ends it', async action => {
  const { router } = setup({ path: '/?about=mail1' })
  const mailField = await screen.findByPlaceholderText(de.inbox.mailQuestionPlaceholder)
  fireEvent.change(mailField, { target: { value: '/postfach', selectionStart: 9 } })
  fireEvent.click(await screen.findByRole('option', { name: new RegExp(digestSkill.name) }))
  await act(() => router.navigate({ to: '/' }))
  const field = await screen.findByPlaceholderText(de.chat.placeholder)
  fireEvent.change(field, { target: { value: '/daily', selectionStart: 6 } })
  fireEvent.click(await screen.findByRole('option', { name: new RegExp(dailySkill.name) }))
  fireEvent.click(screen.getByRole('button', { name: de.skills.keepForFollowUps }))
  fireEvent.change(field, { target: { value: 'Briefing' } })
  fireEvent.click(screen.getByRole('button', { name: de.controls.send }))

  const suggestion = 'Was ist morgen fällig?'
  const mail = { id: 'mail1', account_id: 'a', color_index: 0, sender_name: 'Anna', subject: 'Anfrage', snippet: '', is_flagged: false, internal_date: null, deadline: null }
  const finish = (turn: number) => act(async () => {
    emitFrame({ event: 'start', conversation_id: 'c1', user_message_id: `u${turn}`, model: 'm1' })
    emitFrame({ event: 'suggestions', items: [suggestion] })
    emitFrame({ event: 'mails', mails: [mail] })
    emitFrame({ event: 'done', conversation_id: 'c1', message_id: `a${turn}` })
  })
  const label = action === 'suggestion' ? suggestion : de.mail.draftReply
  await finish(1)
  expect(await screen.findByText(de.skills.durationUntilStopped)).toBeTruthy()
  fireEvent.click(await screen.findByRole('button', { name: label }))
  expect(sentBodies).toHaveLength(2)
  expect(sentBodies[1]).toMatchObject({ conversation_id: 'c1', expected_mail_scope_version: 7, text: action === 'suggestion' ? suggestion : de.mail.draftPrompt })
  expect(sentBodies[1]!.context_message_id).toBe(action === 'suggestion' ? undefined : mail.id)
  expect(sentBodies[1]).toMatchObject({ skill_id: dailySkill.id, skill_revision_id: dailySkill.revision_id })

  await finish(2)
  fireEvent.click(screen.getByRole('button', { name: de.skills.endSkill }))
  fireEvent.click(await screen.findByRole('button', { name: label }))
  expect(sentBodies).toHaveLength(3)
  expect(sentBodies[2]!.skill_id).toBeUndefined()
  expect(sentBodies[2]!.skill_revision_id).toBeUndefined()
  await act(() => router.navigate({ to: '/', search: { about: 'mail1' } }))
  expect(await screen.findByText(de.skills.activeSkill(digestSkill.name))).toBeTruthy()
})

test.each(['home', 'conversation'])('context_revoked clears only the sticky skill used in the turn from %s', async origin => {
  const { router } = setup({ path: '/?about=mail1' })
  const home = { to: '/' } as const
  const conversation = { to: '/chat/$conversationId', params: { conversationId: 'c1' } } as const
  const fromHome = origin === 'home'
  const other = fromHome ? conversation : home
  const targetId = fromHome ? 'c2' : 'c1'
  async function choose(placeholder: string, command: string, skill: SkillOut) {
    const field = await screen.findByPlaceholderText(placeholder)
    fireEvent.change(field, { target: { value: command, selectionStart: command.length } })
    fireEvent.click(await screen.findByRole('option', { name: new RegExp(skill.name) }))
  }

  await choose(de.inbox.mailQuestionPlaceholder, '/postfach', digestSkill)
  await act(() => router.navigate(other))
  await choose(de.chat.placeholder, '/daily', dailySkill)
  await act(() => router.navigate(fromHome ? home : conversation))
  await choose(de.chat.placeholder, '/daily', dailySkill)
  fireEvent.click(screen.getByRole('button', { name: de.skills.keepForFollowUps }))
  fireEvent.change(screen.getByPlaceholderText(de.skills.placeholderWithSkill(dailySkill.name)), { target: { value: 'Briefing' } })
  fireEvent.click(screen.getByRole('button', { name: de.controls.send }))
  expect(sentBodies[0]).toMatchObject({ skill_id: dailySkill.id, skill_revision_id: dailySkill.revision_id })
  expect(sentBodies[0]?.conversation_id).toBe(fromHome ? undefined : targetId)

  await act(async () => {
    emitFrame({ event: 'start', conversation_id: targetId, user_message_id: 'u1', model: 'm1' })
    emitFrame({ event: 'delta', text: 'Angefangen' })
  })
  await waitFor(() => expect(router.state.location.pathname).toBe(`/chat/${targetId}`))
  expect(await screen.findByText(de.skills.durationUntilStopped)).toBeTruthy()
  spyOn(sdk, 'skillsList').mockResolvedValue({ data: [{ ...dailySkill, status: 'disabled' }, digestSkill] } as never)
  await act(async () => { emitFrame({ event: 'error', code: 'context_revoked' }) })
  expect(await screen.findByText(de.errors.context_revoked)).toBeTruthy()
  expect(screen.queryByText(de.skills.activeSkill(dailySkill.name))).toBeNull()
  expect(screen.queryByText(de.skills.unavailableSelection)).toBeNull()

  await act(() => router.navigate(other))
  expect(await screen.findByText(de.skills.activeSkill(dailySkill.name))).toBeTruthy()
  await act(() => router.navigate({ to: '/', search: { about: 'mail1' } }))
  expect(await screen.findByText(de.skills.activeSkill(digestSkill.name))).toBeTruthy()
})

test('quick action and mail-context route both forward an explicitly selected skill', async () => {
  setup()
  const field = await screen.findByPlaceholderText(de.chat.placeholder)
  fireEvent.change(field, { target: { value: '/daily', selectionStart: 6 } })
  fireEvent.click(await screen.findByRole('option', { name: new RegExp(dailySkill.name) }))
  fireEvent.click(screen.getByText(de.quickActions.dueToday.title))
  expect(sentBodies).toHaveLength(1)
  expect(sentBodies[0]!.skill_id).toBe(dailySkill.id)
  expect(sentBodies[0]!.skill_revision_id).toBe(dailySkill.revision_id)
})

test('templates and older revisions combine cleanly with skills and mail-about route', async () => {
  setup({ path: '/?about=mail1' })
  const field = await screen.findByPlaceholderText(de.inbox.mailQuestionPlaceholder)
  fireEvent.change(field, { target: { value: '/Fristen', selectionStart: 8 } })
  fireEvent.click(await screen.findByRole('option', { name: /Version 1/ }))
  expect(await screen.findByText(/Vorlage: Fristen · Version 1/)).toBeTruthy()
  expect(await screen.findByRole('button', { name: de.prompts.adopt })).toBeTruthy()

  const closeBtn = screen.getByRole('button', { name: de.skills.favoritesChip })
  fireEvent.click(closeBtn)
  fireEvent.click(screen.getByRole('button', { name: de.skills.browseAll }))
  expect(await screen.findByRole('listbox')).toBeTruthy()
  fireEvent.click(await screen.findByRole('option', { name: new RegExp(digestSkill.name) }))
  expect(await screen.findByText(de.skills.activeSkill(digestSkill.name))).toBeTruthy()

  const activeField = screen.getByPlaceholderText(de.skills.placeholderWithSkill(digestSkill.name))
  fireEvent.change(activeField, { target: { value: 'Zusammenfassen', selectionStart: 14 } })
  fireEvent.click(screen.getByRole('button', { name: de.controls.send }))
  expect(sentBodies).toHaveLength(1)
  expect(sentBodies[0]).toMatchObject({
    context_message_id: 'mail1',
    prompt_revision_id: 'pr-rev-1',
    skill_id: digestSkill.id,
    skill_revision_id: digestSkill.revision_id,
  })
})

test('historical skill provenance badge never breaks model label and revoked skill blocks confirmation', async () => {
  const revokedSkillContext = {
    id: 'ctx-sk-1',
    app_prompt_version: '15',
    mail_scope_version: 7,
    account_scope_json: JSON.stringify({ selection: null, account_ids: ['a'] }),
    prompt_revision_id: null,
    prompt_hash: null,
    preference_revision_id: null,
    preferences_enabled: false,
    skill_id: dailySkill.id,
    skill_revision_id: dailySkill.revision_id,
    skill_hash: dailySkill.content_hash,
    revoked: true,
  }
  const msgAssistant: ChatMessageOut = {
    id: 'a-skill',
    conversation_id: 'c1',
    role: 'assistant',
    content: 'Hier ist das Tagesbriefing.',
    created_at: '',
    extras: {
      model: 'm1',
      skill: { id: dailySkill.id, slug: dailySkill.slug, origin: 'builtin', revision_id: dailySkill.revision_id, version: dailySkill.version },
    },
    proposal: null,
    proposal_status: '',
    stopped: false,
    context: revokedSkillContext,
    tool_name: '',
  }
  const msgProposal: ChatMessageOut = {
    id: 'p-skill',
    conversation_id: 'c1',
    role: 'proposal',
    content: '',
    created_at: '',
    extras: {},
    proposal_status: 'pending',
    stopped: false,
    context: revokedSkillContext,
    tool_name: '',
    proposal: {
      kind: 'task',
      reason: 'Abhaken',
      items: [],
      task: {
        action: 'complete',
        title: 'Abnahme',
        series_id: 's1',
        occurrence_id: 'o1',
        expected_version: 1,
        expected_occurrence_version: 1,
      },
    } as unknown as ChatMessageOut['proposal'],
  }

  setup({ path: '/chat/c1', history: [msgAssistant, msgProposal] })
  expect(await screen.findByText(de.skills.usedTag(dailySkill.name, dailySkill.version))).toBeTruthy()
  expect(document.querySelector('.le-msg-ai small')?.textContent).toBe('Small')
  expect(screen.getByText(de.skills.revoked)).toBeTruthy()
  expect(screen.getByText(de.skills.cancelOnly)).toBeTruthy()
  expect(screen.queryByRole('button', { name: de.taskChat.confirm })).toBeNull()
})

test('skill_disabled refusal clears stale selection and renders English labels cleanly', async () => {
  setup({ locale: 'en' })
  globalThis.fetch = (() => Promise.resolve(Response.json({ detail: { code: 'skill_disabled' } }, { status: 409 }))) as unknown as typeof fetch
  const field = await screen.findByPlaceholderText(en.chat.placeholder)
  fireEvent.change(field, { target: { value: '/daily', selectionStart: 6 } })
  fireEvent.click(await screen.findByRole('option', { name: new RegExp(dailySkill.name) }))
  expect(await screen.findByText(en.skills.activeDuration)).toBeTruthy()

  const activeField = screen.getByPlaceholderText(en.skills.placeholderWithSkill(dailySkill.name))
  fireEvent.change(activeField, { target: { value: 'Run briefing', selectionStart: 12 } })
  fireEvent.click(screen.getByRole('button', { name: en.controls.send }))

  expect(await screen.findByText(en.errors.skill_disabled)).toBeTruthy()
  expect(screen.queryByRole('button', { name: en.chat.retry })).toBeNull()
  expect(screen.queryByText(en.skills.activeSkill(dailySkill.name))).toBeNull()
})

test('actively selected skill warns and can be removed when disabled, and conversation restart clears it', async () => {
  const { client } = setup({ path: '/chat/c1' })
  const field = await screen.findByPlaceholderText(de.chat.placeholder)
  fireEvent.change(field, { target: { value: '/daily', selectionStart: 6 } })
  fireEvent.click(await screen.findByRole('option', { name: new RegExp(dailySkill.name) }))
  expect(await screen.findByText(de.skills.activeSkill(dailySkill.name))).toBeTruthy()

  // Simulate skill becoming disabled while active in the composer
  act(() => {
    client.setQueryData(['skills'], [{ ...dailySkill, status: 'disabled' }, digestSkill])
  })
  expect(await screen.findByText(de.skills.unavailableSelection)).toBeTruthy()
  fireEvent.click(screen.getAllByRole('button', { name: de.skills.removeSelection }).at(-1)!)
  expect(screen.queryByText(de.skills.activeSkill(dailySkill.name))).toBeNull()

  // Re-select on conversation, then click + Neue Unterhaltung: selection is cleared across restart
  const plainField = await screen.findByPlaceholderText(de.chat.placeholder)
  fireEvent.change(plainField, { target: { value: '/postfach', selectionStart: 9 } })
  fireEvent.click(await screen.findByRole('option', { name: new RegExp(digestSkill.name) }))
  expect(await screen.findByText(de.skills.activeSkill(digestSkill.name))).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: de.shell.newConversation }))
  expect(await screen.findByText(de.chat.greetingFirst)).toBeTruthy()
  expect(screen.queryByText(de.skills.activeSkill(digestSkill.name))).toBeNull()
})
