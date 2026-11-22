import type { VoiceAudio } from './transport'

/** Voice turn coordination, independent of media and network transport. */
type Event = { type: 'turn.start' | 'turn.end' | 'turn.eager_end' | 'turn.resume' } | { type: 'transcription.text.delta'; text: string }
type State = 'idle' | 'listening' | 'answering' | 'speaking' | 'resuming' | 'error'
type Options = {
  conversationId?: string
  schedule: (fn: () => void) => () => void
  scheduleTail: (fn: () => void) => () => void
  suspendInput: () => void
  resumeInput: () => Promise<void>
  send: (text: string, conversationId: string | undefined, audio?: VoiceAudio, sttElapsedMs?: number) => Promise<{ conversationId: string; messageId: string }>
  startLiveAudio?: (signal: AbortSignal) => VoiceAudio
  speak: (reply: { conversationId: string; messageId: string }, signal: AbortSignal) => Promise<void>
  stopChat: () => void
  onState: (state: State) => void
}

export function createLiveVoice(options: Options) {
  let enabled = false, listening = false, epoch = 0, transcript = '', inTurn = false, endTime = 0
  let conversationId = options.conversationId
  let cancelTimer: (() => void) | undefined, cancelTail: (() => void) | undefined
  let playback: AbortController | undefined
  let working = false
  const interrupt = () => {
    playback?.abort(); playback = undefined
    if (working) { options.stopChat(); working = false }
    epoch++
  }
  const resume = (generation: number) => {
    cancelTail?.()
    options.onState('resuming')
    cancelTail = options.scheduleTail(() => {
      cancelTail = undefined
      if (!enabled || epoch !== generation) return
      void options.resumeInput().then(() => {
        if (!enabled || epoch !== generation) return
        listening = true
        options.onState('listening')
      }).catch(() => { if (enabled && epoch === generation) options.onState('error') })
    })
  }
  const submit = () => {
    cancelTimer = undefined
    const text = transcript.trim(); transcript = ''; inTurn = false
    if (!enabled || !listening || !text) return
    listening = false
    options.suspendInput()
    const generation = ++epoch
    working = true
    options.onState('answering')
    if (options.startLiveAudio) playback = new AbortController()
    const liveAudio = playback && options.startLiveAudio?.(playback.signal)
    void options.send(text, conversationId, liveAudio, endTime ? Math.min(60000, Math.max(0, Math.round(performance.now() - endTime))) : undefined).then(async result => {
      if (!enabled || epoch !== generation) return
      working = false
      conversationId = result.conversationId
      if (liveAudio?.isTerminal?.()) { playback = undefined; options.onState('error'); return }
      if (!liveAudio) {
        playback = new AbortController()
        options.onState('speaking')
        try { await options.speak(result, playback.signal) } catch { /* The saved reply remains available as text. */ }
      }
      if (enabled && epoch === generation) { playback = undefined; resume(generation) }
    }).catch(() => {
      if (enabled && epoch === generation) { working = false; options.onState('error') }
    })
  }
  return {
    start() { if (!enabled) { enabled = true; listening = true; options.onState('listening') } },
    stop() {
      if (!enabled) return
      enabled = false; listening = false; cancelTimer?.(); cancelTimer = undefined; cancelTail?.(); cancelTail = undefined; transcript = ''; inTurn = false
      interrupt(); options.onState('idle')
    },
    interrupt() {
      if (!enabled || listening || (!working && !playback)) return
      interrupt(); resume(epoch)
    },
    reconnect() {
      if (!enabled || working || playback) return
      listening = false; cancelTimer?.(); cancelTimer = undefined; transcript = ''; inTurn = false
      resume(++epoch)
    },
    acceptsAudio() { return enabled && listening },
    event(event: Event) {
      if (!enabled || !listening) return
      if (event.type === 'turn.start') {
        if (cancelTimer) { cancelTimer(); cancelTimer = undefined; inTurn = true; return }
        transcript = ''; inTurn = true; options.onState('listening')
      } else if (event.type === 'transcription.text.delta') {
        if (inTurn && transcript.length + event.text.length > 8000) throw new Error('audio_unavailable')
        if (inTurn) transcript += event.text
      } else if (event.type === 'turn.end' && inTurn && !cancelTimer) {
        endTime = performance.now()
        cancelTimer = options.schedule(submit)
      }
    },
  }
}
