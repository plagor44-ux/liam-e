"""Version-checked persistence for owner-defined mail rules."""

from sqlalchemy import select
from contextlib import asynccontextmanager

from db.engine import get_session_maker
from db.models._common import utcnow
from db.models.mail_rule import MailRule
from db.models.mail_folder import MailFolder
from db.models.mail_account import MailAccount


class VersionConflict(Exception):
    pass


async def _check_targets(session, rule: MailRule) -> None:
    for destination_id, kind in ((rule.folder_id, "folder"), (rule.label_id, "label")):
        if destination_id:
            row = (await session.scalars(select(MailFolder).where(MailFolder.id == destination_id).with_for_update())).one_or_none()
            if row is None or row.kind != kind:
                raise ValueError("Invalid destination")
    if rule.account_id and await session.get(MailAccount, rule.account_id) is None:
        raise ValueError("Invalid account")
    if rule.mark_read and rule.mark_unread:
        raise ValueError("Conflicting read actions")


@asynccontextmanager
async def _writer(session):
    if session is None:
        async with get_session_maker().begin() as owned:
            yield owned
    else:
        yield session


async def create(*, session=None, **values) -> MailRule:
    rule = MailRule(**values)
    async with _writer(session) as session:
        await _check_targets(session, rule)
        session.add(rule)
    return rule


async def scope_allows_rule(session, account_id: str | None, permitted: list[str]) -> bool:
    if account_id is not None:
        return account_id in permitted
    active = set((await session.scalars(select(MailAccount.id).where(MailAccount.is_active.is_(True)))).all())
    return bool(active) and active.issubset(set(permitted))


async def get(rule_id: str) -> MailRule | None:
    async with get_session_maker()() as session:
        return await session.get(MailRule, rule_id)


async def list_rules() -> list[MailRule]:
    async with get_session_maker()() as session:
        return list((await session.scalars(select(MailRule).order_by(MailRule.position, MailRule.created_at, MailRule.id))).all())


async def update(rule_id: str, expected_version: int, **values) -> MailRule:
    async with get_session_maker().begin() as session:
        rule = (await session.scalars(select(MailRule).where(MailRule.id == rule_id).with_for_update())).one_or_none()
        if rule is None:
            raise LookupError("Rule not found")
        if rule.version != expected_version:
            raise VersionConflict()
        allowed = {"name", "account_id", "sender_address", "sender_domain", "subject_contains", "recipient", "folder_id", "label_id", "mark_read", "mark_unread", "archive", "position", "is_active"}
        if values.keys() - allowed:
            raise ValueError("Invalid rule fields")
        for key, value in values.items():
            setattr(rule, key, value)
        await _check_targets(session, rule)
        rule.version += 1
        rule.updated_at = utcnow()
        return rule


async def delete_rule(rule_id: str, expected_version: int) -> None:
    async with get_session_maker().begin() as session:
        rule = (await session.scalars(select(MailRule).where(MailRule.id == rule_id).with_for_update())).one_or_none()
        if rule is None:
            raise LookupError("Rule not found")
        if rule.version != expected_version:
            raise VersionConflict()
        await session.delete(rule)
