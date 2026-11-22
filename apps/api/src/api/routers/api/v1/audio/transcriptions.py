"""Transcribe a verified owner's bounded recording after their explicit upload."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from api.locales import resolve_locale

from api.audio.capabilities import audio_capabilities
from api.audio.errors import NO_STORE, audio_error
from api.audio.provider import MistralAudio, get_audio_provider
from api.audio.recording import RECORDING_REQUEST_BODY, Slot, get_transcription_slot, validated_recording

router = APIRouter()


class TranscriptionOut(BaseModel):
    request_id: UUID
    text: str
    model: str


@router.post(
    "", operation_id="audio_transcribe", summary="Transcribe a recording without submitting a chat turn",
    openapi_extra={"requestBody": RECORDING_REQUEST_BODY},
    responses={
        401: {"description": "Owner access could not be verified"},
        413: {"description": "Recording too large"},
        415: {"description": "Unsupported recording format"},
        422: {"description": "Invalid or empty recording"},
        429: {"description": "Transcription already in progress or provider rate limit"},
        503: {"description": "Transcription unavailable"},
        504: {"description": "Transcription timed out"},
    },
)
async def transcribe(
    request: Request,
    response: Response,
    provider: Annotated[MistralAudio, Depends(get_audio_provider)],
    slot: Annotated[Slot, Depends(get_transcription_slot)],
) -> TranscriptionOut:
    capability = audio_capabilities().stt
    if capability.status != "available" or capability.model is None:
        raise audio_error("audio_unavailable")
    try:
        async with slot() as acquired:
            if not acquired:
                raise audio_error("audio_rate_limited")
            async with validated_recording(request, capability.formats) as (upload, kind, request_id, language):
                audio = await upload.read()
            language = await resolve_locale(language)
            text, model = await provider.transcribe(audio, kind=kind, model=capability.model, language=language)
    except (SQLAlchemyError, OSError):
        raise audio_error("audio_unavailable") from None
    response.headers.update(NO_STORE)
    return TranscriptionOut(request_id=request_id, text=text, model=model)
