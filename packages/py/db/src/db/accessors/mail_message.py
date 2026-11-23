"""Accessor for ``mail_message`` rows: upsert during sync, search for the chat agent."""

import json
from collections.abc import Collection
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import String, and_, any_, bindparam, case, cast, delete, exists, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, insert
from sqlalchemy.orm import aliased, undefer
from sqlmodel.ext.asyncio.session import AsyncSession

from db.accessors import mail_reference_snapshot, mail_rule_run, mail_sync_state
from db.models.mail_insight import MailInsight
from db.models.mail_action import MailAction
from db.models.mail_filing import MailFiling
from db.models.mail_label_assignment import MailLabelAssignment

from db.engine import get_session_maker
from db.models.mail_message import IMAP_RETIRED, MailMessage
from db.models.mail_sync_state import MailSyncState


@asynccontextmanager
async def _writer(session):
    if session is None:
        async with get_session_maker().begin() as owned:
            yield owned
    else:
        yield session


def _connector_labels(stored: str, *, folder: str | None = None, is_read: bool | None = None, is_flagged: bool | None = None) -> str:
    try:
        labels = json.loads(stored)
    except (TypeError, ValueError):
        labels = []
    labels = [label for label in labels if isinstance(label, str)] if isinstance(labels, list) else []
    for label, present in (("INBOX", folder == "INBOX" if folder is not None else None),
                           ("TRASH", folder == "TRASH" if folder is not None else None),
                           ("UNREAD", not is_read if is_read is not None else None), ("STARRED", is_flagged)):
        if present and label not in labels:
            labels.append(label)
        elif present is False:
            labels = [value for value in labels if value != label]
    return json.dumps(labels)


_HTML_UNSET = object()


async def upsert_message(
    *,
    account_id: str,
    folder: str,
    provider_uid: str,
    imap_uidvalidity: str | None = None,
    thread_id: str = "",
    subject: str = "",
    sender: str = "",
    recipients_json: str = "[]",
    snippet: str = "",
    body_text: str = "",
    body_html: str | None = None,
    html_version: int | None | object = _HTML_UNSET,
    html_status: str = "pending",
    internal_date: datetime | None = None,
    has_attachments: bool = False,
    is_read: bool | None = False,
    labels_json: str | None = "[]",
    sender_address: str = "",
    list_unsubscribe: str = "",
    list_unsubscribe_post: str = "",
    rfc_message_id: str = "",
    references: str = "",
    cc_json: str = "[]",
    reply_to_json: str = "[]",
    attachments_json: str = "[]",
    is_flagged: bool | None = False,
    body_version: int = 0,
    drop_insight: bool = False,
    unverified_connector: bool = False,
    session: AsyncSession | None = None,
    invalidated_insights: list[str] | None = None,
) -> MailMessage:
    """Insert the message, or refresh it when (account, folder, uid) already exists. `drop_insight`
    removes its insight in the same transaction (the summarized text changed)."""
    async with _writer(session) as session:
        result = await session.execute(
            select(MailMessage).where(
                MailMessage.account_id == account_id,
                MailMessage.folder == folder,
                MailMessage.provider_uid == provider_uid,
            ).with_for_update()
        )
        message = result.scalar_one_or_none()
        new = message is None
        if message is None:
            message = MailMessage(
                account_id=account_id, folder=folder, provider_uid=provider_uid
            )
        reverified = message.unverified_connector and not unverified_connector
        drop_insight = drop_insight or reverified
        message.unverified_connector = unverified_connector
        if imap_uidvalidity is not None:
            message.imap_uidvalidity = imap_uidvalidity
        message.thread_id = thread_id
        message.subject = subject
        message.sender = sender
        message.sender_address = sender_address.lower()
        message.list_unsubscribe = list_unsubscribe
        message.list_unsubscribe_post = list_unsubscribe_post
        message.rfc_message_id = rfc_message_id
        message.references = references
        message.cc_json = cc_json
        message.reply_to_json = reply_to_json
        message.attachments_json = attachments_json
        # A connector can prove inbox membership without returning its flags. Preserve
        # trusted flags on an existing row, and never carry unverified labels into chat.
        if is_flagged is not None or new or reverified:
            message.is_flagged = is_flagged if is_flagged is not None else False
        message.recipients_json = recipients_json
        message.snippet = snippet
        message.body_text = body_text
        message.body_version = body_version
        if html_version is None:
            message.body_html, message.html_status, message.html_version = None, "pending", 0
        elif html_version is not _HTML_UNSET:
            message.body_html, message.html_status, message.html_version = body_html, html_status, html_version
        message.internal_date = internal_date
        message.has_attachments = has_attachments
        if is_read is not None or new or reverified:
            message.is_read = is_read if is_read is not None else True
        message.labels_json = (labels_json if labels_json is not None else
            _connector_labels("[]" if new or reverified else message.labels_json,
                              folder=folder, is_read=is_read, is_flagged=is_flagged))
        session.add(message)
        if drop_insight and message.id:
            removed = await _drop_insight(session, message.id)
            if (removed or reverified) and not message.is_read and invalidated_insights is not None:
                invalidated_insights.append(message.id)
        return message


async def search_messages(
    *, query: str = "", account_id: str = "", limit: int = 50
) -> list[MailMessage]:
    """Case-insensitive substring search over subject, sender, and body across accounts."""
    stmt = select(MailMessage).where(MailMessage.folder != "SPAM")
    if account_id:
        stmt = stmt.where(MailMessage.account_id == account_id)
    if query:
        pattern = f"%{query}%"
        stmt = stmt.where(
            or_(
                MailMessage.subject.ilike(pattern),
                MailMessage.sender.ilike(pattern),
                MailMessage.body_text.ilike(pattern),
            )
        )
    stmt = stmt.order_by(MailMessage.internal_date.desc().nullslast()).limit(limit)
    async with get_session_maker()() as session:
        result = await session.execute(stmt)
        return list(result.scalars().all())


def matches_ids(message_ids: Collection[str]):
    """Match public local IDs, including links retained on a proven connector message."""
    ids = list(message_ids)
    return or_(MailMessage.id.in_(ids), and_(MailMessage.unverified_connector.is_(False),
        func.cardinality(MailMessage.connector_alias_ids) > 0, MailMessage.connector_alias_ids.overlap(ids)))


async def get_message(message_id: str, session: AsyncSession | None = None, *, include_html: bool = False) -> MailMessage | None:
    async with (nullcontext(session) if session is not None else get_session_maker()()) as reader:
        stmt = select(MailMessage).where(matches_ids([message_id]))
        if include_html:
            stmt = stmt.options(undefer(MailMessage.body_html))
        return (await reader.execute(stmt)).scalar_one_or_none()


async def legacy_imap_uidvalidity(message: MailMessage) -> str | None:
    """Bind the pre-upgrade INBOX row and its committed sync cursor in the same read."""
    stmt = select(MailSyncState.cursor).join(MailMessage, MailMessage.account_id == MailSyncState.account_id).where(
        MailMessage.id == message.id, MailMessage.account_id == message.account_id, MailMessage.folder == "INBOX",
        MailMessage.provider_uid == message.provider_uid, MailMessage.rfc_message_id == message.rfc_message_id)
    async with get_session_maker()() as session:
        cursor = await session.scalar(stmt)
    validity, _, position = (cursor or "").partition(":")
    return validity if validity.isdigit() and position.partition(":")[0].isdigit() else None


async def resolve_aliases(message_ids: Collection[str]) -> dict[str, str]:
    """Resolve former local IDs for a single batch before dispatching provider actions."""
    ids = set(message_ids)
    stmt = select(MailMessage.id, MailMessage.connector_alias_ids).where(
        MailMessage.unverified_connector.is_(False), func.cardinality(MailMessage.connector_alias_ids) > 0,
        MailMessage.connector_alias_ids.overlap(list(ids)),
    )
    async with get_session_maker()() as session:
        return {alias: target for target, aliases in await session.execute(stmt) for alias in ids.intersection(aliases)}


async def has_messages_from_accounts(message_ids: Collection[str], account_ids: Collection[str]) -> bool:
    """Check real message IDs and verified aliases in a single read before a bulk write."""
    stmt = select(MailMessage.id).where(matches_ids(message_ids), MailMessage.account_id.in_(account_ids)).limit(1)
    async with get_session_maker()() as session:
        return await session.scalar(stmt) is not None


async def get_scoped_message(message_id: str, account_ids: list[str], *, folder: str) -> MailMessage | None:
    """Read a single indexed row of one folder without probing outside the authorized scope."""
    stmt = select(MailMessage).where(
        matches_ids([message_id]), MailMessage.account_id.in_(account_ids), MailMessage.folder == folder
    )
    async with get_session_maker()() as session:
        return (await session.execute(stmt)).scalar_one_or_none()


async def inbox_counts(account_ids: list[str]) -> dict[str, tuple[int, int]]:
    """Count stored inbox rows and unread rows, restricted in the query itself."""
    stmt = (select(MailMessage.account_id, func.count(), func.count().filter(MailMessage.is_read.is_(False)))
            .where(MailMessage.folder == "INBOX", MailMessage.account_id.in_(account_ids))
            .group_by(MailMessage.account_id))
    async with get_session_maker()() as session:
        return {account_id: (total, unread) for account_id, total, unread in (await session.execute(stmt)).all()}


async def count_for_account(account_id: str) -> int:
    from sqlalchemy import func

    async with get_session_maker()() as session:
        result = await session.execute(
            select(func.count())
            .select_from(MailMessage)
            .where(MailMessage.account_id == account_id)
        )
        return int(result.scalar_one())


async def delete_for_account(account_id: str) -> None:
    async with get_session_maker()() as session:
        ids = select(MailMessage.id).where(MailMessage.account_id == account_id)
        await session.execute(delete(MailFiling).where(MailFiling.message_id.in_(ids)))
        await session.execute(delete(MailLabelAssignment).where(MailLabelAssignment.message_id.in_(ids)))
        result = await session.execute(
            select(MailMessage).where(MailMessage.account_id == account_id)
        )
        for message in result.scalars().all():
            await session.delete(message)
        await session.commit()


@dataclass
class NewsletterRow:
    account_id: str
    sender_address: str
    sender_display: str
    total: int
    unread: int
    last_read_at: datetime | None
    list_unsubscribe: str
    list_unsubscribe_post: str
    labels_json: str


def _listed_folder(folder: str):
    return MailMessage.folder.in_(("ARCHIVE", IMAP_RETIRED)) if folder == "ARCHIVE" else MailMessage.folder == folder


async def list_page(*, chat_only: bool = False, folder: str = "INBOX", account_ids: list[str] | None = None, unread_only: bool = False, important_only: bool = False, with_attachments: bool = False, query: str = "", before: tuple[datetime | None, str] | None = None, limit: int = 50, hide_filed: bool = False, app_folder_id: str | None = None, app_label_id: str | None = None) -> list[MailMessage]:
    date_key = func.coalesce(MailMessage.internal_date, datetime(1970, 1, 1, tzinfo=UTC))
    # App classifications span the locally indexed inbox and archive; trash remains separate.
    indexed = MailMessage.folder.in_(("INBOX", "ARCHIVE", IMAP_RETIRED)) if folder == "INBOX" and (app_folder_id or app_label_id) else _listed_folder(folder)
    stmt = select(MailMessage).where(indexed)
    filing = select(MailFiling.message_id).where(MailFiling.message_id == MailMessage.id)
    if app_folder_id is not None:
        stmt = stmt.where(exists(filing.where(MailFiling.folder_id == app_folder_id)))
    elif hide_filed:
        stmt = stmt.where(~exists(filing.where(MailFiling.folder_id.is_not(None))))
    if app_label_id is not None:
        stmt = stmt.where(exists(select(MailLabelAssignment.message_id).where(
            MailLabelAssignment.message_id == MailMessage.id, MailLabelAssignment.label_id == app_label_id)))
    if account_ids is not None:
        stmt = stmt.where(MailMessage.account_id.in_(account_ids))
    if chat_only:
        stmt = stmt.where(MailMessage.unverified_connector.is_(False))
    if unread_only:
        stmt = stmt.where(MailMessage.is_read.is_(False))
    if important_only:
        stmt = stmt.where(or_(MailMessage.is_flagged.is_(True), exists(select(MailInsight.id).where(MailInsight.message_id == MailMessage.id, or_(MailInsight.deadline.is_not(None), MailInsight.needs_reply.is_(True))))))
    if with_attachments:
        stmt = stmt.where(MailMessage.has_attachments.is_(True))
    if query:
        stmt = stmt.where(MailMessage.search_tsv.op("@@")(func.websearch_to_tsquery("german", query)))
    if before is not None:
        stmt = stmt.where(tuple_(date_key, MailMessage.id) < tuple_(before[0] or datetime(1970, 1, 1, tzinfo=UTC), before[1]))
    stmt = stmt.order_by(date_key.desc(), MailMessage.id.desc()).limit(limit)
    async with get_session_maker()() as session:
        return list((await session.execute(stmt)).scalars().all())


async def count_unread(*, chat_only: bool = False, folder: str = "INBOX", account_ids: list[str] | None = None) -> dict[str, int]:
    async with get_session_maker()() as session:
        query = select(MailMessage.account_id, func.count()).where(_listed_folder(folder), MailMessage.is_read.is_(False))
        if folder == "INBOX" and not chat_only:
            query = query.where(~exists(select(MailFiling.message_id).where(
                MailFiling.message_id == MailMessage.id, MailFiling.folder_id.is_not(None))))
        if account_ids is not None:
            query = query.where(MailMessage.account_id.in_(account_ids))
        if chat_only:
            query = query.where(MailMessage.unverified_connector.is_(False))
        rows = await session.execute(query.group_by(MailMessage.account_id))
        return dict(rows.all())


async def set_state(message_id: str, *, session: AsyncSession | None = None, folder: str | None = None, is_read: bool | None = None, is_flagged: bool | None = None, provider_uid: str | None = None, imap_uidvalidity: str | None = None, labels_json: str | None = None, connector_labels: bool = False) -> MailMessage | None:
    async with _writer(session) as session:
        row = await get_message(message_id, session)
        if row is None:
            return None
        if connector_labels and labels_json is None:
            labels_json = _connector_labels(row.labels_json, folder=folder, is_read=is_read, is_flagged=is_flagged)
        for name, value in (("folder", folder), ("is_read", is_read), ("is_flagged", is_flagged), ("provider_uid", provider_uid),
                            ("imap_uidvalidity", imap_uidvalidity), ("labels_json", labels_json)):
            if value is not None:
                setattr(row, name, value)
        return row


async def delete_duplicate(account_id: str, folder: str, provider_uid: str, *, keep_id: str, rfc_message_id: str) -> bool:
    """Remove only a sync copy of the same identified message, never another mail."""
    if not rfc_message_id:
        return False
    async with get_session_maker().begin() as session:
        keep = await session.scalar(select(MailMessage).where(MailMessage.id == keep_id,
            MailMessage.account_id == account_id).with_for_update())
        duplicate = await session.scalar(select(MailMessage).where(
            MailMessage.account_id == account_id,
            MailMessage.folder == folder,
            MailMessage.provider_uid == provider_uid,
            MailMessage.id != keep_id,
            MailMessage.rfc_message_id == rfc_message_id,
        ).with_for_update())
        if keep is None or duplicate is None:
            return False
        await _merge_copy(session, keep, duplicate, verified=True, redirect=False)
        return True


async def newsletter_senders(*, chat_only: bool = False, account_ids: list[str] | None = None) -> list[NewsletterRow]:
    partition = (MailMessage.account_id, MailMessage.sender_address)
    date_key = func.coalesce(MailMessage.internal_date, datetime(1970, 1, 1, tzinfo=UTC))
    ranked = select(
        MailMessage.account_id, MailMessage.sender_address, MailMessage.sender,
        func.count().over(partition_by=partition).label("total"),
        func.count().filter(MailMessage.is_read.is_(False)).over(partition_by=partition).label("unread"),
        func.max(MailMessage.internal_date).filter(MailMessage.is_read.is_(True)).over(partition_by=partition).label("last_read_at"),
        MailMessage.list_unsubscribe, MailMessage.list_unsubscribe_post, MailMessage.labels_json,
        func.row_number().over(partition_by=partition, order_by=(date_key.desc(), MailMessage.id.desc())).label("rank"),
    ).where(MailMessage.folder == "INBOX", MailMessage.sender_address != "")
    if account_ids is not None:
        ranked = ranked.where(MailMessage.account_id.in_(account_ids))
    if chat_only:
        ranked = ranked.where(MailMessage.unverified_connector.is_(False))
    ranked = ranked.subquery()
    stmt = select(ranked.c.account_id, ranked.c.sender_address, ranked.c.sender, ranked.c.total, ranked.c.unread, ranked.c.last_read_at, ranked.c.list_unsubscribe, ranked.c.list_unsubscribe_post, ranked.c.labels_json).where(ranked.c.rank == 1).order_by(ranked.c.total.desc(), ranked.c.sender_address, ranked.c.account_id)
    async with get_session_maker()() as session:
        return [NewsletterRow(*row) for row in (await session.execute(stmt)).all()]


async def message_ids_from_sender(account_id: str, sender_address: str, *, folder: str = "INBOX") -> list[str]:
    async with get_session_maker()() as session:
        return list((await session.execute(select(MailMessage.id).where(MailMessage.account_id == account_id, MailMessage.sender_address == sender_address.lower(), MailMessage.folder == folder, MailMessage.unverified_connector.is_(False)))).scalars().all())


async def unread_without_insight(*, since: datetime, limit: int, exclude_account_ids: Collection[str] = (),
                                 account_id: str | None = None, verified_ids: Collection[str] | None = None) -> list[MailMessage]:
    stmt = select(MailMessage).where(MailMessage.folder != "SPAM", MailMessage.is_read.is_(False), MailMessage.internal_date >= since, ~exists(select(MailInsight.id).where(MailInsight.message_id == MailMessage.id)))
    if account_id is not None:
        stmt = stmt.where(MailMessage.account_id == account_id)
    if verified_ids is not None:
        stmt = stmt.where(matches_ids(verified_ids), MailMessage.unverified_connector.is_(False))
    if exclude_account_ids:
        stmt = stmt.where(MailMessage.account_id.not_in(list(exclude_account_ids)))
    stmt = stmt.order_by(MailMessage.internal_date.desc()).limit(limit)
    async with get_session_maker()() as session:
        return list((await session.execute(stmt)).scalars().all())


async def known_uids(account_id: str, folder: str, *, verified_only: bool = False, session: AsyncSession | None = None) -> dict[str, str]:
    query = select(MailMessage.provider_uid, MailMessage.id).where(MailMessage.account_id == account_id, MailMessage.folder == folder)
    if verified_only:
        query = query.where(MailMessage.unverified_connector.is_(False))
    async with (nullcontext(session) if session is not None else get_session_maker()()) as session:
        return dict((await session.execute(query)).all())


async def rebuild_imap_uids(session: AsyncSession, account_id: str, candidates: set[str], generation: str) -> set[str]:
    """Keep a UID only when its mailbox generation is known to match."""
    if not candidates:
        return set()
    cursor = await session.scalar(select(MailSyncState.cursor).where(MailSyncState.account_id == account_id))
    legacy_same_generation = (cursor or '').partition(':')[0] == generation
    valid = MailMessage.imap_uidvalidity == generation
    if legacy_same_generation:
        valid = or_(valid, MailMessage.imap_uidvalidity.is_(None))
    rows = await session.scalars(select(MailMessage.provider_uid).where(
        MailMessage.account_id == account_id, MailMessage.folder == 'INBOX',
        MailMessage.provider_uid.in_(candidates), valid))
    return set(rows.all())


async def imap_generation_reset(session: AsyncSession, account_id: str, generation: str) -> bool:
    """A prior mailbox generation cannot identify deliveries in a new generation."""
    cursor = await session.scalar(select(MailSyncState.cursor).where(MailSyncState.account_id == account_id))
    return bool(cursor) and cursor.partition(':')[0] != generation


async def reconcile_label_folders(session: AsyncSession, account_id: str, inbox: set[str], archived: set[str],
                                  *, verified: bool, restored: set[str], trashed: set[str]) -> tuple[set[str], dict[str, str]]:
    """A label provider's UID follows the message. Only a point read can restore a trashed UID."""
    if not inbox and not archived:
        return set(), {}
    rows = (await session.scalars(select(MailMessage).where(
        MailMessage.account_id == account_id, MailMessage.provider_uid.in_(inbox | archived),
        MailMessage.folder.in_(("INBOX", "ARCHIVE", "TRASH")),
    ).order_by(MailMessage.id).with_for_update())).all()
    by_uid: dict[str, dict[str, MailMessage]] = {}
    for row in rows:
        by_uid.setdefault(row.provider_uid, {})[row.folder] = row
    moves = []
    blocked = set()
    restored_ids = {}
    for uid, copies in by_uid.items():
        target = "TRASH" if uid in trashed else "ARCHIVE" if uid in archived else "INBOX"
        active = copies.get("INBOX")
        stored = copies.get("ARCHIVE")
        if trash := copies.get("TRASH"):
            keep = next((row for row in copies.values() if not row.unverified_connector), trash) if trash.unverified_connector else trash
            if uid in inbox and uid in restored:
                target = "INBOX"
            else:
                target = "TRASH"
                blocked.add(uid)
            for other in copies.values():
                if other is not keep:
                    await _merge_copy(session, keep, other, verified=verified)
        elif active is not None and stored is not None:
            # Retain the active inbox identity. An unverified copy cannot displace a
            # verified archive when the only new evidence is that the UID left the inbox.
            keep_archive = active.unverified_connector and not stored.unverified_connector
            keep, duplicate = (stored, active) if keep_archive else (active, stored)
            await _merge_copy(session, keep, duplicate, verified=verified)
        else:
            keep = next(iter(copies.values()))
        if uid in restored and target == "INBOX":
            restored_ids[uid] = keep.id
        if keep.folder != target:
            moves.append((keep, target))
    await session.flush()
    for row, target in moves:
        row.folder = target
        row.labels_json = _connector_labels(row.labels_json, folder=target)
    await session.flush()
    return blocked, restored_ids


async def _merge_copy(session, keep: MailMessage, duplicate: MailMessage, *, verified: bool, redirect: bool = True) -> None:
    if verified and not keep.unverified_connector and not duplicate.unverified_connector:
        await mail_rule_run.merge_verified(session, keep.id, duplicate.id)
        filing = await session.get(MailFiling, duplicate.id)
        if filing is not None:
            stmt = insert(MailFiling).values(message_id=keep.id, folder_id=filing.folder_id)
            await session.execute(stmt.on_conflict_do_nothing())
        labels = (await session.scalars(select(MailLabelAssignment.label_id).where(
            MailLabelAssignment.message_id == duplicate.id))).all()
        for label_id in labels:
            stmt = insert(MailLabelAssignment).values(message_id=keep.id, label_id=label_id)
            await session.execute(stmt.on_conflict_do_nothing())
        if redirect:
            keep.connector_alias_ids = sorted(set(keep.connector_alias_ids + duplicate.connector_alias_ids + [duplicate.id]))
            await session.execute(update(MailAction).where(MailAction.account_id == keep.account_id,
                MailAction.message_id == duplicate.id).values(message_id=keep.id))
    else:
        await mail_reference_snapshot.preserve(session, duplicate)
    await session.execute(delete(MailFiling).where(MailFiling.message_id == duplicate.id))
    await session.execute(delete(MailLabelAssignment).where(MailLabelAssignment.message_id == duplicate.id))
    await session.execute(delete(MailInsight).where(MailInsight.message_id == duplicate.id))
    await session.delete(duplicate)


async def _legacy_conflicts(session, account_id: str, stmt):
    """Bound point reads and rotate past ambiguous pairs on later polls."""
    size = 25
    rows = (await session.execute(stmt.limit(size + 1))).all()
    if len(rows) <= size:
        return rows
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    generation = await session.scalar(select(MailSyncState.connector_generation).where(MailSyncState.account_id == account_id))
    page = ((generation or 1) - 1) % max(1, (total + size - 1) // size)
    return (await session.execute(stmt.offset(page * size).limit(size))).all() if page else rows[:size]


async def trash_conflicts(account_id: str, inbox: Collection[str], moved_out: Collection[str], *, check_unlisted: bool = False) -> dict[str, str]:
    """Also repair existing duplicates that no longer occur in search or history."""
    other = aliased(MailMessage)
    candidates_query = select(MailMessage.provider_uid, MailMessage.thread_id).where(
        MailMessage.account_id == account_id, MailMessage.folder == "TRASH").order_by(MailMessage.provider_uid)
    if not check_unlisted:
        candidates_query = candidates_query.where(exists(select(other.id).where(other.account_id == account_id,
            other.folder == "INBOX", other.provider_uid == MailMessage.provider_uid)))
    trash_action = exists(select(MailAction.id).where(MailAction.message_id == MailMessage.id,
        or_(MailAction.kind == "trash", and_(MailAction.kind == "restore",
            cast(MailAction.previous_json, JSONB)["folder"].astext == "TRASH"))))
    async with get_session_maker()() as session:
        candidates = dict(await _legacy_conflicts(session, account_id, candidates_query))
        if not inbox and not moved_out:
            return candidates
        rows = await session.execute(select(MailMessage.provider_uid, MailMessage.thread_id).where(
            MailMessage.account_id == account_id, or_(
                and_(MailMessage.folder == "TRASH", MailMessage.provider_uid.in_(inbox)),
                and_(MailMessage.folder == "INBOX", MailMessage.provider_uid.in_(moved_out), trash_action))))
        candidates.update(rows.all())
        return candidates


async def trash_origins(account_id: str, uids: Collection[str], *, check_existing: bool = False, imap_validity: str | None = None) -> dict[str, str | None]:
    """A legacy IMAP UID can prompt a point read; only a bound generation identifies the old row."""
    if not uids and not check_existing:
        return {}
    previous = cast(MailAction.previous_json, JSONB)
    recorded = func.coalesce(MailAction.inbox_source_uid, previous["trash_inbox_uid"].astext)
    source = func.coalesce(recorded, previous["provider_uid"].astext)
    stmt = select(source, MailMessage.id).join_from(MailAction, MailMessage, MailAction.message_id == MailMessage.id).where(
        MailAction.account_id == account_id, MailMessage.account_id == account_id,
        or_(recorded.is_not(None), and_(MailAction.kind == "trash", previous["folder"].astext == "INBOX")),
        or_(MailMessage.folder == "TRASH", MailMessage.provider_uid != source))
    if imap_validity is not None:
        validity = func.coalesce(MailAction.imap_source_uidvalidity, previous["imap_source_uidvalidity"].astext)
        stmt = stmt.with_only_columns(source, case((validity == imap_validity, MailMessage.id))).where(
            or_(validity.is_(None), validity == imap_validity))
    async with get_session_maker()() as session:
        found = {}
        if check_existing:
            other = aliased(MailMessage)
            existing = stmt.join(other, and_(other.account_id == account_id, other.folder == "INBOX",
                                              other.provider_uid == source, other.id != MailMessage.id))
            found.update(await _legacy_conflicts(session, account_id, existing.distinct().order_by(source)))
        if uids:
            for uid, original in await session.execute(stmt.where(source.in_(uids))):
                if original is not None or uid not in found:
                    found[uid] = original
        return found


def imap_archive_uid(validity: str, uid: str) -> str:
    return f"inbox:{validity}:{uid}"


async def apply_imap_hidden(session: AsyncSession, account_id: str, validity: str, hidden: dict[str, str], retired: dict[str, str]) -> None:
    for uid, local_id in hidden.items():
        await session.execute(update(MailMessage).where(MailMessage.id == local_id, MailMessage.account_id == account_id,
            MailMessage.folder.in_(("INBOX", "ARCHIVE", IMAP_RETIRED)), MailMessage.provider_uid.in_((uid, imap_archive_uid(validity, uid)))
        ).values(folder="IMAP_DELETED", provider_uid=f"{validity}:{uid}", imap_uidvalidity=validity))
    for qualified, local_id in retired.items():
        generation, _, uid = qualified.partition(":")
        # An expunge observed in this generation still identifies the delivery of a pending move.
        key = imap_archive_uid(validity, uid) if generation == validity and uid else f"retired:{local_id}"
        await session.execute(update(MailMessage).where(MailMessage.id == local_id, MailMessage.account_id == account_id,
            MailMessage.folder == "IMAP_DELETED", MailMessage.provider_uid == qualified
        ).values(folder=IMAP_RETIRED, provider_uid=key))


async def restore_imap_flagged(session: AsyncSession, account_id: str, uid: str, local_id: str, validity: str) -> bool:
    """Reuse the exact row archived by a reversible IMAP flag in this UIDVALIDITY."""
    result = await session.execute(update(MailMessage).where(MailMessage.id == local_id, MailMessage.account_id == account_id,
        or_(and_(MailMessage.folder == "IMAP_DELETED", MailMessage.provider_uid == f"{validity}:{uid}"),
            and_(MailMessage.folder.in_(("ARCHIVE", IMAP_RETIRED)), MailMessage.provider_uid.in_((uid, imap_archive_uid(validity, uid))))),
        or_(MailMessage.imap_uidvalidity.is_(None), MailMessage.imap_uidvalidity == validity)
    ).values(folder="INBOX", provider_uid=uid, imap_uidvalidity=validity))
    return bool(result.rowcount)


async def imap_inbox_moves_in_generation(session: AsyncSession, account_id: str, validity: str) -> dict[str, str]:
    """A move into this generation can commit before the first poll of its new UIDVALIDITY."""
    rows = await session.execute(select(MailMessage.provider_uid, MailMessage.id).where(MailMessage.account_id == account_id,
        MailMessage.folder == "INBOX", MailMessage.imap_uidvalidity == validity,
        exists(select(MailAction.id).where(MailAction.account_id == account_id, MailAction.message_id == MailMessage.id,
            MailAction.kind.in_(("trash", "restore"))))))
    return dict(rows.all())


async def reconcile_native_trash(session: AsyncSession, account_id: str, uid: str, original: str,
                                 *, in_inbox: bool, graph: bool, imap_validity: str | None) -> bool:
    keep = await session.get(MailMessage, original)
    if keep is None or keep.account_id != account_id:
        return False
    if in_inbox and (not graph or keep.folder != "TRASH"):
        return True
    duplicate = await session.scalar(select(MailMessage).where(MailMessage.account_id == account_id,
        MailMessage.provider_uid == uid, MailMessage.folder == "INBOX", MailMessage.id != keep.id))
    if duplicate:
        if not graph and (duplicate.imap_uidvalidity != imap_validity or not keep.rfc_message_id
                          or duplicate.rfc_message_id != keep.rfc_message_id):
            return False
        # Only Graph proves identity across folders. Preserve an old IMAP reference
        # as read-only: another UIDVALIDITY may have reused its numeric source UID.
        await _merge_copy(session, keep, duplicate, verified=graph)
        await session.flush()
    if in_inbox:
        keep.provider_uid, keep.folder = uid, "INBOX"
        await session.flush()
    return True


async def _imap_restore_state(session: AsyncSession, source: MailMessage, keep: MailMessage, updates: dict) -> dict:
    """Reconcile the confirmed destination against the latest poll under the account lock."""
    uid, generation = updates["provider_uid"], updates["imap_uidvalidity"]
    qualified = f"{generation}:{uid}"
    cursor = await session.scalar(select(MailSyncState.cursor).where(MailSyncState.account_id == source.account_id))
    rows = (await session.scalars(select(MailMessage).where(MailMessage.account_id == source.account_id, or_(
        and_(MailMessage.folder == "INBOX", MailMessage.provider_uid == uid),
        and_(MailMessage.folder == "IMAP_DELETED", MailMessage.provider_uid == qualified),
        and_(MailMessage.folder == IMAP_RETIRED, MailMessage.provider_uid == imap_archive_uid(generation, uid)),
    )).order_by(MailMessage.id).with_for_update())).all()
    # RFC 9051 §2.3.1.1: a reset that invalidates previous UIDs increases UIDVALIDITY.
    generations = [(cursor or "").partition(":")[0], *(row.imap_uidvalidity for row in rows)]
    if any(value and value.isdigit() and int(value) > int(generation) for value in generations):
        if (keep.folder, keep.provider_uid) == (source.folder, source.provider_uid):
            return {"folder": IMAP_RETIRED, "provider_uid": f"retired:{keep.id}", "imap_uidvalidity": generation}
        return {"folder": keep.folder, "provider_uid": keep.provider_uid}

    for row in rows:
        if row.imap_uidvalidity != generation or row.rfc_message_id != source.rfc_message_id:
            if row.id != keep.id:
                row.folder, row.provider_uid = IMAP_RETIRED, f"retired:{row.id}"
            continue
        # The poll can already have seen flags or an expunge for this exact destination.
        # An expunged UID cannot reappear in the same generation.
        if updates["folder"] == "INBOX" or row.folder == IMAP_RETIRED:
            updates.update(folder=row.folder, provider_uid=row.provider_uid, is_read=row.is_read, is_flagged=row.is_flagged)
        if row.id != keep.id:
            await _merge_copy(session, keep, row, verified=True)
    return updates


async def commit_trash_move(message: MailMessage, updates: dict, *, action: MailAction | None = None, undo_id: str | None = None) -> None:
    """Publish the provider-confirmed move, its undo record and the sync fence together."""
    async with get_session_maker().begin() as session:
        account = await mail_sync_state.fence_folder_change(session, message.account_id)
        stable_uid = account.auth_type == "mistral_connector" or account.provider == "google"
        updates = updates.copy()
        validity = updates.pop("imap_source_uidvalidity", None)
        left_inbox = message.folder == "INBOX" and updates["folder"] == "TRASH"
        if left_inbox and not stable_uid and account.provider in ("microsoft", "imap"):
            record = await session.get(MailAction, undo_id) if undo_id else action
            if record is None:
                raise LookupError("Trash move has no action record")
            record.inbox_source_uid = message.provider_uid
            record.imap_source_uidvalidity = validity
        keep = await get_message(message.id, session)
        if keep is None:
            if account.auth_type == "mistral_connector":
                raise LookupError("Connector message identity changed during move")
            # A native full rebuild can remove this row between dispatch and success.
            # Carry its public ID and undo history into the confirmed destination.
            keep = MailMessage(**message.model_dump(exclude={"search_tsv"}))
            # HTML is deferred on the dispatched row; reread it after an inbox restore.
            keep.body_html, keep.html_status, keep.html_version = None, "pending", 0
        if account.provider == "imap" and updates["folder"] == "INBOX" and updates.get("imap_uidvalidity"):
            updates = await _imap_restore_state(session, message, keep, updates)
        else:
            target_uid = updates.get("provider_uid", message.provider_uid)
            if stable_uid:
                match = MailMessage.provider_uid == message.provider_uid
            else:
                match = and_(MailMessage.folder == updates["folder"], MailMessage.provider_uid == target_uid)
                if account.provider == "microsoft":
                    match = or_(match, MailMessage.provider_uid == message.provider_uid)
                elif left_inbox and validity:
                    cursor = await session.scalar(select(MailSyncState.cursor).where(MailSyncState.account_id == account.id))
                    same_generation = or_(MailMessage.imap_uidvalidity == validity, and_(MailMessage.imap_uidvalidity.is_(None),
                                          (cursor or "").partition(":")[0] == validity))
                    match = or_(match, and_(MailMessage.folder == "INBOX", MailMessage.provider_uid == message.provider_uid,
                                            MailMessage.rfc_message_id == message.rfc_message_id, same_generation))
            copies = await session.scalars(select(MailMessage).where(MailMessage.account_id == account.id,
                MailMessage.id != keep.id, match).order_by(MailMessage.id).with_for_update())
            for other in copies:
                if account.provider == "imap" and updates["folder"] == "INBOX":
                    # An unbound IMAP destination cannot identify another delivery by UID.
                    other.folder, other.provider_uid = IMAP_RETIRED, f"retired:{other.id}"
                    continue
                if account.provider == "imap" and (not message.rfc_message_id or message.rfc_message_id != other.rfc_message_id):
                    raise ValueError("Conflicting IMAP folder UID")
                await _merge_copy(session, keep, other, verified=True)
        await session.flush()
        for key, value in updates.items():
            setattr(keep, key, value)
        session.add(keep)
        if action is not None:
            action.message_id = keep.id
            session.add(action)


async def archived_connector_members(account_id: str, listed: Collection[str], threads: Collection[str], since: datetime):
    """Find scoped archive candidates when a provider lists message IDs or just thread IDs."""
    if not listed and not threads:
        return []
    async with get_session_maker()() as session:
        stmt = select(MailMessage.provider_uid, MailMessage.thread_id).where(MailMessage.account_id == account_id,
            MailMessage.folder.in_(("ARCHIVE", "TRASH")), or_(MailMessage.provider_uid == any_(bindparam("listed", type_=ARRAY(String))),
            and_(MailMessage.thread_id == any_(bindparam("threads", type_=ARRAY(String))),
                 or_(MailMessage.internal_date >= since, MailMessage.internal_date.is_(None))))).order_by(MailMessage.id)
        return (await session.execute(stmt, {"listed": list(listed), "threads": list(threads)})).all()


async def stale_body_texts(account_id: str, folder: str, version: int, uids: list[str], *, session: AsyncSession | None = None) -> dict[str, str]:
    """Stored text of the given rows whose body comes from an older text extraction, by provider id."""
    if not uids:
        return {}
    stmt = select(MailMessage.provider_uid, MailMessage.body_text).where(
        MailMessage.account_id == account_id, MailMessage.folder == folder, MailMessage.body_version < version, MailMessage.provider_uid.in_(uids))
    async with (nullcontext(session) if session is not None else get_session_maker()()) as session:
        return {uid: text for uid, text in (await session.execute(stmt)).all()}


async def stale_bodies(account_id: str, folder: str, version: int, *, limit: int, exclude: Collection[str] = ()) -> list[MailMessage]:
    """The newest rows whose body comes from an older text extraction, to re-read from the provider;
    `exclude` names rows that wait for a later try."""
    stmt = select(MailMessage).where(MailMessage.account_id == account_id, MailMessage.folder == folder, MailMessage.body_version < version)
    if exclude:
        stmt = stmt.where(MailMessage.id.not_in(list(exclude)))
    stmt = (stmt.order_by(MailMessage.internal_date.desc().nulls_last(), MailMessage.id).limit(limit))
    async with get_session_maker()() as session:
        return list((await session.execute(stmt)).scalars().all())


async def stale_html_bodies(account_id: str, version: int, *, limit: int, exclude: Collection[str] = ()) -> list[MailMessage]:
    """Only INBOX rows are safe to re-read with the current provider adapters."""
    stmt = select(MailMessage).where(MailMessage.account_id == account_id, MailMessage.folder == "INBOX", MailMessage.html_version < version)
    if exclude:
        stmt = stmt.where(MailMessage.id.not_in(list(exclude)))
    stmt = stmt.order_by(MailMessage.internal_date.desc().nulls_last(), MailMessage.id).limit(limit)
    async with get_session_maker()() as session:
        return list((await session.execute(stmt)).scalars().all())


async def set_html(message_id: str, *, expected_uid: str, expected_message_id: str, expected_uidvalidity: str | None, version: int, status: str, html: str | None, session: AsyncSession | None = None) -> None:
    """Do not attach a late response to a moved, reidentified or already refreshed mail."""
    if status not in {"available", "absent", "too_large", "unavailable"}:
        raise ValueError("invalid HTML state")
    async with _writer(session) as session:
        row = (await session.execute(select(MailMessage).where(MailMessage.id == message_id).with_for_update())).scalar_one_or_none()
        if row is None or row.folder != "INBOX" or row.provider_uid != expected_uid or row.rfc_message_id != expected_message_id or row.imap_uidvalidity != expected_uidvalidity or row.html_version >= version:
            return
        row.body_html, row.html_status, row.html_version = html, status, version


async def _drop_insight(session, message_id: str) -> bool:
    # The row is written first: its lock makes an insight being stored for the old text either land
    # before this delete or see the new text (mail_insight.store_if_missing, `summarized`).
    await session.flush()
    removed = await session.execute(delete(MailInsight).where(MailInsight.message_id == message_id))
    return bool(removed.rowcount)


async def set_body(message_id: str, *, session: AsyncSession | None = None, body_version: int, body_text: str | None = None, snippet: str | None = None, drop_insight: bool = False) -> None:
    """Store a re-read body; without text only the version moves on (the provider no longer has the mail).
    `drop_insight` removes its insight in the same transaction."""
    async with _writer(session) as session:
        row = await get_message(message_id, session)
        if row is None:
            return
        row.body_version = body_version
        if body_text is not None:
            row.body_text = body_text
        if snippet is not None:
            row.snippet = snippet
        if drop_insight:
            await _drop_insight(session, message_id)


async def delete_folder(account_id: str, folder: str, *, session: AsyncSession | None = None, exclude_uids: Collection[str] = ()) -> int:
    conditions = [MailMessage.account_id == account_id, MailMessage.folder == folder]
    if exclude_uids:
        conditions.append(MailMessage.provider_uid.not_in(exclude_uids))
    async with _writer(session) as session:
        ids = select(MailMessage.id).where(*conditions)
        await session.execute(delete(MailFiling).where(MailFiling.message_id.in_(ids)))
        await session.execute(delete(MailLabelAssignment).where(MailLabelAssignment.message_id.in_(ids)))
        result = await session.execute(delete(MailMessage).where(*conditions))
        return result.rowcount


async def find_by_rfc_message_id(account_id: str, rfc_message_id: str) -> MailMessage | None:
    async with get_session_maker()() as session:
        return (await session.execute(select(MailMessage).where(MailMessage.account_id == account_id, MailMessage.rfc_message_id == rfc_message_id).limit(1))).scalar_one_or_none()
