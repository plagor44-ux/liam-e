# Audio capability probe on 2026-11-19 CET

[#26](https://github.com/plagor44-ux/liam-e/issues/26) ran against the model configuration for
main at 3388f19. The installed SDK was mistralai 3.7.0. The probe used HTTPX 0.28.1 and the app's
local workspace credential resolver. Each call sent a single short German synthetic sample to
the official provider API. The isolated process kept the credential in memory. Samples and
temporary output stayed in local scratch space except for the direct inference requests.

| Capability and method | UTC time on 18 Nov | Result |
| --- | --- | --- |
| STT, multipart PCM WAV, `POST /v1/audio/transcriptions` with `voxtral-mini-transcribe-3` and `language=de` | 23:08:41 | HTTP 200 in 454 ms. 88,812 B, 2.013 s. Response named `voxtral-mini-transcribe-3` and the normalized transcript matched. |
| STT, Chromium WebM/Opus, same endpoint and model | 23:11:41 | HTTP 200 in 456 ms. 36,038 B, 2.220 s of decoded audio. Same returned model and transcript match. |
| STT, locally encoded fragmented MP4/AAC, same endpoint and model | 23:12:03 | HTTP 200 in 342 ms. 18,464 B, 2.090 s of decoded audio. Same returned model and transcript match. |
| TTS, JSON `POST /v1/audio/speech` with `voxtral-mini-tts-3-0`, German preset `voice_481` and `response_format=mp3` | 23:08:08 | HTTP 200 in 607 ms. The decoded response had 18,576 B of MP3, mono at 22,050 Hz. The response did not identify the resolved model revision. |

The STT response reported the requested model identifier without a separate version suffix.
The SDK has methods for both calls. This run verifies the documented HTTP contracts with the
workspace credential. The measured TTS sample rate was 22,050 Hz, while the general provider
format docs describe 24 kHz.
[File transcription](https://docs.mistral.ai/studio/audio/speech-to-text/transcribe-files),
[speech generation](https://docs.mistral.ai/studio/audio/text-to-speech/generate-speech),
[preset voices](https://docs.mistral.ai/studio/audio/text-to-speech/voices),
[audio formats](https://docs.mistral.ai/studio/audio/specs-and-limits/audio-formats).

## Browser and container evidence

| Check | Evidence |
| --- | --- |
| Chromium 157.0.8103.38 on Linux, Playwright 1.62.0 | Real browser muxer fed by the eSpeak WAV through a fake microphone. One 48 kHz mono track used a programmatic permission grant. Both a default recorder and an explicit `audio/webm;codecs=opus` recorder wrote actual WebM/Opus. This Chromium build rejected the explicit AAC MIME declaration. Closing the probe stopped the fake track. |
| Local Chromium permission and embedding | Loopback was a secure context. The top-level page reported `prompt`, the cross-origin frame without microphone delegation reported `denied`, and the delegated frame reported `prompt`. Real prompts, devices and Studio framing remain open. |
| Chromium MP3 playback | An actual button click with active user activation played the provider MP3 to the `ended` event in headless Chromium with volume set to zero. FFprobe measured a 2.480 s format duration, while the browser exposed 2.691 s. |
| iOS Safari | **BLOCKED** without an iOS device. WebKit documents MP4/AAC in the original recorder implementation and WebM/Opus from Safari 18.4. Verify actual recorder bytes, permissions and playback inside the intended Studio frame on the supported iOS versions. The local MP4 fixture is codec evidence only. |

Microphone access requires a secure context and user permission. A cross-origin frame also
depends on both the embedding policy and document policy.
[Media Capture and Streams](https://www.w3.org/TR/mediacapture-streams/),
[WebKit MediaRecorder](https://webkit.org/blog/11353/),
[WebKit Safari 18.4](https://webkit.org/blog/16574/webkit-features-in-safari-18-4/).

### Candidate duration check

FFmpeg/FFprobe 9.0.2 ran only as offline command-line tools on synthetic local fixtures. The
probe checked the file byte count before invoking a demuxer, checked the WebM EBML DocType or
MP4 `ftyp` header, required exactly one audio stream with the declared container's codec,
and compared decoded sample count with container duration when present. For the decoded count
it resampled to 48 kHz mono signed 16-bit PCM, regenerated timestamps from sample counts and
stopped at 121 seconds. Each tool ran with a three-second subprocess timeout, one decode thread,
64 MiB per-allocation limit and only the local file protocol. PCM and parser stderr stayed local.

- Actual Chromium WebM/Opus and synthetic fragmented MP4/AAC passed. Cross-declared WebM as
  MP4, a 12-byte truncated header and a 10,485,761-byte file failed.
- Both 121-second fixtures failed. Both 119.5-second fixtures passed. The exact 120-second
  WebM fixture was rejected conservatively when its metadata reported 120.008 seconds although
  the decoded sample count measured 120.000 seconds.
- At this stage #27 still needed a bounded production validator with coverage for actual browser
  files, malformed structures, timestamp gaps, codec padding and timeout. The image used for this
  initial check had no audio validator.

The follow-up local check adds `--validate-container` to the probe. It checks the declared
container and codec, packet timestamp span, container duration when present and independently
decoded sample count. It uses FFmpeg and FFprobe with seekable descriptor input, a 10 MiB file
limit, a 50,000-packet limit, a three-second timeout per tool and a 64 MiB limit per allocation.
Results contain only measurements and fixed reason codes.
[FFmpeg descriptor protocol](https://ffmpeg.org/ffmpeg-protocols.html#fd).

| Local follow-up fixture with FFmpeg and FFprobe 9.0.2 | Result |
| --- | --- |
| Chromium 157.0.8103.38 on Linux, default recorder and explicit WebM/Opus, Playwright 1.62.0 with synthetic microphone input | Both accepted. Each decoded duration was 2.220 s. Both runs stopped all microphone tracks. |
| Chromium 157.0.8103.38 on Linux, Playwright 1.61.0, automated probe with Chromium's synthetic microphone | Default and explicit WebM/Opus each produced 16,516 bytes and 1.500 s of decoded audio. Both passed the validator and stopped the microphone track. Explicit MP4/AAC was reported as unsupported. |
| 119.5 s and 121 s of synthetic audio in WebM/Opus and fragmented MP4/AAC | Both formats accepted the shorter sample and rejected the longer sample. |
| WebM without aggregate duration metadata and a packet span of 122.258 s | Rejected based on packet timestamps. |
| WebM with 121 s of samples and a packet span below 120 s | Rejected based on decoded sample count. |
| Exact-duration WebM with 120.008 s in container metadata, wrong container or codec, truncation in the header or media stream, oversized fixture and subprocess timeout | Rejected with fixed reasons. |

## Release decision

Both chat audio capabilities remain disabled on the Studio deployment at `017a89f`.
The owner accepted the effective Mistral data handling for the selected models on
25 November 2026 (see [Mistral data retention](#mistral-data-retention)). Personal or
mail-derived content still waits for the remaining checks below.

| Capability | Remaining Studio evidence |
| --- | --- |
| STT (#27) | Record and upload WebM/Opus on the owner's target device. Check that the deployed gateway carries the intended upload limit and that a synthetic transcribe request succeeds with the deployed API module's credential. List MP4/AAC only after validation in the deployed runtime and a real Safari recording test. |
| TTS (#28) | Check synthesis from the deployed API module, the request and response limits, and playback on the owner's target device. |

The existing Studio owner session opened the device check without a new connector consent.
A fresh session may encounter Studio's app consent. Independently, the audio routes call
only `GET /v1/users/me` to verify the Studio caller and the two audio endpoints at
`https://api.mistral.ai` using the app's inference credential. Inference authorization is
independent of connector grants. STT returns text to the composer for editing. Sending it
explicitly starts a regular chat turn with its existing scopes. TTS reads an eligible reply from
app history, strips Markdown and sends its speech text to Mistral.

To verify inference from production while the release gates are closed, use an expiring,
owner-only smoke with a fixed bundled recording and a fixed sample text. Exercise the real
production validator, app credential and provider adapter with those fixtures. Keep the
regular chat routes gated until the applicable checks and data-handling decision are recorded.
The device check's [model check](#synthetic-model-check) is that smoke, and its
[upload limit check](#upload-limit-at-the-gateway) measures the gateway bound. Both are
described below with the order of the remaining owner steps.

## Runtime contract

`GET /api/v1/audio/capabilities` is browser-only and `Cache-Control: no-store`. It verifies the
caller and then reads release attestations locally. Inference runs only on the audio POSTs.
Verified callers receive independent `stt` and `tts` objects. Both are `unknown` until their
own current model and evidence match.

Set `AUDIO_STUDIO_OWNER` on the API module for the intended deployment. The JSON object contains
`user_id`, `organization_id`, `workspace_id` and the exact HTTPS browser `origin` without a
trailing slash. The server uses the request-scoped Mistral Apps gateway token for the identity
lookup at `GET /v1/users/me`. It matches all three IDs and requires an explicit null `api_key`
field in the authenticated identity response. Workspace API keys, missing or ambiguous identity,
and audio POSTs from another origin return 401. Values belong in the deployment configuration.
[Mistral Users API](https://docs.mistral.ai/api/endpoint/beta/users).

`AUDIO_STT_MODEL` and `AUDIO_TTS_MODEL` default to the candidates above. The corresponding
`AUDIO_STT_VERIFICATION` and `AUDIO_TTS_VERIFICATION` are optional JSON objects with a matching
`model`, timezone-aware `checked_at`, `outcome` and optional `resolved_model`. A `failed` outcome
reports `unavailable`. Availability requires `outcome: "verified"`, explicit
`scope: "studio_release"`, one or more checked `formats` and `app_limits`. The release scope
records the owner's review of the deployed Studio path for that capability, including the trusted
owner check, runtime credential, gateway bounds, target devices and effective workspace data
handling. A successful local provider check can use `scope: "provider"` and leaves the
capability `unknown`. Missing or mismatched evidence also leaves it `unknown`.

The STT attestation must explicitly match the app's limits of `max_upload_bytes: 10485760`,
`max_duration_seconds: 120` and `max_concurrent_requests: 1`. Each listed input format needs
proof that the deployed path enforces size, container and duration. TTS requires positive
`max_input_characters`, `max_response_bytes` and `timeout_seconds` that the deployed path enforces.
Both attestations remain unset. When a deployment, credential, model, format, limit or
data-handling term changes, clear the affected attestation until its release checks have run again.

## Production runtime follow-up

- The existing signed-in Chromium session reached the production gateway on the same origin.
  The probe initiated no new connector consent. A fresh session and the corresponding
  embedded-flow permissions still need separate evidence.
- The documented Users API resolved the deployment workspace key to the same owner and tenant
  and reported an API-key context. That credential is excluded by the app's verifier.
- The production-target API image installs Debian FFmpeg and FFprobe 7.1.5, both with the
  seekable descriptor protocol. As the image's non-root runtime user, the validator accepted
  synthetic WebM/Opus at 1.5 and 119.5 seconds and rejected 121 seconds. FFprobe 7.1.5 omitted
  the first AAC packet's duration on the local fragmented MP4 fixture, so the validator
  rejected it conservatively. Keep fragmented MP4 out of the Studio formats pending a
  compatible, validated runtime.

## Mistral data retention

On 19 November 2026, the Admin organization ID matched the ID in the production deployment
manifest. **Admin → Vibe → Datenschutz** showed no active Zero Data Retention (ZDR) entry.
The API training opt-in and Labs toggle were off in the separate API privacy settings.
The Vibe chat expiry control applies to Vibe chats. Mistral's standard API terms describe
processing followed by 30 rolling days of retention of inputs and outputs for abuse
monitoring, subject to exceptions and any applicable organization agreement. Use that standard
as the release assumption until Mistral confirms a different effective policy for the app's
inference credential. Keep verification calls synthetic.

Owner decision (25 November 2026): the release accepts this standard API retention for audio
requests, as for the chat text the app already sends to Mistral. ZDR is not a release
condition. Revisit the decision if the models, the credential or Mistral's terms change.
[Mistral Privacy Policy](https://legal.mistral.ai/terms/privacy-policy/),
[Admin privacy settings](https://admin.mistral.ai/vibe/privacy).

Mistral covers `/v1/audio/transcriptions` and `/v1/audio/speech` under approved organization
ZDR for eligible stateless requests. It excludes Public Preview and Labs models, even under an
approved organization setting. `voxtral-mini-transcribe-3` aliases `voxtral-mini-transcribe-3-0`
and the configured `voxtral-mini-tts-3-0` uses the same Voxtral 3 generation. Both are Public
Preview. If the release requires ZDR, obtain organization activation and confirm coverage for
the two selected model IDs before attesting. The Audio API's policy is separate from the app's
Postgres chat history and from any Studio Observability traces.
[Mistral zero data retention](https://docs.mistral.ai/admin/monitor-comply/zero-data-retention),
[Voxtral model mapping](https://docs.mistral.ai/studio/audio/migrate-from-voxtral-2),
[Transcribe 3 model card](https://docs.mistral.ai/models/voxtral-mini-transcribe-3-0),
[TTS 3 model card](https://docs.mistral.ai/models/voxtral-mini-tts-3-0).

## App routes with live inference

On 19 November 2026, the production-target API image ran the real audio routes, PostgreSQL
lock and reply lookup against a disposable loopback database. The app used the workspace
inference credential and sent only the bundled synthetic utterance and the same fixed text.
The test supplied a loopback owner fixture and in-process release attestations. Studio gateway
behavior and the deployed module's credential still require the production smoke.

| App route and model | Result |
| --- | --- |
| WebM/Opus STT with `voxtral-mini-transcribe-3` | HTTP 200. The normalized draft matched the synthetic source. |
| TTS with `voxtral-mini-tts-3-0` and `voice_481` | HTTP 200. FFmpeg decoded 19,121 bytes of MP3 to 2.717 seconds. |
| WebM/Opus STT with `voxtral-mini-2602` | HTTP 200. The normalized draft matched the synthetic source. |
| TTS with `voxtral-mini-tts-2603` and `voice_481` | App HTTP 503. Mistral lists this German voice as a Voxtral 3 addition. |
| TTS with `voxtral-mini-tts-2603` and `gb_jane_neutral` in the local process | HTTP 200. FFmpeg decoded 15,947 bytes of MP3 to 2.220 seconds. Acceptability of this preset for German playback remains untested. |

The routes denied a missing owner, missing verification and a stopped assistant reply.
Successful responses had `Cache-Control: no-store`, and the chat message count stayed at two
throughout. A release with the legacy models would require checking model support, retention
coverage and the selected voice.
[Mistral preset voice compatibility](https://docs.mistral.ai/studio/audio/voice-agents/configuration#voxtral-tts-2-voices).

A follow-up Playwright run used Chromium 158.0.8127.18 and the same API image with the configured
Voxtral 3 models. The browser's recorder received the bundled synthetic utterance through a
fake microphone. After the explicit upload, the provider transcript appeared in the editable
composer alongside existing text. A separate manual send reached the synthetic chat fixture.
Reading that fixture's saved assistant reply used Mistral's live MP3, which Chromium played
through the `ended` event. Playwright restricted the browser to the loopback app and API.

## Temporary owner device check

Open `/audio-device-test` in an existing Studio session. The API module setting
`AUDIO_DEVICE_TEST_UNTIL` takes an ISO 8601 timestamp with a timezone and enables the check
through that time for at most three days ahead. Each request requires the Studio owner binding.
The sample, upload and availability endpoints return 404 to the owner after expiry. Clear the
setting to close the check earlier.

The page uses the chat recorder's actual WebM/Opus output and the same multipart upload limits
and FFmpeg checks as transcription. The API releases the temporary file and returns a fixed
editable test draft. Playback uses a bundled German MP3 generated offline with eSpeak NG
and FFmpeg. Recording and playback use no audio model, chat turn or connector. The whole
check, including the two sections below, leaves the independent STT and TTS release
attestations in effect.

### Upload limit at the gateway

“Check the upload limit” uploads two zero-filled files declared as WebM/Opus to the device
check's recording route: exactly the app's `max_upload_bytes` (10,485,760 bytes) and one byte
more. The app rejects both before any model is involved. Only the code each size must produce
proves that the whole body reached the app: 415 `audio_format_unsupported` at the limit, because
the container check runs after the full upload, and 413 `audio_too_large` above it. Any other
app code, for example a busy transcription slot, makes the result unclear. An answer without an
app code, or a dropped connection, means the gateway or the connection stopped the upload. The
page then tries 8, 4, 2 and 1 MiB and reports the largest size that arrived in full. If the
proven bound is below the app limit, lower `MAX_BYTES` in `apps/api/src/api/audio/containers.py`
to it before attesting, because the attestation must match `TRANSCRIPTION_APP_LIMITS` exactly.
The normal case uploads about 20 MB.

### Synthetic model check

“Run the model check” calls `POST /api/v1/audio/device-test/inference`
(`audio_device_test_inference`). Like every audio POST it requires the Studio owner and the
configured browser origin. It closes with the device check and holds the transcription lock, so
a second run waits (429). The route validates the bundled synthetic recording
`apps/api/src/api/audio/device-sample.webm` with the production validator. It sends that file
to `/v1/audio/transcriptions` with `AUDIO_STT_MODEL` and the language `de`, and the fixed sample
sentence to `/v1/audio/speech` with `AUDIO_TTS_MODEL` and the default German voice. Speech uses
the largest limits an attestation may carry: 500 characters, 1,000,000 bytes and 20 seconds.
Both calls use the deployed API module's own inference credential, one after the other, and a
failure of one does not stop the other.

For each capability the response names the outcome, a fixed code on failure, the configured
model and the duration of the provider call. STT adds the reported model and whether the
normalized transcript equals the sample sentence; TTS adds the MP3 size. The response contains
no transcript text and no audio, nothing is stored, and the attestations stay unchanged. Each
run is a paid provider call. The tests replace the provider, so CI never calls Mistral.

The WebM fixture is the bundled MP3's eSpeak NG utterance, transcoded once:

```bash
ffmpeg -v error -nostdin -i apps/api/src/api/audio/device-sample.mp3 -map 0:a:0 -ac 1 -ar 48000 \
  -c:a libopus -b:a 32k -fflags +bitexact -flags:a +bitexact -map_metadata -1 -f webm \
  apps/api/src/api/audio/device-sample.webm
```

With FFmpeg n9.1, two runs produced the same 9,449-byte file. The production validator accepted
it with 2.064 seconds of decoded audio.

### Remaining owner steps before attesting

1. Set `AUDIO_DEVICE_TEST_UNTIL` on the deployed API module, at most three days ahead.
2. Open `/audio-device-test` in the Studio session on the desktop and on the iPhone (Safari 18.4
   or later). Record and upload, check the microphone prompt inside the Studio frame, and play
   the sample.
3. Run “Check the upload limit” once and record the verdict and each line.
4. Run “Run the model check” once and record both results with `checked_at`.
5. Open the app in a fresh Studio session and record whether an app consent appears.
6. Write `AUDIO_STT_VERIFICATION` and `AUDIO_TTS_VERIFICATION` with `scope: studio_release`, the
   checked format `audio/webm;codecs=opus`, the configured model and, for STT, the reported model
   as `resolved_model`. STT carries the exact app limits; TTS carries the model check's speech
   limits or lower ones. Clear `AUDIO_DEVICE_TEST_UNTIL`.
7. Confirm that `GET /api/v1/audio/capabilities` reports both as `available`, then run one
   regular transcription and one reply read aloud with synthetic speech.

## Repeat the Chromium recording offline

[The browser probe](../../../tools/audio_browser_probe.cjs) exercises Chromium's default recorder
and the supported explicit formats from the audio capability contract. Playwright grants microphone
access in a disposable context using Chromium's synthetic microphone. The container probe checks
the bytes from the real browser muxer. The output is one JSON line per recorder option with versions,
MIME type, track state and validation results. The script deletes the temporary clips after the run.

Install Playwright in a scratch directory or provide an existing installation on `NODE_PATH`.
Use FFmpeg and FFprobe with the descriptor protocol, as in the container check.

```bash
playwright_dir=$(mktemp -d)
npm install --prefix "$playwright_dir" --no-save --no-package-lock playwright@1.61.0
"$playwright_dir/node_modules/.bin/playwright" install chromium
NODE_PATH="$playwright_dir/node_modules" node tools/audio_browser_probe.cjs
```

You can pass an installed Chromium executable as the only argument. An unsupported explicit format
is reported separately. The script exits with status 1 if recording or validation fails or if a
microphone track remains active. This loopback run uses a synthetic input and a programmatic permission
grant. The Studio embedding and real-device checks remain required.

## Reproduce with synthetic data

Create a private temporary directory and use eSpeak NG 1.52.0 to generate the German fixture.
Run the provider probe from the checkout with existing authorized app configuration. Set
`probe_script` to its absolute path if that checkout is separate. Enable `--live` only for this
synthetic data. Every inference call uses the same German utterance.

```bash
scratch=$(mktemp -d)
probe_script=tools/audio_provider_probe.py
espeak-ng -v de -w "$scratch/synthetic.wav" 'Eine gelbe Ente schwimmt im Teich.'
bash tools/uv.sh run --no-sync python "$probe_script" tts "$scratch" --live
bash tools/uv.sh run --no-sync python "$probe_script" wav "$scratch" --live

ffmpeg -v error -i "$scratch/synthetic.wav" -map 0:a:0 -c:a aac \
  -movflags frag_keyframe+empty_moov+default_base_moof "$scratch/recorded.mp4"
bash tools/uv.sh run --no-sync python "$probe_script" mp4 "$scratch" --live
```

The probe script is [audio_provider_probe.py](../../../tools/audio_provider_probe.py). For the
browser pass, launch an isolated Playwright Chromium with `--use-fake-device-for-media-stream`
and `--use-file-for-fake-audio-capture=<scratch>/synthetic.wav`. Serve an empty test page on
loopback, grant its microphone permission in the browser context and call
`getUserMedia({audio:true})`. Record for 2,270 ms, wait for the final data and stop events, then
write the resulting blob as `recorded.webm`. Repeat with explicit WebM/Opus, MP4/AAC and the
default recorder option. Check the top frame and cross-origin test frames with and without
`allow="microphone"` using `isSecureContext`, the document feature policy and the permission
query. Serve the returned `output.mp3` from that origin, play it from a button handler, and
observe the `ended` event. Stop all tracks and close the browser and local servers.

```bash
bash tools/uv.sh run --no-sync python "$probe_script" webm "$scratch" --live
ffprobe -v error -show_entries \
  format=format_name,duration,size:stream=codec_name,sample_rate,channels \
  -of json "$scratch/recorded.webm"
ffprobe -v error -show_entries \
  format=format_name,duration,size:stream=codec_name,sample_rate,channels \
  -of json "$scratch/output.mp3"
```

Check the two fixture files locally with either command. An accepted file exits with 0 and a
rejected file exits with 1. The option runs without provider credentials.

```bash
bash tools/uv.sh run --no-sync python "$probe_script" webm "$scratch" --validate-container
bash tools/uv.sh run --no-sync python "$probe_script" mp4 "$scratch" --validate-container
bash tools/uv.sh run --no-sync pytest -q tests/test_audio_container_probe.py
```

The test suite generates the synthetic boundary fixtures. Close the local processes and delete
the scratch directory after inspecting the results.

## Repository checks

- `bash tools/uv.sh run --no-sync pytest -q apps/api/tests tests` passed 1,172 tests, including the owner boundary, separate capability states, and publication hygiene.
- `bun run check` passed build, type checks, and generated-client drift checks. `bun run test -- --output-style=static` passed the three project test targets. The database target skipped 205 PostgreSQL cases.
- The API suite emitted a pre-existing `Connection._cancel` coroutine warning, which also appeared on the unchanged baseline.
- The follow-up review added offline regression tests for provider-only evidence, exact STT limits, and redaction of invalid probe arguments.
- The container follow-up ran `bash tools/uv.sh run --no-sync pytest tests apps/api/tests packages/py -q`
  with 1,279 passed and 211 skipped. `bun run check` and `bunx nx run web:test` passed.
