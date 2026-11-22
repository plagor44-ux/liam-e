"""Synthesize only an eligible assistant reply from the application's own history."""

import asyncio
from typing import Annotated

from db.accessors import chat_context, chat_conversation, chat_message, ui_settings
from api.locales import Locale, resolve_locale
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import ClientDisconnect

from api.audio.capabilities import audio_capabilities
from api.audio.errors import NO_STORE, audio_error
from api.audio.provider import MistralAudio, get_audio_provider
from api.audio.requests import bounded_body
from api.audio.speech_text import speech_text
from api.mail.agent import visible_messages

router = APIRouter()
_MAX_REFERENCE_BYTES = 2048
_MAX_MARKDOWN_CHARACTERS = 16_384


class SpeechReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)
    locale: Locale | None = None


async def _speech_reference(request: Request) -> SpeechReference:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise audio_error("audio_invalid")
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in bounded_body(request, _MAX_REFERENCE_BYTES):
                body.extend(chunk)
        return SpeechReference.model_validate_json(body)
    except TimeoutError:
        raise audio_error("audio_timeout") from None
    except (ValidationError, ClientDisconnect):
        raise audio_error("audio_invalid") from None


async def _visible_reply(reference: SpeechReference) -> str:
    # This database is scoped to one app owner. Bind the selected row to its conversation and
    # apply the same tool-message visibility rule as the history route.
    conversation = await chat_conversation.get_conversation(reference.conversation_id)
    message = await chat_message.get_message(reference.message_id)
    if (
        conversation is None or message is None or message.conversation_id != conversation.id
        or message.role != "assistant" or message.stopped or not message.content.strip()
        or not message.context_id or not visible_messages([message])
    ):
        raise audio_error("audio_message_unavailable")

    context = await chat_context.get_context(message.context_id)
    if context is None or context.conversation_id != conversation.id or context.revoked or not await chat_context.is_live(context.id):
        raise audio_error("audio_message_unavailable")

    if len(message.content) > _MAX_MARKDOWN_CHARACTERS:
        raise audio_error("audio_text_too_long")
    text = speech_text(message.content)
    if not text:
        raise audio_error("audio_message_unavailable")
    return text


@router.post(
    "", operation_id="audio_speech", summary="Read aloud a saved, complete assistant reply",
    response_class=Response,
    openapi_extra={"requestBody": {"required": True, "content": {
        "application/json": {"schema": SpeechReference.model_json_schema()},
    }}},
    responses={
        200: {"content": {"audio/mpeg": {"schema": {"type": "string", "format": "binary"}}}},
        401: {"description": "Owner access could not be verified"},
        404: {"description": "The reply is not available for speech"},
        413: {"description": "Speech reference too large"},
        422: {"description": "Invalid speech reference or reply text too long"},
        429: {"description": "Provider rate limit"},
        503: {"description": "Speech unavailable"},
        504: {"description": "Speech timed out"},
    },
)
async def synthesize(
    request: Request, provider: Annotated[MistralAudio, Depends(get_audio_provider)],
) -> Response:
    capability = audio_capabilities().tts
    limits = capability.app_limits
    if capability.status != "available" or capability.model is None or limits is None:
        raise audio_error("audio_unavailable")
    reference = await _speech_reference(request)
    try:
        text = await _visible_reply(reference)
    except (SQLAlchemyError, OSError):
        raise audio_error("audio_unavailable") from None
    if len(text) > limits.max_input_characters:
        raise audio_error("audio_text_too_long")

    locale = await resolve_locale(reference.locale)
    pace = (await ui_settings.get_or_create()).speech_pace
    audio = await provider.synthesize(text, model=capability.model, limits=limits, locale=locale, pace=pace)
    return Response(audio, media_type="audio/mpeg", headers=NO_STORE)
