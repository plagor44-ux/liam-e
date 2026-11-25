// i18n-exempt: wire formats, refusal codes and byte sizes only; the copy lives in the catalogues.
import { audioDeviceTestInference, audioDeviceTestRecord } from '../api/generated/sdk.gen'
import type { DeviceInferenceOut } from '../api/generated/types.gen'
import { unwrap } from '../api/unwrap'

const WEBM = 'audio/webm;codecs=opus'
const MIB = 1024 * 1024
/** Tried from the largest down when the gateway stops an upload at the app's own limit. */
export const FALLBACK_SIZES = [8 * MIB, 4 * MIB, 2 * MIB, MIB]
// The app's own refusals that are not structured `{ code }` details.
const APP_DETAILS = new Set(['audio_device_test_closed', 'unauthorized'])

/** One upload's answer: `code` is set only when liam-e itself answered; status 0 means no HTTP answer. */
export type UploadAnswer = { bytes: number; status: number; code: string | null }
export type Upload = (bytes: number, signal?: AbortSignal) => Promise<UploadAnswer>
/** How one answer reads: the body reached the app in full, the app answered early, or something stopped it. */
export type AnswerKind = 'received' | 'app' | 'stopped' | 'network'
export type UploadLimitResult = {
  limit: number
  answers: UploadAnswer[]
  verdict: 'passes' | 'limited' | 'unclear'
  /** For `limited`: the largest upload that reached liam-e in full, if any did. */
  provenBytes: number | null
}

export function appCode(error: unknown): string | null {
  const detail = (error as { detail?: unknown } | null | undefined)?.detail
  if (typeof detail === 'string') return APP_DETAILS.has(detail) ? detail : null
  const code = (detail as { code?: unknown } | null | undefined)?.code
  return typeof code === 'string' && /^audio_[a-z_]+$/.test(code) ? code : null
}

/**
 * The code that proves liam-e read the whole body: the empty file fails the container check at
 * or below the limit, and the size check above it. Any other app answer can come before the body.
 */
export const expectedCode = (bytes: number, limit: number) =>
  bytes > limit ? 'audio_too_large' : 'audio_format_unsupported'

export function answerKind(answer: UploadAnswer, limit: number): AnswerKind {
  if (answer.code === expectedCode(answer.bytes, limit)) return 'received'
  if (answer.code !== null) return 'app'
  return answer.status === 0 ? 'network' : 'stopped'
}

/** Uploads `bytes` zero bytes declared as WebM/Opus. liam-e rejects them before any model call. */
export const uploadEmpty: Upload = async (bytes, signal) => {
  const file = new Blob([new Uint8Array(bytes)], { type: WEBM })
  try {
    const { error, response } = await audioDeviceTestRecord({ body: { file, request_id: crypto.randomUUID() }, signal })
    return { bytes, status: response?.status ?? 0, code: response?.ok ? null : appCode(error) }
  } catch (error) {
    if (signal?.aborted) throw error
    return { bytes, status: 0, code: null }
  }
}

/** Sends the limit and one byte more, then smaller sizes only if the gateway stopped the first. */
export async function checkUploadLimit(limit: number, upload: Upload = uploadEmpty, signal?: AbortSignal): Promise<UploadLimitResult> {
  const atLimit = await upload(limit, signal)
  const answers = [atLimit, await upload(limit + 1, signal)]
  const first = answerKind(atLimit, limit)
  if (first === 'received') return { limit, answers, verdict: 'passes', provenBytes: null }
  if (first === 'app') return { limit, answers, verdict: 'unclear', provenBytes: null }
  for (const bytes of FALLBACK_SIZES.filter(size => size < limit)) {
    const answer = await upload(bytes, signal)
    answers.push(answer)
    const kind = answerKind(answer, limit)
    if (kind === 'received') return { limit, answers, verdict: 'limited', provenBytes: bytes }
    if (kind === 'app') return { limit, answers, verdict: 'unclear', provenBytes: null }
  }
  return { limit, answers, verdict: 'limited', provenBytes: null }
}

export const runInference = (signal?: AbortSignal): Promise<DeviceInferenceOut> =>
  unwrap(audioDeviceTestInference({ signal, throwOnError: true }))
