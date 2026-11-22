"""Stream speech for a saved, authorized assistant response."""
import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from api.audio.errors import NO_STORE, audio_error
from api.audio.live_access import live_remaining
from api.audio.live_provider import mistral_live
from api.routers.api.v1.audio.speech import SpeechReference, _speech_reference, _visible_reply

router = APIRouter()
MAX_SPEECH_BYTES = 24_000_000  # float32 PCM: same duration budget as 12 MB of 16-bit PCM


@router.post(
    '', operation_id='audio_live_speech', response_class=StreamingResponse,
    summary='Stream PCM speech for a saved assistant reply during the test window',
    openapi_extra={'requestBody': {'required': True, 'content': {
        'application/json': {'schema': SpeechReference.model_json_schema()},
    }}},
    responses={200: {'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}},
)
async def synthesize(request: Request) -> StreamingResponse:
    remaining = live_remaining(request)
    if not remaining:
        raise audio_error('audio_unavailable')
    reference = await _speech_reference(request)
    try:
        text = await _visible_reply(reference)
    except (SQLAlchemyError, OSError):
        raise audio_error('audio_unavailable') from None
    if len(text) > 8000:
        raise audio_error('audio_text_too_long')

    async def frames() -> AsyncIterator[bytes]:
        total = 0
        try:
            async with asyncio.timeout(min(60, remaining)), mistral_live('speech') as session:
                await session.append(text)
                await session.finish()
                # StreamingResponse owns disconnect detection and cancels this iterator.
                while True:
                    chunk = await session.receive()
                    if chunk is None:
                        return
                    total += len(chunk)
                    if total > MAX_SPEECH_BYTES:
                        raise RuntimeError('audio_unavailable')
                    yield chunk
        except Exception:
            # Abort the response without exposing provider error text.
            raise RuntimeError('audio_unavailable') from None

    return StreamingResponse(frames(), media_type='application/octet-stream', headers=NO_STORE)
