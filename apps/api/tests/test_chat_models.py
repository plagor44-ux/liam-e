"""Model catalogue contracts."""
from types import SimpleNamespace as NS

import pytest
from support.fake_mistral import FakeMistral

pytestmark = pytest.mark.anyio


async def test_models_list_keeps_function_calling_models_with_german_labels(monkeypatch):
    from api.mail import models_catalog as catalog
    card = NS(id='mistral-medium-2508', aliases=['mistral-medium-latest'], capabilities=NS(completion_chat=True, function_calling=True), deprecation=None)
    bad = NS(id='bad', aliases=[], capabilities=NS(completion_chat=True, function_calling=False), deprecation=None)
    fake = FakeMistral(models=[NS(data=[card, bad])])
    monkeypatch.setattr(catalog, 'mistral_client', lambda: fake)
    catalog.reset_cache()
    models = await catalog.list_chat_models()
    assert any(m.id == 'mistral-medium-latest' and m.label == 'Mistral Medium' and m.description for m in models)
    assert not any(m.id == 'bad' for m in models)


async def test_model_catalog_cache_and_deprecation(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from api.mail import models_catalog as catalog
    old = NS(id='retired', aliases=[], capabilities=NS(completion_chat=True, function_calling=True), deprecation=datetime.now(timezone.utc) - timedelta(hours=1))
    fake = FakeMistral(models=[NS(data=[old])])
    monkeypatch.setattr(catalog, 'mistral_client', lambda: fake)
    catalog.reset_cache()
    first = await catalog.list_chat_models()
    second = await catalog.list_chat_models()
    assert not any(m.id == 'retired' for m in first)
    assert first == second and len(fake.requests) == 1


async def test_patch_model_and_title(monkeypatch):
    from db.models.chat_conversation import ChatConversation
    from api.routers.api.v1.chat import conversations
    from api.mail.models_catalog import ChatModelOut
    row = ChatConversation(title='Alt', model='old')
    async def get(id): return row if id == row.id else None
    async def update(id, expected_version, *, model=None, title=None, **kw):
        if model is not None: row.model = model
        if title is not None: row.title = title
        return row
    async def models(): return [ChatModelOut(id='valid', label='Valid', description='', is_default=False)]
    monkeypatch.setattr(conversations.conversation_access, 'get_conversation', get)
    monkeypatch.setattr(conversations.chat_context, 'configure', update)
    monkeypatch.setattr(conversations, 'list_chat_models', models)
    updated = await conversations.update_conversation(row.id, conversations.ChatConversationUpdateIn(expected_version=1, model='valid', title='  Neu  '))
    assert (updated.model, updated.title) == ('valid', 'Neu')
    updated = await conversations.update_conversation(row.id, conversations.ChatConversationUpdateIn(expected_version=1, model=None))
    assert (updated.model, updated.title) == ('', 'Neu')
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await conversations.update_conversation(row.id, conversations.ChatConversationUpdateIn(expected_version=1, model='unknown'))
    assert exc.value.status_code == 422 and exc.value.detail['code'] == 'model_unknown'
    with pytest.raises(HTTPException) as exc:
        await conversations.update_conversation(row.id, conversations.ChatConversationUpdateIn(expected_version=1, model=''))
    assert exc.value.detail['code'] == 'model_unknown'


async def test_models_list_falls_back_to_default_on_api_error(monkeypatch):
    from api.mail import models_catalog as catalog
    fake = FakeMistral(models=[RuntimeError('offline')])
    monkeypatch.setattr(catalog, 'mistral_client', lambda: fake)
    catalog.reset_cache()
    models = await catalog.list_chat_models()
    assert len(models) == 1 and models[0].is_default
    assert models[0].availability == 'unverified' and not models[0].recommended
    assert models[0].price is None


def card(model_id, *aliases):
    return NS(id=model_id, aliases=list(aliases), capabilities=NS(completion_chat=True, function_calling=True))


async def test_prices_and_recommendation_follow_workspace_bindings(monkeypatch):
    from api.mail import models_catalog as catalog
    fake = FakeMistral(models=[NS(data=[
        card('mistral-small-2603', 'mistral-small-latest'), card('mistral-small-latest'),
        card('codestral-2508'), card('zai-glm-5-3'), card('private-model'),
    ])])
    monkeypatch.setattr(catalog, 'mistral_client', lambda: fake)
    monkeypatch.setattr(catalog.mail_env, 'mail_chat_model', 'mistral-medium-latest')
    catalog.reset_cache()
    models = {model.id: model for model in await catalog.list_chat_models()}
    small = models['mistral-small-latest']
    assert small.resolved_id == 'mistral-small-2603' and small.group == 'general'
    assert [m.id for m in models.values() if m.recommended] == [small.id]
    assert (small.price.input_per_million, small.price.output_per_million, small.price.currency) == (0.15, 0.60, 'USD')
    assert small.price.source == 'https://docs.mistral.ai/inference/pricing'
    assert small.price.checked_on.isoformat() == '2026-11-19'
    assert models['codestral-2508'].group == 'specialist'
    assert models['zai-glm-5-3'].group == 'third_party'
    assert models['private-model'].group == 'other' and models['private-model'].price is None
    assert models['mistral-medium-latest'].availability == 'unlisted'
    catalog.require_available_model('mistral-small-2603', list(models.values()))


@pytest.mark.parametrize('bindings', [
    [card('mistral-small-latest')],
    [card('mistral-small-future', 'mistral-small-latest')],
    [card('mistral-small-2603', 'mistral-small-latest'), card('mistral-small-future', 'mistral-small-latest')],
])
async def test_unresolved_new_or_conflicting_alias_has_no_price_or_recommendation(monkeypatch, bindings):
    from api.mail import models_catalog as catalog
    fake = FakeMistral(models=[NS(data=[*bindings, card('codestral-2508'), card('zai-glm-5-3')])])
    monkeypatch.setattr(catalog, 'mistral_client', lambda: fake)
    catalog.reset_cache()
    models = await catalog.list_chat_models()
    alias = next(model for model in models if model.id == 'mistral-small-latest')
    assert alias.availability == 'available' and alias.price is None
    assert not alias.recommended
    assert not any(model.recommended for model in models if model.group != 'general')


async def test_catalogue_refresh_replaces_old_price_and_distinguishes_outage_from_removal(monkeypatch):
    from fastapi import HTTPException
    from api.mail import models_catalog as catalog
    alias = 'mistral-small-latest'
    fake = FakeMistral(models=[NS(data=[card(alias, 'mistral-small-2603')]), RuntimeError('offline'),
                               NS(data=[card('mistral-small-future', alias)])])
    monkeypatch.setattr(catalog, 'mistral_client', lambda: fake)
    monkeypatch.setattr(catalog.mail_env, 'mail_chat_model', 'missing-default')
    catalog.reset_cache()
    available = await catalog.list_chat_models()
    assert next(model for model in available if model.id == alias).price is not None
    with pytest.raises(HTTPException) as missing:
        catalog.require_available_model('missing-default', available)
    assert missing.value.status_code == 422 and missing.value.detail['code'] == 'model_unknown'
    monkeypatch.setattr(catalog, '_cached_at', float('-inf'))
    offline = await catalog.list_chat_models()
    assert all(model.availability == 'unverified' and model.price is None for model in offline)
    with pytest.raises(HTTPException) as unavailable:
        catalog.require_available_model(alias, offline)
    assert unavailable.value.status_code == 503
    refreshed = await catalog.list_chat_models()
    model = next(model for model in refreshed if model.id == alias)
    assert model.availability == 'available' and model.price is None and not model.recommended


async def test_available_generalist_fallback_and_versioned_model_card_price(monkeypatch):
    from api.mail import models_catalog as catalog
    fake = FakeMistral(models=[NS(data=[card('mistral-medium-3-5', 'mistral-medium-latest'),
                                        card('ministral-8b-2512'), card('codestral-2508')])])
    monkeypatch.setattr(catalog, 'mistral_client', lambda: fake)
    catalog.reset_cache()
    models = {model.id: model for model in await catalog.list_chat_models()}
    assert [model.id for model in models.values() if model.recommended] == ['ministral-8b-2512']
    price = models['mistral-medium-latest'].price
    assert price.source == 'https://docs.mistral.ai/models/mistral-medium-3-5-26-04'
    assert (price.input_per_million, price.output_per_million) == (1.50, 7.50)


async def test_workspace_ownership_and_conflicting_capabilities_override_familiar_names(monkeypatch):
    from api.mail import models_catalog as catalog
    incompatible = card('incompatible', 'shared-latest')
    incompatible.capabilities.function_calling = False
    foreign = card('mistral-large-4')
    foreign.owned_by = 'synthetic-vendor'
    custom = card('mistral-medium-3-5')
    custom.type = 'fine-tuned'
    fake = FakeMistral(models=[NS(data=[card('mistral-small-2603', 'shared-latest'), incompatible, foreign, custom])])
    monkeypatch.setattr(catalog, 'mistral_client', lambda: fake)
    catalog.reset_cache()
    models = {model.id: model for model in await catalog.list_chat_models()}
    assert 'shared-latest' not in models
    assert models['mistral-small-2603'].recommended
    assert models[foreign.id].group == 'third_party' and models[foreign.id].price is None
    assert models[custom.id].group == 'other' and not models[custom.id].recommended and models[custom.id].price is None


async def test_simultaneous_reads_share_one_workspace_snapshot(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from api.mail import models_catalog as catalog

    async def read():
        await asyncio.sleep(0)
        return NS(data=[card('mistral-small-2603', 'mistral-small-latest')])

    listing = AsyncMock(side_effect=read)
    monkeypatch.setattr(catalog, 'mistral_client', lambda: NS(models=NS(list_async=listing)))
    catalog.reset_cache()
    first, second = await asyncio.gather(catalog.list_chat_models(), catalog.list_chat_models())
    listing.assert_awaited_once()
    assert first == second

async def test_slow_catalog_refresh_survives_voice_timeout_and_fills_cache(monkeypatch):
    import asyncio
    from api.mail import models_catalog as catalog
    calls = []
    card = NS(id='mistral-small-2603', aliases=['mistral-small-latest'], capabilities=NS(completion_chat=True, function_calling=True), deprecation=None)
    async def slow_list():
        calls.append(1)
        await asyncio.sleep(.08)
        return NS(data=[card])
    monkeypatch.setattr(catalog, 'mistral_client', lambda: NS(models=NS(list_async=slow_list)))
    catalog.reset_cache()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(catalog.list_chat_models('mistral-small-latest'), timeout=.01)
    await asyncio.sleep(.1)
    models = await catalog.list_chat_models('mistral-small-latest')
    assert len(calls) == 1
    assert catalog.catalog_ready()
    assert any(model.id == 'mistral-small-latest' and model.resolved_id == 'mistral-small-2603' for model in models)
    catalog.reset_cache()

async def test_background_catalog_failure_returns_unverified_without_logging_details(monkeypatch, caplog):
    from api.mail import models_catalog as catalog
    async def broken(): raise RuntimeError('private workspace content')
    catalog.reset_cache()
    monkeypatch.setattr(catalog, '_workspace_cards', broken)
    models = await catalog.list_chat_models('mistral-small-latest')
    assert all(model.availability == 'unverified' for model in models)
    assert 'RuntimeError' in caplog.text and 'private workspace content' not in caplog.text
    catalog.reset_cache()
