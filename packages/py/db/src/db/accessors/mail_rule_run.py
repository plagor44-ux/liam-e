"""Deduplicate rule execution per message and rule version, and provider actions across versions.

A run records what one rule version did to one mail. Its provider actions are additionally
claimed in ``mail_rule_action`` under the identity (rule, mail, action kind), so an edit that
creates a new version never repeats an action the rule already handled for that mail. Only the
run named by the claim may dispatch it. Locks are taken in the order runs, messages, rules, then
action claims.
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from db.engine import get_session_maker
from db.models.mail_rule_action import MailRuleAction
from db.models.mail_rule_run import MailRuleRun

PROVIDER_KINDS = ("mark_read", "mark_unread", "archive")
_OBSERVED_OUTCOMES = ("partial_permission", "partial", "reconciled_permission", "reconciled")
_TERMINAL_OUTCOMES = {"done", "skipped_permission", "skipped_unsupported", "skipped_stale", "overridden", *_OBSERVED_OUTCOMES}
# Skips recorded before any dispatch; an explicit manual apply may claim these again.
_REARMABLE = {"skipped_unsupported", "skipped_permission", "skipped_stale"}


class RuleInactive(Exception):
    """Manual application was refused because the rule is paused."""


def enabled_kinds(rule) -> list[str]:
    return [kind for kind in PROVIDER_KINDS if getattr(rule, kind)]


def _rearmable(action: MailRuleAction) -> bool:
    return action.status in _REARMABLE and not action.dispatched


async def _owner(session, action: MailRuleAction, runs: dict[str, MailRuleRun]) -> MailRuleRun | None:
    # A run's version never changes, so reading it needs no lock (claims are locked after runs).
    return runs.get(action.run_id) or await session.get(MailRuleRun, action.run_id)


async def _reclaimable(session, action: MailRuleAction, version: int, runs: dict[str, MailRuleRun]) -> bool:
    """A proven skip, or a pending claim whose older run can never dispatch it.

    Dispatch requires the run's version to equal the rule's under the rule lock, and versions only
    grow, so a pending claim of an older version would end as a stale skip without being sent.
    """
    if _rearmable(action):
        return True
    if action.status != "pending":
        return False
    owner = await _owner(session, action, runs)
    return owner is None or owner.rule_version < version


async def _claim(session, run: MailRuleRun, kind: str, *, rearm: bool,
                 runs: dict[str, MailRuleRun] | None = None) -> tuple[MailRuleAction, bool]:
    """Claim one provider action for ``run``; an explicit manual apply may reclaim an undispatched one."""
    from db.models._common import utcnow

    candidate = MailRuleAction(rule_id=run.rule_id, message_id=run.message_id, kind=kind, run_id=run.id)
    created = (await session.execute(insert(MailRuleAction).values(**candidate.model_dump()).on_conflict_do_nothing(
        constraint="uq_mail_rule_action_identity").returning(MailRuleAction))).scalar_one_or_none()
    if created is not None:
        return created, True
    action = (await session.scalars(select(MailRuleAction).where(
        MailRuleAction.rule_id == run.rule_id, MailRuleAction.message_id == run.message_id,
        MailRuleAction.kind == kind).with_for_update())).one()
    if rearm and await _reclaimable(session, action, run.rule_version, runs or {}):
        action.run_id, action.status, action.updated_at = run.id, "pending", utcnow()
        return action, True
    return action, False


async def _lock_claim(session, run: MailRuleRun, kind: str) -> MailRuleAction | None:
    return (await session.scalars(select(MailRuleAction).where(
        MailRuleAction.rule_id == run.rule_id, MailRuleAction.message_id == run.message_id,
        MailRuleAction.kind == kind).with_for_update())).one_or_none()


async def _in_progress(session, action: MailRuleAction, version: int, runs: dict[str, MailRuleRun]) -> bool:
    """An open claim its run can still finish: not stopped by an uncertain outcome, not outdated."""
    if action.status not in {"pending", "dispatching"}:
        return False
    owner = await _owner(session, action, runs)
    return (owner is not None and owner.status != "unknown"
            and (action.status == "dispatching" or owner.rule_version == version))


async def _owned_action(session, run: MailRuleRun, kind: str, expected: str) -> MailRuleAction | None:
    """Lock the claim ``run`` holds in state ``expected``; a run from before the claims table gets one."""
    candidate = MailRuleAction(rule_id=run.rule_id, message_id=run.message_id, kind=kind, run_id=run.id,
                               status=expected, dispatched=expected != "pending")
    await session.execute(insert(MailRuleAction).values(**candidate.model_dump()).on_conflict_do_nothing(
        constraint="uq_mail_rule_action_identity"))
    action = (await session.scalars(select(MailRuleAction).where(
        MailRuleAction.rule_id == run.rule_id, MailRuleAction.message_id == run.message_id,
        MailRuleAction.kind == kind).with_for_update())).one()
    return action if action.run_id == run.id and action.status == expected else None


def _merged_outcome(values: set[str | None]) -> str:
    """Conservative outcome when two records of the same provider action converge."""
    if values & {"unknown", "dispatching"}:
        return "unknown"
    if "done" in values:
        return "done"
    if observed := next((outcome for outcome in _OBSERVED_OUTCOMES if outcome in values), None):
        return observed
    for outcome in ("skipped_permission", "skipped_unsupported", "skipped_stale", "overridden"):
        if outcome in values:
            return outcome
    return "pending"


async def new_provider_work(rule_id: str, version: int, kinds: list[str], message_ids: list[str], *, new_version: bool) -> set[str]:
    """Mails on which applying the rule now would claim at least one provider action.

    An action already claimed for the mail by any version of the rule does not count, except one
    an explicit apply would claim again (see ``_reclaimable``). A run of the current version that
    is still reserved or has an uncertain outcome claims nothing new; ``new_version`` previews an
    edit, whose apply would run under the next version.
    """
    if not kinds or not message_ids:
        return set()
    applied_version = version + 1 if new_version else version
    async with get_session_maker()() as session:
        rows = (await session.scalars(select(MailRuleAction).where(
            MailRuleAction.rule_id == rule_id, MailRuleAction.message_id.in_(message_ids),
            MailRuleAction.kind.in_(kinds)))).all()
        blocked = set() if new_version else set((await session.scalars(select(MailRuleRun.message_id).where(
            MailRuleRun.rule_id == rule_id, MailRuleRun.rule_version == version, MailRuleRun.message_id.in_(message_ids),
            MailRuleRun.status.in_(("queued", "unknown"))))).all())
        handled = {(row.message_id, row.kind) for row in rows
                   if not await _reclaimable(session, row, applied_version, {})}
    return {message_id for message_id in message_ids
            if message_id not in blocked and any((message_id, kind) not in handled for kind in kinds)}


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
    folded: dict[str, str] = {}
    for other in (row for row in rows if row.message_id == duplicate_id):
        current = kept.get((other.rule_id, other.rule_version))
        if current is None:
            other.message_id = keep_id
            kept[(other.rule_id, other.rule_version)] = other
            continue
        folded[other.id] = current.id
        left, right = json.loads(current.provider_results_json), json.loads(other.provider_results_json)
        merged = {kind: _merged_outcome({left.get(kind), right.get(kind)}) for kind in left.keys() | right.keys()}
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
    actions = (await session.scalars(select(MailRuleAction).where(MailRuleAction.message_id.in_([keep_id, duplicate_id]))
                                     .order_by(MailRuleAction.id).with_for_update())).all()
    claimed = {(row.rule_id, row.kind): row for row in actions if row.message_id == keep_id}
    for other in (row for row in actions if row.message_id == duplicate_id):
        current = claimed.get((other.rule_id, other.kind))
        if current is None:
            other.message_id, other.run_id, other.updated_at = keep_id, folded.get(other.run_id, other.run_id), utcnow()
            claimed[(other.rule_id, other.kind)] = other
            continue
        # Both copies claimed the action: keep one claim, never more permissive than either.
        current.status = _merged_outcome({current.status, other.status})
        current.dispatched = current.dispatched or other.dispatched or current.status == "unknown"
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
    """Commit a confirmed batch and its newly claimed provider jobs together.

    Each enabled provider action is claimed once per rule and mail across versions. A claim that
    exists already is reported as queued while its run can still dispatch it, otherwise as
    previously handled. This explicit apply claims again only what was never sent: a proven
    pre-dispatch skip or a pending claim of an older version (see ``_reclaimable``).
    """
    import json
    from db.accessors import mail_rule
    from db.models._common import new_id, utcnow
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
        if not rule.is_active:
            raise RuleInactive()
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
            owned = []
            for kind in enabled_kinds(rule):
                if not created and run.status == 'queued':
                    # A reserved run claims its actions when its worker prepares it.
                    result['provider_queued'] += 1
                    continue
                # Within one run an uncertain outcome still stops its remaining actions.
                if created or run.status != 'unknown':
                    action, claimed = await _claim(session, run, kind, rearm=True, runs=by_run_id)
                else:
                    action, claimed = await _lock_claim(session, run, kind), False
                if claimed:
                    owned.append(kind)
                elif action is not None and await _in_progress(session, action, version, by_run_id):
                    result['provider_queued'] += 1
                else:
                    result['provider_skipped'] += 1
            if changed:
                result['app_changed'] += 1
            if changed or owned:
                result['matched'] += 1
                realized.add(message.id)
            if created and not changed and not owned:
                await session.delete(run)
                continue
            if created:
                provider = {kind: 'pending' for kind in owned}
                if rule.folder_id and winner and winner.id != rule.id:
                    provider['_folder'] = 'overridden'
                run.provider_results_json = json.dumps(provider)
                run.status = 'local_done' if owned else 'done'
                run.updated_at = utcnow()
            elif owned:
                run.provider_results_json = json.dumps(json.loads(run.provider_results_json) | {kind: 'pending' for kind in owned})
                run.status, run.updated_at = 'local_done', utcnow()
            if owned:
                payload = {'account_id': message.account_id, 'message_id': message.id,
                           'rules': [[rule.id, rule.version]], 'snapshot': snapshot}
                # An earlier job of this run may still be open; claims keep extra jobs harmless.
                suffix = '' if created else f':{new_id()}'
                job = MailJob(kind='mail_rule_apply', payload_json=json.dumps(payload),
                    dedupe_key=f'mail-rule-manual:{rule.id}:{rule.version}:{message.id}{suffix}')
                await session.execute(insert(MailJob).values(**job.model_dump()).on_conflict_do_nothing())
                result['provider_queued'] += len(owned)
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
            # Automatic runs claim only actions no version of the rule has claimed for this mail.
            pending = {kind: "pending" for kind in enabled_kinds(rule) if (await _claim(session, run, kind, rearm=False))[1]}
            has_provider_actions = bool(pending)
            if rule.folder_id and winner and winner.id != rule.id:
                pending['_folder'] = 'overridden'
            run.provider_results_json = json.dumps(pending)
            run.status = "local_done" if has_provider_actions else "done"
        run.updated_at = utcnow()
        return run


async def record_action(run_id: str, kind: str, *, expected: str, result: str) -> tuple[MailRuleRun, bool]:
    """Conditionally transition one provider action; a dispatched action is never resent.

    The run and its claim move together. A run that no longer holds the claim, for example after
    a verified duplicate merge, records ``skipped_stale`` instead of dispatching.
    """
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
            action = await _owned_action(session, run, kind, expected)
            if action is None and expected == "pending":
                result = "skipped_stale"
                changed = False
            elif action is not None:
                action.status, action.updated_at = result, utcnow()
                action.dispatched = action.dispatched or result == "dispatching"
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
