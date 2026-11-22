import { afterEach, describe, expect, test } from 'bun:test'
import type { ChatStreamRequest } from '../api/generated/types.gen'
import { ChatStreamError, streamChatTurn, streamVoiceChatTurn } from './transport'

const BODY: ChatStreamRequest = { text: 'Hallo' }

const originalFetch = globalThis.fetch, originalSocket = globalThis.WebSocket

/** A 404 whose body read fails with `error`, as a body still arriving when the caller aborts does. */
const refusingResponse = (error: Error): Response => {
  const response = new Response(null, { status: 404 })
  Object.defineProperty(response, 'json', { value: () => Promise.reject(error) })
  return response
}

/** Stand-in for `fetch`, which in Bun also carries `preconnect`. */
const respondWith = (response: Response): void => {
  globalThis.fetch = Object.assign(() => Promise.resolve(response), {
    preconnect: () => {
      /* no-op: the stand-in answers without a connection to warm */
    },
  })
}

/** The error `streamChatTurn` rejects with, or a failure if it yields instead. */
const rejectionOf = async (signal: AbortSignal): Promise<unknown> => {
  try {
    await Array.fromAsync(streamChatTurn(BODY, signal))
  } catch (error) {
    return error
  }
  throw new Error('Expected the stream to reject')
}

afterEach(() => {
  globalThis.fetch = originalFetch
  globalThis.WebSocket = originalSocket
})

describe('streamChatTurn refusals', () => {
  test('an abort while the refusal body is read passes through unchanged', async () => {
    const abort = new DOMException('The operation was aborted.', 'AbortError')
    respondWith(refusingResponse(abort))

    const error = await rejectionOf(new AbortController().signal)

    expect(error instanceof ChatStreamError).toBe(false)
    expect(error instanceof Error).toBe(true)
    expect((error as Error).name).toBe('AbortError')
  })

  test('a refused turn keeps the detail code and never quotes the response text', async () => {
    const body = JSON.stringify({ detail: { code: 'conversation_not_found', message: 'x' } })
    respondWith(new Response(body, { status: 404 }))

    const error = await rejectionOf(new AbortController().signal)

    expect(error instanceof ChatStreamError).toBe(true)
    const refusal = error as ChatStreamError
    expect(refusal.code).toBe('conversation_not_found')
    expect(refusal.detailCode).toBe('conversation_not_found')
    expect(refusal.message).not.toContain('x')
  })

  test('a refusal for an unknown model keeps its own code, other refusals stay internal', async () => {
    respondWith(new Response(JSON.stringify({ detail: { code: 'model_unknown' } }), { status: 422 }))
    expect(((await rejectionOf(new AbortController().signal)) as ChatStreamError).code).toBe('model_unknown')
    respondWith(new Response(JSON.stringify({ detail: { code: 'something_else' } }), { status: 422 }))
    expect(((await rejectionOf(new AbortController().signal)) as ChatStreamError).code).toBe('internal')
  })
})


test('voice transport plays server audio before done and acknowledges only after playback', async () => {
  let socket!: FakeSocket, finish!: () => void
  const sent: unknown[] = [], chunks: string[] = []
  class FakeSocket {
    static OPEN = 1
    readyState = 1
    onopen?: () => void; onmessage?: (event: { data: string }) => void; onclose?: () => void; onerror?: () => void
    constructor() { socket = this; queueMicrotask(() => this.onopen?.()) }
    send(data: string) { sent.push(JSON.parse(data)) }
    close() { this.readyState = 3; this.onclose?.() }
    emit(event: object) { this.onmessage?.({ data: JSON.stringify(event) }) }
  }
  globalThis.WebSocket = FakeSocket as unknown as typeof WebSocket
  const signal = new AbortController().signal
  const result = Array.fromAsync(streamVoiceChatTurn({ text: 'Hallo', conversation_id: 'c', voice_response: true }, signal, {
    push: chunk => chunks.push(chunk), end: () => new Promise(resolve => { finish = resolve }), fail: () => {},
  }))
  await Promise.resolve()
  socket.emit({ event: 'start', conversation_id: 'c', user_message_id: 'u', model: 'm' })
  socket.emit({ event: 'delta', text: 'Hallo.' })
  socket.emit({ event: 'voice_audio', audio: 'AAAAAA==' })
  socket.emit({ event: 'voice_audio_end' })
  expect(chunks).toEqual(['AAAAAA=='])
  expect(sent).toEqual([{ text: 'Hallo', conversation_id: 'c', voice_response: true }])
  finish(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
  expect(sent[1]).toEqual({ event: 'playback_finished' })
  socket.emit({ event: 'done', conversation_id: 'c', message_id: 'new', stopped: false })
  expect((await result).map(event => event.event)).toEqual(['start', 'delta', 'done'])
})
