# Audio and live voice

Part of the agent instructions: [AGENTS.md](../../AGENTS.md) links here from its index, and its rules apply here too.

`audio_capabilities_get` and the paid `audio_transcribe` and `audio_speech` operations stay browser-only. The audio subtree verifies the Studio gateway's
caller with the Mistral Users API against the API module's `AUDIO_STUDIO_OWNER` binding.
Workspace keys and missing identity are denied. Audio POSTs also require the configured
browser origin. STT and TTS are gated independently
by matching release attestations. STT validates the container, codec and duration before
contacting Mistral. A transient PostgreSQL lock limits transcription to one active request
per app database. The browser appends a successful transcript to the current draft.
Speech takes a conversation and message reference, reads a visible and complete assistant
reply from local history. The effective language selects the saved preset, or `voice_481` for
German and `voice_478` for English by default; the revision-guarded pace selects a provider-only
tag, never stored in history.
Voice settings are revision guarded per language, with `null` selecting the existing default. The
owner-only preset catalogue is cached for ten minutes (failures for one minute), excludes custom voices, and falls back
to the default when the selected preset cannot be verified. A paid owner-and-origin-bound preview
speaks only a fixed localized sample, using the existing TTS attestation, bounds and one-at-a-time
database lock. Both operations are browser-only. Live turns select the preset of the resolved
language; its fixed progress phrase and reply use the same voice.
Speech attestations can set lower limits within 500 characters, 1,000,000 MP3 bytes and
20 seconds. All audio responses use `Cache-Control: no-store`.
The owner-only `audio_device_test_get`, `audio_device_test_record`, `audio_device_test_inference`
and `audio_device_test_sample` operations expire independently and remain browser-only.
Live voice is a separate test-only, owner-and-origin-bound path. Its status and speech POSTs
remain browser-only, and the transcription WebSocket is not part of OpenAPI or the agent
facade. `AUDIO_LIVE_TEST_ENABLED`, `AUDIO_LIVE_TEST_ORIGIN`, and `AUDIO_LIVE_TEST_UNTIL`
must be set explicitly; the last is an offset-aware future time at most 72 hours away.
Manual speech accepts only a saved, authorized assistant-message reference. Live voice turns use
an owner- and origin-bound WebSocket running the same chat agent and resolved conversation context.
Its speech provider receives fixed localized progress phrases and visible assistant deltas generated
by that turn, grouped into sentences, with an early first phrase at a safe punctuation or word boundary.
At most one fixed progress phrase precedes reply audio; a timer phrase is suppressed after the first
answer token. Email addresses and URLs with a scheme, www prefix or path are removed from spoken
reply text without changing history; bare hostnames can be sender names and remain. The speech session
opens alongside the model stream and closes on completion, failure, interruption or revocation;
browser-supplied speech text is never accepted. STT receives the effective locale before
audio; the speech voice and pace are fixed for each turn. Content-free timings contain no text or
identifiers; only recognized public model IDs enter timing logs. Scope and the live window are rechecked
during synthesis and while playback is outstanding. The socket stays open until playback finishes
or the turn is stopped, disconnected or revoked. A stopped assistant fragment is not fed back into
the model for a subsequent voice turn. Live transport and provider behavior require Studio verification.
`ui_settings.voice_model` is revision guarded and defaults to `chat`. A separately selected model
must be available and support chat tools; if it later becomes unavailable or cannot be verified,
voice turns use the conversation's model. Text turns keep the conversation model. Voice turns use
`none` reasoning for verified Small 4 and Medium 3.5, `low` for Large 4, and omit the parameter
for other or unresolved models. When setting the parameter, the resolved model ID is pinned.
The device check's recording and sample routes validate WebM and return a fixed test draft or
bundled sample without calling an inference service. Only the explicit, paid
`audio_device_test_inference` smoke calls Mistral: one at a time under the transcription lock, it
sends the bundled synthetic WebM/Opus recording and the fixed sample sentence with the app's
inference credential. It returns outcome codes, models, durations and sizes, never text or audio,
and never sets release attestations.
See the [audio probe](../superpowers/probes/2026-11-19-audio-capabilities.md).
