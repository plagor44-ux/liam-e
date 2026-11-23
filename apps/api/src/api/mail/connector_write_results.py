"""Validate Studio write receipts without trusting HTTP status or free-form provider prose."""

import json
import re
from typing import Any

LABEL_TOOLS = frozenset({"label_message", "unlabel_message", "label_thread", "unlabel_thread"})
PERMISSION_MISSING = "provider_permission_missing"
_PERMISSION_TEXT = re.compile(r"\b(?:insufficient (?:authentication scopes|permissions?)|permission denied)\b", re.I)
_PERMISSION_CODES = {403, "403", "PERMISSION_DENIED", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions"}


class WriteRefused(ValueError):
    code = "connector_write_refused"

    def __init__(self):
        super().__init__(self.code)


class PermissionMissing(WriteRefused):
    code = PERMISSION_MISSING

    def __init__(self, labels: list[str] | None = None):
        super().__init__()
        self.labels = labels


class WriteUnconfirmed(WriteRefused):
    code = "connector_write_unconfirmed"


def text_parts(envelope: dict) -> list[str]:
    parts = envelope.get("content")
    if not isinstance(parts, list):
        return []
    return [part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]


def _permission_code(code: Any) -> bool:
    return isinstance(code, (str, int)) and code in _PERMISSION_CODES


def _has_labels(record: Any, requested: set[str], *, adding: bool) -> bool:
    labels = record.get("labelIds") if isinstance(record, dict) else None
    if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
        return False
    return requested.issubset(labels) if adding else requested.isdisjoint(labels)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _success(tool: str, args: dict, record: dict) -> bool:
    if tool == "send_email":
        message_id = record.get("messageId", record.get("id"))
        return (_nonempty(message_id) and message_id != args.get("replyToMessageId")
                and _nonempty(record.get("threadId"))
                and ("labelIds" not in record or _has_labels(record, {"SENT"}, adding=True)))
    requested = args.get("labelIds")
    if not isinstance(requested, list) or not requested or not all(_nonempty(label) for label in requested):
        return False
    adding = tool.startswith("label_")
    key = "threadId" if tool.endswith("_thread") else "messageId"
    if not _nonempty(args.get(key)) or record.get(key, record.get("id")) != args[key]:
        return False
    labels = set(requested)
    if key == "threadId" and "messages" in record:
        messages = record["messages"]
        return (isinstance(messages, list) and bool(messages)
                and all(_has_labels(message, labels, adding=adding) for message in messages))
    return _has_labels(record, labels, adding=adding)


def confirm(tool: str, args: dict, envelope: Any) -> None:
    if not isinstance(envelope, dict):
        raise WriteUnconfirmed()
    structured = envelope.get("structuredContent", envelope.get("structured_content"))
    records = [envelope]
    if isinstance(structured, dict):
        records.append(structured)
    plain_diagnostics = []
    for text in text_parts(envelope):
        try:
            payload = json.loads(text)
        except ValueError:
            plain_diagnostics.append(text)
        else:
            if isinstance(payload, dict):
                records.append(payload)
            elif isinstance(payload, str):
                plain_diagnostics.append(payload)

    refused = False
    diagnostics = list(plain_diagnostics)
    for record in records:
        error = record.get("error")
        details = error if isinstance(error, dict) else {}
        if any(_permission_code(source.get(key)) for source in (record, details) for key in ("code", "status", "reason")):
            raise PermissionMissing()
        if (error or record.get("isError") or record.get("is_error") or record.get("success") is False
                or record.get("status") in ("error", "failed", "refused")):
            refused = True
        diagnostics.extend(value for value in (error, record.get("message"), details.get("message")) if isinstance(value, str))

    receipts = [structured] if isinstance(structured, dict) else records
    if not refused and any(_success(tool, args, record) for record in receipts):
        # Contradictory standalone permission text cannot accompany a confirmed write.
        if any(_PERMISSION_TEXT.search(text) for text in plain_diagnostics):
            raise PermissionMissing()
        return
    if any(_PERMISSION_TEXT.search(text) for text in diagnostics):
        raise PermissionMissing()
    if refused:
        raise WriteRefused()
    raise WriteUnconfirmed()
