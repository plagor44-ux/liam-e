"""Coordinate one authorized chat stream and its server-derived spoken sentences."""
import asyncio
import base64
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, suppress

from db.accessors import chat_context
from fastapi import WebSocket
from websockets.exceptions import InvalidStatus

from api.audio.live_access import live_remaining
from api.audio.live_provider import LiveAudioError, mistral_live
from api.audio.pace import paced_text
from api.mail.chat_events import ChatEvent

_LOG = logging.getLogger(__name__)
_MAX_AUDIO = 24_000_000
_SENTENCE_END = re.compile(r'[.!?](?=\s)')


class VoiceRevoked(Exception):
    pass


async def run_voice_turn(socket: WebSocket, events: AsyncIterator[ChatEvent], context_id: str, locale: str = "en", pace: str = "normal", stt_elapsed_ms: int | None = None, started_at: float | None = None) -> None:
    """Only deltas produced by this turn can enter the speech provider.

    Keep the socket open until the browser acknowledges playback completion, so revocation
    and window checks also cover audio already queued in the browser.
    """
    started_at = started_at if started_at is not None else time.monotonic()
    first_token_ms = first_sentence_ms = first_audio_ms = None
    gap_ms = 0.0
    audio_seconds = 0.0
    characters = 0
    last_audio_end = None
    sentences: asyncio.Queue[str | None] = asyncio.Queue(maxsize=16)
    send_lock = asyncio.Lock()
    done = None
    audio_ended = asyncio.Event()
    acknowledged = asyncio.Event()

    async def send(event: dict) -> None:
        async with send_lock:
            await socket.send_json(event)

    async def check() -> None:
        if not live_remaining(socket) or not await chat_context.is_live(context_id):
            raise VoiceRevoked()

    async def chat() -> None:
        nonlocal done, first_token_ms, first_sentence_ms, characters
        buffer = ''
        async for event in events:
            if event.event == 'error':
                await send(event.model_dump(mode='json'))
                raise VoiceRevoked()
            if event.event == 'done':
                if event.stopped:
                    await send(event.model_dump(mode='json'))
                    raise VoiceRevoked()
                done = event
                await send({'event': 'voice_text_done', 'conversation_id': event.conversation_id, 'message_id': event.message_id})
                if buffer.strip():
                    if first_sentence_ms is None: first_sentence_ms = (time.monotonic() - started_at) * 1000
                    await sentences.put(buffer)
                await sentences.put(None)
                return
            await send(event.model_dump(mode='json'))
            if event.event == 'delta':
                if first_token_ms is None: first_token_ms = (time.monotonic() - started_at) * 1000
                characters += len(event.text)
                buffer += event.text
                while match := _SENTENCE_END.search(buffer):
                    end = match.end()
                    if first_sentence_ms is None: first_sentence_ms = (time.monotonic() - started_at) * 1000
                    await sentences.put(buffer[:end])
                    buffer = buffer[end:]
                if len(buffer) > 16_384:
                    raise RuntimeError('voice_text_limit')
        raise RuntimeError('voice_chat_incomplete')

    async def speech() -> None:
        nonlocal first_audio_ms, gap_ms, audio_seconds, last_audio_end
        total = 0
        failed = False
        try:
            sentence = await sentences.get()
            if sentence is not None:
                async with AsyncExitStack() as stack:
                    async with asyncio.timeout(15):
                        session = await stack.enter_async_context(mistral_live('speech', locale=locale))
                    while sentence is not None:
                        async with asyncio.timeout(60):
                            await check()
                            await session.append(paced_text(sentence, pace, 8000))
                            await session.finish()
                            first_of_sentence = True
                            while True:
                                audio = await session.receive()
                                if audio is None:
                                    break
                                now = time.monotonic()
                                if first_audio_ms is None: first_audio_ms = (now - started_at) * 1000
                                duration = len(audio) / 96000
                                if first_of_sentence and last_audio_end is not None: gap_ms += max(0, now - last_audio_end) * 1000
                                first_of_sentence = False
                                last_audio_end = max(now, last_audio_end or now) + duration
                                audio_seconds += duration
                                total += len(audio)
                                if total > _MAX_AUDIO:
                                    raise RuntimeError('voice_audio_limit')
                                for offset in range(0, len(audio), 24_000):
                                    await check()
                                    await send({'event': 'voice_audio', 'audio': base64.b64encode(audio[offset:offset + 24_000]).decode('ascii')})
                        sentence = await sentences.get()
        except VoiceRevoked:
            raise
        except Exception as error:
            terminal = isinstance(error, InvalidStatus) and getattr(error.response, 'status_code', None) in (401, 403)
            code = 'provider_auth' if terminal else 'provider_protocol' if isinstance(error, LiveAudioError) else 'timeout' if isinstance(error, TimeoutError) else 'stream_failure'
            _LOG.warning('live voice speech failed code=%s class=%s', code, type(error).__name__)
            failed = True
            await send({'event': 'voice_audio_error', 'terminal': terminal})
        if failed:
            while await sentences.get() is not None:
                pass
        audio_ended.set()
        await send({'event': 'voice_audio_end'})

    async def guard() -> None:
        while True:
            await check()
            await asyncio.sleep(.25)

    async def receive() -> None:
        while True:
            message = await socket.receive_json()
            if message != {'event': 'playback_finished'} or not audio_ended.is_set() or acknowledged.is_set():
                raise RuntimeError('voice_invalid_ack')
            acknowledged.set()

    tasks = [asyncio.create_task(chat()), asyncio.create_task(speech()), asyncio.create_task(guard()), asyncio.create_task(receive())]
    async def finish() -> None:
        await asyncio.gather(*tasks[:2])
        await acknowledged.wait()
        await check()
        if done is None:
            raise RuntimeError('voice_chat_incomplete')
        await send(done.model_dump(mode='json'))
    completion = asyncio.create_task(finish())
    try:
        finished, _ = await asyncio.wait({completion, tasks[2], tasks[3]}, return_when=asyncio.FIRST_COMPLETED)
        for task in finished:
            task.result()
    except VoiceRevoked:
        with suppress(Exception):
            await send({'event': 'error', 'code': 'context_revoked'})
    finally:
        completion.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(completion, *tasks, return_exceptions=True)
        _LOG.info('voice_metrics locale=%s pace=%s stt_elapsed_ms=%s first_token_ms=%s first_sentence_ms=%s first_audio_ms=%s gap_ms=%s audio_seconds_per_character=%s',
                  locale, pace, stt_elapsed_ms, round(first_token_ms) if first_token_ms is not None else None,
                  round(first_sentence_ms) if first_sentence_ms is not None else None,
                  round(first_audio_ms) if first_audio_ms is not None else None, round(gap_ms),
                  round(audio_seconds / characters, 4) if characters else None)
        with suppress(Exception):
            await events.aclose()
