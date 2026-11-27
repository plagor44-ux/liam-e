"""Shared fixtures for the generated API tests."""

import pytest
from api.routers.api.internal.health import draining
from env.telemetry import env as telemetry_env
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mistralai_capabilities.fastapi.hooks import FastAPIHooks

telemetry_env.telemetry_enabled = False


def build_app(
    *,
    hooks: FastAPIHooks | None = None,
    routers: str = "api.routers",
    prefix: str = "",
) -> FastAPI:
    # Importing api.main constructs its module-level app, so do it only after telemetry is disabled.
    from api.main import create_app

    app = create_app(hooks=hooks if hooks is not None else FastAPIHooks.empty())
    if routers != "api.routers" or prefix:
        from mistralai_capabilities.fastapi.routing import create_api_router

        app.router.routes.clear()
        app.include_router(create_api_router(routers, prefix=prefix))
    app.dependency_overrides[draining] = lambda: False
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app())


@pytest.fixture
def fake_store(monkeypatch):
    from support.fake_store import FakeStore

    return FakeStore(monkeypatch)


@pytest.fixture(autouse=True)
def forget_reread_waits():
    """The re-read backoff lives in the process (api.mail.sync); no test inherits another's waits."""
    from api.mail import sync

    sync._reread_backoff.clear()
    yield
    sync._reread_backoff.clear()


@pytest.fixture(autouse=True)
def no_extraction_database(monkeypatch):
    """Chat turns schedule a detached suggestion extraction; unit tests never reach a database.

    Tests of the extractor replace the claim themselves.
    """
    from unittest.mock import AsyncMock
    from api.preferences import extraction

    monkeypatch.setattr(extraction.records, "claim", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def ui_locale_in_browser_mode(monkeypatch):
    """API unit tests default to browser mode without creating a configuration row."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from db.accessors import ui_settings

    preference = AsyncMock(return_value=SimpleNamespace(locale="browser", browser_locale=None, speech_pace="normal", voice_model="chat"))
    monkeypatch.setattr(ui_settings, "get_or_create", preference)
    monkeypatch.setattr(ui_settings, "remember_browser_locale", AsyncMock())
    return preference


@pytest.fixture(autouse=True)
def default_chat_model_app_default(monkeypatch):
    """API unit tests default to the built-in app model without creating a configuration row."""
    from unittest.mock import AsyncMock

    from db.accessors import chat_model_settings
    from db.models.chat_model_settings import ChatModelSettings

    setting = AsyncMock(return_value=ChatModelSettings())
    monkeypatch.setattr(chat_model_settings, "get", setting)
    return setting


@pytest.fixture(autouse=True)
def default_agent_access_off(monkeypatch):
    """API unit tests default to outside access off without touching a database."""
    from unittest.mock import AsyncMock

    from db.accessors import agent_access

    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=False))


@pytest.fixture
def anyio_backend() -> str:
    """Run `@pytest.mark.anyio` tests on asyncio only (the app's event loop)."""
    return "asyncio"
