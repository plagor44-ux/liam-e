import { useEffect, useRef, useState } from 'react'
import type { DeviceInferenceOut } from '../api/generated/types.gen'
import { apiErrorMessage, messageForCode } from '../api/unwrap'
import { Button, Notice } from '../ds'
import { useI18n } from '../i18n/I18n'
import { answerKind, checkUploadLimit, runInference, type UploadAnswer, type UploadLimitResult } from './device-checks'

type Run<T> = { status: 'idle' } | { status: 'running' } | { status: 'done'; result: T } | { status: 'error'; message: string }
type Props = { disabled: boolean; onBusy: (busy: boolean) => void }

/** One run at a time; leaving the page aborts the run and drops its result. */
function useRun<T>(task: (signal: AbortSignal) => Promise<T>, onBusy: (busy: boolean) => void) {
  const { texts } = useI18n()
  const [state, setState] = useState<Run<T>>({ status: 'idle' })
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])
  const start = async () => {
    const current = new AbortController()
    controller.current = current
    setState({ status: 'running' })
    onBusy(true)
    try {
      const result = await task(current.signal)
      if (!current.signal.aborted) setState({ status: 'done', result })
    } catch (error) {
      if (!current.signal.aborted) setState({ status: 'error', message: apiErrorMessage(error, texts) })
    } finally {
      if (!current.signal.aborted) onBusy(false)
    }
  }
  return [state, start] as const
}

function useNumbers() {
  const { texts, localeTag } = useI18n()
  const copy = texts.audio.deviceTest
  const numbers = new Intl.NumberFormat(localeTag)
  return {
    size: (bytes: number) => copy.bytes(numbers.format(bytes)),
    duration: (ms: number) => copy.milliseconds(numbers.format(ms)),
  }
}

export function UploadLimitCheck({ limit, disabled, onBusy }: Props & { limit: number }) {
  const { texts } = useI18n()
  const copy = texts.audio.deviceTest
  const { size } = useNumbers()
  const [state, start] = useRun(signal => checkUploadLimit(limit, undefined, signal), onBusy)
  const line = (answer: UploadAnswer) => {
    const label = size(answer.bytes)
    switch (answerKind(answer, limit)) {
      case 'received': return copy.uploadReceived(label, answer.code ?? '')
      case 'app': return copy.uploadApp(label, answer.code ?? '')
      case 'stopped': return copy.uploadStopped(label, String(answer.status))
      case 'network': return copy.uploadNoAnswer(label)
    }
  }
  const verdict = (result: UploadLimitResult) => {
    if (result.verdict === 'passes') return <Notice tone="success">{copy.uploadPasses}</Notice>
    if (result.verdict === 'unclear') return <Notice tone="info">{copy.uploadUnclear}</Notice>
    const smallest = result.answers.at(-1)?.bytes ?? limit
    return <Notice tone="danger">
      {result.provenBytes === null ? copy.uploadLimitedNone(size(smallest)) : copy.uploadLimited(size(result.provenBytes))}
    </Notice>
  }

  return <section className="grid gap-3" aria-labelledby="device-upload-limit">
    <h2 className="text-lg font-semibold" id="device-upload-limit">{copy.uploadLimit}</h2>
    <p>{copy.uploadLimitIntro(size(limit))}</p>
    <Button variant="secondary" className="justify-self-start" disabled={disabled || state.status === 'running'}
      onClick={() => void start()}>{copy.uploadLimitRun}</Button>
    <div className="grid gap-2" aria-live="polite">
      {state.status === 'running' && <p role="status">{copy.uploadLimitRunning}</p>}
      {state.status === 'error' && <Notice tone="danger">{state.message}</Notice>}
      {state.status === 'done' && <>
        <ul className="grid gap-1">{state.result.answers.map(answer => <li key={answer.bytes}>{line(answer)}</li>)}</ul>
        {verdict(state.result)}
      </>}
    </div>
  </section>
}

export function InferenceCheck({ disabled, onBusy }: Props) {
  const { texts, localeTag } = useI18n()
  const copy = texts.audio.deviceTest
  const { size, duration } = useNumbers()
  const [state, start] = useRun(runInference, onBusy)
  const reason = (code: string | null | undefined) => {
    if (!code) return ''
    return Object.hasOwn(copy.reasons, code)
      ? copy.reasons[code as keyof typeof copy.reasons]
      : messageForCode(code, texts) ?? ''
  }
  const results = ({ stt, tts, checked_at }: DeviceInferenceOut) => <>
    {stt.outcome === 'passed'
      ? <Notice tone={stt.matches_sample ? 'success' : 'info'}>
          {copy.sttPassed(stt.model ?? '', stt.reported_model ?? '', duration(stt.elapsed_ms))}{' '}
          {stt.matches_sample ? copy.sttMatches : copy.sttDiffers}
        </Notice>
      : <Notice tone="danger">{copy.sttFailed(stt.code ?? '')} {reason(stt.code)}</Notice>}
    {tts.outcome === 'passed'
      ? <Notice tone="success">{copy.ttsPassed(tts.model ?? '', duration(tts.elapsed_ms), size(tts.audio_bytes ?? 0))}</Notice>
      : <Notice tone="danger">{copy.ttsFailed(tts.code ?? '')} {reason(tts.code)}</Notice>}
    <p>{copy.checkedAt(new Intl.DateTimeFormat(localeTag, { dateStyle: 'medium', timeStyle: 'medium' }).format(new Date(checked_at)))}</p>
  </>

  return <section className="grid gap-3" aria-labelledby="device-inference">
    <h2 className="text-lg font-semibold" id="device-inference">{copy.inference}</h2>
    <p>{copy.inferenceIntro}</p>
    <Button variant="secondary" className="justify-self-start" disabled={disabled || state.status === 'running'}
      onClick={() => void start()}>{copy.inferenceRun}</Button>
    <div className="grid gap-2" aria-live="polite">
      {state.status === 'running' && <p role="status">{copy.inferenceRunning}</p>}
      {state.status === 'error' && <Notice tone="danger">{state.message}</Notice>}
      {state.status === 'done' && results(state.result)}
    </div>
  </section>
}
