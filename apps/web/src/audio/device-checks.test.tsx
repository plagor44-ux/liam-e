// i18n-exempt: synthetic test fixtures, refusal codes and model IDs.
import { afterEach, expect, mock, spyOn, test } from 'bun:test'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as sdk from '../api/generated/sdk.gen'
import type { DeviceInferenceOut } from '../api/generated/types.gen'
import { I18nProvider } from '../i18n/I18n'
import { en } from '../i18n/en'
import { de } from '../i18n/de'
import { answerKind, appCode, checkUploadLimit, FALLBACK_SIZES, uploadEmpty, type UploadAnswer } from './device-checks'
import { InferenceCheck, UploadLimitCheck } from './DeviceChecks'

const LIMIT = 10 * 1024 * 1024
const copy = en.audio.deviceTest

afterEach(() => {
  cleanup()
  mock.restore()
})

/** A fake gateway: `passes` is the largest body it forwards to the app. */
function gateway(passes: number, appAnswer?: (bytes: number) => string) {
  const sizes: number[] = []
  const upload = async (bytes: number): Promise<UploadAnswer> => {
    sizes.push(bytes)
    if (bytes > passes) return { bytes, status: 413, code: null }
    return { bytes, status: bytes > LIMIT ? 413 : 415, code: appAnswer?.(bytes) ?? (bytes > LIMIT ? 'audio_too_large' : 'audio_format_unsupported') }
  }
  return { upload, sizes }
}

test('only liam-e’s own refusals count as an app answer', () => {
  expect(appCode({ detail: { code: 'audio_too_large', message: 'x' } })).toBe('audio_too_large')
  expect(appCode({ detail: 'audio_device_test_closed' })).toBe('audio_device_test_closed')
  expect(appCode({ detail: 'unauthorized' })).toBe('unauthorized')
  for (const body of ['<html>413 Request Entity Too Large</html>', { detail: 'session_expired' },
    { error: { code: 'audio_too_large' } }, { detail: { code: 'payload_too_large' } }, null, undefined, new TypeError('x')]) {
    expect(appCode(body)).toBeNull()
  }
})

test('only the code a size must produce proves the body reached the app', () => {
  expect(answerKind({ bytes: LIMIT, status: 415, code: 'audio_format_unsupported' }, LIMIT)).toBe('received')
  expect(answerKind({ bytes: LIMIT + 1, status: 413, code: 'audio_too_large' }, LIMIT)).toBe('received')
  // A 413 from the app at the limit would mean the app limit is wrong, not that the body arrived.
  expect(answerKind({ bytes: LIMIT, status: 413, code: 'audio_too_large' }, LIMIT)).toBe('app')
  expect(answerKind({ bytes: LIMIT, status: 429, code: 'audio_rate_limited' }, LIMIT)).toBe('app')
  expect(answerKind({ bytes: LIMIT, status: 413, code: null }, LIMIT)).toBe('stopped')
  expect(answerKind({ bytes: LIMIT, status: 0, code: null }, LIMIT)).toBe('network')
})

test('a gateway that forwards the app limit needs only the two boundary uploads', async () => {
  const { upload, sizes } = gateway(Infinity)
  const result = await checkUploadLimit(LIMIT, upload)
  expect(result).toMatchObject({ verdict: 'passes', provenBytes: null })
  expect(sizes).toEqual([LIMIT, LIMIT + 1])
})

test('a gateway that stops the limit is narrowed with smaller uploads until one arrives', async () => {
  const { upload, sizes } = gateway(5 * 1024 * 1024)
  const result = await checkUploadLimit(LIMIT, upload)
  expect(result).toMatchObject({ verdict: 'limited', provenBytes: 4 * 1024 * 1024 })
  expect(sizes).toEqual([LIMIT, LIMIT + 1, 8 * 1024 * 1024, 4 * 1024 * 1024])
})

test('a gateway that stops every size reports no proven bound', async () => {
  const { upload, sizes } = gateway(0)
  expect(await checkUploadLimit(LIMIT, upload)).toMatchObject({ verdict: 'limited', provenBytes: null })
  expect(sizes).toEqual([LIMIT, LIMIT + 1, ...FALLBACK_SIZES])
})

test('an early app answer is unclear and starts no further uploads', async () => {
  const { upload, sizes } = gateway(Infinity, () => 'audio_rate_limited')
  expect(await checkUploadLimit(LIMIT, upload)).toMatchObject({ verdict: 'unclear', provenBytes: null })
  expect(sizes).toEqual([LIMIT, LIMIT + 1])
})

test('uploadEmpty sends zero bytes declared as WebM/Opus and reads the answer', async () => {
  const record = spyOn(sdk, 'audioDeviceTestRecord').mockImplementation((async () => ({
    error: { detail: { code: 'audio_format_unsupported', message: 'x' } },
    response: new Response(null, { status: 415 }),
  })) as never)
  expect(await uploadEmpty(64)).toEqual({ bytes: 64, status: 415, code: 'audio_format_unsupported' })
  const body = (record.mock.calls[0]?.[0] as { body: { file: Blob; request_id: string } }).body
  expect(body.file.size).toBe(64)
  expect(body.file.type).toBe('audio/webm;codecs=opus')
  expect(new Uint8Array(await body.file.arrayBuffer()).every(byte => byte === 0)).toBe(true)
  expect(body.request_id).toMatch(/^[0-9a-f-]{36}$/)

  record.mockImplementation((async () => { throw new TypeError('connection reset') }) as never)
  expect(await uploadEmpty(64)).toEqual({ bytes: 64, status: 0, code: null })

  const aborted = new AbortController()
  aborted.abort()
  record.mockImplementation((async () => { throw new DOMException('aborted', 'AbortError') }) as never)
  await expect(uploadEmpty(64, aborted.signal)).rejects.toThrow('aborted')
})

test('the upload check lists each answer and its verdict, locking the page meanwhile', async () => {
  spyOn(sdk, 'audioDeviceTestRecord').mockImplementation((async ({ body }: { body: { file: Blob } }) => {
    const bytes = body.file.size
    if (bytes > 3 * 1024 * 1024) return { error: '<html>Request Entity Too Large</html>', response: new Response(null, { status: 413 }) }
    return { error: { detail: { code: 'audio_format_unsupported', message: 'x' } }, response: new Response(null, { status: 415 }) }
  }) as never)
  const busy: boolean[] = []
  render(<I18nProvider locale="en"><UploadLimitCheck limit={LIMIT} disabled={false} onBusy={value => busy.push(value)} /></I18nProvider>)
  fireEvent.click(screen.getByRole('button', { name: copy.uploadLimitRun }))
  await screen.findByText(copy.uploadLimited(copy.bytes('2,097,152')))
  expect(screen.getByText(copy.uploadStopped(copy.bytes('10,485,760'), '413'))).toBeTruthy()
  expect(screen.getByText(copy.uploadReceived(copy.bytes('2,097,152'), 'audio_format_unsupported'))).toBeTruthy()
  expect(busy).toEqual([true, false])
})

const passed: DeviceInferenceOut = {
  checked_at: '2026-11-25T10:00:00Z',
  stt: { outcome: 'passed', code: null, model: 'voxtral-mini-transcribe-3', elapsed_ms: 812, reported_model: 'voxtral-mini-transcribe-3-0', matches_sample: true },
  tts: { outcome: 'passed', code: null, model: 'voxtral-mini-tts-3-0', elapsed_ms: 1204, audio_bytes: 19121 },
}

test('the model check runs only on request and shows models, durations and sizes', async () => {
  const inference = spyOn(sdk, 'audioDeviceTestInference').mockImplementation((async () => ({ data: passed })) as never)
  render(<I18nProvider locale="en"><InferenceCheck disabled={false} onBusy={() => {}} /></I18nProvider>)
  expect(inference).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: copy.inferenceRun }))
  await screen.findByText(copy.ttsPassed('voxtral-mini-tts-3-0', copy.milliseconds('1,204'), copy.bytes('19,121')))
  expect(screen.getByText(new RegExp(copy.sttMatches))).toBeTruthy()
  expect(inference).toHaveBeenCalledTimes(1)
})

test('failed capabilities show their code and cause in German', async () => {
  spyOn(sdk, 'audioDeviceTestInference').mockImplementation((async () => ({ data: {
    ...passed,
    stt: { outcome: 'failed', code: 'recording_rejected', model: 'voxtral-mini-transcribe-3', elapsed_ms: 0, reported_model: null, matches_sample: null },
    tts: { outcome: 'failed', code: 'audio_timeout', model: 'voxtral-mini-tts-3-0', elapsed_ms: 20000, audio_bytes: null },
  } })) as never)
  const texts = de.audio.deviceTest
  render(<I18nProvider locale="de"><InferenceCheck disabled={false} onBusy={() => {}} /></I18nProvider>)
  fireEvent.click(screen.getByRole('button', { name: texts.inferenceRun }))
  await screen.findByText(`${texts.sttFailed('recording_rejected')} ${texts.reasons.recording_rejected}`)
  expect(screen.getByText(`${texts.ttsFailed('audio_timeout')} ${de.errors.audio_timeout}`)).toBeTruthy()
})

test('a refused model check request shows the server’s reason and unlocks again', async () => {
  spyOn(sdk, 'audioDeviceTestInference').mockImplementation((async () => {
    throw { detail: { code: 'audio_rate_limited', message: 'x' } }
  }) as never)
  const busy: boolean[] = []
  render(<I18nProvider locale="en"><InferenceCheck disabled={false} onBusy={value => busy.push(value)} /></I18nProvider>)
  fireEvent.click(screen.getByRole('button', { name: copy.inferenceRun }))
  await screen.findByText(en.errors.audio_rate_limited)
  await waitFor(() => expect(busy).toEqual([true, false]))
  expect((screen.getByRole('button', { name: copy.inferenceRun }) as HTMLButtonElement).disabled).toBe(false)
})
