"""Bounded provider preset inventory; custom and workspace voices are never accepted."""
import asyncio
import json
import re
import time
from typing import Literal

import httpx
from pydantic import BaseModel
from utils.mistral import mistral_app_token


class Voice(BaseModel):
    id: str
    name: str
    languages: list[str]


class VoiceCatalogue(BaseModel):
    verified: bool
    voices: list[Voice]


DEFAULTS = {'de': 'voice_481', 'en': 'voice_478'}
SAMPLES = {'de': 'Hallo, ich bin Liam. Wie kann ich dir helfen?',
           'en': 'Hello, I am Liam. How can I help you?'}
_cache: VoiceCatalogue | None = None
_cached_at = 0.0
_lock = asyncio.Lock()
_warm_task: asyncio.Task | None = None


def reset_cache() -> None:
    global _cache, _cached_at, _lock, _warm_task
    if _warm_task is not None and not _warm_task.done():
        _warm_task.cancel()
    _cache, _cached_at, _lock, _warm_task = None, 0.0, asyncio.Lock(), None


async def _fetch() -> list[Voice]:
    # Cursor pagination, with hard bounds even if the upstream response is malformed.
    voices: list[Voice] = []
    token = None
    seen: set[str] = set()
    async with asyncio.timeout(10), httpx.AsyncClient(base_url='https://api.mistral.ai', timeout=10, trust_env=False, follow_redirects=False) as client:
        for _ in range(20):
            params = {'type': 'preset', 'page_size': '50'}
            if token:
                params['page_token'] = token
            async with client.stream('GET', '/v2/audio/voices', params=params, headers={'Authorization': f'Bearer {mistral_app_token()}'}) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 1_000_000:
                        raise ValueError('voice catalogue too large')
                    body.extend(chunk)
            page = json.loads(body)
            if not isinstance(page, dict) or not isinstance(page.get('data'), list):
                raise ValueError('invalid voice catalogue')
            for item in page['data']:
                if not isinstance(item, dict) or item.get('type') != 'preset':
                    continue
                identifier = item.get('slug') or item.get('id')
                name, languages = item.get('name'), item.get('languages') or []
                if (not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identifier)
                        or not isinstance(name, str) or not 1 <= len(name) <= 100
                        or not isinstance(languages, list) or len(languages) > 20
                        or any(not isinstance(lang, str) or len(lang) > 30 for lang in languages)):
                    continue
                if identifier not in seen:
                    seen.add(identifier)
                    voices.append(Voice(id=identifier, name=name, languages=languages))
            token = page.get('next_page_token')
            if token is None:
                return voices
            if not isinstance(token, str) or not 1 <= len(token) <= 512:
                raise ValueError('invalid cursor')
    raise ValueError('too many catalogue pages')


async def list_voices() -> VoiceCatalogue:
    global _cache, _cached_at
    async with _lock:
        if _cache is not None and time.monotonic() - _cached_at < 600:
            return _cache
        try:
            result = VoiceCatalogue(verified=True, voices=await _fetch())
        except Exception:
            return VoiceCatalogue(verified=False, voices=[])
        _cache, _cached_at = result, time.monotonic()
        return result


def prewarm() -> None:
    global _warm_task
    if _warm_task is None or _warm_task.done():
        _warm_task = asyncio.create_task(list_voices())


async def effective_voice(selected: str | None, locale: Literal['de', 'en']) -> str:
    if selected:
        catalogue = await list_voices()
        if catalogue.verified and any(voice.id == selected for voice in catalogue.voices):
            return selected
    return DEFAULTS[locale]
