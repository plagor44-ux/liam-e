import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { apiBaseUrl } from '../api/client-config'
import { audioLiveSpeech, audioLiveStatus } from '../api/generated/sdk.gen'
import { unwrap } from '../api/unwrap'
import { IconButton } from '../ds'
import { useFocusTrap } from '../ds/useFocusTrap'
import { useI18n } from '../i18n/I18n'
import { createLiveVoice } from './live-voice'
import type { VoiceAudio } from './transport'

type Reply = { conversationId: string; messageId: string }
type Props = { conversationId: string; disabled: boolean; contextKey: string; send: (text: string, audio?: VoiceAudio) => Promise<Reply | undefined>; stopChat: () => void; registerStop: (stop: (() => void) | null) => void; onStart?: () => void }

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

export async function playReply(context: AudioContext, reply: Reply, signal: AbortSignal) {
  // The generated schema describes binary output as Blob, but parseAs: stream returns response.body.
  const stream = await unwrap(audioLiveSpeech({ body: { conversation_id: reply.conversationId, message_id: reply.messageId }, parseAs: 'stream', signal, throwOnError: true })) as unknown as ReadableStream<Uint8Array> | null
  if (!stream) throw new Error('audio_unavailable')
  return playPcm(context, stream, signal)
}

async function playPcm(context: AudioContext, stream: ReadableStream<Uint8Array>, signal: AbortSignal) {
  if (signal.aborted) { await stream.cancel(); return }
  const reader = stream.getReader(), sources = new Set<AudioBufferSourceNode>()
  const endings: Promise<void>[] = []
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
      endings.push(new Promise<void>(resolve => { source.onended = () => { sources.delete(source); resolve() } }))
      next = Math.max(next, context.currentTime); source.start(next); next += buffer.duration
    }
    if (!signal.aborted && (!gotAudio || remaining.length)) throw new Error('audio_unavailable')
    if (!signal.aborted) {
      await new Promise<void>(resolve => {
        const finish = () => { signal.removeEventListener('abort', finish); resolve() }
        signal.addEventListener('abort', finish, { once: true })
        if (signal.aborted) finish()
        else void Promise.all(endings).then(finish)
      })
    }
  } catch (error) { abort(); throw error }
  finally { signal.removeEventListener('abort', abort); reader.releaseLock() }
}


function liveAudio(context: AudioContext, signal: AbortSignal, onPlaying: () => void): VoiceAudio {
  let writer!: ReadableStreamDefaultController<Uint8Array>, closed = false, terminal = false
  const aborter = new AbortController()
  const stream = new ReadableStream<Uint8Array>({ start(controller) { writer = controller } })
  const stop = () => { closed = true; aborter.abort() }
  signal.addEventListener('abort', stop, { once: true })
  const playing = playPcm(context, stream, aborter.signal).catch(() => {}).finally(() => signal.removeEventListener('abort', stop))
  return {
    push(encoded) {
      if (closed) return
      const raw = atob(encoded), bytes = new Uint8Array(raw.length)
      for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
      onPlaying(); writer.enqueue(bytes)
    },
    async end() { if (!closed) { closed = true; writer.close() } await playing },
    fail: isTerminal => { terminal = isTerminal === true; stop() },
    isTerminal: () => terminal,
  }
}

export function LiveVoice({ conversationId, disabled, contextKey, send, stopChat, registerStop, onStart }: Props) {
  const { texts } = useI18n()
  const [available, setAvailable] = useState(false)
  const [state, setState] = useState<'idle' | 'starting' | 'listening' | 'answering' | 'speaking' | 'resuming' | 'error'>('idle')
  const active = useRef<{ stop: () => void; interrupt: () => void } | null>(null)
  const control = useRef<HTMLDivElement>(null)
  const endButton = useRef<HTMLButtonElement>(null)
  const restoreFocus = useRef(false)
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
  const shown = state !== 'idle' && state !== 'error'
  const trap = useFocusTrap(shown, stop)
  useEffect(() => { if (!shown && restoreFocus.current) { control.current?.querySelector('button')?.focus(); restoreFocus.current = false } }, [shown])

  const start = async () => {
    if (disabled || active.current || document.hidden) return
    restoreFocus.current = true
    onStart?.()
    setState('starting')
    let stream: MediaStream | undefined, socket: WebSocket | undefined, context: AudioContext | undefined, processor: ScriptProcessorNode | undefined
    let socketClosed: Promise<void> = Promise.resolve(), started = false
    const session = { stop: () => { voice.stop(); processor?.disconnect(); socket?.close(); stream?.getTracks().forEach(track => track.stop()); void context?.close() }, interrupt: () => voice.interrupt() }
    const openSocket = async () => {
      await socketClosed
      if (active.current !== session) return
      const url = new URL(`${apiBaseUrl()}/api/v1/audio/live/transcription`, window.location.href); url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
      const candidate = new WebSocket(url); socket = candidate; candidate.binaryType = 'arraybuffer'
      let closed!: () => void
      let opened = false
      socketClosed = new Promise(resolve => { closed = resolve })
      await new Promise<void>((resolve, reject) => {
        candidate.onopen = () => { opened = true; resolve() }
        candidate.onerror = () => reject(new Error('audio_unavailable'))
        candidate.onclose = event => { closed(); if (!opened) reject(new Error('audio_unavailable')); else if (socket === candidate && active.current === session) { socket = undefined; if (!started || event?.code === 1008) { stop(); setState('error') } else voice.reconnect() } }
      })
      if (active.current !== session || socket !== candidate) { candidate.close(); return }
      candidate.onmessage = message => { if (socket !== candidate) return; try { voice.event(JSON.parse(message.data)) } catch { stop(); setState('error') } }
    }
    const voice = createLiveVoice({ conversationId, schedule: fn => { const timer = setTimeout(fn, 480); return () => clearTimeout(timer) },
      scheduleTail: fn => { const timer = setTimeout(fn, 400); return () => clearTimeout(timer) },
      suspendInput: () => { const previous = socket; socket = undefined; previous?.close() },
      resumeInput: async () => {
        for (let attempt = 0; attempt < 3; attempt++) {
          if (active.current !== session) return
          try { await openSocket(); return } catch { if (attempt === 2) throw new Error('audio_unavailable') }
          await new Promise(resolve => setTimeout(resolve, 350 * (attempt + 1)))
        }
      },
      send: async (text, _conversation, audio) => { const result = await current.current.send(text, audio); if (!result) throw new Error('chat_failed'); return result },
      startLiveAudio: signal => liveAudio(context!, signal, () => { if (!signal.aborted) setState('speaking') }),
      speak: (reply, signal) => playReply(context!, reply, signal), stopChat: () => current.current.stopChat(),
      onState: next => { if (next === 'error') { stop(); setState('error') } else setState(next) } })
    active.current = session
    try {
      context = new AudioContext(); await context.resume()
      if (active.current !== session) return
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      if (active.current !== session || document.hidden) { stream.getTracks().forEach(track => track.stop()); return }
      await openSocket()
      if (active.current !== session) return
      const source = context.createMediaStreamSource(stream); processor = context.createScriptProcessor(4096, 1, 1)
      processor.onaudioprocess = event => {
        if (!voice.acceptsAudio() || socket?.readyState !== WebSocket.OPEN) return
        if (socket.bufferedAmount >= 96000) { stop(); setState('error'); return }
        socket.send(pcm(event.inputBuffer.getChannelData(0), context!.sampleRate))
      }
      source.connect(processor); processor.connect(context.destination)
      started = true; voice.start()
    } catch { if (active.current === session) { stop(); setState('error') } }
  }
  if (!available) return null
  const status = state === 'starting' ? texts.audio.liveStarting : state === 'resuming' ? texts.audio.liveResuming : state === 'listening' ? texts.audio.liveListening : state === 'answering' ? texts.audio.liveAnswering : state === 'speaking' ? texts.audio.liveSpeaking : state === 'error' ? texts.audio.liveError : ''
  const finish = () => { stop() }
  return <div className="le-audio-controls" ref={control}>{!shown && <><span className="le-audio-status le-voice-status" role="status">{status}</span><IconButton size="lg" icon="audio-lines" label={texts.audio.liveStart} disabled={disabled} onClick={() => void start()} /></>}
    {shown && createPortal(<div className="le-voice-overlay"><div ref={trap} tabIndex={-1} className="le-voice-panel" role="dialog" aria-modal="true" aria-label={texts.audio.liveTitle}>
      <h2 className="heading">{texts.audio.liveTitle}</h2><div className={`le-voice-indicator le-voice-indicator--${state}`} aria-hidden="true"><span /><span /><span /><span /><span /></div>
      <p className="le-voice-state" role="status" aria-live="polite">{status}</p><p className="le-voice-hint">{texts.audio.liveHint}</p>
      {(state === 'answering' || state === 'speaking') && <button type="button" className="le-btn le-btn--secondary" onClick={() => { active.current?.interrupt(); endButton.current?.focus() }}>{texts.audio.liveInterrupt}</button>}
      <button ref={endButton} type="button" className="le-btn le-btn--primary" aria-label={texts.audio.liveStop} onClick={finish}>{texts.audio.liveStop}</button>
    </div></div>, document.body)}</div>
}
