"""The supported output languages in the API."""

from typing import Literal

from db.accessors import ui_settings

Locale = Literal["de", "en"]
DEFAULT_LOCALE: Locale = "en"


async def resolve_locale(browser_hint: Locale | None = None) -> Locale:
    """Respect the installation setting even when a client has stale UI state."""
    settings = await ui_settings.get_or_create()
    if browser_hint is not None:
        await ui_settings.remember_browser_locale(browser_hint)
    if settings.locale == "de":
        return "de"
    if settings.locale == "en":
        return "en"
    return browser_hint or getattr(settings, "browser_locale", None) or DEFAULT_LOCALE
