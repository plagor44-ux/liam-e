import { expect, test } from 'bun:test'
import { createLiveVoice } from './live-voice'

test('waits for late transcript, carries conversation across turns and cancels playback on interruption', async () => {
  const calls: Array<[string, string | undefined]> = []
  let flush: (() => void) | undefined
  let playbackStops = 0
  const voice = createLiveVoice({
    schedule: f => { flush = f; return () => { flush = undefined } },
    send: async (text, conversation) => { calls.push([text, conversation]); return { conversationId: 'c1', messageId: 'm1' } },
    speak: async (_text, signal) => { signal.addEventListener('abort', () => { playbackStops++ }) },
    stopChat: () => {}, onState: () => {},
  })
  voice.start()
  voice.event({ type: 'turn.start' })
  voice.event({ type: 'transcription.text.delta', text: 'Testente ist' })
  voice.event({ type: 'turn.end' })
  voice.event({ type: 'transcription.text.delta', text: ' gelb.' })
  flush!(); await new Promise(resolve => setTimeout(resolve, 0))
  expect(calls).toEqual([['Testente ist gelb.', undefined]])
  voice.event({ type: 'turn.start' })
  expect(playbackStops).toBe(1)
  voice.event({ type: 'transcription.text.delta', text: 'Welche Farbe?' })
  voice.event({ type: 'turn.end' }); flush!(); await new Promise(resolve => setTimeout(resolve, 0))
  expect(calls).toEqual([['Testente ist gelb.', undefined], ['Welche Farbe?', 'c1']])
  voice.stop()
})

test('stop discards pending and late results', async () => {
  let deliver: ((value: { conversationId: string; messageId: string }) => void) | undefined
  let flush: (() => void) | undefined
  let spoken = 0, stops = 0
  const voice = createLiveVoice({
    schedule: f => { flush = f; return () => { flush = undefined } },
    send: () => new Promise(resolve => { deliver = resolve }), speak: async () => { spoken++ },
    stopChat: () => { stops++ }, onState: () => {},
  })
  voice.start(); voice.event({ type: 'turn.start' }); voice.event({ type: 'transcription.text.delta', text: 'Hallo' }); voice.event({ type: 'turn.end' }); flush!()
  voice.stop(); deliver!({ conversationId: 'c', messageId: 'spät' }); await Promise.resolve()
  expect(spoken).toBe(0); expect(stops).toBe(1)
  voice.event({ type: 'turn.start' }); voice.event({ type: 'transcription.text.delta', text: 'ignoriert' })
})

test('speech interrupts a still-playing response and stop cancels the pending turn', async () => {
  let flush: (() => void) | undefined, stopped = 0, interrupted = false
  const states: string[] = []
  const voice = createLiveVoice({
    conversationId: 'c', schedule: fn => { flush = fn; return () => { flush = undefined } },
    send: async () => ({ conversationId: 'c', messageId: 'm' }),
    speak: async (_reply, signal) => new Promise<void>(resolve => signal.addEventListener('abort', () => { interrupted = true; resolve() })),
    stopChat: () => { stopped++ }, onState: state => { states.push(state) },
  })
  voice.start(); voice.event({ type: 'turn.start' }); voice.event({ type: 'transcription.text.delta', text: 'Hallo' }); voice.event({ type: 'turn.end' }); flush!()
  expect(states.at(-1)).toBe('answering')
  await Promise.resolve(); expect(states.at(-1)).toBe('speaking'); voice.event({ type: 'turn.start' })
  expect(interrupted).toBe(true)
  expect(states.at(-1)).toBe('listening')
  voice.event({ type: 'transcription.text.delta', text: 'Abbruch' }); voice.event({ type: 'turn.end' })
  voice.stop(); expect(flush).toBeUndefined(); expect(stopped).toBe(0)
})

test('an eager end and resume never submit a partial utterance', () => {
  let scheduled = 0
  const voice = createLiveVoice({ schedule: () => { scheduled++; return () => {} }, send: async () => ({ conversationId: 'c', messageId: 'm' }), speak: async () => {}, stopChat: () => {}, onState: () => {} })
  voice.start(); voice.event({ type: 'turn.start' }); voice.event({ type: 'transcription.text.delta', text: 'Noch nicht fertig' })
  voice.event({ type: 'turn.eager_end' }); voice.event({ type: 'turn.resume' })
  expect(scheduled).toBe(0)
  voice.event({ type: 'turn.end' }); expect(scheduled).toBe(1)
  voice.stop()
})

test('duplicate end events yield one chat turn and ignored late deltas cannot create another', async () => {
  let flush!: () => void, calls = 0
  const voice = createLiveVoice({ schedule: fn => { flush = fn; return () => {} }, send: async () => { calls++; return { conversationId: 'c', messageId: 'm' } }, speak: async () => {}, stopChat: () => {}, onState: () => {} })
  voice.start(); voice.event({ type: 'turn.start' }); voice.event({ type: 'transcription.text.delta', text: 'Hallo' }); voice.event({ type: 'turn.end' }); voice.event({ type: 'turn.end' })
  flush(); await Promise.resolve(); voice.event({ type: 'transcription.text.delta', text: 'zu spät' }); voice.event({ type: 'turn.end' })
  expect(calls).toBe(1); voice.stop()
})
