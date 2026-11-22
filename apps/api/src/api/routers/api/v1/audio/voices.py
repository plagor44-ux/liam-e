"""Owner-only preset inventory and fixed-text, attested speech preview."""
import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import ClientDisconnect

from api.audio.capabilities import audio_capabilities
from api.audio.errors import NO_STORE, audio_error
from api.audio.provider import MistralAudio, get_audio_provider
from api.audio.recording import Slot, get_transcription_slot
from api.audio.requests import bounded_body
from api.audio.voices import SAMPLES, VoiceCatalogue, list_voices
from db.accessors import ui_settings

router = APIRouter()


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    locale: Literal['de', 'en']
    voice: str = Field(min_length=1, max_length=100)


@router.get('', operation_id='audio_voices_list')
async def inventory(response: Response) -> VoiceCatalogue:
    response.headers.update(NO_STORE)
    return await list_voices()


async def _preview_request(request: Request) -> PreviewRequest:
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
        raise audio_error('audio_invalid')
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in bounded_body(request, 1024):
                body.extend(chunk)
        return PreviewRequest.model_validate_json(body)
    except TimeoutError:
        raise audio_error('audio_timeout') from None
    except (ValidationError, ClientDisconnect):
        raise audio_error('audio_invalid') from None


@router.post('/preview', operation_id='audio_voice_preview', response_class=Response,
             openapi_extra={'requestBody': {'required': True, 'content': {'application/json': {'schema': PreviewRequest.model_json_schema()}}}},
             responses={200: {'content': {'audio/mpeg': {'schema': {'type': 'string', 'format': 'binary'}}}}})
async def preview(body: Annotated[PreviewRequest, Depends(_preview_request)], provider: Annotated[MistralAudio, Depends(get_audio_provider)],
                  slot: Annotated[Slot, Depends(get_transcription_slot)]) -> Response:
    capability = audio_capabilities().tts
    if capability.status != 'available' or capability.model is None or capability.app_limits is None:
        raise audio_error('audio_unavailable')
    catalogue = await list_voices()
    if not catalogue.verified:
        raise audio_error('audio_unavailable')
    if not any(voice.id == body.voice for voice in catalogue.voices):
        raise audio_error('audio_invalid')
    sample = SAMPLES[body.locale]
    async with slot() as acquired:
        if not acquired:
            raise audio_error('audio_rate_limited')
        pace = (await ui_settings.get_or_create()).speech_pace
        audio = await provider.synthesize(sample, model=capability.model, limits=capability.app_limits,
                                          locale=body.locale, pace=pace, voice=body.voice)
    return Response(audio, media_type='audio/mpeg', headers=NO_STORE)
