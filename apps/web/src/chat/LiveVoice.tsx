import { useCallback, useEffect, useRef, useState } from 'react'
import { apiBaseUrl } from '../api/client-config'
import { audioLiveSpeech, audioLiveStatus } from '../api/generated/sdk.gen'
import { unwrap } from '../api/unwrap'
import { IconButton } from '../ds'
import { useI18n } from '../i18n/I18n'
import { createLiveVoice } from './live-voice'

type Reply = { conversationId: string; messageId: string }
type Props = { conversationId: string; disabled: boolean; contextKey: string; send: (text: string) => Promise<Reply | undefined>; stopChat: () => void; registerStop: (stop: (() => void) | null) => void; onStart?: () => void }

// Transcription input is signed 16-bit little-endian mono at 24 kHz.
function pcm(input: Float32Array, inputRate: number): ArrayBuffer {
  const count = Math.floor(input.length * 24000 / inputRate)
  const output = new ArrayBuffer(count * 2), view = new DataView(output)
  for (let i = 0; i < count; i++) {
    const sample = Math.max(-1, Math.min(1, input[Math.floor(i * inputRate / 24000)] ?? 0))
    view.setInt16(i * 2, sample < 0 ? sample * 32768 : sample * 32767, true)
  }
  return output
}

async function playReply(context: AudioContext, reply: Reply, signal: AbortSignal) {
  // The generated schema describes binary output as Blob, but parseAs: stream returns response.body.
  const stream = await unwrap(audioLiveSpeech({ body: { conversation_id: reply.conversationId, message_id: reply.messageId }, parseAs: 'stream', signal, throwOnError: true })) as unknown as ReadableStream<Uint8Array> | null
  if (!stream) throw new Error('audio_unavailable')
  if (signal.aborted) { await stream.cancel(); return }
  const reader = stream.getReader(), sources = new Set<AudioBufferSourceNode>()
  let next = context.currentTime, remaining = new Uint8Array(0), gotAudio = false
  const abort = () => { void reader.cancel(); sources.forEach(source => { try { source.stop() } catch { /* already stopped */ } }) }
  signal.addEventListener('abort', abort, { once: true })
  try {
    while (!signal.aborted) {
      const { value, done } = await reader.read()
      if (done || signal.aborted) break
      let bytes = value
      if (remaining.length) { bytes = new Uint8Array(value.length + remaining.length); bytes.set(remaining); bytes.set(value, remaining.length) }
      const length = Math.floor(bytes.length / 4)
      remaining = bytes.slice(length * 4)
      if (!length) continue
      gotAudio = true
      const buffer = context.createBuffer(1, length, 24000), samples = buffer.getChannelData(0), view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
      // Mistral PCM speech is float32 little-endian, unlike transcription input.
      for (let i = 0; i < length; i++) { const sample = view.getFloat32(i * 4, true); if (!Number.isFinite(sample)) throw new Error('audio_unavailable'); samples[i] = Math.max(-1, Math.min(1, sample)) }
      const source = context.createBufferSource(); source.buffer = buffer; source.connect(context.destination); sources.add(source)
      source.onended = () => sources.delete(source)
      next = Math.max(next, context.currentTime); source.start(next); next += buffer.duration
    }
    if (!signal.aborted && (!gotAudio || remaining.length)) throw new Error('audio_unavailable')
    if (!signal.aborted && next > context.currentTime) await new Promise<void>(resolve => {
      const timer = setTimeout(resolve, (next - context.currentTime) * 1000)
      signal.addEventListener('abort', () => { clearTimeout(timer); resolve() }, { once: true })
    })
  } finally { signal.removeEventListener('abort', abort); reader.releaseLock() }
}

export function LiveVoice({ conversationId, disabled, contextKey, send, stopChat, registerStop, onStart }: Props) {
  const { texts } = useI18n()
  const [available, setAvailable] = useState(false)
  const [state, setState] = useState<'idle' | 'starting' | 'listening' | 'answering' | 'error'>('idle')
  const active = useRef<{ stop: () => void } | null>(null)
  const current = useRef({ send, stopChat }); current.current = { send, stopChat }
  const stop = useCallback(() => { const session = active.current; active.current = null; session?.stop(); setState('idle') }, [])
  useEffect(() => { registerStop(stop); return () => registerStop(null) }, [registerStop, stop])
  useEffect(() => {
    if (!globalThis.isSecureContext || typeof AudioContext === 'undefined' || !navigator.mediaDevices?.getUserMedia) return
    const controller = new AbortController()
    void unwrap(audioLiveStatus({ signal: controller.signal, throwOnError: true })).then(result => { if (!controller.signal.aborted) setAvailable(result.available === true) }).catch(() => {})
    return () => controller.abort()
  }, [])
  useEffect(() => () => stop(), [contextKey, stop])
  useEffect(() => { const hidden = () => { if (document.hidden) stop() }; document.addEventListener('visibilitychange', hidden); return () => document.removeEventListener('visibilitychange', hidden) }, [stop])

  const start = async () => {
    if (disabled || active.current || document.hidden) return
    onStart?.()
    setState('starting')
    let stream: MediaStream | undefined, socket: WebSocket | undefined, context: AudioContext | undefined, processor: ScriptProcessorNode | undefined
    const session = { stop: () => { voice.stop(); processor?.disconnect(); socket?.close(); stream?.getTracks().forEach(track => track.stop()); void context?.close() } }
    const voice = createLiveVoice({ conversationId, schedule: fn => { const timer = setTimeout(fn, 480); return () => clearTimeout(timer) },
      send: async text => { const result = await current.current.send(text); if (!result) throw new Error('chat_failed'); return result },
      speak: (reply, signal) => playReply(context!, reply, signal), stopChat: () => current.current.stopChat(),
      onState: next => { if (next === 'error') { stop(); setState('error') } else setState(next) } })
    active.current = session
    try {
      context = new AudioContext(); await context.resume()
      if (active.current !== session) return
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      if (active.current !== session || document.hidden) { stream.getTracks().forEach(track => track.stop()); return }
      const url = new URL(`${apiBaseUrl()}/api/v1/audio/live/transcription`, window.location.href); url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(url); socket.binaryType = 'arraybuffer'
      await new Promise<void>((resolve, reject) => { socket!.onopen = () => resolve(); socket!.onerror = () => reject(new Error('audio_unavailable')); socket!.onclose = () => reject(new Error('audio_unavailable')) })
      if (active.current !== session) return
      socket.onmessage = message => { try { voice.event(JSON.parse(message.data)) } catch { stop(); setState('error') } }
      socket.onclose = () => { if (active.current === session) { stop(); setState('error') } }
      const source = context.createMediaStreamSource(stream); processor = context.createScriptProcessor(4096, 1, 1)
      processor.onaudioprocess = event => {
        if (socket?.readyState !== WebSocket.OPEN) return
        if (socket.bufferedAmount >= 96000) { stop(); setState('error'); return }
        socket.send(pcm(event.inputBuffer.getChannelData(0), context!.sampleRate))
      }
      source.connect(processor); processor.connect(context.destination)
      voice.start()
    } catch { if (active.current === session) { stop(); setState('error') } }
  }
  if (!available) return null
  const label = state === 'idle' || state === 'error' ? texts.audio.liveStart : texts.audio.liveStop
  const status = state === 'starting' ? texts.audio.liveStarting : state === 'listening' ? texts.audio.liveListening : state === 'answering' ? texts.audio.liveAnswering : state === 'error' ? texts.audio.liveError : ''
  return <div className="le-audio-controls"><span className="le-audio-status" role="status">{status}</span><IconButton size="lg" icon={state === 'idle' || state === 'error' ? 'mic' : 'square'} label={label} disabled={disabled && state === 'idle'} onClick={() => { if (active.current) stop(); else void start() }} /></div>
}
