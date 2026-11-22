"""Workspace chat models and the public list prices their reported IDs resolve to."""
import asyncio
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Literal, NamedTuple

from pydantic import BaseModel
from db.accessors import chat_model_settings
from env.mail import env as mail_env
from utils.mistral import mistral_client
from api.errors import refuse

logger = logging.getLogger(__name__)
MODEL_TEXTS = {
    'mistral-large-latest': ('Mistral Large', 'Für anspruchsvolle Aufgaben und ausführliche Antworten.'),
    'mistral-medium-latest': ('Mistral Medium', 'Für vielseitige Aufgaben im Alltag.'),
    'mistral-small-latest': ('Mistral Small', 'Für schnelle Antworten auf einfache Fragen.'),
    'magistral-medium-latest': ('Magistral Medium', 'Für Aufgaben, die mehr Nachdenken brauchen.'),
    'magistral-small-latest': ('Magistral Small', 'Für schnelles Nachdenken über einfache Aufgaben.'),
}
_cache = None
_cached_at = 0.0
_refresh_lock = asyncio.Lock()
_refresh_task = None


class ChatModelPrice(BaseModel):
    input_per_million: float
    output_per_million: float
    currency: Literal['USD'] = 'USD'
    source: str
    checked_on: date


# USD per million uncached input / output tokens in the linked public price list.
# Never look up a moving '-latest' alias in this table.
PRICE_SOURCE = 'https://docs.mistral.ai/inference/pricing'
PRICE_DATE = date(2026, 11, 19)


class ListedModel(NamedTuple):
    label: str
    input_rate: float
    output_rate: float
    source: str = PRICE_SOURCE


PUBLISHED_RATES = {
    'mistral-small-2603': ListedModel('Mistral Small 4', 0.15, 0.60),
    'mistral-large-4': ListedModel('Mistral Large 4', 0.70, 2.10),
    'mistral-large-4-0': ListedModel('Mistral Large 4', 0.70, 2.10, 'https://docs.mistral.ai/models/mistral-large-4-0'),
    'mistral-large-2512': ListedModel('Mistral Large 3', 0.50, 1.50, 'https://docs.mistral.ai/models/mistral-large-3-25-12'),
    'mistral-medium-3-5': ListedModel('Mistral Medium 3.5', 1.50, 7.50, 'https://docs.mistral.ai/models/mistral-medium-3-5-26-04'),
    'ministral-14b-2512': ListedModel('Ministral 3 14B', 0.20, 0.20),
    'ministral-8b-2512': ListedModel('Ministral 3 8B', 0.15, 0.15),
    'ministral-3b-2512': ListedModel('Ministral 3 3B', 0.10, 0.10),
    'codestral-2508': ListedModel('Codestral', 0.30, 0.90),
    'voxtral-small-2507': ListedModel('Voxtral Small', 0.10, 0.40),  # Text input only.
    'zai-glm-5-3-flash': ListedModel('Z.ai GLM 5.3 Flash', 0.15, 0.50),
    'zai-glm-5-3': ListedModel('Z.ai GLM 5.3', 1.40, 4.40),
    'deepseek-v4-1-flash-1017': ListedModel('DeepSeek V4.1 Flash 1017', 0.30, 1.20),
}
ModelGroup = Literal['general', 'specialist', 'third_party', 'other']
Availability = Literal['available', 'unlisted', 'unverified']


class ChatModelOut(BaseModel):
    id: str
    label: str
    description: str
    is_default: bool
    is_app_default: bool = False
    availability: Availability = 'available'
    group: ModelGroup = 'other'
    resolved_id: str | None = None
    price: ChatModelPrice | None = None
    recommended: bool = False


DefaultModelStatus = Literal['app_default', 'available', 'unlisted', 'unverified']


class DefaultModelState(NamedTuple):
    default_model: str | None
    effective_default_model: str
    app_default_model: str
    status: DefaultModelStatus
    models: list[ChatModelOut] | None = None


def reset_cache():
    global _cache, _cached_at, _refresh_lock, _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        _refresh_task.cancel()
    _cache = None
    _cached_at = 0.0
    _refresh_lock = asyncio.Lock()
    _refresh_task = None


def _group(model_id: str) -> ModelGroup:
    if model_id.startswith(('zai-', 'deepseek-', 'meta-', 'qwen-', 'openai-')):
        return 'third_party'
    if model_id.startswith(('codestral-', 'devstral-', 'labs-devstral-', 'magistral-', 'voxtral-', 'pixtral-')):
        return 'specialist'
    if model_id.startswith(('mistral-small-', 'mistral-medium-', 'mistral-large-', 'ministral-')):
        return 'general'
    return 'other'


def _model(model_id: str, default: str, availability: Availability, resolved_id: str | None = None,
           group: ModelGroup | None = None, *, app_default: str | None = None) -> ChatModelOut:
    family = _group(resolved_id or model_id)
    selection_family = _group(model_id)
    group = group or (selection_family if selection_family in ('third_party', 'specialist') else family)
    listed = PUBLISHED_RATES.get(resolved_id) if resolved_id and group == family else None
    # A history row containing an alias must keep its name when that alias moves.
    label, description = MODEL_TEXTS.get(model_id, (listed.label if listed and model_id == resolved_id else model_id, ''))
    price = ChatModelPrice(input_per_million=listed.input_rate, output_per_million=listed.output_rate,
                           source=listed.source, checked_on=PRICE_DATE) if listed else None
    app_model = mail_env.mail_chat_model if app_default is None else app_default
    return ChatModelOut(id=model_id, label=label, description=description, is_default=model_id == default,
                        is_app_default=model_id == app_model, availability=availability, group=group,
                        resolved_id=resolved_id, price=price)


def _expired(value):
    if not value: return False
    if isinstance(value, datetime):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value) < datetime.now(timezone.utc)
    if isinstance(value, date): return value < date.today()
    if isinstance(value, str):
        try: return date.fromisoformat(value[:10]) < date.today()
        except ValueError: return False
    return False


def _workspace_models(cards, default: str, *, app_default: str | None = None) -> list[ChatModelOut]:
    app_model = mail_env.mail_chat_model if app_default is None else app_default
    bindings: dict[str, set[str]] = defaultdict(set)
    primary_ids: set[str] = set()
    unsupported: set[str] = set()
    groups: dict[str, ModelGroup] = {}
    for card in cards:
        capabilities = getattr(card, 'capabilities', None)
        model_id = getattr(card, 'id', None)
        if not isinstance(model_id, str) or not model_id or _expired(getattr(card, 'deprecation', None)):
            continue
        names = {model_id, *(alias for alias in (getattr(card, 'aliases', None) or []) if isinstance(alias, str) and alias)}
        if not getattr(capabilities, 'completion_chat', False) or not getattr(capabilities, 'function_calling', False):
            unsupported.update(names)
            continue
        primary_ids.add(model_id)
        owner = getattr(card, 'owned_by', None)
        if getattr(card, 'type', None) == 'fine-tuned':
            groups.update(dict.fromkeys(names, 'other'))
        elif isinstance(owner, str) and owner.lower() not in ('', 'mistral', 'mistral ai', 'mistralai'):
            for name in names:
                groups.setdefault(name, 'third_party')
        moving = {name for name in names if name.endswith('-latest')}
        targets = names - moving if model_id.endswith('-latest') else {model_id}
        choices = moving or {model_id}
        if default in names:
            choices.add(default)
        if app_model in names:
            choices.add(app_model)
        for choice in choices:
            bindings[choice].update(targets)

    # When two versions claim the same alias, either concrete ID can still be
    # selected if its own card supports the chat tools. An alias also advertised
    # by an incompatible card cannot establish tool support or a price.
    ambiguous_targets = {target for name, targets in bindings.items() if len(targets) > 1 or name in unsupported for target in targets}
    for target in ambiguous_targets & primary_ids:
        bindings.setdefault(target, {target})
    models = []
    for name, targets in bindings.items():
        if name in unsupported:
            continue
        version = next(iter(targets)) if len(targets) == 1 and targets.isdisjoint(unsupported) else None
        models.append(_model(name, default, 'available', version, groups.get(name), app_default=app_model))
    # Prefer Small 4 for everyday tool use. If absent, choose the cheapest listed
    # input rate among the other verified Mistral generalists, then output rate.
    generalists = [model for model in models if model.group == 'general' and model.price]
    if generalists:
        min(generalists, key=lambda m: (m.resolved_id != 'mistral-small-2603', m.price.input_per_million,
                                        m.price.output_per_million, not m.is_default, m.id)).recommended = True
    if not any(model.is_default for model in models):
        models.append(_model(default, default, 'unlisted', app_default=app_model))
    if app_model != default and not any(model.is_app_default for model in models):
        models.append(_model(app_model, default, 'unlisted', app_default=app_model))
    return sorted(models, key=lambda m: (not m.is_default, m.label))


def _has_available_model(model_id: str, models: list[ChatModelOut]) -> bool:
    return bool(model_id and model_id.strip()) and any(
        model_id in (model.id, model.resolved_id) and model.availability == 'available'
        for model in models
    )


def require_available_model(model_id: str, models: list[ChatModelOut]) -> None:
    if not model_id or not model_id.strip():
        raise refuse(422, 'model_unknown', 'Dieses Modell gibt es nicht mehr. Wähle ein anderes.')
    if any(model.availability == 'unverified' for model in models):
        raise refuse(503, 'model_catalog_unavailable', 'Die Modellliste konnte nicht geladen werden. Versuch es noch einmal.')
    if not _has_available_model(model_id, models):
        raise refuse(422, 'model_unknown', 'Dieses Modell gibt es nicht mehr. Wähle ein anderes.')


async def _workspace_cards():
    global _cache, _cached_at
    async with _refresh_lock:
        if _cache is not None and time.monotonic() - _cached_at < 600:
            return list(_cache)
        try:
            response = await mistral_client().models.list_async()
        except Exception as error:
            logger.warning('%s', type(error).__name__)
            return None
        data = getattr(response, 'data', None)
        if data is None:
            return None
        cards = list(data)
        _cache, _cached_at = cards, time.monotonic()
        return list(cards)


def prewarm_catalog() -> asyncio.Task:
    """Share one bounded refresh; caller cancellation never cancels the refresh."""
    global _refresh_task
    if _refresh_task is None or _refresh_task.done():
        async def refresh():
            try:
                return await asyncio.wait_for(_workspace_cards(), timeout=15)
            except TimeoutError:
                return None
            except Exception as error:
                logger.warning('%s', type(error).__name__)
                return None
        _refresh_task = asyncio.create_task(refresh())
    return _refresh_task


def catalog_ready() -> bool:
    return _cache is not None and time.monotonic() - _cached_at < 600


async def list_chat_models(default: str | None = None, *, app_default: str | None = None) -> list[ChatModelOut]:
    app_model = mail_env.mail_chat_model if app_default is None else app_default
    effective = app_model if default is None else default
    cards = await asyncio.shield(prewarm_catalog())
    if cards is None:
        out = [_model(effective, effective, 'unverified', app_default=app_model)]
        if app_model != effective:
            out.append(_model(app_model, effective, 'unverified', app_default=app_model))
        return out
    return _workspace_models(cards, effective, app_default=app_model)


async def default_model_state(stored: str | None, *, include_models: bool = True) -> DefaultModelState:
    app_default = mail_env.mail_chat_model
    if stored is None:
        models = await list_chat_models(app_default, app_default=app_default) if include_models else None
        return DefaultModelState(
            default_model=None,
            effective_default_model=app_default,
            app_default_model=app_default,
            status='app_default',
            models=models,
        )
    candidate = await list_chat_models(stored, app_default=app_default)
    if any(model.availability == 'unverified' for model in candidate):
        return DefaultModelState(
            default_model=stored,
            effective_default_model=stored,
            app_default_model=app_default,
            status='unverified',
            models=candidate if include_models else None,
        )
    if _has_available_model(stored, candidate):
        return DefaultModelState(
            default_model=stored,
            effective_default_model=stored,
            app_default_model=app_default,
            status='available',
            models=candidate if include_models else None,
        )
    fallback = await list_chat_models(app_default, app_default=app_default) if include_models else None
    return DefaultModelState(
        default_model=stored,
        effective_default_model=app_default,
        app_default_model=app_default,
        status='unlisted',
        models=fallback,
    )


async def resolve_default_chat_model() -> str:
    row = await chat_model_settings.get()
    state = await default_model_state(row.default_model, include_models=False)
    return state.effective_default_model


async def list_effective_chat_models() -> list[ChatModelOut]:
    row = await chat_model_settings.get()
    state = await default_model_state(row.default_model, include_models=True)
    assert state.models is not None
    return state.models
