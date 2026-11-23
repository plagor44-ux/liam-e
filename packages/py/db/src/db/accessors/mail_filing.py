"""Transactional app-side filing; provider state remains untouched."""

from dataclasses import dataclass
from contextlib import asynccontextmanager

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from db.engine import get_session_maker
from db.models.mail_filing import MailFiling
from db.models.mail_folder import MailFolder
from db.models.mail_label_assignment import MailLabelAssignment
from db.models.mail_message import MailMessage
from db.models.mail_rule import MailRule
from db.models.mail_filing_usage import MailFilingUsage


@dataclass(frozen=True)
class Filing:
    folder_id: str | None
    label_ids: list[str]


async def create_destination(name: str, kind: str) -> MailFolder:
    name = name.strip()
    if not name or len(name) > 120 or kind not in ("folder", "label"):
        raise ValueError("Invalid folder or label")
    row = MailFolder(name=name, kind=kind)
    async with get_session_maker().begin() as session:
        session.add(row)
    return row


async def list_destinations() -> list[MailFolder]:
    async with get_session_maker()() as session:
        return list((await session.scalars(select(MailFolder).order_by(MailFolder.kind, MailFolder.name, MailFolder.id))).all())


async def destination_page(cursor: str | None, limit: int) -> tuple[list[MailFolder], str | None]:
    """Read a bounded ID-ordered page; callers may sort the complete inventory for display."""
    query = select(MailFolder).order_by(MailFolder.id).limit(limit + 1)
    if cursor is not None:
        query = query.where(MailFolder.id > cursor)
    async with get_session_maker()() as session:
        rows = list((await session.scalars(query)).all())
    return rows[:limit], rows[limit - 1].id if len(rows) > limit else None


async def rename_destination(destination_id: str, name: str) -> MailFolder:
    name = name.strip()
    if not name or len(name) > 120:
        raise ValueError("Invalid name")
    async with get_session_maker().begin() as session:
        row = (await session.scalars(select(MailFolder).where(MailFolder.id == destination_id).with_for_update())).one_or_none()
        if row is None:
            raise LookupError("Destination not found")
        row.name = name
        return row


async def delete_destination(destination_id: str) -> None:
    async with get_session_maker().begin() as session:
        row = (await session.scalars(select(MailFolder).where(MailFolder.id == destination_id).with_for_update())).one_or_none()
        if row is None:
            raise LookupError("Destination not found")
        in_use = await session.scalar(select(MailRule.id).where(or_(
            MailRule.folder_id == destination_id, MailRule.label_id == destination_id)).limit(1))
        if in_use is not None:
            raise ValueError("Destination is used by a rule")
        await session.execute(update(MailFiling).where(MailFiling.folder_id == destination_id).values(folder_id=None))
        await session.execute(delete(MailLabelAssignment).where(MailLabelAssignment.label_id == destination_id))
        await session.execute(delete(MailFilingUsage).where(MailFilingUsage.destination_id == destination_id))
        await session.delete(row)


_UNSET = object()


@asynccontextmanager
async def _writer(session):
    if session is None:
        async with get_session_maker().begin() as owned:
            yield owned
    else:
        yield session


async def assign(message_ids: list[str], *, folder_id: str | None | object = _UNSET,
                 add_labels: list[str] | None = None, remove_labels: list[str] | None = None,
                 session=None, account_ids: list[str] | None = None) -> None:
    ids = sorted(set(message_ids))
    labels = sorted(set(add_labels or []))
    removed = sorted(set(remove_labels or []))
    if set(labels) & set(removed):
        raise ValueError("A label cannot be added and removed together")
    async with _writer(session) as session:
        query = select(MailMessage).where(or_(MailMessage.id.in_(ids),
            (MailMessage.unverified_connector.is_(False) & MailMessage.connector_alias_ids.overlap(ids))))
        if account_ids is not None:
            query = query.where(MailMessage.account_id.in_(account_ids), MailMessage.unverified_connector.is_(False))
        messages = (await session.scalars(query.order_by(MailMessage.id).with_for_update())).all()
        resolved = {row.id: row.id for row in messages}
        for row in messages:
            if not row.unverified_connector:
                for alias in row.connector_alias_ids:
                    if alias not in resolved:
                        resolved[alias] = row.id
        if any(message_id not in resolved for message_id in ids):
            raise LookupError("Message not found")
        ids = sorted({resolved[message_id] for message_id in ids})
        destinations = ([folder_id] if isinstance(folder_id, str) else []) + labels + removed
        rows = (await session.scalars(select(MailFolder).where(MailFolder.id.in_(destinations)).with_for_update())).all()
        kinds = {row.id: row.kind for row in rows}
        if isinstance(folder_id, str) and kinds.get(folder_id) != "folder":
            raise ValueError("Folder not found")
        if any(kinds.get(label) != "label" for label in labels + removed):
            raise ValueError("Label not found")
        if removed:
            await session.execute(delete(MailLabelAssignment).where(
                MailLabelAssignment.message_id.in_(ids), MailLabelAssignment.label_id.in_(removed)))
        for message_id in ids:
            if folder_id is not _UNSET:
                stmt = insert(MailFiling).values(message_id=message_id, folder_id=folder_id)
                await session.execute(stmt.on_conflict_do_update(index_elements=[MailFiling.message_id], set_={"folder_id": folder_id}))
            for label_id in labels:
                stmt = insert(MailLabelAssignment).values(message_id=message_id, label_id=label_id)
                await session.execute(stmt.on_conflict_do_nothing())
        if ids:
            from db.accessors import mail_filing_usage
            await mail_filing_usage.record(session, ([folder_id] if isinstance(folder_id, str) else []) + labels)


async def for_message(message_id: str) -> Filing:
    async with get_session_maker()() as session:
        folder_id = await session.scalar(select(MailFiling.folder_id).where(MailFiling.message_id == message_id))
        labels = (await session.scalars(select(MailLabelAssignment.label_id).where(
            MailLabelAssignment.message_id == message_id).order_by(MailLabelAssignment.label_id))).all()
        return Filing(folder_id=folder_id, label_ids=list(labels))


async def for_messages(message_ids: list[str]) -> dict[str, Filing]:
    if not message_ids:
        return {}
    async with get_session_maker()() as session:
        folders = (await session.execute(select(MailFiling.message_id, MailFiling.folder_id).where(
            MailFiling.message_id.in_(message_ids)))).all()
        labels = (await session.execute(select(MailLabelAssignment.message_id, MailLabelAssignment.label_id).where(
            MailLabelAssignment.message_id.in_(message_ids)).order_by(MailLabelAssignment.label_id))).all()
    folder_map = dict(folders)
    label_map: dict[str, list[str]] = {}
    for message_id, label_id in labels:
        label_map.setdefault(message_id, []).append(label_id)
    return {message_id: Filing(folder_map.get(message_id), label_map.get(message_id, [])) for message_id in message_ids}
