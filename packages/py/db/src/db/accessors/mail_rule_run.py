"""Deduplicate rule execution per message and rule version."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from db.engine import get_session_maker
from db.models.mail_rule_run import MailRuleRun


async def create_once(rule_id: str, rule_version: int, message_id: str, account_id: str) -> tuple[MailRuleRun, bool]:
    candidate = MailRuleRun(rule_id=rule_id, rule_version=rule_version, message_id=message_id, account_id=account_id)
    stmt = insert(MailRuleRun).values(**candidate.model_dump()).on_conflict_do_nothing(
        constraint="uq_mail_rule_run_version_message").returning(MailRuleRun)
    async with get_session_maker().begin() as session:
        created = (await session.execute(stmt)).scalar_one_or_none()
        if created is not None:
            return created, True
        existing = (await session.scalars(select(MailRuleRun).where(
            MailRuleRun.rule_id == rule_id, MailRuleRun.rule_version == rule_version,
            MailRuleRun.message_id == message_id))).one()
        return existing, False


async def prepare(run_id: str, matcher, snapshot: dict[str, int] | None = None) -> MailRuleRun:
    """Claim a run and apply local assignments while the rule and message are locked."""
    import json

    from db.models._common import utcnow
    from db.models.mail_filing import MailFiling
    from db.models.mail_label_assignment import MailLabelAssignment
    from db.models.mail_message import MailMessage
    from db.models.mail_rule import MailRule

    async with get_session_maker().begin() as session:
        run = (await session.scalars(select(MailRuleRun).where(MailRuleRun.id == run_id).with_for_update())).one()
        if run.status != "queued":
            return run
        message = (await session.scalars(select(MailMessage).where(MailMessage.id == run.message_id).with_for_update())).one_or_none()
        rules = list((await session.scalars(select(MailRule).order_by(MailRule.position, MailRule.created_at, MailRule.id).with_for_update())).all())
        if snapshot is not None:
            rules = [item for item in rules if snapshot.get(item.id) == item.version]
        rule = next((item for item in rules if item.id == run.rule_id), None)
        if rule is None or rule.version != run.rule_version or not rule.is_active:
            run.status = "skipped_stale"
        elif message is None or message.account_id != run.account_id or not matcher(rule, message):
            run.status = "skipped_conflict"
        else:
            winner = next((item for item in rules if item.folder_id and matcher(item, message)), None)
            if rule.folder_id and winner and winner.id == rule.id:
                stmt = insert(MailFiling).values(message_id=message.id, folder_id=rule.folder_id)
                await session.execute(stmt.on_conflict_do_update(index_elements=[MailFiling.message_id],
                    set_={"folder_id": rule.folder_id}, where=MailFiling.folder_id.is_(None)))
            if rule.label_id:
                stmt = insert(MailLabelAssignment).values(message_id=message.id, label_id=rule.label_id)
                await session.execute(stmt.on_conflict_do_nothing())
            pending = {kind: "pending" for kind, enabled in (("mark_read", rule.mark_read), ("mark_unread", rule.mark_unread), ("archive", rule.archive)) if enabled}
            run.provider_results_json = json.dumps(pending)
            run.status = "local_done" if pending else "done"
        run.updated_at = utcnow()
        return run


async def record_action(run_id: str, kind: str, *, expected: str, result: str) -> tuple[MailRuleRun, bool]:
    """Conditionally transition one provider action; a dispatched action is never resent."""
    import json
    from db.models._common import utcnow
    from db.models.mail_rule import MailRule

    async with get_session_maker().begin() as session:
        run = (await session.scalars(select(MailRuleRun).where(MailRuleRun.id == run_id).with_for_update())).one()
        states = json.loads(run.provider_results_json)
        changed = states.get(kind) == expected
        if changed:
            if result == "dispatching":
                rule = (await session.scalars(select(MailRule).where(MailRule.id == run.rule_id).with_for_update())).one_or_none()
                if rule is None or not rule.is_active or rule.version != run.rule_version:
                    result = "skipped_stale"
                    changed = False
            states[kind] = result
            run.provider_results_json = json.dumps(states)
            if "unknown" in states.values():
                run.status = "unknown"
            elif all(state in {"done", "skipped_unsupported", "skipped_stale"} for state in states.values()):
                run.status = "done"
            run.updated_at = utcnow()
        return run, changed


async def list_for_rule(rule_id: str, limit: int = 100) -> list[MailRuleRun]:
    async with get_session_maker()() as session:
        rows = await session.scalars(select(MailRuleRun).where(MailRuleRun.rule_id == rule_id)
                                     .order_by(MailRuleRun.created_at.desc(), MailRuleRun.id.desc()).limit(limit))
        return list(rows.all())
