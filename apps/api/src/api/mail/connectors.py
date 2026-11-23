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
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any
from urllib.parse import quote

import httpx
from utils.mistral import mistral_api_url, mistral_app_token


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

    def __init__(self, labels: list[str] | None):
        super().__init__("Connector label change not confirmed")
        self.labels = labels


async def _call_tool(connector: str, tool: str, arguments: dict[str, Any], timeout: float = _TOOL_TIMEOUT_SECONDS) -> str:
    """Call one connector tool; returns the joined text content of the MCP-style result."""
    ref = quote(connector, safe="")
    path = f"/v1/connectors/{ref}/tools/{quote(tool, safe='')}/call"
    credential = mailbox_credential.get()
    if credential is not None:
        path += f"?credentials_name={quote(credential, safe='')}"
    data = await _post(
        path,
        {"arguments": arguments},
        **({"timeout": timeout} if timeout != _TOOL_TIMEOUT_SECONDS else {}),
    )
    if tool in {"label_message", "unlabel_message"} and (data.get("isError") or data.get("is_error")):
        raise ValueError("Connector refused the label change")
    return "\n".join(
        part.get("text", "") for part in data.get("content", []) if part.get("text")
    )


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
            except (ValueError, httpx.HTTPError) as error:
                if not (present or absent):
                    raise
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
                    raise
                try:
                    labels = await thread_labels(account.provider, message.thread_id, {message.provider_uid}, minimal_only=True)
                except (ValueError, httpx.HTTPError):
                    raise error from None
                raise LabelChangeUnconfirmed(labels.get(message.provider_uid)) from None
            # HTTP 200 can contain a text diagnostic. Confirm TRASH before removing INBOX.
            expected.update(change)
            await _confirm_label_change(account.provider, message.thread_id, message.provider_uid, present, absent)


async def _confirm_label_change(provider: str, thread_id: str, uid: str, present: set[str], absent: set[str]) -> None:
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
    async with timeout(_TOOL_TIMEOUT_SECONDS):
        for delay in (0.0, 0.5, 1.0):
            if delay:
                await sleep(delay)
            observed = await thread_labels(provider, thread_id, {uid}, minimal_only=bool(delay), on_read=record)
            if all(evidence(observed.get(uid))):
                return

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
