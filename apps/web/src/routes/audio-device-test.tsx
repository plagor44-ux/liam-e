import { useQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { useCallback, useEffect, useState } from 'react'
import { audioDeviceTestGet, audioDeviceTestRecord, audioDeviceTestSample } from '../api/generated/sdk.gen'
import type { AudioDeviceTestRecordData, DeviceTestOut } from '../api/generated/types.gen'
import { unwrap } from '../api/unwrap'
import { InferenceCheck, UploadLimitCheck } from '../audio/DeviceChecks'
import { AudioRecorder, supportedAudioFormat } from '../chat/AudioRecorder'
import { SpeechControls, useSpeechPlayback } from '../chat/SpeechPlayback'
import { Notice, TextField } from '../ds'
import { I18nProvider, useI18n } from '../i18n/I18n'
import { FALLBACK_LOCALE, isLocale } from '../i18n/locales'

const submit = (body: AudioDeviceTestRecordData['body'], signal: AbortSignal) =>
  unwrap(audioDeviceTestRecord({ body, signal, throwOnError: true }))
const sample = (_: string, signal: AbortSignal) =>
  unwrap(audioDeviceTestSample({ signal, parseAs: 'blob', throwOnError: true }))

function Check({ config }: { config: DeviceTestOut }) {
  const { texts, localeTag } = useI18n()
  const copy = texts.audio.deviceTest
  const [draft, setDraft] = useState('')
  const [recording, setRecording] = useState(false)
  // The recorder and both checks share the transcription slot, so only one runs at a time.
  const [checking, setChecking] = useState(false)
  const format = supportedAudioFormat({ status: 'available', formats: [config.format] })
  const speech = useSpeechPlayback('device-test', true, recording, sample)
  const lock = useCallback((active: boolean) => {
    if (active) speech.stop()
    setRecording(active)
  }, [speech.stop])
  const until = new Intl.DateTimeFormat(localeTag, { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(config.expires_at))

  return <>
    <p>{copy.intro}</p>
    <Notice tone="info">{copy.until(until)}</Notice>
    <section className="grid gap-3" aria-labelledby="device-capture">
      <h2 className="text-lg font-semibold" id="device-capture">{copy.capture}</h2>
      {format
        ? <AudioRecorder format={format} limits={config.app_limits} disabled={checking} onLock={lock}
            onTranscript={text => setDraft(previous => previous.trim() ? previous + '\n\n' + text : text)}
            submit={submit} copy={copy} />
        : <Notice tone="info">{copy.webmUnsupported}</Notice>}
      <TextField multiline label={copy.draft} rows={3} value={draft}
        onChange={event => setDraft(event.currentTarget.value)} />
    </section>
    <section className="grid gap-3" aria-labelledby="device-playback">
      <h2 className="text-lg font-semibold" id="device-playback">{copy.playback}</h2>
      <p lang="de">{config.sample_text}</p>
      <SpeechControls messageId="sample" player={speech} disabled={recording} copy={copy} />
    </section>
    <UploadLimitCheck limit={config.app_limits.max_upload_bytes} disabled={recording || checking} onBusy={setChecking} />
    <InferenceCheck disabled={recording || checking} onBusy={setChecking} />
  </>
}

function DeviceTest() {
  const { texts } = useI18n()
  const { data: config, isPending, isError } = useQuery({
    queryKey: ['audio', 'device-test'],
    queryFn: ({ signal }) => unwrap(audioDeviceTestGet({ signal, throwOnError: true })),
    enabled: typeof window !== 'undefined',
    retry: false,
  })
  const expiresAt = config?.expires_at
  const [closedAt, setClosedAt] = useState<string>()
  useEffect(() => {
    if (!expiresAt) return
    const timer = setTimeout(() => setClosedAt(expiresAt), Math.max(0, Date.parse(expiresAt) - Date.now()))
    return () => clearTimeout(timer)
  }, [expiresAt])
  const active = config && !isError && closedAt !== expiresAt && Date.parse(config.expires_at) > Date.now()
  const copy = texts.audio.deviceTest
  return <main className="mx-auto grid max-w-xl gap-6 px-5 py-8">
    <h1 className="text-2xl font-semibold">{copy.title}</h1>
    {active ? <Check config={config} /> : <p role="status">{isPending ? copy.loading : copy.closed}</p>}
  </main>
}

function DeviceTestPage() {
  const { locale } = Route.useRouteContext()
  return <I18nProvider locale={isLocale(locale) ? locale : FALLBACK_LOCALE}><DeviceTest /></I18nProvider>
}

export const Route = createFileRoute('/audio-device-test')({
  component: DeviceTestPage,
  staticData: { landing: false },
})
