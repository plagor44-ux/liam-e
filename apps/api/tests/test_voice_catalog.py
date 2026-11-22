"""Preset inventory stays bounded and never authorizes custom voices."""
import pytest

from api.audio import voices


@pytest.fixture(autouse=True)
def clear_voice_cache():
    voices.reset_cache()
    yield
    voices.reset_cache()


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


@pytest.mark.asyncio
async def test_failed_catalogue_is_cached_briefly_and_logs_only_exception_class(monkeypatch, caplog):
    from types import SimpleNamespace
    now = [1000.0]
    calls = []
    async def failed():
        calls.append(1)
        raise RuntimeError('sensitive provider response')
    voices.reset_cache()
    monkeypatch.setattr(voices, '_fetch', failed)
    monkeypatch.setattr(voices, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    assert not (await voices.list_voices()).verified
    assert not (await voices.list_voices()).verified
    assert len(calls) == 1
    assert 'RuntimeError' in caplog.text and 'sensitive provider response' not in caplog.text
    now[0] += 61
    assert not (await voices.list_voices()).verified
    assert len(calls) == 2
    voices.reset_cache()
