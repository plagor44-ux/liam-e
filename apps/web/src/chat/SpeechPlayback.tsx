import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { audioSpeech } from '../api/generated/sdk.gen'
import { errorCode, messageForCode, unwrap } from '../api/unwrap'
import { IconButton } from '../ds'
import { useI18n } from '../i18n/I18n'

type Phase = 'loading' | 'ready' | 'playing' | 'finished' | 'error'
type State = { messageId: string; phase: Phase; error?: string }
type Job = { messageId: string; controller: AbortController; audio?: HTMLAudioElement; url?: string }
type LoadSpeech = (messageId: string, signal: AbortSignal) => Promise<Blob>

export function useSpeechPlayback(conversationId: string, enabled: boolean, recording: boolean, load?: LoadSpeech) {
  const { texts, browserLocale } = useI18n()
  const [state, setState] = useState<State | null>(null)
  const active = useRef<Job | null>(null)

  const release = useCallback(() => {
    const job = active.current
    active.current = null
    if (!job) return
    job.controller.abort()
    if (job.audio) {
      job.audio.pause()
      job.audio.removeAttribute('src')
      job.audio.load()
    }
    if (job.url) URL.revokeObjectURL(job.url)
  }, [])

  const stop = useCallback(() => {
    release()
    setState(null)
  }, [release])

  useLayoutEffect(() => {
    setState(null)
    return release
  }, [conversationId, enabled, release])
  useLayoutEffect(() => {
    if (recording && active.current) stop()
  }, [recording, stop])

  const finish = (job: Job, phase: 'finished' | 'error', error?: string) => {
    if (active.current !== job) return
    release()
    setState({ messageId: job.messageId, phase, error })
  }

  const play = async (job: Job) => {
    if (active.current !== job || !job.audio) return
    try {
      await job.audio.play()
      if (active.current === job) setState({ messageId: job.messageId, phase: 'playing' })
    } catch (error) {
      if (active.current !== job) return
      // A browser may require another gesture after the download. Keep the bytes locally.
      if (error instanceof DOMException && error.name === 'NotAllowedError') {
        setState({ messageId: job.messageId, phase: 'ready' })
      } else finish(job, 'error', texts.audio.speechPlaybackError)
    }
  }

  const start = async (messageId: string) => {
    if (!enabled || recording || active.current?.messageId === messageId) return
    release()
    const job: Job = { messageId, controller: new AbortController() }
    active.current = job
    setState({ messageId, phase: 'loading' })
    try {
      const signal = job.controller.signal
      const blob = load ? await load(messageId, signal) : await unwrap(audioSpeech({
        body: { conversation_id: conversationId, message_id: messageId, locale: browserLocale },
        signal, parseAs: 'blob', throwOnError: true,
      }))
      if (active.current !== job) return
      if (!blob.size || blob.type !== 'audio/mpeg') {
        finish(job, 'error', texts.audio.speechPlaybackError)
        return
      }
      job.url = URL.createObjectURL(blob)
      job.audio = new Audio(job.url)
      job.audio.addEventListener('ended', () => finish(job, 'finished'), { once: true })
      job.audio.addEventListener('error', () => finish(job, 'error', texts.audio.speechPlaybackError), { once: true })
      await play(job)
    } catch (error) {
      finish(job, 'error', messageForCode(errorCode(error), texts) ?? texts.errors.audio_unavailable)
    }
  }

  return {
    state, stop, start,
    resume: () => { if (active.current && !recording) void play(active.current) },
  }
}

export function SpeechControls({ messageId, player, disabled, copy }: {
  messageId: string
  player: ReturnType<typeof useSpeechPlayback>
  disabled: boolean
  copy?: { speech: string; speechLoading: string; speechPlaying: string }
}) {
  const { texts } = useI18n()
  const notices = copy ?? texts.audio
  const state = player.state?.messageId === messageId ? player.state : null
  const playing = state?.phase === 'loading' || state?.phase === 'playing'
  const ready = state?.phase === 'ready'
  const status = !state ? '' : state.phase === 'error' ? state.error :
    state.phase === 'loading' ? notices.speechLoading :
      state.phase === 'playing' ? notices.speechPlaying :
        ready ? texts.audio.speechReady : texts.audio.speechFinished
  return <div className="le-audio-controls">
    <IconButton size="lg" icon={playing ? 'square' : ready ? 'play' : 'volume-2'}
      label={playing ? texts.audio.speechStop : ready ? texts.audio.speechPlay : notices.speech}
      disabled={disabled}
      onClick={playing ? player.stop : ready ? player.resume : () => { void player.start(messageId) }} />
    {ready && <IconButton size="lg" icon="square" label={texts.audio.speechStop} onClick={player.stop} />}
    <span className="le-audio-status" role="status">{status}</span>
  </div>
}
