"""Optimistic index updates and provider-backed, single-use undo."""

import json
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from db.accessors import mail_account as account_access
from db.accessors import mail_action as action_access
from db.accessors import mail_message as message_access
from db.models.mail_action import MailAction
from db.models.mail_account import MailAccount, gmail_studio
from fastapi import HTTPException
from db.models.mail_message import IMAP_RETIRED, MailMessage
from sqlalchemy.exc import IntegrityError

from api.errors import refuse
from api.mail import action_capabilities, connector_throttle, connector_write_results as write_result, connectors

ActionKind = Literal["archive", "trash", "spam", "mark_read", "mark_unread", "restore"]
FOLDER_ACTION_SOURCES = {
    "archive": ("INBOX",),
    "trash": ("INBOX", "ARCHIVE"),
    "spam": ("INBOX", "ARCHIVE"),
    "restore": ("ARCHIVE", "TRASH"),
}
FOLDER_ACTION_DESTINATIONS = {"archive": "ARCHIVE", "trash": "TRASH", "spam": "SPAM", "restore": "INBOX"}
logger = logging.getLogger(__name__)


class ActionFailedError(RuntimeError):
    """The provider could not complete an action."""


class ActionPartialError(ActionFailedError):
    """The action failed, but the observed folder was reconciled in the index."""

    def __init__(self, folder: str, error_code: str | None = None, *, reconciled: bool = False, retryable: bool = False):
        super().__init__("The provider folder was reconciled after an incomplete action")
        self.folder = folder
        self.error_code = error_code
        self.reconciled = reconciled
        self.retryable = retryable
        self.outcome: Literal["reconciled", "partial"] = "reconciled" if reconciled else "partial"


class ActionUnsupportedError(RuntimeError):
    """The action is not supported for this message or provider."""


@dataclass
class UndoResult:
    restored: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    error_code: str | None = None
    unsupported: list[str] = field(default_factory=list)
    retryable: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)


async def write_connector_labels(account, message: MailMessage, *, added: list[str], removed: list[str]) -> None:
    """Keep connector writes inside the guarded action boundary."""
    from api.mail.connector_mailbox import inbox_only

    if "TRASH" in added or "TRASH" in removed:
        await connectors.move_trash_labels(account, message, added, removed)
        return
    target = {"target": message} if inbox_only(account) else {}
    for tool, labels in (("label_message", added), ("unlabel_message", removed)):
        if labels:
            await connectors.call_write_tool(account, tool, {"messageId": message.provider_uid, "labelIds": labels}, **target)


async def _write_provider_state(message: MailMessage, updates: dict, *, folder: str) -> None:
    """Merge a concurrent sync copy once; provider success must never be rolled back."""
    try:
        try:
            await message_access.set_state(message.id, **updates)
        except IntegrityError:
            if "provider_uid" not in updates or not await message_access.delete_duplicate(
                message.account_id, folder, updates["provider_uid"],
                keep_id=message.id, rfc_message_id=message.rfc_message_id,
            ):
                raise
            await message_access.set_state(message.id, **updates)
    except Exception as error:
        logger.warning("%s", type(error).__name__)
        raise


async def _reconcile_connector_labels(message: MailMessage, labels: list[str] | None) -> str | None:
    if labels is None:
        return None
    folder_labels = ("TRASH", "SPAM", "INBOX")
    observed = set(labels)
    current = set(json.loads(message.labels_json)) | {message.folder}
    folder = connectors.observed_folder(observed)
    if message.folder == folder and current.intersection(folder_labels) == observed.intersection(folder_labels):
        return None
    await message_access.commit_trash_move(message, {
        "folder": folder, "labels_json": json.dumps(sorted(observed)),
        "is_read": "UNREAD" not in observed, "is_flagged": "STARRED" in observed,
    })
    return folder


async def require_bulk_support(message_ids: list[str], kind: ActionKind) -> None:
    for account in await account_access.list_accounts():
        try:
            await action_capabilities.require_action(account, kind)
        except HTTPException:
            if await message_access.has_messages_from_accounts(message_ids, [account.id]):
                raise


def _log_action_failure(account: MailAccount, kind: ActionKind, code: str) -> None:
    provider = "connector" if account.auth_type == connectors.CONNECTOR else account.provider
    if provider not in {"connector", "google", "microsoft", "imap"}:
        provider = "unknown"
    logger.warning("Mail action failed action=%s provider=%s code=%s", kind, provider, code)


async def apply_action(message_id: str, kind: ActionKind) -> MailAction:
    message = await message_access.get_message(message_id)
    if message is None:
        raise refuse(404, "message_not_found", "Die Mail ist nicht mehr da.")
    account = await account_access.get_account(message.account_id)
    if account is None:
        raise ActionUnsupportedError()
    if gmail_studio(account):
        async with connector_throttle.action(account.id):
            latest = await message_access.get_message(message_id)
            if latest is None or latest.account_id != account.id:
                raise refuse(404, "message_not_found", "Die Mail ist nicht mehr da.")
            return await _apply_action(latest, account, kind)
    return await _apply_action(message, account, kind)


async def _apply_action(message: MailMessage, account: MailAccount, kind: ActionKind) -> MailAction:
    from api.mail import action_backends

    message_id = message.id
    await action_capabilities.require_action(account, kind)
    if message.folder in {"IMAP_DELETED", IMAP_RETIRED}:
        raise ActionUnsupportedError()
    retry_partial_trash = (kind == "trash" and gmail_studio(account) and message.folder == "TRASH"
                           and {"INBOX", "TRASH"} <= set(json.loads(message.labels_json)))
    if kind in FOLDER_ACTION_SOURCES and message.folder not in FOLDER_ACTION_SOURCES[kind] and not retry_partial_trash:
        raise ActionUnsupportedError()
    backend = action_backends.backend_for(account)
    previous = {key: getattr(message, key) for key in ("folder", "is_read", "provider_uid", "labels_json")}
    changes = {"folder": FOLDER_ACTION_DESTINATIONS[kind]} if kind in FOLDER_ACTION_SOURCES else {"is_read": kind == "mark_read"}
    if gmail_studio(account) or kind == "trash" or (kind == "restore" and message.folder == "TRASH"):
        try:
            updates = await backend.apply(message, kind)
        except (ActionUnsupportedError, HTTPException):
            raise
        except connector_throttle.RateLimited as error:
            _log_action_failure(account, kind, connector_throttle.CODE)
            folder = await _reconcile_connector_labels(message, error.labels)
            if folder is not None:
                raise ActionPartialError(folder, connector_throttle.CODE, retryable=error.retryable) from None
            raise
        except write_result.PermissionMissing as error:
            folder = await _reconcile_connector_labels(message, error.labels)
            if folder is not None:
                raise ActionPartialError(folder, error.code, reconciled=error.provider_unchanged) from None
            raise
        except connectors.LabelChangeUnconfirmed as error:
            folder = await _reconcile_connector_labels(message, error.labels)
            if folder is not None:
                raise ActionPartialError(folder, reconciled=error.provider_unchanged) from None
            _log_action_failure(account, kind, type(error).__name__)
            raise ActionFailedError() from None
        except Exception as error:
            _log_action_failure(account, kind, type(error).__name__)
            raise ActionFailedError() from None
        previous.update(updates.pop("connector_previous", {}))
        action = MailAction(message_id=message.id, account_id=account.id, kind=kind, previous_json=json.dumps(previous))
        await message_access.commit_trash_move(message, changes | updates, action=action)
        return action
    optimistic_written = False
    try:
        await message_access.set_state(message_id, **changes)
        optimistic_written = True
        updates = await backend.apply(message, kind)
    except Exception as error:
        if optimistic_written:
            await message_access.set_state(message_id, **previous)
        if isinstance(error, (ActionUnsupportedError, write_result.PermissionMissing)):
            raise
        _log_action_failure(account, kind, type(error).__name__)
        raise ActionFailedError() from None
    await _write_provider_state(message, updates, folder=changes.get("folder", message.folder))
    try:
        return await action_access.record(message_id=message.id, account_id=message.account_id, kind=kind, previous_json=json.dumps(previous))
    except Exception as error:
        _log_action_failure(account, kind, type(error).__name__)
        raise


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def undo_actions(action_ids: list[str], *, now: datetime) -> UndoResult:
    result = UndoResult()
    rows = await action_access.get_many(action_ids)
    single = len(set(action_ids)) == 1
    known = {row.id for row in rows}
    result.skipped.extend(key for key in dict.fromkeys(action_ids) if key not in known)
    for action in sorted(rows, key=lambda row: (_aware(row.created_at), row.id), reverse=True):
        account = await account_access.get_account(action.account_id)
        serialized = account is not None and gmail_studio(account)
        async with (connector_throttle.action(action.account_id) if serialized else nullcontext()):
            if serialized:
                fresh = await action_access.get_many([action.id])
                if not fresh:
                    result.skipped.append(action.id)
                    continue
                action, = fresh
                account = await account_access.get_account(action.account_id)
            # Keep the single-token unsupported response. Groups resolve capabilities per token.
            if single and account is not None and action.undone_at is None:
                try:
                    await action_capabilities.require_action(account, action.kind)
                except HTTPException as error:
                    if error.status_code != 403:
                        raise
            await _undo_action(action, account, result, now)
    if result.unsupported and not result.restored and len(result.unsupported) == len(result.failed):
        result.error_code = action_capabilities.ERROR_CODE
    return result


async def _undo_action(action: MailAction, account: MailAccount | None, result: UndoResult, now: datetime) -> None:
    from api.mail import action_backends

    message = await message_access.get_message(action.message_id)
    if action.undone_at is not None or message is None:
        result.skipped.append(action.id)
        return
    if _aware(action.created_at) < _aware(now) - timedelta(hours=24):
        result.expired.append(action.id)
        return
    latest = await action_access.latest_open(action.message_id)
    if latest is None or latest.id != action.id:
        result.skipped.append(action.id)
        return
    if not await action_access.mark_undone(action.id, now):
        result.skipped.append(action.id)
        return
    try:
        if account is None:
            raise ActionUnsupportedError()
        await action_capabilities.require_action(account, action.kind)
        previous = json.loads(action.previous_json)
        # This provider evidence belongs to the action, not the message being restored.
        previous.pop("trash_inbox_uid", None)
        previous.pop("imap_source_uidvalidity", None)
        backend = action_backends.backend_for(account)
        if gmail_studio(account) and isinstance(backend, action_backends.LabelBackend):
            updates = await backend.revert(message, previous, kind=action.kind)
        else:
            updates = await backend.revert(message, previous)
    except (connectors.LabelChangeUnconfirmed, write_result.PermissionMissing, connector_throttle.RateLimited) as error:
        if isinstance(error, write_result.PermissionMissing):
            result.error_code = error.code
        elif isinstance(error, connector_throttle.RateLimited):
            result.error_code = result.error_code or connector_throttle.CODE
            (result.retryable if error.retryable else result.unconfirmed).append(action.id)
            assert account is not None
            _log_action_failure(account, action.kind, connector_throttle.CODE)
        try:
            await _reconcile_connector_labels(message, error.labels)
        finally:
            await action_access.release(action.id)
        result.failed.append(action.id)
    except Exception as error:
        if isinstance(error, HTTPException) and isinstance(error.detail, dict):
            code = error.detail.get("code")
            if code == write_result.PERMISSION_MISSING:
                result.error_code = code
            elif error.status_code == 409 and code == action_capabilities.ERROR_CODE:
                result.unsupported.append(action.id)
        logger.warning("%s", type(error).__name__)
        await action_access.release(action.id)
        result.failed.append(action.id)
    else:
        if gmail_studio(account) or action.kind == "trash" or (action.kind == "restore" and previous["folder"] == "TRASH"):
            await message_access.commit_trash_move(message, previous | updates, undo_id=action.id)
        else:
            await _write_provider_state(message, previous | updates, folder=previous["folder"])
        result.restored.append(action.id)
