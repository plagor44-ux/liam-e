import { afterEach, beforeEach, expect, mock, spyOn, test } from 'bun:test'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import { unwrap } from '../api/unwrap'
import { I18nProvider } from '../i18n/I18n'
import { LiveVoice } from './LiveVoice'

const original = { fetch: globalThis.fetch, AudioContext: globalThis.AudioContext, WebSocket: globalThis.WebSocket, secure: globalThis.isSecureContext, media: navigator.mediaDevices }
beforeEach(() => { spyOn(sdk, 'audioLiveStatus').mockResolvedValue({ data: { available: true } } as never) })
afterEach(() => { cleanup(); mock.restore(); Object.assign(globalThis, { fetch: original.fetch, AudioContext: original.AudioContext, WebSocket: original.WebSocket, isSecureContext: original.secure }); Object.defineProperty(navigator, 'mediaDevices', { value: original.media, configurable: true }) })

test('status uses the generated client and cancels its request on unmount', async () => {
  let signal: AbortSignal | undefined
  spyOn(sdk, 'audioLiveStatus').mockImplementation(((options: { signal: AbortSignal }) => {
    signal = options.signal
    return new Promise(() => {})
  }) as never)
  Object.assign(globalThis, { AudioContext: class {}, isSecureContext: true })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: () => {} } })
  const view = render(<LiveVoice conversationId="c" contextKey="c" disabled={false} registerStop={() => {}} stopChat={() => {}} send={async () => undefined} />)
  await waitFor(() => expect(signal).toBeDefined())
  expect(signal?.aborted).toBe(false)
  view.unmount()
  expect(signal?.aborted).toBe(true)
})

test('voice controls use the English catalogue', async () => {
  Object.assign(globalThis, { AudioContext: class {}, isSecureContext: true })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: () => {} } })
  render(<I18nProvider locale="en"><LiveVoice conversationId="c" contextKey="c" disabled={false} registerStop={() => {}} stopChat={() => {}} send={async () => undefined} /></I18nProvider>)
  expect(await screen.findByLabelText('Start voice mode')).toBeTruthy()
})

test('generated speech client returns chunks without buffering and respects cancellation', async () => {
  let canceled = false
  const controller = new AbortController()
  const source = new ReadableStream<Uint8Array>({ start(stream) { stream.enqueue(new Uint8Array([1, 2])) }, cancel() { canceled = true } })
  const fetchStream = (async (request: Request) => {
    expect(request.signal.aborted).toBe(false)
    expect(await request.json()).toEqual({ conversation_id: 'c', message_id: 'm' })
    return new Response(source, { headers: { 'Content-Type': 'application/octet-stream' } })
  }) as typeof fetch
  const result = await unwrap(sdk.audioLiveSpeech({ body: { conversation_id: 'c', message_id: 'm' }, baseUrl: 'http://localhost', parseAs: 'stream', signal: controller.signal, throwOnError: true, fetch: fetchStream })) as unknown as ReadableStream<Uint8Array>
  const reader = result.getReader()
  expect((await reader.read()).value).toEqual(new Uint8Array([1, 2]))
  await reader.cancel()
  controller.abort()
  expect(canceled).toBe(true)
})

test('two synthetic turns play audio, interruption and ending release resources', async () => {
  let ws: FakeSocket, stopped = 0, played = 0, closed = 0
  const decoded: Float32Array[] = []
  const requests: string[] = []
  class FakeSocket {
    static OPEN = 1
    readyState = 1; bufferedAmount = 0; binaryType = ''
    onopen?: () => void; onerror?: () => void; onclose?: () => void; onmessage?: (e: { data: string }) => void
    constructor() { ws = this; queueMicrotask(() => this.onopen?.()) }
    send() {} close() { closed++; this.onclose?.() }
    emit(value: object) { this.onmessage?.({ data: JSON.stringify(value) }) }
  }
  class FakeContext {
    sampleRate = 24000; currentTime = 0; destination = {}
    async resume() {} async close() { closed++ }
    createMediaStreamSource() { return { connect() {} } }
    createScriptProcessor() { return { connect() {}, disconnect() {}, onaudioprocess: null } }
    createBuffer(_channels: number, length: number, rate: number) { expect(rate).toBe(24000); const samples = new Float32Array(length); decoded.push(samples); return { duration: 0, getChannelData: () => samples } }
    createBufferSource() { return { connect() {}, start() { played++ }, stop() {}, onended: null, buffer: null } }
  }
  Object.assign(globalThis, { AudioContext: FakeContext, WebSocket: FakeSocket, isSecureContext: true })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: async () => ({ getTracks: () => [{ stop: () => { stopped++ } }] }) } })
  const speech = spyOn(sdk, 'audioLiveSpeech').mockImplementation((async () => ({ data: new ReadableStream({ start(c) { c.enqueue(new Uint8Array([0, 0])); c.enqueue(new Uint8Array([0, 63])); c.close() } }) })) as never)
  render(<LiveVoice conversationId="c1" contextKey="c1" disabled={false} registerStop={() => {}} stopChat={() => {}} send={async text => { requests.push(text); return { conversationId: 'c1', messageId: `m${requests.length}` } }} />)
  fireEvent.click(await screen.findByLabelText('Sprachmodus starten'))
  await waitFor(() => expect(screen.getByText('Zuhören')).toBeTruthy())
  expect(screen.getByRole('dialog', { name: 'Sprachmodus' })).toBeTruthy()
  expect(document.activeElement).toBe(screen.getByLabelText('Sprachmodus beenden'))
  const turn = async (text: string) => { await act(async () => { ws.emit({ type: 'turn.start' }); ws.emit({ type: 'transcription.text.delta', text }); ws.emit({ type: 'turn.end' }); await new Promise(resolve => setTimeout(resolve, 510)) }) }
  await turn('Merk dir: Die Testente ist gelb.')
  await waitFor(() => expect(played).toBe(1))
  await turn('Welche Farbe hat die Testente?')
  await waitFor(() => expect(played).toBe(2))
  expect(decoded.map(samples => [...samples])).toEqual([[0.5], [0.5]])
  expect(requests).toEqual(['Merk dir: Die Testente ist gelb.', 'Welche Farbe hat die Testente?'])
  expect(speech.mock.calls[0]?.[0]).toMatchObject({ body: { conversation_id: 'c1', message_id: 'm1' }, parseAs: 'stream', throwOnError: true })
  expect(speech.mock.calls[0]?.[0]?.signal).toBeInstanceOf(AbortSignal)
  fireEvent.click(screen.getByLabelText('Sprachmodus beenden'))
  expect(stopped).toBe(1); expect(closed).toBeGreaterThanOrEqual(2)
})

test('denied microphone permission leaves no running session', async () => {
  class FakeContext { async resume() {} async close() {} }
  Object.assign(globalThis, { AudioContext: FakeContext, isSecureContext: true })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: async () => { throw new Error('denied') } } })
  globalThis.fetch = (async () => Response.json({ available: true })) as unknown as typeof fetch
  render(<LiveVoice conversationId="c" contextKey="c" disabled={false} registerStop={() => {}} stopChat={() => {}} send={async () => undefined} />)
  fireEvent.click(await screen.findByLabelText('Sprachmodus starten'))
  expect(await screen.findByText('Sprachmodus nicht verfügbar')).toBeTruthy()
})

test('navigation while microphone permission is pending stops the late track', async () => {
  let resolvePermission!: (stream: unknown) => void, stopped = 0
  class FakeContext { async resume() {} async close() {} }
  Object.assign(globalThis, { AudioContext: FakeContext, isSecureContext: true })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: () => new Promise(resolve => { resolvePermission = resolve }) } })
  globalThis.fetch = (async () => Response.json({ available: true })) as unknown as typeof fetch
  const view = render(<LiveVoice conversationId="c" contextKey="c" disabled={false} registerStop={() => {}} stopChat={() => {}} send={async () => undefined} />)
  fireEvent.click(await screen.findByLabelText('Sprachmodus starten'))
  await waitFor(() => expect(resolvePermission).toBeDefined())
  view.unmount()
  await act(async () => { resolvePermission({ getTracks: () => [{ stop: () => { stopped++ } }] }) })
  expect(stopped).toBe(1)
})

test('the mode can be ended while permission is pending', async () => {
  let resolvePermission!: (stream: unknown) => void, stopped = 0
  class FakeContext { async resume() {} async close() {} }
  Object.assign(globalThis, { AudioContext: FakeContext, isSecureContext: true })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: () => new Promise(resolve => { resolvePermission = resolve }) } })
  globalThis.fetch = (async () => Response.json({ available: true })) as unknown as typeof fetch
  render(<LiveVoice conversationId="c" contextKey="c" disabled={false} registerStop={() => {}} stopChat={() => {}} send={async () => undefined} />)
  fireEvent.click(await screen.findByLabelText('Sprachmodus starten'))
  await waitFor(() => expect(resolvePermission).toBeDefined())
  fireEvent.keyDown(screen.getByRole('dialog', { name: 'Sprachmodus' }), { key: 'Escape' })
  await act(async () => { resolvePermission({ getTracks: () => [{ stop: () => { stopped++ } }] }) })
  expect(stopped).toBe(1)
  expect(screen.getByLabelText('Sprachmodus starten')).toBeTruthy()
  expect(document.activeElement).toBe(screen.getByLabelText('Sprachmodus starten'))
})
