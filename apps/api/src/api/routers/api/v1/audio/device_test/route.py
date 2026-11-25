"""The temporary owner device check validates recordings, serves a bundled speech sample and
runs an explicit synthetic inference smoke.

Recording and playback never call an audio model. Only the inference smoke does: it sends the
bundled synthetic recording to speech recognition and the fixed sample sentence to speech
synthesis with the app's own inference credential, exactly as the regular routes would. It
returns fixed outcome codes, models, durations and sizes, never transcript text or audio, and it
never changes the release attestations or any chat history.
"""

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from env.audio import env
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from api.audio import recording
from api.audio.capabilities import MODEL_PATTERN, TRANSCRIPTION_APP_LIMITS, SpeechLimitsOut, TranscriptionLimitsOut
from api.audio.containers import check_recording
from api.audio.errors import NO_STORE, audio_error
from api.audio.provider import MistralAudio, get_audio_provider

_WEBM: Literal["audio/webm;codecs=opus"] = "audio/webm;codecs=opus"
_SAMPLE_TEXT = "Eine gelbe Ente schwimmt im Teich."
_SAMPLE_FILE = Path(recording.__file__).with_name("device-sample.mp3")
# The same eSpeak NG utterance as the MP3, transcoded once to WebM/Opus with bit-exact FFmpeg flags.
_SAMPLE_RECORDING = _SAMPLE_FILE.with_suffix(".webm")
# The largest speech limits a release attestation may carry (see capabilities._SpeechVerification).
_SMOKE_SPEECH_LIMITS = SpeechLimitsOut(max_input_characters=500, max_response_bytes=1_000_000, timeout_seconds=20)


def active_device_test() -> datetime:
    try:
        expires = datetime.fromisoformat(env.audio_device_test_until or "")
        now = datetime.now(timezone.utc)
        if expires.utcoffset() is not None and now < expires <= now + timedelta(days=3):
            return expires
    except ValueError:
        pass
    raise HTTPException(404, detail="audio_device_test_closed", headers=NO_STORE)


router = APIRouter(dependencies=[Depends(active_device_test)])


class DeviceTestOut(BaseModel):
    expires_at: datetime
    sample_text: str
    format: Literal["audio/webm;codecs=opus"]
    app_limits: TranscriptionLimitsOut


class DeviceRecordingOut(BaseModel):
    request_id: UUID
    text: str


class InferenceCheckOut(BaseModel):
    outcome: Literal["passed", "failed"]
    # A fixed refusal code (`audio_*`), `invalid_model_configuration` or `recording_rejected`.
    code: str | None = None
    # The configured model ID, omitted when it is not a valid Voxtral ID.
    model: str | None
    # Duration of the provider call; 0 when the check stopped before calling the provider.
    elapsed_ms: int


class TranscriptionCheckOut(InferenceCheckOut):
    reported_model: str | None = None
    # Whether the normalized transcript equals the sample sentence. The text itself is not returned.
    matches_sample: bool | None = None


class SpeechCheckOut(InferenceCheckOut):
    audio_bytes: int | None = None


class DeviceInferenceOut(BaseModel):
    checked_at: datetime
    stt: TranscriptionCheckOut
    tts: SpeechCheckOut


def _normalized(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _refusal_code(exc: HTTPException) -> str:
    detail = exc.detail
    return detail["code"] if isinstance(detail, dict) and isinstance(detail.get("code"), str) else "audio_unavailable"


def _valid_model(model: str) -> bool:
    return re.fullmatch(MODEL_PATTERN, model) is not None


async def _check_transcription(provider: MistralAudio) -> TranscriptionCheckOut:
    model = env.audio_stt_model
    if not _valid_model(model):
        return TranscriptionCheckOut(outcome="failed", code="invalid_model_configuration", model=None, elapsed_ms=0)
    with _SAMPLE_RECORDING.open("rb") as fixture:
        # The production validator, so the deployed image's FFmpeg tools are part of the smoke.
        report = await run_in_threadpool(check_recording, "webm", fixture)
        fixture.seek(0)
        audio = fixture.read()
    if report["status"] != "accepted":
        return TranscriptionCheckOut(outcome="failed", code="recording_rejected", model=model, elapsed_ms=0)
    # Durations cover the provider call only.
    started = time.monotonic()
    try:
        text, reported_model = await provider.transcribe(audio, kind="webm", model=model, language="de")
    except HTTPException as exc:
        return TranscriptionCheckOut(
            outcome="failed", code=_refusal_code(exc), model=model, elapsed_ms=_elapsed_ms(started),
        )
    return TranscriptionCheckOut(
        outcome="passed", model=model, elapsed_ms=_elapsed_ms(started), reported_model=reported_model,
        matches_sample=_normalized(text) == _normalized(_SAMPLE_TEXT),
    )


async def _check_speech(provider: MistralAudio) -> SpeechCheckOut:
    model = env.audio_tts_model
    if not _valid_model(model):
        return SpeechCheckOut(outcome="failed", code="invalid_model_configuration", model=None, elapsed_ms=0)
    started = time.monotonic()
    try:
        audio = await provider.synthesize(_SAMPLE_TEXT, model=model, limits=_SMOKE_SPEECH_LIMITS, locale="de")
    except HTTPException as exc:
        return SpeechCheckOut(outcome="failed", code=_refusal_code(exc), model=model, elapsed_ms=_elapsed_ms(started))
    return SpeechCheckOut(outcome="passed", model=model, elapsed_ms=_elapsed_ms(started), audio_bytes=len(audio))


@router.get("", operation_id="audio_device_test_get")
async def status(response: Response, expires_at: Annotated[datetime, Depends(active_device_test)]) -> DeviceTestOut:
    response.headers.update(NO_STORE)
    return DeviceTestOut(expires_at=expires_at, sample_text=_SAMPLE_TEXT, format=_WEBM, app_limits=TRANSCRIPTION_APP_LIMITS)


@router.post(
    "/recording", operation_id="audio_device_test_record",
    openapi_extra={"requestBody": recording.RECORDING_REQUEST_BODY},
)
async def validate(
    request: Request, response: Response,
    slot: Annotated[recording.Slot, Depends(recording.get_transcription_slot)],
) -> DeviceRecordingOut:
    try:
        async with slot() as acquired:
            if not acquired:
                raise audio_error("audio_rate_limited")
            async with recording.validated_recording(request, (_WEBM,)) as (_, _, request_id, _):
                active_device_test()
                response.headers.update(NO_STORE)
                return DeviceRecordingOut(request_id=request_id, text=_SAMPLE_TEXT)
    except (SQLAlchemyError, OSError):
        raise audio_error("audio_unavailable") from None


@router.post(
    "/inference", operation_id="audio_device_test_inference",
    summary="Run the synthetic speech recognition and speech synthesis smoke once",
    responses={
        401: {"description": "Owner access or browser origin could not be verified"},
        404: {"description": "The device check is closed"},
        429: {"description": "Another audio request holds the transcription slot"},
        503: {"description": "The transcription slot is unavailable"},
    },
)
async def inference(
    response: Response,
    provider: Annotated[MistralAudio, Depends(get_audio_provider)],
    slot: Annotated[recording.Slot, Depends(recording.get_transcription_slot)],
) -> DeviceInferenceOut:
    try:
        async with slot() as acquired:
            if not acquired:
                raise audio_error("audio_rate_limited")
            # One after the other, so each duration measures one provider call. A failure of one
            # capability is reported and never stops the other.
            stt = await _check_transcription(provider)
            tts = await _check_speech(provider)
    except (SQLAlchemyError, OSError):
        raise audio_error("audio_unavailable") from None
    response.headers.update(NO_STORE)
    return DeviceInferenceOut(checked_at=datetime.now(timezone.utc), stt=stt, tts=tts)


@router.get(
    "/sample", operation_id="audio_device_test_sample", response_class=Response,
    responses={200: {"content": {"audio/mpeg": {"schema": {"type": "string", "format": "binary"}}}}},
)
def sample() -> Response:
    return Response(_SAMPLE_FILE.read_bytes(), media_type="audio/mpeg", headers=NO_STORE)
