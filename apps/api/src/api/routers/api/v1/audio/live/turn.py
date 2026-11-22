"""An owner-bound live voice turn using the ordinary chat agent and context resolver."""
import asyncio
import logging
from contextlib import suppress

from db.accessors import chat_conversation
from fastapi import APIRouter, WebSocket
from pydantic import ValidationError

from api.audio.live_access import live_remaining
from api.audio.voice_turn import run_voice_turn
from api.mail.agent import run_turn_events
from api.mail.context_resolver import resolve_context
from api.routers.api.v1.chat.stream import ChatStreamRequest

router = APIRouter()
_LOG = logging.getLogger(__name__)


@router.websocket('')
async def voice_turn(socket: WebSocket) -> None:
    remaining = live_remaining(socket)
    if not remaining:
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        async with asyncio.timeout(min(600, remaining)):
            raw = await asyncio.wait_for(socket.receive_text(), timeout=5)
            if len(raw) > 100_000:
                await socket.close(code=1009)
                return
            body = ChatStreamRequest.model_validate_json(raw)
            if not body.voice_response or not body.conversation_id:
                await socket.close(code=1008)
                return
            if await chat_conversation.get_conversation(body.conversation_id) is None:
                await socket.send_json({'event': 'error', 'code': 'internal'})
                return
            resolved = await resolve_context(**body.model_dump(exclude={'locale', 'voice_response'}))
            events = run_turn_events(**body.model_dump(), resolved_context=resolved)
            await run_voice_turn(socket, events, resolved[1].id)
    except (ValidationError, ValueError):
        with suppress(Exception): await socket.close(code=1008)
    except Exception as error:
        _LOG.warning('live voice turn failed class=%s', type(error).__name__)
        with suppress(Exception): await socket.close(code=1011)
    finally:
        with suppress(Exception): await socket.close()
