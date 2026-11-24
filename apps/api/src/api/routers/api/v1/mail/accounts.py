"""Mail account management: list, connect (OAuth2 or IMAP app password), delete, sync.

Credentials are stored encrypted (``api.mail.crypto``) and never returned by any endpoint.
"""

import imaplib
import logging
from urllib.parse import urlencode
from datetime import datetime
from typing import Annotated, Any, Literal

import httpx
from db.accessors import chat_settings
from db.accessors import mail_insight as insight_access
from db.accessors import mail_action as action_access
from db.accessors import mail_account as account_access
from db.accessors import mail_credential as credential_access
from db.accessors import mail_message as message_access
from db.accessors import mail_job as job_access
from db.accessors import mail_sync_state as sync_state_access
from db.models.mail_account import MailAccount, gmail_studio
from env.mail import env as mail_env
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, model_validator, field_validator
from utils.mistral import MistralNotConfiguredError

from api.errors import refuse
from api.mail import action_capabilities, connectors, crypto, providers, secrets, transport
from api.mail.netguard import BlockedAddressError
from api.mail import jobs
from api.routers.api.v1.mail.summary import AccountSummaryOut, account_summary
from api.mail.state import color_indexes
from api.mail.service import AccountConflictError, check_imap_login, upsert_account

router = APIRouter()
logger = logging.getLogger(__name__)


class AccountOut(AccountSummaryOut):
    chat_eligibility: chat_settings.ChatEligibility | None = None
    auth_type: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    message_count: int


async def account_out(account: MailAccount, accounts: list[MailAccount], unread: dict[str, int]) -> AccountOut:
    sync_state = await sync_state_access.get_state(account.id)
    summary = await account_summary(account, color_indexes(accounts), unread, sync_state)
    return AccountOut(**summary.model_dump(), chat_eligibility=chat_settings.eligibility(account, sync_state), auth_type=account.auth_type, is_active=account.is_active,
                      created_at=account.created_at, updated_at=account.updated_at,
                      message_count=await message_access.count_for_account(account.id))


async def current_account_out(account: MailAccount) -> AccountOut:
    return await account_out(account, await account_access.list_accounts(), await message_access.count_unread(folder="INBOX"))


@router.post("/{account_id}/retry-provider-actions", operation_id="mail_account_retry_provider_actions",
             summary="Allow another Gmail action attempt after reconnecting in Studio")
async def retry_provider_actions(account_id: str) -> AccountOut:
    account = await account_access.get_account(account_id)
    if account is None:
        raise refuse(404, "account_not_found", "Das Konto ist nicht mehr da.")
    if not account.is_active or not gmail_studio(account) or not await sync_state_access.retry_connector_writes(account_id):
        raise action_capabilities.unsupported()
    return await current_account_out(account)


class AccountUpdateIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("display_name", mode="before")
    @classmethod
    def strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class ImapPresetOut(BaseModel):
    domain: str
    imap_host: str
    smtp_host: str


class AccountOptionsOut(BaseModel):
    gmail_connector: bool
    outlook_connector: bool
    google_oauth: bool
    microsoft_oauth: bool
    imap_presets: list[ImapPresetOut]


class ImapConnectRequest(BaseModel):
    email_address: str
    password: str = Field(
        default="",
        description="IMAP app password (e.g. iCloud/Yahoo app-specific password). Exactly one of password / password_secret.",
    )
    password_secret: str = Field(
        default="",
        description=(
            "Name of a Mistral workspace secret holding the app password (lowercase, e.g. "
            "'yahoo-app-password') — one secret per account. Only the reference is stored."
        ),
    )
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_starttls: bool = False

    @model_validator(mode="after")
    def _exactly_one_password(self) -> "ImapConnectRequest":
        if bool(self.password) == bool(self.password_secret):
            raise ValueError(
                "Genau eines von 'password' oder 'password_secret' angeben."
            )
        return self


class OAuthAuthorizeOut(BaseModel):
    authorization_url: str
    state: str


def mistral_not_configured() -> HTTPException:
    """The refusal for a deployment that has no Mistral credential of its own."""
    return refuse(
        503, "mistral_not_configured",
        "liam-e hat keinen Zugang zu Mistral Studio. Hinterleg für die App einen MISTRAL_API_KEY aus diesem Workspace, starte sie neu und versuch es dann noch mal.",
    )


def _redirect_base(request: Request) -> str:
    return (
        mail_env.mail_oauth_redirect_base_url or str(request.base_url).rstrip("/")
    ).rstrip("/")


@router.get(
    "", operation_id="mail_accounts_list", summary="List all connected mail accounts"
)
async def list_accounts() -> list[AccountOut]:
    """List all connected mail accounts with provider, auth type and e-mail address.

    Credentials are never part of the response.
    """
    accounts = await account_access.list_accounts()
    unread = await message_access.count_unread(folder="INBOX")
    return [await account_out(a, accounts, unread) for a in accounts]


@router.post(
    "/imap",
    operation_id="mail_account_connect_imap",
    status_code=201,
    summary="Connect an IMAP/SMTP account",
)
async def connect_imap(body: ImapConnectRequest) -> AccountOut:
    """Connect an IMAP/SMTP account with an app password and store its credentials encrypted.

    Exactly one of ``password`` or ``password_secret`` is required; ``password_secret`` names a
    workspace secret (for example ``yahoo-app-password``) whose value is resolved now and re-read
    at runtime, so only the reference is stored. Server hosts must be allow-listed and the IMAP
    login is checked before the account is created; answers 201 with the new account.
    An address already using another connection type answers 409 without changing credentials.
    Answers 503 when the app itself lacks what it needs (a Mistral credential that may read
    secrets, or the credential encryption key); no account is created then.
    """
    preset = providers.guess_imap_settings(body.email_address)
    imap_host = body.imap_host or (preset[0] if preset else "")
    imap_port = body.imap_port if body.imap_host else (preset[1] if preset else 993)
    smtp_host = body.smtp_host or (preset[2] if preset else "")
    smtp_port = body.smtp_port if body.smtp_host else (preset[3] if preset else 465)
    smtp_starttls = body.smtp_starttls or (preset[4] if preset else False)
    for host in (imap_host, smtp_host):
        try:
            await transport.check_mail_host(host)
        except transport.MailHostNotAllowedError as error:
            raise refuse(
                422, "host_not_allowed",
                f"Den Server {host} kennt liam-e nicht. Trag ihn in Mistral Studio unter MAIL_IMAP_ALLOWED_HOSTS ein und verbinde das Konto dann noch einmal.",
            ) from error
        except BlockedAddressError as error:
            raise refuse(
                422, "host_not_allowed",
                "Der Mailserver ist nicht öffentlich erreichbar. Prüfe die Serveradresse und verbinde das Konto dann noch einmal.",
            ) from error
    # Resolve a secret reference once now to fail fast; only the reference is stored.
    password = body.password
    secret_name = ""
    if body.password_secret:
        try:
            secret_name = secrets.validate_secret_name(body.password_secret)
            password = await secrets.resolve_workspace_secret(secret_name)
        except secrets.SecretNameNotAllowedError as error:
            raise refuse(
                422, "secret_not_allowed",
                "Der Name muss auf „-app-password“ enden, etwa „yahoo-app-password“, und darf nur Kleinbuchstaben, Ziffern und Bindestriche enthalten. Leg das Secret in Mistral Studio unter so einem Namen an.",
            ) from error
        except secrets.WorkspaceSecretNotFoundError as error:
            raise refuse(
                404, "secret_not_found",
                "Das Secret wurde nicht gefunden oder ist nicht lesbar. Prüfe den Namen und die Freigabe in Mistral Studio.",
            ) from error
        except MistralNotConfiguredError as error:
            raise mistral_not_configured() from error
        except secrets.WorkspaceSecretAccessDeniedError as error:
            raise refuse(
                503, "secret_access_denied",
                "Mistral Studio lässt liam-e die Secrets nicht lesen. Prüfe, ob der MISTRAL_API_KEY der App zu diesem Workspace gehört und noch gültig ist.",
            ) from error
        except httpx.HTTPError as error:
            raise refuse(
                502, "secret_unavailable",
                "Mistral Studio antwortet gerade nicht, das Secret ließ sich nicht lesen. Versuch es gleich noch mal.",
            ) from error
    try:
        await check_imap_login(
            email_address=body.email_address,
            password=password,
            host=imap_host,
            port=imap_port,
        )
    except (imaplib.IMAP4.error, OSError, BlockedAddressError, transport.MailHostNotAllowedError) as error:
        raise refuse(
            502, "login_failed",
            "Die Anmeldung beim Mailserver hat nicht geklappt. Prüfe die Serveradresse und das App-Passwort.",
        ) from error
    payload: dict[str, Any] = {
        "imap_host": imap_host,
        "imap_port": imap_port,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_starttls": smtp_starttls,
    }
    if body.password_secret:
        payload["password_secret"] = secret_name
    else:
        payload["password"] = body.password
    # Encrypt before creating the account, so a missing key leaves no account without credentials.
    try:
        encrypted_payload = crypto.encrypt_payload(payload)
    except crypto.TokenEncryptionNotConfiguredError as error:
        raise refuse(
            503, "encryption_not_configured",
            "liam-e kann das Konto nicht verschlüsselt speichern, weil der Schlüssel fehlt. Leg in Mistral Studio das Secret „mail_token_encryption_key“ an, falls es fehlt, starte die App neu und verbinde das Konto dann noch einmal.",
        ) from error
    try:
        account = await upsert_account(
            provider=providers.IMAP,
            auth_type="imap_password",
            email_address=body.email_address,
        )
    except AccountConflictError as error:
        raise refuse(
            409, "account_exists",
            "Diese Adresse ist schon über einen anderen Weg verbunden. Entferne das Konto zuerst, wenn du die Verbindung wechseln willst.",
        ) from error
    await credential_access.store_credential(
        account_id=account.id,
        kind="imap_password",
        encrypted_payload=encrypted_payload,
    )
    return await current_account_out(account)


@router.get(
    "/oauth/{provider}/authorize",
    operation_id="mail_account_oauth_authorize",
    summary="Start the OAuth2 flow for google or microsoft; open the returned URL in the browser",
)
async def oauth_authorize(
    provider: Literal["google", "microsoft"], request: Request, account_id: str | None = None
) -> OAuthAuthorizeOut:
    """Start the OAuth2 flow for ``google`` or ``microsoft`` and return the URL to open in a browser.

    The response carries the ``authorization_url`` plus the signed ``state`` that the provider
    echoes back to the callback; nothing is redirected here. Answers 503 when that provider's
    OAuth client is not configured.
    """
    if account_id is not None:
        account = await account_access.get_account(account_id)
        if account is None:
            raise refuse(404, "account_not_found", "Das Konto gibt es nicht mehr.")
        if account.auth_type != "oauth2" or account.provider != provider:
            raise refuse(422, "reconnect_unsupported", "Dieses Konto lässt sich über diese Anmeldung nicht neu verbinden. Entferne es und verbinde es danach neu.")
    if not providers.oauth_configured(provider):
        raise refuse(503, "oauth_not_configured", "Die Anmeldung ist nicht eingerichtet. Prüfe die Einstellungen in Mistral Studio.")
    redirect_uri = (
        f"{_redirect_base(request)}/api/v1/mail/accounts/oauth/{provider}/callback"
    )
    payload = {"provider": provider, "redirect_uri": redirect_uri}
    if account_id is not None:
        payload["account_id"] = account_id
    state = providers.sign_state(payload)
    return OAuthAuthorizeOut(
        authorization_url=providers.authorization_url(
            provider, redirect_uri=redirect_uri, state=state
        ),
        state=state,
    )


@router.get(
    "/oauth/{provider}/callback",
    operation_id="mail_account_oauth_callback",
    summary="Complete OAuth and redirect to accounts",
    response_class=RedirectResponse,
    status_code=303,
    responses={303: {"description": "Redirect to accounts"}},
)
async def oauth_callback(provider: Literal["google", "microsoft"], code: str | None = None,
                         state: str | None = None, error: str | None = None) -> RedirectResponse:
    """Complete OAuth and redirect without exposing provider details."""
    def redirect(key: str, value: str) -> RedirectResponse:
        return RedirectResponse("/accounts?" + urlencode({key: value}), status_code=303)

    if error is not None:
        return redirect("error", "oauth_denied")
    try:
        payload = providers.verify_state(state or "")
        if payload.get("provider") != provider:
            raise ValueError("provider mismatch")
    except (ValueError, TypeError):
        return redirect("error", "oauth_state_invalid")
    try:
        if not code:
            raise ValueError("missing code")
        tokens = await providers.exchange_code(provider, code=code, redirect_uri=payload["redirect_uri"])
        email_address = await providers.fetch_account_email(provider, access_token=tokens["access_token"])
        if not email_address:
            raise ValueError("missing address")
    except Exception as exc:
        logger.warning("OAuth exchange failed: %s", type(exc).__name__)
        return redirect("error", "oauth_exchange_failed")
    account_id = payload.get("account_id")
    if account_id is not None:
        account = await account_access.get_account(account_id)
        if account is None or account.provider != provider or account.auth_type != "oauth2":
            return redirect("error", "oauth_state_invalid")
        if account.email_address.lower() != email_address.lower():
            return redirect("error", "oauth_account_mismatch")
    else:
        try:
            account = await upsert_account(provider=provider, auth_type="oauth2", email_address=email_address)
        except AccountConflictError:
            return redirect("error", "account_exists")
    await credential_access.store_credential(account_id=account.id, kind="oauth2_tokens", encrypted_payload=crypto.encrypt_payload(tokens))
    await jobs.enqueue_sync(account.id)
    return redirect("connected", account.id)


@router.get("/options", operation_id="mail_account_options")
async def account_options() -> AccountOptionsOut:
    """Return available account connection methods and IMAP presets."""
    try:
        names = {item["name"] for item in await connectors.list_connectors()}
    except Exception as exc:
        logger.warning("Connector listing failed: %s", type(exc).__name__)
        names = set()
    gmail = bool(names & {"gmail", "google_mail"})
    return AccountOptionsOut(gmail_connector=gmail, outlook_connector="outlook" in names,
        google_oauth=providers.oauth_configured("google") and not gmail,
        microsoft_oauth=providers.oauth_configured("microsoft"),
        imap_presets=[ImapPresetOut(domain=domain, imap_host=preset[0], smtp_host=preset[2])
                      for domain, preset in sorted(providers.IMAP_PRESETS.items())])


@router.patch("/{account_id}", operation_id="mail_account_update")
async def update_account(account_id: str, body: AccountUpdateIn) -> AccountOut:
    """Rename an existing account."""
    account = await account_access.update_display_name(account_id, body.display_name)
    if account is None:
        raise refuse(404, "account_not_found", "Das Konto gibt es nicht mehr.")
    return await current_account_out(account)


@router.delete(
    "/{account_id}",
    operation_id="mail_account_delete",
    status_code=204,
    summary="Delete an account and all its data",
)
async def delete_account(account_id: str) -> None:
    """Delete a mail account together with its credentials, synced messages and sync state.

    ``account_id`` selects the account; the data cannot be restored. Answers 204 on success and
    404 for an unknown account.
    """
    deleted = await account_access.delete_account(account_id)
    if not deleted:
        raise refuse(404, "account_not_found", "Das Konto gibt es nicht mehr.")
    await insight_access.delete_for_account(account_id)
    await message_access.delete_for_account(account_id)
    await action_access.delete_for_account(account_id)
    await job_access.delete_for_account(account_id)
    await credential_access.delete_for_account(account_id)
    await sync_state_access.delete_for_account(account_id)


@router.post(
    "/{account_id}/sync",
    operation_id="mail_account_sync",
    status_code=202,
    summary="Schedule an account sync",
)
async def sync_account(account_id: str) -> AccountSummaryOut:
    """Schedule a sync for an existing account, deduplicating open jobs."""
    account = await account_access.get_account(account_id)
    if account is None:
        raise refuse(404, "account_not_found", "Das Konto gibt es nicht mehr.")
    await jobs.enqueue_sync(account_id)
    accounts = await account_access.list_accounts()
    unread = await message_access.count_unread(folder="INBOX")
    return await account_summary(account, color_indexes(accounts), unread, await sync_state_access.get_state(account.id))
