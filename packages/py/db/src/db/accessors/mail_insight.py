"""Access insights by message."""

import json
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from db.engine import get_session_maker
from db.models.mail_insight import MailInsight
from db.models.mail_message import MailMessage


def seed_row(*, id: str, message_id: str, points: list[str], deadline: date, followups: list[str]) -> MailInsight:
    """Construct an insight with a stable ID for the local seed transaction."""
    return MailInsight(id=id, message_id=message_id, points_json=json.dumps(points), deadline=deadline,
                       needs_reply=True, followups_json=json.dumps(followups), model="mistral-small-latest", locale="de")


async def get(message_id: str) -> MailInsight | None:
    async with get_session_maker()() as session:
        return (await session.execute(select(MailInsight).where(MailInsight.message_id == message_id))).scalar_one_or_none()


async def get_for_messages(ids: list[str]) -> dict[str, MailInsight]:
    async with get_session_maker()() as session:
        rows = (await session.execute(select(MailInsight).where(MailInsight.message_id.in_(ids)))).scalars().all()
        return {row.message_id: row for row in rows}


async def store_if_missing(message_id: str, *, points: list[str], deadline: date | None, needs_reply: bool, followups: list[str], model: str,
                 summarized: str | None = None, locale: str | None = None) -> MailInsight | None:
    """Keep the first insight for a language; replace it when the requested locale changes.

    With `summarized`, keep the mail row locked and store only if the body still matches.
    A body change in flight returns None so the caller can summarize the new text.
    """
    row = MailInsight(message_id=message_id, points_json=json.dumps(points), deadline=deadline, needs_reply=needs_reply, followups_json=json.dumps(followups), model=model, locale=locale)
    statement = insert(MailInsight).values(**row.model_dump())
    if locale is None:
        stmt = statement.on_conflict_do_nothing(index_elements=[MailInsight.message_id]).returning(MailInsight)
    else:
        stmt = statement.on_conflict_do_update(index_elements=[MailInsight.message_id], set_={
            'points_json': row.points_json, 'deadline': row.deadline, 'needs_reply': row.needs_reply,
            'followups_json': row.followups_json, 'model': row.model, 'locale': locale, 'created_at': row.created_at,
        }, where=MailInsight.locale.is_distinct_from(locale)).returning(MailInsight)
    async with get_session_maker()() as session:
        if summarized is not None:
            current = (await session.execute(select(MailMessage.body_text).where(MailMessage.id == message_id).with_for_update(read=True))).scalar_one_or_none()
            if current != summarized:
                return None
        result = (await session.execute(stmt)).scalar_one_or_none()
        if result is None:
            result = (await session.execute(select(MailInsight).where(MailInsight.message_id == message_id))).scalar_one_or_none()
        await session.commit()
        return result


async def delete_for_account(account_id: str) -> None:
    async with get_session_maker()() as session:
        await session.execute(delete(MailInsight).where(MailInsight.message_id.in_(select(MailMessage.id).where(MailMessage.account_id == account_id))))
        await session.commit()
