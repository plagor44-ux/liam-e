/** Voice turn coordination, independent of media and network transport. */
type Event = { type: 'turn.start' | 'turn.end' | 'turn.eager_end' | 'turn.resume' } | { type: 'transcription.text.delta'; text: string }
type State = 'idle' | 'listening' | 'answering' | 'speaking' | 'error'
type Options = {
  conversationId?: string
  schedule: (fn: () => void) => () => void
  send: (text: string, conversationId: string | undefined) => Promise<{ conversationId: string; messageId: string }>
  speak: (reply: { conversationId: string; messageId: string }, signal: AbortSignal) => Promise<void>
  stopChat: () => void
  onState: (state: State) => void
}

export function createLiveVoice(options: Options) {
  let enabled = false, epoch = 0, transcript = '', inTurn = false
  let conversationId = options.conversationId
  let cancelTimer: (() => void) | undefined
  let playback: AbortController | undefined
  let working = false
  const interrupt = () => {
    playback?.abort(); playback = undefined
    if (working) { options.stopChat(); working = false }
    epoch++
  }
  const submit = () => {
    cancelTimer = undefined
    const text = transcript.trim(); transcript = ''; inTurn = false
    if (!enabled || !text) return
    if (working || playback) interrupt()
    const generation = ++epoch
    working = true
    options.onState('answering')
    void options.send(text, conversationId).then(async result => {
      if (!enabled || epoch !== generation) return
      working = false
      conversationId = result.conversationId
      if (inTurn) return
      playback = new AbortController()
      options.onState('speaking')
      await options.speak(result, playback.signal)
      if (enabled && epoch === generation) options.onState('listening')
    }).catch(() => {
      if (enabled && epoch === generation) { working = false; options.onState('error') }
    })
  }
  return {
    start() { if (!enabled) { enabled = true; options.onState('listening') } },
    stop() {
      if (!enabled) return
      enabled = false; cancelTimer?.(); cancelTimer = undefined; transcript = ''; inTurn = false
      interrupt(); options.onState('idle')
    },
    event(event: Event) {
      if (!enabled) return
      if (event.type === 'turn.start') {
        interrupt()
        if (cancelTimer) { cancelTimer(); cancelTimer = undefined; submit() }
        transcript = ''; inTurn = true; options.onState('listening')
      } else if (event.type === 'transcription.text.delta') {
        if (inTurn && transcript.length + event.text.length > 8000) throw new Error('audio_unavailable')
        if (inTurn) transcript += event.text
      } else if (event.type === 'turn.end' && inTurn && !cancelTimer) {
        cancelTimer = options.schedule(submit)
      }
    },
  }
}
