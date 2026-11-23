"""Deterministic classification of indexed mail."""

import json
from dataclasses import dataclass
from email.utils import getaddresses

from db.models.mail_message import MailMessage
from db.models.mail_rule import MailRule


@dataclass(frozen=True)
class Assignments:
    folder_id: str | None
    label_ids: list[str]


def matches(rule: MailRule, message: MailMessage) -> bool:
    if not rule.is_active or message.folder != "INBOX":
        return False
    if rule.account_id and rule.account_id != message.account_id:
        return False
    sender = message.sender_address.strip().casefold()
    if rule.sender_address and sender != rule.sender_address.strip().casefold():
        return False
    if rule.sender_domain and sender.rpartition("@")[2] != rule.sender_domain.strip().lstrip("@").casefold():
        return False
    if rule.subject_contains and rule.subject_contains.strip().casefold() not in message.subject.casefold():
        return False
    if rule.recipient:
        values = []
        for raw in (message.recipients_json, message.cc_json):
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, list):
                    values.extend(value for value in decoded if isinstance(value, str))
            except (TypeError, ValueError):
                pass
        addresses = {address.casefold() for _, address in getaddresses(values)}
        if rule.recipient.strip().casefold() not in addresses:
            return False
    return True


def select_assignments(rules: list[MailRule], message: MailMessage) -> Assignments:
    folder = None
    labels: list[str] = []
    for rule in sorted(rules, key=lambda row: (row.position, row.created_at, row.id)):
        if not matches(rule, message):
            continue
        if folder is None and rule.folder_id:
            folder = rule.folder_id
        if rule.label_id and rule.label_id not in labels:
            labels.append(rule.label_id)
    return Assignments(folder_id=folder, label_ids=labels)


async def prepare_rule_run(rule_id: str, expected_version: int, message_id: str, *, snapshot: dict[str, int] | None = None):
    """Persist the app-side result before any provider action is dispatched."""
    from db.accessors import mail_message, mail_rule_run

    message = await mail_message.get_message(message_id)
    if message is None:
        raise LookupError("Message not found")
    run, _ = await mail_rule_run.create_once(rule_id, expected_version, message.id, message.account_id)
    return await mail_rule_run.prepare(run.id, matches, snapshot=snapshot)


async def dispatch_rule_run(run, *, recover: bool = False):
    """Dispatch supported provider actions at most once for a prepared run."""
    from fastapi import HTTPException
    from db.accessors import mail_account, mail_rule_run
    from api.mail import action_capabilities, actions

    if run.status not in {"local_done", "unknown"}:
        return run
    states = json.loads(run.provider_results_json)
    for kind, state in states.items():
        if state == "dispatching":
            if recover:
                run, _ = await mail_rule_run.record_action(run.id, kind, expected="dispatching", result="unknown")
            continue
        if state != "pending" or run.status == "unknown":
            continue
        account = await mail_account.get_account(run.account_id)
        if account is None:
            run, _ = await mail_rule_run.record_action(run.id, kind, expected="pending", result="skipped_stale")
            continue
        try:
            action_capabilities.require_action(account, kind)
        except HTTPException as error:
            if error.status_code != 409:
                raise
            run, _ = await mail_rule_run.record_action(run.id, kind, expected="pending", result="skipped_unsupported")
            continue
        run, claimed = await mail_rule_run.record_action(run.id, kind, expected="pending", result="dispatching")
        if not claimed:
            continue
        try:
            await actions.apply_action(run.message_id, kind)
        except Exception:
            run, _ = await mail_rule_run.record_action(run.id, kind, expected="dispatching", result="unknown")
        else:
            run, _ = await mail_rule_run.record_action(run.id, kind, expected="dispatching", result="done")
    return run


async def execute_rule_run(rule_id: str, expected_version: int, message_id: str, *, recover: bool = False):
    run = await prepare_rule_run(rule_id, expected_version, message_id)
    return await dispatch_rule_run(run, recover=recover)
