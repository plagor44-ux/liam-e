"""Stream one agent turn as server-sent events."""
import asyncio
import logging
from typing import Literal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress

from db.accessors import chat_conversation
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, RootModel
from starlette.responses import JSONResponse, StreamingResponse
from mistralai_capabilities.fastapi.sse import event_stream_response, format_sse

from api.errors import quiet_validation_route, refuse
from api.locales import DEFAULT_LOCALE, Locale
from api.mail.agent import run_turn_events
from api.mail.context_resolver import resolve_context
from api.mail.chat_events import ChatEvent as ChatEventUnion, ErrorEvent
from api.mail.models_catalog import list_chat_models, require_available_model

logger = logging.getLogger(__name__)
router = APIRouter(route_class=quiet_validation_route('chat_invalid', 'Prüfe die Nachricht: 1–8.000 Zeichen.'))


class ChatEvent(RootModel[ChatEventUnion]):
    """A discriminated event in the streamed response."""


class ChatStreamRequest(BaseModel):
    conversation_id: str | None = None
    text: str = Field(min_length=1, max_length=8000)
    expected_mail_scope_version: int | None = Field(default=None, ge=1)
    account_id: str | None = None
    prompt_revision_id: str | None = None
    skill_id: str | None = Field(default=None, max_length=100)
    skill_revision_id: str | None = Field(default=None, max_length=100)
    without_preferences: bool = False
    voice_response: bool = Field(default=False, description="Request a concise spoken response for this turn")
    locale: Locale = Field(default=DEFAULT_LOCALE, description="Browser language when the saved setting follows the browser")
    context_message_id: str | None = None
    context_occurrence_id: str | None = None
    calendar_source_ids: list[str] | None = Field(default=None, description="Omit to include every currently readable calendar. An explicit list narrows this turn.")
    calendar_timezone: str = Field(default='Europe/Berlin', max_length=100)
    task_source: Literal['none', 'all', 'unlinked', 'account', 'account_and_unlinked'] = 'none'
    task_source_account_id: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, description='Model for a new conversation; ignored when conversation_id is set (use PATCH on the conversation).')


class EventStreamResponse(JSONResponse):
    media_type = 'text/event-stream'


async def sse_frames(events: AsyncIterator[ChatEventUnion], is_disconnected: Callable[[], Awaitable[bool]], *, ping_seconds: float = 15.0) -> AsyncIterator[str]:
    pending = None
    try:
        while True:
            if await is_disconnected():
                break
            if pending is None:
                pending = asyncio.ensure_future(anext(events))
            done, _ = await asyncio.wait({pending}, timeout=ping_seconds)
            if not done:
                yield ': ping\n\n'
                continue
            completed, pending = pending, None
            try:
                event = completed.result()
            except StopAsyncIteration:
                break
            yield format_sse(data=event.model_dump(mode='json'), event=event.event)
            if event.event in ('done', 'error'):
                break
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except Exception as error:
        logger.warning('%s', type(error).__name__)
        yield format_sse(data=ErrorEvent(code='internal').model_dump(mode='json'), event='error')
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pending
        await events.aclose()


@router.post('', operation_id='chat_stream', response_class=EventStreamResponse, response_model=ChatEvent, description='Streams one agent turn. It never sends mail itself; proposals require explicit confirmation.')
async def chat_stream(body: ChatStreamRequest, request: Request) -> StreamingResponse:
    if body.conversation_id is not None and await chat_conversation.get_conversation(body.conversation_id) is None:
        raise refuse(404, 'conversation_not_found', 'Diese Unterhaltung gibt es nicht mehr. Starte eine neue Unterhaltung.')
    if body.conversation_id is None and body.model is not None:
        require_available_model(body.model, await list_chat_models())
    resolved = await resolve_context(**body.model_dump(exclude={'locale', 'voice_response'}))
    events = run_turn_events(**body.model_dump(), resolved_context=resolved)
    return event_stream_response(sse_frames(events, request.is_disconnected))
