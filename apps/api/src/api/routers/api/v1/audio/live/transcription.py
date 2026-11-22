"""Relay bounded PCM frames to Mistral after the inherited owner check."""
import asyncio
from contextlib import suppress

from fastapi import APIRouter, WebSocket

from api.audio.live_access import live_remaining
from api.audio.live_provider import mistral_live
from api.audio.voices import prewarm as prewarm_voices
from api.locales import resolve_locale
from api.mail.models_catalog import prewarm_catalog

router = APIRouter()
_slot = asyncio.Semaphore(1)


@router.websocket('')
async def transcribe(websocket: WebSocket) -> None:
    remaining = live_remaining(websocket)
    if not remaining:
        await websocket.close(code=1008)
        return
    try:
        # A prior browser socket can close before its upstream provider session has released the slot.
        await asyncio.wait_for(_slot.acquire(), timeout=min(4, remaining))
    except TimeoutError:
        await websocket.close(code=1013)
        return
    async def upload(session):
        while True:
            frame = await websocket.receive_bytes()
            await session.append(frame)

    async def download(session):
        while True:
            await websocket.send_json(await session.receive())

    try:
        hint = getattr(websocket, "query_params", {}).get("locale")
        if hint not in {None, "de", "en"}:
            await websocket.close(code=1008)
            return
        locale = await resolve_locale(hint)
        remaining = live_remaining(websocket)
        if not remaining:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        prewarm_catalog()
        prewarm_voices()
        async with asyncio.timeout(min(600, remaining)), mistral_live('transcription', locale=locale) as session:
            tasks = [asyncio.create_task(upload(session)), asyncio.create_task(download(session))]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        with suppress(Exception):
            await websocket.close(code=1011)
    finally:
        _slot.release()
