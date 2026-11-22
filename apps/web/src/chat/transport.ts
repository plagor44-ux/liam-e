import { apiBaseUrl } from '../api/client-config'
import type { ChatEvent, ChatStreamRequest } from '../api/generated/types.gen'
import { parseSse } from './sse'

// Refusals before the stream starts that the conversation view explains on their own.
const KNOWN_REFUSALS = ['settings_unavailable', 'chat_settings_changed', 'chat_account_not_granted', 'preferences_unavailable', 'conversation_not_found', 'model_unknown', 'model_catalog_unavailable', 'conversation_scope_change_requires_new', 'calendar_reference_unavailable', 'context_unavailable', 'context_invalid', 'version_conflict', 'skill_not_found', 'skill_revision_unavailable', 'skill_disabled', 'skill_tool_unavailable'] as const
export const SKILL_REFUSALS = new Set<string>(['skill_not_found', 'skill_revision_unavailable', 'skill_disabled', 'skill_tool_unavailable'])
export type ChatRefusal = typeof KNOWN_REFUSALS[number]
const isKnownRefusal = (code: string | undefined): code is ChatRefusal => (KNOWN_REFUSALS as readonly (string | undefined)[]).includes(code)

export class ChatStreamError extends Error {
  constructor(public code: 'internal' | 'aborted' | ChatRefusal, public detailCode?: string) {
    super('Chat stream failed')
    this.name = 'ChatStreamError'
  }
}

/** Whether `error` is a caller's abort: a browser `DOMException` is not an `Error`. */
const isAbort = (error: unknown): boolean =>
  typeof error === 'object' && error !== null && 'name' in error && error.name === 'AbortError'

export async function* streamChatTurn(body: ChatStreamRequest, signal: AbortSignal): AsyncIterable<ChatEvent> {
  try {
    const response = await fetch(`${apiBaseUrl()}/api/v1/chat/stream`, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' }, body: JSON.stringify(body), signal })
    if (!response.ok) {
      let detailCode: string | undefined
      try {
        const json = await response.json()
        if (typeof json?.detail?.code === 'string') detailCode = json.detail.code
      } catch (error) {
        // Reading the refusal body can end in the caller's abort: that abort is not a refusal.
        if (isAbort(error)) throw error
        /* No JSON detail. */
      }
      throw new ChatStreamError(isKnownRefusal(detailCode) ? detailCode : 'internal', detailCode)
    }
    if (!response.body) throw new ChatStreamError('aborted')
    for await (const frame of parseSse(response.body)) {
      let event: ChatEvent
      try { event = JSON.parse(frame.data) as ChatEvent } catch { throw new ChatStreamError('internal') }
      yield event
      if (event.event === 'done' || event.event === 'error') return
    }
    throw new ChatStreamError('aborted')
  } catch (error) {
    if (error instanceof ChatStreamError || isAbort(error)) throw error
    throw new ChatStreamError('aborted')
  }
}

type VoiceRefusal = { event: 'refusal'; code?: string }
export type VoiceTextDone = { event: 'voice_text_done'; conversation_id: string; message_id: string }
export type VoiceAudio = { push: (data: string) => void; end: () => Promise<void>; fail: (terminal?: boolean) => void; isTerminal?: () => boolean }

/** A voice socket carries only server-produced chat deltas and audio for the same turn. */
export async function* streamVoiceChatTurn(body: ChatStreamRequest, signal: AbortSignal, audio: VoiceAudio): AsyncIterable<ChatEvent | VoiceTextDone> {
  const url = new URL(`${apiBaseUrl()}/api/v1/audio/live/turn`, window.location.href)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  const socket = new WebSocket(url)
  const pending: (ChatEvent | VoiceTextDone | VoiceRefusal)[] = []
  let wake: (() => void) | undefined, closed = false, terminal = false
  const notify = () => { wake?.(); wake = undefined }
  const abort = () => { closed = true; socket.close(); notify() }
  signal.addEventListener('abort', abort, { once: true })
  socket.onopen = () => { if (!signal.aborted) socket.send(JSON.stringify(body)) }
  socket.onmessage = message => {
    try {
      const event = JSON.parse(message.data)
      if (event.event === 'voice_audio') audio.push(event.audio)
      else if (event.event === 'voice_audio_error') audio.fail(event.terminal === true)
      else if (event.event === 'voice_audio_end') {
        void audio.end().catch(() => {}).then(() => { if (!signal.aborted && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ event: 'playback_finished' })) })
      } else { pending.push(event as ChatEvent | VoiceTextDone | VoiceRefusal); notify() }
    } catch { audio.fail(); socket.close(); closed = true; notify() }
  }
  socket.onclose = () => { closed = true; notify() }
  socket.onerror = () => { closed = true; notify() }
  try {
    if (signal.aborted) abort()
    while (!closed || pending.length) {
      if (!pending.length) { await new Promise<void>(resolve => { wake = resolve }); continue }
      const event = pending.shift()!
      if (event.event === 'refusal') {
        const code = typeof event.code === 'string' ? event.code : undefined
        throw new ChatStreamError(isKnownRefusal(code) ? code : 'internal', code)
      }
      yield event
      if (event.event === 'done' || event.event === 'error') { terminal = true; return }
    }
    if (!terminal) throw new ChatStreamError('aborted')
  } finally { signal.removeEventListener('abort', abort); socket.close() }
}
