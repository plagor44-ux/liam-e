"""HTTP contracts of the UI settings: allowlist, server-owned scope, revision conflicts."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from conftest import build_app
from db.accessors import ui_settings as access
from db.models.ui_settings import UiSettings


def row(revision=1, swipe_left='archive', swipe_right='toggle_read', locale='browser', speech_pace='normal') -> UiSettings:
    return UiSettings(id='ui', owner_scope='installation', swipe_left=swipe_left,
                      swipe_right=swipe_right, locale=locale, speech_pace=speech_pace, revision=revision, updated_at=datetime.now(timezone.utc))


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(access, 'get_or_create', AsyncMock(return_value=row()))
    monkeypatch.setattr(access, 'update', AsyncMock(side_effect=lambda expected_revision, **changes: row(
        revision=expected_revision + 1, **changes)))
    return TestClient(build_app())


def test_get_returns_the_canonical_first_configuration(client):
    response = client.get('/api/v1/settings/ui')
    assert response.status_code == 200
    body = response.json()
    assert body['swipe_left'] == 'archive' and body['swipe_right'] == 'toggle_read'
    assert body['revision'] == 1
    assert datetime.fromisoformat(body['updated_at']).tzinfo is not None
    assert body['locale'] == 'browser'
    assert set(body) == {'swipe_left', 'swipe_right', 'locale', 'speech_pace', 'revision', 'updated_at'}


def test_patch_applies_valid_actions_and_bumps_the_revision(client):
    response = client.patch('/api/v1/settings/ui', json={'expected_revision': 1, 'swipe_left': 'trash', 'swipe_right': 'none'})
    assert response.status_code == 200
    assert response.json()['swipe_left'] == 'trash' and response.json()['swipe_right'] == 'none'
    assert response.json()['revision'] == 2
    access.update.assert_awaited_once_with(1, swipe_left='trash', swipe_right='none')


@pytest.mark.parametrize('locale', ['browser', 'de', 'en'])
def test_patch_language_only_keeps_the_swipe_settings(client, locale):
    response = client.patch('/api/v1/settings/ui', json={'expected_revision': 1, 'locale': locale})
    assert response.status_code == 200
    assert response.json()['locale'] == locale
    assert response.json()['swipe_left'] == 'archive'
    access.update.assert_awaited_once_with(1, locale=locale)


@pytest.mark.parametrize('payload', [
    {'expected_revision': 1, 'locale': 'fr'},
    {'expected_revision': 1, 'speech_pace': 'faster'},
    {'expected_revision': 1, 'speech_pace': None},
    {'expected_revision': 1, 'locale': None},
    {'expected_revision': 1, 'locale': 'en', 'owner_scope': 'untrusted'},
    {'expected_revision': 1, 'swipe_left': 'spam'},
    {'expected_revision': 1, 'swipe_left': 'delete'},
    {'expected_revision': 1, 'swipe_left': 'send'},
    {'expected_revision': 1, 'swipe_left': None},
    {'expected_revision': 1},
    {'expected_revision': 0, 'swipe_left': 'none'},
    {'expected_revision': 1, 'swipe_left': 'none', 'owner_scope': 'untrusted'},
    {'expected_revision': 1, 'swipe_left': 'none', 'owner': 'someone'},
])
def test_patch_refuses_invalid_values_and_browser_owner_parameters(client, payload):
    response = client.patch('/api/v1/settings/ui', json=payload)
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'ui_settings_invalid'
    # The refusal names the problem without echoing the rejected payload.
    assert 'spam' not in response.text and 'untrusted' not in response.text and 'someone' not in response.text


def test_patch_reports_a_stale_revision_as_conflict(client, monkeypatch):
    monkeypatch.setattr(access, 'update', AsyncMock(side_effect=access.StaleRevision(1)))
    response = client.patch('/api/v1/settings/ui', json={'expected_revision': 1, 'swipe_left': 'trash'})
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'ui_settings_conflict'


def test_database_failures_answer_503(client, monkeypatch):
    monkeypatch.setattr(access, 'get_or_create', AsyncMock(side_effect=OperationalError('down', None, Exception())))
    monkeypatch.setattr(access, 'update', AsyncMock(side_effect=OperationalError('down', None, Exception())))
    assert client.get('/api/v1/settings/ui').status_code == 503
    assert client.patch('/api/v1/settings/ui', json={'expected_revision': 1, 'swipe_left': 'trash'}).status_code == 503


@pytest.mark.parametrize('pace', ['slow', 'normal', 'fast'])
def test_pace_is_revision_guarded(client, pace):
    response = client.patch('/api/v1/settings/ui', json={'expected_revision': 1, 'speech_pace': pace})
    assert response.status_code == 200 and response.json()['speech_pace'] == pace
    access.update.assert_awaited_once_with(1, speech_pace=pace)


def test_speech_pace_persists_across_browser_locale_confirm_and_reread(monkeypatch):
    stored = row()

    async def get_current():
        return stored

    async def apply_update(expected_revision, **changes):
        nonlocal stored
        if stored.revision != expected_revision:
            raise access.StaleRevision(expected_revision)
        data = {
            'swipe_left': stored.swipe_left,
            'swipe_right': stored.swipe_right,
            'locale': stored.locale,
            'speech_pace': stored.speech_pace,
            **changes,
        }
        stored = row(revision=stored.revision + 1, **data)
        return stored

    monkeypatch.setattr(access, 'get_or_create', AsyncMock(side_effect=get_current))
    monkeypatch.setattr(access, 'update', AsyncMock(side_effect=apply_update))
    monkeypatch.setattr(access, 'remember_browser_locale', AsyncMock())
    stateful_client = TestClient(build_app())

    patched = stateful_client.patch('/api/v1/settings/ui', json={'expected_revision': 1, 'locale': 'en', 'speech_pace': 'fast'})
    assert patched.status_code == 200
    assert patched.json()['locale'] == 'en'
    assert patched.json()['speech_pace'] == 'fast'
    assert patched.json()['revision'] == 2

    assert stateful_client.post('/api/v1/settings/ui/browser-locale', json={'locale': 'en'}).status_code == 200
    reread = stateful_client.get('/api/v1/settings/ui')
    assert reread.status_code == 200
    assert reread.json()['speech_pace'] == 'fast'
    assert reread.json()['locale'] == 'en'
    assert reread.json()['revision'] == 2


def test_browser_locale_confirmation_validates_hint(client):
    response = client.post('/api/v1/settings/ui/browser-locale', json={'locale': 'de'})
    assert response.status_code == 200 and response.json() == {'confirmed': True}
    access.remember_browser_locale.assert_awaited_once_with('de')
    assert client.post('/api/v1/settings/ui/browser-locale', json={'locale': 'fr'}).status_code == 422
