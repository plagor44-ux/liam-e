"""Coordinate one authorized chat stream and its server-derived spoken sentences."""
import asyncio
import base64
import logging
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, suppress

from db.accessors import chat_context
from fastapi import WebSocket
from websockets.exceptions import InvalidStatus

from api.audio.live_access import live_remaining
from api.audio.live_provider import LiveAudioError, mistral_live
from api.mail.chat_events import ChatEvent

_LOG = logging.getLogger(__name__)
_MAX_AUDIO = 24_000_000
_SENTENCE_END = re.compile(r'[.!?](?=\s)')


class VoiceRevoked(Exception):
    pass


async def run_voice_turn(socket: WebSocket, events: AsyncIterator[ChatEvent], context_id: str) -> None:
    """Only deltas produced by this turn can enter the speech provider.

    Keep the socket open until the browser acknowledges playback completion, so revocation
    and window checks also cover audio already queued in the browser.
    """
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
        nonlocal done
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
                    await sentences.put(buffer)
                await sentences.put(None)
                return
            await send(event.model_dump(mode='json'))
            if event.event == 'delta':
                buffer += event.text
                while match := _SENTENCE_END.search(buffer):
                    end = match.end()
                    await sentences.put(buffer[:end])
                    buffer = buffer[end:]
                if len(buffer) > 16_384:
                    raise RuntimeError('voice_text_limit')
        raise RuntimeError('voice_chat_incomplete')

    async def speech() -> None:
        total = 0
        failed = False
        try:
            sentence = await sentences.get()
            if sentence is not None:
                async with AsyncExitStack() as stack:
                    async with asyncio.timeout(15):
                        session = await stack.enter_async_context(mistral_live('speech'))
                    while sentence is not None:
                        async with asyncio.timeout(60):
                            await check()
                            await session.append(sentence)
                            await session.finish()
                            while True:
                                audio = await session.receive()
                                if audio is None:
                                    break
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
        with suppress(Exception):
            await events.aclose()
