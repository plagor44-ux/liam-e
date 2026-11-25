"""Access to the Mistral Studio connectors of this workspace (gmail, outlook, ...).

A connector keeps its own OAuth credentials inside Mistral, so providers that exist as Studio
connectors need no OAuth client of our own: the app calls their tools through the Connectors REST
API with its own credential (``MISTRAL_API_KEY``, or the worker's service-account token file).
Mail accounts connected this way have ``auth_type="mistral_connector"`` and store no credential row.

The SDK's ``beta.connectors.call_tool`` wrapper is deprecated, so this module speaks the REST API
directly: ``GET /v1/connectors``, ``GET /v1/connectors/{name}/tools``,
``POST /v1/connectors/{name}/tools/{tool}/call``.
"""

from __future__ import annotations

import json
import logging
from asyncio import sleep, timeout
from collections.abc import Awaitable, Callable, Collection
from contextlib import nullcontext
from contextvars import ContextVar
from typing import Any
from urllib.parse import quote

import httpx
from db.accessors import mail_job, mail_sync_state
from utils.mistral import mistral_api_url, mistral_app_token

from api.mail import action_capabilities, connector_throttle, connector_write_results as write_result


logger = logging.getLogger(__name__)
_TOOL_TIMEOUT_SECONDS = 120

# auth_type of accounts that live on a Mistral Studio connector.
CONNECTOR = "mistral_connector"
# Bound operations keep using this credential even when Studio changes the workspace default.
mailbox_credential: ContextVar[str | None] = ContextVar("mailbox_credential", default=None)

# Connector names whose tool sets are mail tools (search_threads / get_thread / send_email / ...).
MAIL_CONNECTOR_NAMES = ("gmail", "outlook", "google_mail")


def _headers() -> dict[str, str]:
    # Read per call, so a rotated token file is picked up (see utils.mistral.mistral_app_token).
    return {
        "Authorization": f"Bearer {mistral_app_token()}",
        "Accept": "application/json",
    }


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(
            f"{mistral_api_url()}{path}", headers=_headers(), params=params
        )
        response.raise_for_status()
        return response.json()


async def _post(path: str, payload: dict[str, Any], timeout: float = _TOOL_TIMEOUT_SECONDS) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{mistral_api_url()}{path}", headers=_headers(), json=payload
        )
        response.raise_for_status()
        return response.json()


async def list_connectors() -> list[dict[str, Any]]:
    """The workspace's connectors as ``[{name, id}]``."""
    data = await _get("/v1/connectors", {"page_size": 100})
    return [
        {"name": item.get("name", ""), "id": item.get("id", "")}
        for item in data.get("items", [])
    ]


async def list_tools(connector: str) -> list[dict[str, Any]]:
    """A connector's tools as ``[{name, description, input_schema}]``."""
    ref = quote(connector, safe="")
    data = await _get(f"/v1/connectors/{ref}/tools", {"page_size": 100})
    tools = data if isinstance(data, list) else data.get("items", [])
    return [
        {
            "name": tool.get("name", ""),
            "description": tool.get("description") or "",
            "input_schema": tool.get("inputSchema") or tool.get("input_schema") or {},
        }
        for tool in tools
    ]


READ_TOOLS = frozenset({"search_threads", "get_thread", "list_labels", "list_drafts"})
WRITE_TOOLS = frozenset({"send_email", "label_message", "unlabel_message"})


class ToolNotAllowedError(ValueError):
    """Raised before dispatch when a tool is outside the requested capability."""


class LabelChangeUnconfirmed(ValueError):
    """A failed mutation can still leave a message-specific provider state."""

    def __init__(self, labels: list[str] | None, *, provider_unchanged: bool = False):
        super().__init__("Connector label change not confirmed")
        self.labels = labels
        self.provider_unchanged = provider_unchanged


async def _call_tool(
    connector: str, tool: str, arguments: dict[str, Any], timeout: float = _TOOL_TIMEOUT_SECONDS, *,
    before_retry: Callable[[], Awaitable[None]] | None = None,
) -> str:
    """Call one connector tool; returns the joined text content of the MCP-style result."""
    ref = quote(connector, safe="")
    path = f"/v1/connectors/{ref}/tools/{quote(tool, safe='')}/call"
    credential = mailbox_credential.get()
    if credential is not None:
        path += f"?credentials_name={quote(credential, safe='')}"
    writing = tool in WRITE_TOOLS or tool in write_result.LABEL_TOOLS

    async def dispatch():
        try:
            return await _post(path, {"arguments": arguments},
                               **({"timeout": timeout} if timeout != _TOOL_TIMEOUT_SECONDS else {}))
        except httpx.HTTPStatusError as error:
            if writing and error.response.status_code == 403:
                raise write_result.PermissionMissing() from None
            raise

    try:
        data = await connector_throttle.tool(dispatch, write=writing, before_retry=before_retry)
        if writing:
            write_result.confirm(tool, arguments, data)
    except write_result.WriteRefused as error:
        if not isinstance(error, write_result.WriteUnconfirmed):
            logger.warning("%s", error.code)
        raise
    return "\n".join(write_result.text_parts(data))


async def call_read_tool(connector: str, tool: str, arguments: dict[str, Any]) -> str:
    """Call an explicitly allowed read-only tool."""
    if tool not in READ_TOOLS:
        raise ToolNotAllowedError("Tool is not a permitted read tool")
    return await _call_tool(connector, tool, arguments)


async def call_read_tool_json(connector: str, tool: str, arguments: dict[str, Any]) -> Any:
    """Call an allowed read tool and parse its JSON text result."""
    return json.loads(await call_read_tool(connector, tool, arguments))


async def call_write_tool(account, tool: str, arguments: dict[str, Any], *, target=None) -> str:
    """Call an allowed mutation; reserved for send and confirmed mail actions."""
    if tool not in WRITE_TOOLS:
        raise ToolNotAllowedError("Tool is not a permitted write tool")
    from api.mail.connector_mailbox import bound_inbox_message, bound_mailbox, inbox_only
    if tool in {"label_message", "unlabel_message"}:
        await action_capabilities.require_action(account, "label")
    if target is not None and tool in {"label_message", "unlabel_message"} and inbox_only(account):
        if arguments.get("messageId") != target.provider_uid:
            raise ToolNotAllowedError("Label target does not match the verified message")
        guard = bound_inbox_message(account, target)
    else:
        guard = bound_mailbox(account)
    async with guard:
        return await _call_tool(account.provider, tool, arguments)


async def move_trash_labels(account, message, added: list[str], removed: list[str]) -> None:
    """Both label mutations and their readbacks must use one proven mailbox."""
    from api.mail.connector_mailbox import bound_inbox_message, bound_mailbox, inbox_only

    await action_capabilities.require_action(account, "trash")
    if message.account_id != account.id:
        raise ToolNotAllowedError("Mail does not belong to this account")
    if not message.thread_id or not message.provider_uid:
        raise ToolNotAllowedError("Confirming a connector move requires both thread and message IDs")
    present: set[str] = set()
    absent: set[str] = set()
    binding = None
    for tool, change, expected in (("label_message", added, present), ("unlabel_message", removed, absent)):
        if not change:
            continue
        guard = bound_inbox_message(account, message) if inbox_only(account) else bound_mailbox(account)
        async with guard as proof:
            # Reprove the target before each write. A change of the mailbox between
            # the two calls must not send half the move to a different account.
            selected = proof[0] if proof is not None else mailbox_credential.get()
            if binding is not None and selected != binding:
                raise ValueError("Connector mailbox changed during move")
            binding = selected
            try:
                await _call_tool(account.provider, tool, {"messageId": message.provider_uid, "labelIds": change})
            except write_result.WriteUnconfirmed:
                # The current point read can prove a write with an incomplete receipt.
                pass
            except (ValueError, httpx.HTTPError) as error:
                if not (present or absent):
                    raise
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
                    raise
                try:
                    labels = await thread_labels(account.provider, message.thread_id, {message.provider_uid}, minimal_only=True)
                except (ValueError, httpx.HTTPError):
                    raise error from None
                if isinstance(error, write_result.PermissionMissing):
                    raise write_result.PermissionMissing(labels.get(message.provider_uid)) from None
                raise LabelChangeUnconfirmed(labels.get(message.provider_uid)) from None
            # HTTP 200 can contain a text diagnostic. Confirm TRASH before removing INBOX.
            expected.update(change)
            await _confirm_label_change(account.provider, message.thread_id, message.provider_uid, present, absent)


def observed_folder(labels: Collection[str]) -> str:
    return next((folder for folder in ("TRASH", "SPAM", "INBOX") if folder in labels), "ARCHIVE")


async def change_gmail_labels(
    account, message, added: list[str], removed: list[str], *, action: str, destination: str | None = None,
    source_folders: Collection[str] | None = None, read_status: bool | None = None, folder_undo: bool = False,
) -> tuple[list[str], list[str]]:
    """Confirm only the verified Gmail operations on the same named binding and exact delivery."""
    from api.mail.connector_mailbox import MailboxUnavailable, bound_mailbox

    allowed = {"archive": {"INBOX"}, "trash": {"INBOX", "TRASH"},
               "restore": {"INBOX", "TRASH", "UNREAD"}, "mark_read": {"UNREAD"}, "mark_unread": {"UNREAD"}}
    if (action not in allowed or not set(added + removed) <= allowed[action]
            or message.account_id != account.id or message.unverified_connector
            or not message.thread_id or not message.provider_uid):
        raise action_capabilities.unsupported()
    state = await action_capabilities.require_action(account, action)
    evidence = state.write_evidence() if state else None
    if state is None or evidence is None:
        raise MailboxUnavailable()
    proof = evidence[:2]
    provider, thread, uid = account.provider, message.thread_id, message.provider_uid
    present: set[str] = set()
    absent: set[str] = set()
    folder_labels = {"INBOX", "TRASH"}
    destination_labels = folder_labels.intersection({destination}) if destination is not None else None

    async def verify_retry_binding(expected_labels: set[str], changing: list[str]) -> None:
        latest = await mail_sync_state.get_state(account.id)
        latest_proof = latest.write_evidence() if latest else None
        if latest is None or latest_proof is None or latest_proof[:2] != proof:
            raise MailboxUnavailable()
        if latest_proof[2] == "blocked":
            raise write_result.PermissionMissing()
        async with bound_mailbox(account, cursor=latest.connector_cursor,
                                 generation=latest.connector_generation, observed=latest):
            observed = (await thread_labels(provider, thread, {uid}, minimal_only=True)).get(uid)
        relevant = folder_labels | {"SPAM"} | set(changing)
        if observed is None or set(observed).intersection(relevant) != expected_labels.intersection(relevant):
            raise LabelChangeUnconfirmed(observed)

    async with bound_mailbox(account, cursor=state.connector_cursor, generation=state.connector_generation, observed=state):
        initial_labels = (await thread_labels(provider, thread, {uid}, minimal_only=True)).get(uid)
        if initial_labels is None:
            raise LabelChangeUnconfirmed(None)
        undo_snapshot = initial_labels
        if source_folders is not None and destination_labels is not None:
            if "SPAM" in initial_labels:
                raise LabelChangeUnconfirmed(initial_labels, provider_unchanged=True)
            current = folder_labels.intersection(initial_labels)
            if folder_undo:
                source_labels = folder_labels.intersection(source_folders)
                if current == destination_labels:
                    return initial_labels, initial_labels
                # A retry may see the destination label added before the source label was removed.
                if current not in (source_labels, source_labels | destination_labels):
                    raise LabelChangeUnconfirmed(initial_labels, provider_unchanged=True)
                added, removed = sorted(destination_labels - current), sorted(current - destination_labels)
            else:
                live_folder = observed_folder(initial_labels)
                if action == "trash" and message.folder == "TRASH":
                    if current != folder_labels:
                        # An out-of-date retry must not start a new provider move.
                        raise LabelChangeUnconfirmed(initial_labels, provider_unchanged=True)
                    # Complete only the missing removal. A subsequent Undo restores the Inbox.
                    undo_snapshot = [label for label in initial_labels if label != "TRASH"]
                    live_folder = observed_folder(undo_snapshot)
                    added, removed = [], ["INBOX"]
                elif destination == "INBOX":
                    # Restore changes only folder labels. The pinned read supplies the source.
                    completing_partial = current == folder_labels and folder_labels.issubset(json.loads(message.labels_json))
                    added = [] if completing_partial else ["INBOX"]
                    removed = ["TRASH"] if live_folder == "TRASH" else []
                    if completing_partial:
                        undo_snapshot = [label for label in initial_labels if label != "INBOX"]
                if live_folder not in source_folders:
                    raise LabelChangeUnconfirmed(initial_labels, provider_unchanged=True)
        if read_status is not None:
            # Undo derives its UNREAD change from Gmail, even if sync already replayed the saved flag.
            added = ["UNREAD"] if not read_status and "UNREAD" not in initial_labels else []
            removed = ["UNREAD"] if read_status and "UNREAD" in initial_labels else []

        labels = initial_labels
        step_has_evidence = False
        provider_changed = False
        for tool, changes, expected in (("label_message", added, present), ("unlabel_message", removed, absent)):
            has_confirmed_step = bool(present or absent)
            # A confirmed first write can implicitly finish the whole move. Keep the
            # first write's receipt checks even if its requested label is already set.
            if provider_changed and has_confirmed_step:
                adding = tool == "label_message"
                changes = [label for label in changes if (label in labels) != adding]
            if not changes:
                continue
            if step_has_evidence:
                await mail_sync_state.record_connector_write(account.id, proof, outcome="confirmed")
            current = await mail_sync_state.get_state(account.id)
            current_proof = current.write_evidence() if current else None
            if current is None or current_proof is None or current_proof[:2] != proof:
                raise MailboxUnavailable()
            if current_proof[2] == "blocked":
                raise write_result.PermissionMissing(labels, provider_unchanged=not provider_changed)

            # The first write uses the anchor just checked above. Later writes recheck it
            # because Studio can reconnect the same credential name to another mailbox.
            binding = (bound_mailbox(account, cursor=current.connector_cursor,
                                     generation=current.connector_generation, observed=current)
                       if has_confirmed_step else nullcontext())
            labels_before = set(labels)
            has_receipt = True
            dispatched = False
            try:
                async with binding:
                    try:
                        await _call_tool(provider, tool, {"messageId": uid, "labelIds": changes},
                                         before_retry=lambda: verify_retry_binding(labels_before, changes))
                    except write_result.WriteUnconfirmed:
                        has_receipt = False
                    dispatched = True
                    expected.update(changes)
                    labels = await _confirm_label_change(provider, thread, uid, present, absent, point_only=True)
            except connector_throttle.RateLimited as error:
                if dispatched:
                    await mail_job.queue_connector_label_check(account.id, proof[0], message.id, thread, uid)
                if dispatched or provider_changed:
                    # A step may already have taken effect. Keep confirmed intermediate
                    # evidence and leave uncertain changes to the next sync.
                    raise connector_throttle.RateLimited(
                        retryable=not dispatched and error.retryable,
                        labels=labels if provider_changed else None) from None
                raise
            except write_result.PermissionMissing:
                await mail_sync_state.record_connector_write(account.id, proof, outcome="blocked")
                raise write_result.PermissionMissing(labels, provider_unchanged=not provider_changed) from None
            except write_result.WriteRefused:
                if has_confirmed_step:
                    raise LabelChangeUnconfirmed(labels, provider_unchanged=not provider_changed) from None
                raise
            except LabelChangeUnconfirmed as error:
                if error.labels is not None:
                    observed = set(error.labels)
                    requested_unchanged = labels_before.symmetric_difference(observed).isdisjoint(changes)
                    error.provider_unchanged = requested_unchanged and not provider_changed
                    # Unrelated label changes do not describe this action, but they make the
                    # observation inconclusive for the permission pause.
                    if not has_receipt and observed == labels_before:
                        if await mail_sync_state.record_connector_write(account.id, proof, outcome="unchanged"):
                            raise write_result.PermissionMissing(error.labels, provider_unchanged=error.provider_unchanged) from None
                raise
            except (httpx.HTTPError, TimeoutError) as error:
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 503:
                    await mail_job.queue_connector_label_check(account.id, proof[0], message.id, thread, uid)
                    raise connector_throttle.RateLimited(
                        retryable=False, labels=labels if provider_changed else None) from None
                if not has_confirmed_step:
                    raise
                # Read the current state of the same live mailbox. Never repeat an uncertain write.
                recovery = await mail_sync_state.get_state(account.id)
                recovery_proof = recovery.write_evidence() if recovery else None
                if recovery is None or recovery_proof is None or recovery_proof[0] != proof[0]:
                    raise MailboxUnavailable() from None
                async with bound_mailbox(account, cursor=recovery.connector_cursor,
                                         generation=recovery.connector_generation, observed=recovery, check_after=True):
                    observed = (await thread_labels(provider, thread, {uid}, minimal_only=True)).get(uid)
                requested_unchanged = observed is not None and labels_before.symmetric_difference(observed).isdisjoint(changes)
                raise LabelChangeUnconfirmed(observed, provider_unchanged=requested_unchanged and not provider_changed) from None
            else:
                changed_labels = labels_before.symmetric_difference(labels)
                changed_requested_labels = not changed_labels.isdisjoint(changes)
                provider_changed = provider_changed or changed_requested_labels
                step_has_evidence = has_receipt or changed_requested_labels

    if destination_labels is not None and (
        "SPAM" in labels or observed_folder(labels) != destination
        or folder_labels.intersection(labels) != destination_labels
    ):
        raise LabelChangeUnconfirmed(labels, provider_unchanged=not provider_changed)
    if step_has_evidence:
        # The write is already confirmed. A replaced pin cannot acquire this evidence.
        await mail_sync_state.record_connector_write(account.id, proof, outcome="confirmed")
    return undo_snapshot, labels


async def _confirm_label_change(
    provider: str, thread_id: str, uid: str, present: set[str], absent: set[str], *, point_only: bool = False,
) -> list[str]:
    observations: list[tuple[str, tuple[bool, bool, bool]]] = []
    latest_point: list[str] | None = None

    def evidence(labels: list[str] | None) -> tuple[bool, bool, bool]:
        if labels is None:
            return False, False, False
        return True, present.issubset(labels), absent.isdisjoint(labels)

    def record(fmt: str, labels: dict[str, list[str]]) -> None:
        nonlocal latest_point
        if fmt == "MINIMAL":
            latest_point = labels.get(uid)
        observations.append((fmt, evidence(labels.get(uid))))

    # A full read can replay old labels. After the initial observation, only
    # the point read can confirm the change. All delays and reads share one deadline.
    delays = (0.0, 1.0, 2.0) if point_only else (0.0, 0.5, 1.0)
    async with timeout(_TOOL_TIMEOUT_SECONDS):
        for delay in delays:
            if delay:
                await sleep(delay)
            observed = await thread_labels(provider, thread_id, {uid}, minimal_only=point_only or bool(delay), on_read=record)
            labels = observed.get(uid)
            if labels is not None and all(evidence(labels)):
                return labels

    for attempt, (fmt, checks) in enumerate(observations, 1):
        logger.warning(
            "Connector label change not confirmed format=%s attempt=%d labels_present=%s required_met=%s forbidden_absent=%s",
            fmt, attempt, *checks,
        )
    raise LabelChangeUnconfirmed(latest_point)


async def thread_labels(
    provider: str, thread_id: str, uids: set[str], *, minimal_only: bool = False,
    on_read: Callable[[str, dict[str, list[str]]], None] | None = None,
) -> dict[str, list[str]]:
    """Point-read exact thread members, with a full read for missing labels."""
    found = {}
    formats = ("MINIMAL",) if minimal_only else ("MINIMAL", "FULL_CONTENT")
    for fmt in formats:
        thread = await call_read_tool_json(provider, "get_thread", {"threadId": thread_id, "messageFormat": fmt})
        valid = isinstance(thread, dict) and thread.get("id") == thread_id and isinstance(thread.get("messages"), list)
        if valid:
            for item in thread["messages"]:
                if isinstance(item, dict) and isinstance(uid := item.get("id"), str) and uid in uids:
                    labels = item.get("labelIds")
                    if isinstance(labels, list) and all(isinstance(label, str) for label in labels):
                        found[uid] = labels
        if on_read is not None:
            on_read(fmt, found)
        if not valid or uids <= found.keys():
            break
    return found
