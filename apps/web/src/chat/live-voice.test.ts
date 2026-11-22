import { expect, test } from 'bun:test'
import { createLiveVoice } from './live-voice'

test('echo is ignored during answering, playback and tail; listening resumes on a fresh input', async () => {
  let flush!: () => void, tail!: () => void, finishSend!: (value: { conversationId: string; messageId: string }) => void, finishPlayback!: () => void
  let pauses = 0, resumes = 0, stops = 0, aborted = 0
  const turns: string[] = [], states: string[] = []
  const voice = createLiveVoice({
    conversationId: 'c', schedule: fn => { flush = fn; return () => {} }, scheduleTail: fn => { tail = fn; return () => {} },
    suspendInput: () => { pauses++ }, resumeInput: async () => { resumes++ },
    send: text => { turns.push(text); return new Promise(resolve => { finishSend = resolve }) },
    speak: (_reply, signal) => new Promise<void>(resolve => { finishPlayback = resolve; signal.addEventListener('abort', () => { aborted++; resolve() }) }),
    stopChat: () => { stops++ }, onState: state => { states.push(state) },
  })
  const utterance = (text: string) => { voice.event({ type: 'turn.start' }); voice.event({ type: 'transcription.text.delta', text }); voice.event({ type: 'turn.end' }) }
  voice.start(); utterance('Hallo'); flush()
  expect(voice.acceptsAudio()).toBe(false); expect(pauses).toBe(1); expect(states.at(-1)).toBe('answering')
  utterance('Echo während der Antwort'); expect(turns).toEqual(['Hallo']); expect(stops).toBe(0)
  finishSend({ conversationId: 'c', messageId: 'm' }); await Promise.resolve()
  expect(states.at(-1)).toBe('speaking'); utterance('Echo während der Wiedergabe')
  expect(aborted).toBe(0); expect(stops).toBe(0)
  finishPlayback(); await Promise.resolve(); await Promise.resolve()
  utterance('Echo im Nachlauf'); expect(voice.acceptsAudio()).toBe(false)
  tail(); await Promise.resolve(); await Promise.resolve()
  expect(resumes).toBe(1); expect(voice.acceptsAudio()).toBe(true); expect(states.at(-1)).toBe('listening')
  utterance('Nächste Frage'); flush(); expect(turns).toEqual(['Hallo', 'Nächste Frage'])
  voice.stop()
})

test('explicit interruption cancels playback and resumes only after tail', async () => {
  let flush!: () => void, tail!: () => void, canceled = 0, stops = 0, resumeCount = 0
  const voice = createLiveVoice({ schedule: fn => { flush = fn; return () => {} }, scheduleTail: fn => { tail = fn; return () => {} }, suspendInput: () => {}, resumeInput: async () => { resumeCount++ },
    send: async () => ({ conversationId: 'c', messageId: 'm' }),
    speak: (_reply, signal) => new Promise<void>(resolve => signal.addEventListener('abort', () => { canceled++; resolve() })),
    stopChat: () => { stops++ }, onState: () => {} })
  voice.start(); voice.event({ type: 'turn.start' }); voice.event({ type: 'transcription.text.delta', text: 'Hallo' }); voice.event({ type: 'turn.end' }); flush()
  await Promise.resolve(); voice.interrupt(); expect(canceled).toBe(1); expect(stops).toBe(0); expect(voice.acceptsAudio()).toBe(false)
  tail(); await Promise.resolve(); await Promise.resolve(); expect(resumeCount).toBe(1); expect(voice.acceptsAudio()).toBe(true)
  voice.stop()
})

test('explicit interruption during generation stops chat and discards late results', async () => {
  let flush!: () => void, tail!: () => void, deliver!: (value: { conversationId: string; messageId: string }) => void
  let stops = 0, spoken = 0
  const voice = createLiveVoice({ schedule: fn => { flush = fn; return () => {} }, scheduleTail: fn => { tail = fn; return () => {} }, suspendInput: () => {}, resumeInput: async () => {},
    send: () => new Promise(resolve => { deliver = resolve }), speak: async () => { spoken++ }, stopChat: () => { stops++ }, onState: () => {} })
  voice.start(); voice.event({ type: 'turn.start' }); voice.event({ type: 'transcription.text.delta', text: 'Hallo' }); voice.event({ type: 'turn.end' }); flush()
  voice.interrupt(); expect(stops).toBe(1); deliver({ conversationId: 'c', messageId: 'late' }); await Promise.resolve(); expect(spoken).toBe(0)
  tail(); await Promise.resolve(); await Promise.resolve(); expect(voice.acceptsAudio()).toBe(true); voice.stop()
})

test('stopping cancels the tail and discards late events', async () => {
  let flush!: () => void, tail!: () => void, tailCanceled = false, resumes = 0
  const voice = createLiveVoice({ schedule: fn => { flush = fn; return () => {} }, scheduleTail: fn => { tail = fn; return () => { tailCanceled = true } }, suspendInput: () => {}, resumeInput: async () => { resumes++ }, send: async () => ({ conversationId: 'c', messageId: 'm' }), speak: async () => {}, stopChat: () => {}, onState: () => {} })
  voice.start(); voice.event({ type: 'turn.start' }); voice.event({ type: 'transcription.text.delta', text: 'Hallo' }); voice.event({ type: 'turn.end' }); flush()
  await Promise.resolve(); await Promise.resolve(); voice.stop(); expect(tailCanceled).toBe(true); tail(); await Promise.resolve(); expect(resumes).toBe(0)
})

test('eager ends and duplicate ends do not submit partial or duplicate turns', () => {
  let scheduled = 0
  const voice = createLiveVoice({ schedule: () => { scheduled++; return () => {} }, scheduleTail: () => () => {}, suspendInput: () => {}, resumeInput: async () => {}, send: async () => ({ conversationId: 'c', messageId: 'm' }), speak: async () => {}, stopChat: () => {}, onState: () => {} })
  voice.start(); voice.event({ type: 'turn.start' }); voice.event({ type: 'transcription.text.delta', text: 'Noch nicht fertig' }); voice.event({ type: 'turn.eager_end' }); voice.event({ type: 'turn.resume' }); expect(scheduled).toBe(0)
  voice.event({ type: 'turn.end' }); voice.event({ type: 'turn.end' }); expect(scheduled).toBe(1); voice.stop()
})
