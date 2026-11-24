"""Deduplicate rule execution per message and rule version."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from db.engine import get_session_maker
from db.models.mail_rule_run import MailRuleRun

_OBSERVED_OUTCOMES = ("partial_permission", "partial", "reconciled_permission", "reconciled")
_TERMINAL_OUTCOMES = {"done", "skipped_permission", "skipped_unsupported", "skipped_stale", "overridden", *_OBSERVED_OUTCOMES}


async def record_imap_rebuild(session, account_id: str, generation: str) -> None:
    """Record a rule-level notice without creating runs for ambiguous messages."""
    await record_import_notice(session, account_id, f'imap-rebuild:{account_id}:{generation}', 'skipped_imap_rebuild')


async def record_import_notice(session, account_id: str, notice_id: str, status: str) -> None:
    """Record one notice per applicable rule and import identity."""
    from sqlalchemy import or_
    from db.models.mail_rule import MailRule

    rules = (await session.execute(select(MailRule.id, MailRule.version).where(
        MailRule.is_active.is_(True), or_(MailRule.account_id.is_(None), MailRule.account_id == account_id)))).all()
    for rule_id, version in rules:
        existing = await session.scalar(select(MailRuleRun.id).where(
            MailRuleRun.rule_id == rule_id, MailRuleRun.message_id == notice_id))
        if existing is not None:
            continue
        row = MailRuleRun(rule_id=rule_id, rule_version=version, account_id=account_id,
                          message_id=notice_id, status=status)
        await session.execute(insert(MailRuleRun).values(**row.model_dump()).on_conflict_do_nothing(
            constraint='uq_mail_rule_run_version_message'))


async def merge_verified(session, keep_id: str, duplicate_id: str) -> None:
    """Preserve rule/version deduplication when verified mail identities converge."""
    import json
    from db.models._common import utcnow

    rows = (await session.scalars(select(MailRuleRun).where(MailRuleRun.message_id.in_([keep_id, duplicate_id]))
                                  .order_by(MailRuleRun.rule_id, MailRuleRun.rule_version, MailRuleRun.id).with_for_update())).all()
    kept = {(row.rule_id, row.rule_version): row for row in rows if row.message_id == keep_id}
    for other in (row for row in rows if row.message_id == duplicate_id):
        current = kept.get((other.rule_id, other.rule_version))
        if current is None:
            other.message_id = keep_id
            kept[(other.rule_id, other.rule_version)] = other
            continue
        left, right = json.loads(current.provider_results_json), json.loads(other.provider_results_json)
        merged = {}
        for kind in left.keys() | right.keys():
            values = {left.get(kind), right.get(kind)}
            if values & {'unknown', 'dispatching'}:
                merged[kind] = 'unknown'
            elif 'done' in values:
                merged[kind] = 'done'
            elif observed := next((outcome for outcome in _OBSERVED_OUTCOMES if outcome in values), None):
                merged[kind] = observed
            elif 'skipped_permission' in values:
                merged[kind] = 'skipped_permission'
            elif 'skipped_unsupported' in values:
                merged[kind] = 'skipped_unsupported'
            elif 'skipped_stale' in values:
                merged[kind] = 'skipped_stale'
            elif 'overridden' in values:
                merged[kind] = 'overridden'
            else:
                merged[kind] = 'pending'
        current.provider_results_json = json.dumps(merged)
        if 'unknown' in merged.values() or 'unknown' in (current.status, other.status):
            current.status = 'unknown'
        elif 'pending' in merged.values():
            current.status = 'local_done'
        elif merged or 'done' in (current.status, other.status):
            current.status = 'done'
        elif 'skipped_stale' in (current.status, other.status):
            current.status = 'skipped_stale'
        elif 'skipped_conflict' in (current.status, other.status):
            current.status = 'skipped_conflict'
        current.updated_at = utcnow()
        await session.delete(other)
    await session.flush()


async def create_once(rule_id: str, rule_version: int, message_id: str, account_id: str) -> tuple[MailRuleRun, bool]:
    async with get_session_maker().begin() as session:
        return await _create_once(session, rule_id, rule_version, message_id, account_id)


async def _create_once(session, rule_id: str, rule_version: int, message_id: str, account_id: str) -> tuple[MailRuleRun, bool]:
    candidate = MailRuleRun(rule_id=rule_id, rule_version=rule_version, message_id=message_id, account_id=account_id)
    stmt = insert(MailRuleRun).values(**candidate.model_dump()).on_conflict_do_nothing(
        constraint="uq_mail_rule_run_version_message").returning(MailRuleRun)
    created = (await session.execute(stmt)).scalar_one_or_none()
    if created is not None:
        return created, True
    existing = (await session.scalars(select(MailRuleRun).where(
        MailRuleRun.rule_id == rule_id, MailRuleRun.rule_version == rule_version,
        MailRuleRun.message_id == message_id))).one()
    return existing, False


def local_changes(message, rule, rules, folder_id, label_ids, matcher):
    winner = next((item for item in rules if item.folder_id and matcher(item, message)), None)
    folder_change = bool(rule.folder_id and not folder_id and winner and winner.id == rule.id)
    missing_labels = [label_id for label_id in rule.label_ids if label_id not in label_ids]
    return winner, folder_change, missing_labels


async def _apply_assignments(session, message, rule, rules, matcher):
    from db.models.mail_filing import MailFiling
    from db.models.mail_label_assignment import MailLabelAssignment

    filing = await session.get(MailFiling, message.id)
    labels = (await session.scalars(select(MailLabelAssignment.label_id).where(MailLabelAssignment.message_id == message.id))).all()
    winner, folder_change, missing = local_changes(message, rule, rules, filing.folder_id if filing else None, labels, matcher)
    changed = False
    if folder_change:
        stmt = insert(MailFiling).values(message_id=message.id, folder_id=rule.folder_id)
        result = await session.execute(stmt.on_conflict_do_update(index_elements=[MailFiling.message_id],
            set_={"folder_id": rule.folder_id}, where=MailFiling.folder_id.is_(None)).returning(MailFiling.message_id))
        changed = result.scalar_one_or_none() is not None
    for label_id in missing:
        stmt = insert(MailLabelAssignment).values(message_id=message.id, label_id=label_id)
        result = await session.execute(stmt.on_conflict_do_nothing().returning(MailLabelAssignment.message_id))
        changed = result.scalar_one_or_none() is not None or changed
    return winner, changed


async def apply_manual(rule_id: str, version: int, candidates, matcher, preview_ids: list[str] | None = None) -> dict[str, int]:
    """Commit a confirmed batch and its first-time provider jobs together."""
    import json
    from db.accessors import mail_rule
    from db.models._common import utcnow
    from db.models.mail_job import MailJob
    from db.models.mail_message import MailMessage
    from db.models.mail_rule import MailRule

    result = {"matched": 0, "app_changed": 0, "provider_skipped": 0, "provider_queued": 0, "conflicts": 0}
    realized = set()
    async with get_session_maker().begin() as session:
        runs = {}
        # Reserve run identities before message and rule locks, matching the worker lock order.
        for message in sorted(candidates, key=lambda item: item.id):
            runs[message.id] = await _create_once(session, rule_id, version, message.id, message.account_id)
        ids = sorted(runs)
        locked_runs = (await session.scalars(select(MailRuleRun).where(MailRuleRun.id.in_([run.id for run, _ in runs.values()]))
            .order_by(MailRuleRun.id).with_for_update())).all()
        by_run_id = {run.id: run for run in locked_runs}
        messages = (await session.scalars(select(MailMessage).where(MailMessage.id.in_(ids)).order_by(MailMessage.id).with_for_update())).all()
        by_message_id = {message.id: message for message in messages}
        rules = list((await session.scalars(select(MailRule).order_by(MailRule.id).with_for_update())).all())
        rules.sort(key=lambda item: (item.position, item.created_at, item.id))
        rule = next((item for item in rules if item.id == rule_id), None)
        if rule is None:
            raise LookupError('Rule not found')
        if rule.version != version:
            raise mail_rule.VersionConflict()
        active_rules = [item for item in rules if item.is_active]
        snapshot = [[item.id, item.version] for item in active_rules]
        for candidate in candidates:
            original, created = runs[candidate.id]
            run = by_run_id[original.id]
            message = by_message_id.get(candidate.id)
            if message is None or message.account_id != candidate.account_id or not matcher(rule, message):
                result['conflicts'] += 1
                if created:
                    await session.delete(run)
                continue
            winner, changed = await _apply_assignments(session, message, rule, active_rules, matcher)
            provider = {kind: 'pending' for kind, enabled in (('mark_read', rule.mark_read), ('mark_unread', rule.mark_unread), ('archive', rule.archive)) if enabled}
            if not created:
                states = json.loads(run.provider_results_json)
                for kind in provider:
                    if run.status == 'queued' or (run.status != 'unknown' and states.get(kind) in {'pending', 'dispatching'}):
                        result['provider_queued'] += 1
                    else:
                        result['provider_skipped'] += 1
            if changed:
                result['app_changed'] += 1
            if changed or (created and provider):
                result['matched'] += 1
                realized.add(message.id)
            if not created:
                continue
            if not changed and not provider:
                await session.delete(run)
                continue
            if rule.folder_id and winner and winner.id != rule.id:
                provider['_folder'] = 'overridden'
            run.provider_results_json = json.dumps(provider)
            run.status = 'local_done' if any(value == 'pending' for value in provider.values()) else 'done'
            run.updated_at = utcnow()
            if run.status == 'local_done':
                payload = {'account_id': message.account_id, 'message_id': message.id,
                           'rules': [[rule.id, rule.version]], 'snapshot': snapshot}
                job = MailJob(kind='mail_rule_apply', payload_json=json.dumps(payload),
                    dedupe_key=f'mail-rule-manual:{rule.id}:{rule.version}:{message.id}')
                await session.execute(insert(MailJob).values(**job.model_dump()).on_conflict_do_nothing())
                result['provider_queued'] += len([value for value in provider.values() if value == 'pending'])
    if preview_ids is not None:
        result['conflicts'] = len(set(preview_ids) - realized)
    return result


async def prepare(run_id: str, matcher, snapshot: dict[str, int] | None = None) -> MailRuleRun:
    """Claim a run and apply local assignments while the rule and message are locked."""
    import json

    from db.models._common import utcnow
    from db.models.mail_message import MailMessage
    from db.models.mail_rule import MailRule

    async with get_session_maker().begin() as session:
        run = (await session.scalars(select(MailRuleRun).where(MailRuleRun.id == run_id).with_for_update())).one()
        if run.status != "queued":
            return run
        message = (await session.scalars(select(MailMessage).where(MailMessage.id == run.message_id).with_for_update())).one_or_none()
        # Deletion also locks multiple rules by ID; sort by priority only after all locks are held.
        rules = list((await session.scalars(select(MailRule).order_by(MailRule.id).with_for_update())).all())
        rules.sort(key=lambda item: (item.position, item.created_at, item.id))
        if snapshot is not None:
            rules = [item for item in rules if snapshot.get(item.id) == item.version]
        rule = next((item for item in rules if item.id == run.rule_id), None)
        if rule is None or rule.version != run.rule_version or not rule.is_active:
            run.status = "skipped_stale"
        elif message is None or message.account_id != run.account_id or not matcher(rule, message):
            run.status = "skipped_conflict"
        else:
            winner, _ = await _apply_assignments(session, message, rule, rules, matcher)
            pending = {kind: "pending" for kind, enabled in (("mark_read", rule.mark_read), ("mark_unread", rule.mark_unread), ("archive", rule.archive)) if enabled}
            has_provider_actions = bool(pending)
            if rule.folder_id and winner and winner.id != rule.id:
                pending['_folder'] = 'overridden'
            run.provider_results_json = json.dumps(pending)
            run.status = "local_done" if has_provider_actions else "done"
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
            elif _TERMINAL_OUTCOMES.issuperset(states.values()):
                run.status = "done"
            run.updated_at = utcnow()
        return run, changed


async def list_for_rule(rule_id: str, limit: int = 100) -> list[MailRuleRun]:
    async with get_session_maker()() as session:
        rows = await session.scalars(select(MailRuleRun).where(MailRuleRun.rule_id == rule_id)
                                     .order_by(MailRuleRun.created_at.desc(), MailRuleRun.id.desc()).limit(limit))
        return list(rows.all())
