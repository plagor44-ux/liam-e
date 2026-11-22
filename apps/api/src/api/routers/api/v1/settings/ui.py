"""UI settings contract: a server-owned scope, validated values, revision-guarded updates.

The owner is the fixed installation scope of the single-user deployment, never a request
identity: the body cannot name or override it (unknown fields are refused, not ignored). GET
initializes the canonical configuration exactly once, so a second device reads the same
confirmed assignment. PATCH validates the action and locale allowlists and swaps on ``expected_revision``;
a concurrent change answers 409 with the way to reload instead of winning silently.
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from api.errors import refuse
from db.accessors import ui_settings


class PrivateSettingsRoute(APIRoute):
    """Validation failures name the problem without echoing the rejected payload."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request):
            try:
                return await handler(request)
            except SQLAlchemyError:
                raise refuse(503, 'settings_unavailable', 'Die Einstellungen sind gerade nicht erreichbar. Versuch es gleich noch einmal.') from None
            except RequestValidationError:
                raise refuse(422, 'ui_settings_invalid', 'Prüfe die Sprache oder die Wischaktion. Gib mindestens eine Einstellung an.') from None
        return private_handler


router = APIRouter(route_class=PrivateSettingsRoute, responses={
    409: {'description': 'Expected revision is stale'},
    422: {'description': 'Invalid fields; the rejected payload is not echoed'},
})

SwipeAction = Literal['none', 'archive', 'toggle_read', 'trash']
UiLocale = Literal['browser', 'de', 'en']
SpeechPace = Literal['slow', 'normal', 'fast']


class UiSettingsOut(BaseModel):
    swipe_left: SwipeAction
    swipe_right: SwipeAction
    locale: UiLocale
    speech_pace: SpeechPace = 'normal'
    revision: int
    updated_at: datetime


class UiSettingsUpdate(BaseModel):
    """A change to the UI settings; the owner scope is not part of the contract."""

    model_config = ConfigDict(extra='forbid')

    expected_revision: int = Field(ge=1)
    swipe_left: SwipeAction | None = None
    swipe_right: SwipeAction | None = None
    locale: UiLocale | None = None
    speech_pace: SpeechPace | None = None

    @model_validator(mode='after')
    def require_explicit_change(self):
        fields = ('swipe_left', 'swipe_right', 'locale', 'speech_pace')
        changed = self.model_fields_set.intersection(fields)
        if not changed or any(getattr(self, key) is None for key in changed):
            raise ValueError('ui_settings_invalid')
        return self


@router.get('', operation_id='settings_ui_get')
async def get_ui_settings() -> UiSettingsOut:
    """The confirmed settings, initializing the canonical defaults on first read."""
    row = await ui_settings.get_or_create()
    return UiSettingsOut.model_validate(row, from_attributes=True)


@router.patch('', operation_id='settings_ui_update')
async def update_ui_settings(body: UiSettingsUpdate) -> UiSettingsOut:
    """Apply a validated change while the caller holds the current revision."""
    try:
        row = await ui_settings.update(body.expected_revision, **body.model_dump(exclude={'expected_revision'}, exclude_unset=True))
    except ui_settings.StaleRevision:
        raise refuse(409, 'ui_settings_conflict', 'Die Einstellungen wurden inzwischen geändert. Lade sie neu und nimm deine Auswahl dann bewusst auf.') from None
    return UiSettingsOut.model_validate(row, from_attributes=True)


class BrowserLocaleIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    locale: Literal['de', 'en']


class BrowserLocaleOut(BaseModel):
    confirmed: bool


@router.post('/browser-locale', operation_id='settings_ui_browser_locale_confirm')
async def confirm_browser_locale(body: BrowserLocaleIn) -> BrowserLocaleOut:
    """Remember this browser hint for jobs without a browser request."""
    await ui_settings.remember_browser_locale(body.locale)
    return BrowserLocaleOut(confirmed=True)
