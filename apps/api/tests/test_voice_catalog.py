"""Preset inventory stays bounded and never authorizes custom voices."""
import pytest

from api.audio import voices


@pytest.mark.asyncio
async def test_catalog_accepts_only_presets_and_falls_back_on_failure(monkeypatch):
    async def listed():
        return [voices.Voice(id='voice_123', name='Example', languages=['en'])]

    monkeypatch.setattr(voices, '_fetch', listed)
    voices.reset_cache()
    assert (await voices.list_voices()).voices[0].id == 'voice_123'
    assert await voices.effective_voice('voice_123', 'de') == 'voice_123'
    assert await voices.effective_voice('missing', 'de') == 'voice_481'
    voices.reset_cache()
    async def failed():
        raise OSError('provider details must not escape')
    monkeypatch.setattr(voices, '_fetch', failed)
    result = await voices.list_voices()
    assert not result.verified
    assert await voices.effective_voice('voice_123', 'en') == 'voice_478'


@pytest.mark.asyncio
async def test_fetch_filters_custom_voices_and_follows_cursor(monkeypatch):
    import httpx
    import respx

    monkeypatch.setattr(voices, 'mistral_app_token', lambda: 'synthetic-token')
    with respx.mock:
        route = respx.get('https://api.mistral.ai/v2/audio/voices')
        route.side_effect = [httpx.Response(200, json={'data': [
            {'type': 'preset', 'slug': 'voice_123', 'name': 'Preset', 'languages': ['en']},
            {'type': 'custom', 'slug': 'custom', 'name': 'Private', 'languages': ['de']},
        ], 'next_page_token': 'next'}), httpx.Response(200, json={'data': [
            {'type': 'preset', 'slug': 'voice_456', 'name': 'Another', 'languages': ['de']},
        ], 'next_page_token': None})]
        result = await voices._fetch()
    assert [voice.id for voice in result] == ['voice_123', 'voice_456']
    assert route.calls[1].request.url.params['page_token'] == 'next'
