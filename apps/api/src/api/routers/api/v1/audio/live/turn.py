"""An owner-bound live voice turn using the ordinary chat agent and context resolver."""
import asyncio
import logging
import time
from contextlib import suppress

from db.accessors import chat_conversation
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from api.audio.live_access import live_remaining
from api.audio.voice_turn import run_voice_turn
from api.audio.metrics import VoiceMetrics
from api.mail.agent import run_turn_events
from api.mail.context_resolver import resolve_context
from api.mail.models_catalog import list_chat_models, require_available_model
from api.routers.api.v1.chat.stream import ChatStreamRequest
from api.locales import resolve_locale
from db.accessors import ui_settings

router = APIRouter()
_LOG = logging.getLogger(__name__)


@router.websocket('')
async def voice_turn(socket: WebSocket) -> None:
    remaining = live_remaining(socket)
    if not remaining:
        await socket.close(code=1008)
        return
    await socket.accept()
    metrics = VoiceMetrics()
    try:
        async with asyncio.timeout(min(600, remaining)):
            raw = await asyncio.wait_for(socket.receive_text(), timeout=5)
            received_at = time.monotonic()
            if len(raw) > 100_000:
                await socket.close(code=1009)
                return
            body = ChatStreamRequest.model_validate_json(raw)
            if not body.voice_response or body.conversation_id == '':
                await socket.close(code=1008)
                return
            if body.conversation_id is not None and await chat_conversation.get_conversation(body.conversation_id) is None:
                await socket.send_json({'event': 'refusal', 'code': 'conversation_not_found'})
                return
            try:
                if body.conversation_id is None and body.model is not None:
                    require_available_model(body.model, await list_chat_models())
                resolved = await resolve_context(**body.model_dump(exclude={'locale', 'voice_response', 'voice_stt_elapsed_ms'}))
            except HTTPException as error:
                code = error.detail.get('code') if isinstance(error.detail, dict) else None
                await socket.send_json({'event': 'refusal', 'code': code if isinstance(code, str) else 'internal'})
                return
            locale = await resolve_locale(body.locale)
            settings = await ui_settings.get_or_create()
            events = run_turn_events(**body.model_dump(exclude={'voice_stt_elapsed_ms'}), resolved_context=resolved, resolved_locale=locale, voice_metrics=metrics, voice_model=settings.voice_model, live_voice_turn=True)
            pace = settings.speech_pace
            await run_voice_turn(socket, events, resolved[1].id, locale=locale, pace=pace, stt_elapsed_ms=body.voice_stt_elapsed_ms, started_at=received_at, metrics=metrics)
    except (ValidationError, ValueError):
        with suppress(Exception): await socket.close(code=1008)
    except WebSocketDisconnect:
        _LOG.info('live voice turn ended code=client_disconnect')
    except Exception as error:
        _LOG.warning('live voice turn failed class=%s', type(error).__name__)
        with suppress(Exception): await socket.close(code=1011)
    finally:
        if not metrics.logged:
            metrics.logged = True
            _LOG.info('voice_metrics outcome=pre_turn model=None reasoning_effort=None tool_rounds=0 thinking_chunks=0 tts_connect_ms=None')
        with suppress(Exception): await socket.close()
