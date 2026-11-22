import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { audioTranscribe } from '../api/generated/sdk.gen'
import type { AudioTranscribeData, TranscriptionCapabilityOut } from '../api/generated/types.gen'
import { messageForCode, errorCode, unwrap } from '../api/unwrap'
import { IconButton } from '../ds'
import type { IconName } from '../ds/Icon'
import { useI18n } from '../i18n/I18n'

type Phase = 'idle' | 'permission' | 'recording' | 'ready' | 'uploading'
type Capture = {
  stream?: MediaStream
  recorder?: MediaRecorder
  timer?: ReturnType<typeof setTimeout>
  chunks: Blob[]
  bytes: number
  file?: Blob
  upload?: AbortController
  sendWhenStopped: boolean
}

function stopMedia(capture: Capture) {
  clearTimeout(capture.timer)
  try {
    if (capture.recorder && capture.recorder.state !== 'inactive') capture.recorder.stop()
  } finally {
    capture.stream?.getTracks().forEach(track => track.stop())
    capture.stream = undefined
  }
}

export function supportedAudioFormat(capability?: Pick<TranscriptionCapabilityOut, 'status' | 'formats'>): string | undefined {
  if (capability?.status !== 'available' || !globalThis.isSecureContext ||
    typeof MediaRecorder === 'undefined' || !navigator.mediaDevices?.getUserMedia) return
  return capability.formats.find(format => MediaRecorder.isTypeSupported(format))
}

type SubmitRecording = (
  body: AudioTranscribeData['body'], signal: AbortSignal,
) => Promise<{ request_id: string; text: string }>

const transcribe: SubmitRecording = (body, signal) => unwrap(audioTranscribe({ body, signal, throwOnError: true }))

type Props = {
  format: string
  limits: TranscriptionCapabilityOut['app_limits']
  disabled: boolean
  onLock: (locked: boolean) => void
  onTranscript: (text: string) => void
  submit?: SubmitRecording
  copy?: { recording: string; uploading: string; transcribed: string }
}

export function AudioRecorder({ format, limits, disabled, onLock, onTranscript, submit = transcribe, copy }: Props) {
  const { texts, browserLocale } = useI18n()
  const notices = copy ?? texts.audio
  const [phase, setPhase] = useState<Phase>('idle')
  const [notice, setNotice] = useState('')
  const active = useRef<Capture | null>(null)

  const clear = useCallback(() => {
    const current = active.current
    if (!current) return
    active.current = null
    current.upload?.abort()
    stopMedia(current)
    current.chunks.length = 0
    current.file = undefined
    onLock(false)
  }, [onLock])

  const reset = useCallback(() => {
    clear()
    setPhase('idle')
  }, [clear])

  useLayoutEffect(() => clear, [clear])
  useEffect(() => {
    const onHidden = () => {
      if (!document.hidden) return
      const current = active.current
      if (!current) return
      if (!current.recorder) {
        reset()
      } else if (!current.upload) {
        current.sendWhenStopped = false
        stopMedia(current)
      }
    }
    document.addEventListener('visibilitychange', onHidden)
    return () => document.removeEventListener('visibilitychange', onHidden)
  }, [reset])

  const fail = (message: string) => {
    reset()
    setNotice(message)
  }

  const upload = async (capture: Capture) => {
    if (active.current !== capture || capture.upload || !capture.file) return
    const controller = new AbortController()
    capture.upload = controller
    setPhase('uploading')
    try {
      const requestId = crypto.randomUUID()
      const extension = format.startsWith('audio/mp4') ? 'mp4' : 'webm'
      const file = new File([capture.file], 'recording.' + extension, { type: capture.file.type })
      const result = await submit({ file, request_id: requestId, language: browserLocale }, controller.signal)
      if (active.current !== capture) return
      if (result.request_id !== requestId || !result.text.trim()) {
        fail(texts.errors.audio_empty)
        return
      }
      onTranscript(result.text)
      reset()
      setNotice(notices.transcribed)
    } catch (error) {
      if (active.current === capture) fail(messageForCode(errorCode(error), texts) ?? texts.errors.audio_unavailable)
    }
  }

  const start = async () => {
    if (disabled || active.current || document.hidden) return
    const capture: Capture = { chunks: [], bytes: 0, sendWhenStopped: false }
    active.current = capture
    onLock(true)
    setNotice('')
    setPhase('permission')
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (active.current !== capture || document.hidden) {
        stream.getTracks().forEach(track => track.stop())
        if (active.current === capture) reset()
        return
      }
      capture.stream = stream
      const recorder = new MediaRecorder(stream, { mimeType: format })
      capture.recorder = recorder
      recorder.addEventListener('dataavailable', event => {
        if (active.current !== capture || !event.data.size) return
        capture.bytes += event.data.size
        if (capture.bytes > limits.max_upload_bytes) {
          fail(texts.errors.audio_too_large)
          return
        }
        capture.chunks.push(event.data)
      })
      recorder.addEventListener('error', () => {
        if (active.current === capture) fail(texts.audio.microphoneError)
      }, { once: true })
      recorder.addEventListener('stop', () => {
        if (active.current !== capture) return
        stopMedia(capture)
        if (!capture.bytes) {
          fail(texts.errors.audio_empty)
          return
        }
        capture.file = new Blob(capture.chunks, { type: capture.chunks[0]?.type || recorder.mimeType })
        capture.chunks.length = 0
        if (capture.sendWhenStopped && !document.hidden) void upload(capture)
        else setPhase('ready')
      }, { once: true })
      recorder.start(250)
      // Leave room for container padding at the server's exact duration limit.
      capture.timer = setTimeout(() => stopMedia(capture), Math.max(0, limits.max_duration_seconds * 1000 - 1000))
      setPhase('recording')
    } catch {
      if (active.current === capture) fail(texts.audio.microphoneError)
    }
  }

  const sendRecording = () => {
    const capture = active.current
    if (!capture || document.hidden) return
    if (capture.file) void upload(capture)
    else {
      capture.sendWhenStopped = true
      stopMedia(capture)
    }
  }

  const discard = () => {
    reset()
    setNotice('')
  }
  let action: { icon: IconName; label: string; run: () => void } =
    { icon: 'mic', label: texts.audio.record, run: () => { void start() } }
  if (phase === 'permission' || phase === 'uploading') {
    action = { icon: 'x', label: texts.audio.cancel, run: discard }
  } else if (phase === 'recording' || phase === 'ready') {
    action = {
      icon: phase === 'recording' ? 'square' : 'arrow-up',
      label: phase === 'recording' ? texts.audio.stopAndUpload : texts.audio.upload,
      run: sendRecording,
    }
  }
  const status = phase === 'idle' ? notice :
    phase === 'recording' || phase === 'uploading' ? notices[phase] : texts.audio[phase]
  return <div className="le-audio-controls">
    <span className="le-audio-status" role="status">{status}</span>
    <IconButton size="lg" icon={action.icon} label={action.label} onClick={action.run}
      disabled={phase === 'idle' && disabled} />
    {(phase === 'recording' || phase === 'ready') && <IconButton size="lg" icon="trash-2"
      label={texts.audio.discard} onClick={discard} />}
  </div>
}
