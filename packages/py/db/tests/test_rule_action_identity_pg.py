"""Each provider action of a rule runs at most once per mail across rule versions (#152)."""

import asyncio
import json
import os

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("RUN_PG_CONTRACT"), reason="RUN_PG_CONTRACT is not set")

NOTHING = {"matched": 0, "app_changed": 0, "provider_skipped": 0, "provider_queued": 0, "conflicts": 0}


async def _setup(monkeypatch, *, gate=None, writer=None):
    """A synthetic account, a matching inbox mail, a label and stubbed provider boundaries."""
    from unittest.mock import AsyncMock
    from api.mail import action_capabilities, actions
    from db.accessors import mail_filing, mail_message
    from db.engine import get_session_maker
    from db.models.mail_account import MailAccount

    async with get_session_maker().begin() as session:
        session.add(MailAccount(id="synthetic", provider="google", auth_type="oauth2", email_address="owner@example.org"))
    gate, writer = gate or AsyncMock(), writer or AsyncMock()
    monkeypatch.setattr(action_capabilities, "require_action", gate)
    monkeypatch.setattr(actions, "apply_action", writer)
    label = await mail_filing.create_destination("Work", "label")
    message = await mail_message.upsert_message(account_id="synthetic", folder="INBOX", provider_uid="one",
                                                sender_address="person@example.org")
    return message, label, gate, writer


async def _apply(rule):
    from api.routers.api.v1.mail.rules import ApplyIn, apply_rule
    return await apply_rule(rule.id, ApplyIn(expected_version=rule.version, confirm_apply=True))


async def _preview(rule, **draft):
    from api.routers.api.v1.mail.rules import RulePreviewIn, preview
    fields = {key: getattr(rule, key) for key in ("name", "sender_domain", "label_ids", "mark_read", "mark_unread", "archive", "position")}
    body = RulePreviewIn(**fields | draft, saved_rule_id=rule.id, expected_version=rule.version, preview_draft=bool(draft))
    return (await preview(body))["matched"]


async def _drain() -> int:
    from api.mail import jobs
    count = 0
    while await jobs.run_once():
        count += 1
        assert count < 20
    return count


async def _claims(rule_id):
    from sqlalchemy import select
    from db.engine import get_session_maker
    from db.models.mail_rule_action import MailRuleAction
    async with get_session_maker()() as session:
        rows = (await session.scalars(select(MailRuleAction).where(MailRuleAction.rule_id == rule_id))).all()
    return {(row.message_id, row.kind): (row.status, row.dispatched) for row in rows}


def test_rename_queues_no_repeated_provider_action(pg_db, monkeypatch):
    async def run():
        from db.accessors import mail_rule, mail_rule_run
        message, label, _, writer = await _setup(monkeypatch)
        rule = await mail_rule.create(name="Read", sender_domain="example.org", label_ids=[label.id], mark_read=True)
        assert await _preview(rule) == 1
        assert await _apply(rule) == NOTHING | {"matched": 1, "app_changed": 1, "provider_queued": 1}
        assert await _drain() == 1
        writer.assert_awaited_once_with(message.id, "mark_read")
        renamed = await mail_rule.update(rule.id, rule.version, name="Read everything")
        assert await _preview(renamed) == 0
        assert await _apply(renamed) == NOTHING | {"provider_skipped": 1}
        assert await _drain() == 0
        writer.assert_awaited_once()
        assert [run.rule_version for run in await mail_rule_run.list_for_rule(rule.id)] == [1]
        assert await _claims(rule.id) == {(message.id, "mark_read"): ("done", True)}
    asyncio.run(run())


def test_added_action_runs_once_and_readded_action_does_not_repeat(pg_db, monkeypatch):
    async def run():
        from unittest.mock import call
        from db.accessors import mail_rule
        message, label, _, writer = await _setup(monkeypatch)
        rule = await mail_rule.create(name="Read", sender_domain="example.org", label_ids=[label.id], mark_read=True)
        await _apply(rule)
        await _drain()
        assert await _preview(rule, archive=True) == 1
        added = await mail_rule.update(rule.id, rule.version, archive=True)
        assert await _preview(added) == 1
        assert await _apply(added) == NOTHING | {"matched": 1, "provider_skipped": 1, "provider_queued": 1}
        assert await _drain() == 1
        assert writer.await_args_list == [call(message.id, "mark_read"), call(message.id, "archive")]
        assert await _apply(added) == NOTHING | {"provider_skipped": 2}
        removed = await mail_rule.update(added.id, added.version, archive=False)
        assert await _apply(removed) == NOTHING | {"provider_skipped": 1}
        readded = await mail_rule.update(removed.id, removed.version, archive=True)
        assert await _preview(readded) == 0
        assert await _apply(readded) == NOTHING | {"provider_skipped": 2}
        assert await _drain() == 0
        assert writer.await_count == 2
        assert await _claims(rule.id) == {(message.id, "mark_read"): ("done", True), (message.id, "archive"): ("done", True)}
    asyncio.run(run())


def test_edit_before_dispatch_hands_the_action_to_the_next_apply_once(pg_db, monkeypatch):
    async def run():
        from db.accessors import mail_rule, mail_rule_run
        message, label, _, writer = await _setup(monkeypatch)
        rule = await mail_rule.create(name="Read", sender_domain="example.org", label_ids=[label.id], mark_read=True)
        assert await _apply(rule) == NOTHING | {"matched": 1, "app_changed": 1, "provider_queued": 1}
        renamed = await mail_rule.update(rule.id, rule.version, name="Read soon")
        # The queued run of version 1 can no longer dispatch, so the action was never handled.
        assert await _preview(renamed) == 1
        assert await _apply(renamed) == NOTHING | {"matched": 1, "provider_queued": 1}
        assert await _drain() == 2
        writer.assert_awaited_once_with(message.id, "mark_read")
        states = {run.rule_version: json.loads(run.provider_results_json) for run in await mail_rule_run.list_for_rule(rule.id)}
        assert states == {1: {"mark_read": "skipped_stale"}, 2: {"mark_read": "done"}}
        assert await _apply(renamed) == NOTHING | {"provider_skipped": 1}
        assert await _drain() == 0
    asyncio.run(run())


def test_unknown_outcome_is_never_resent_by_any_version(pg_db, monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        from api.mail import rules
        from db.accessors import mail_rule
        writer = AsyncMock(side_effect=RuntimeError("synthetic uncertain result"))
        message, label, _, _ = await _setup(monkeypatch, writer=writer)
        rule = await mail_rule.create(name="Read", sender_domain="example.org", label_ids=[label.id], mark_read=True)
        await _apply(rule)
        await _drain()
        assert await _claims(rule.id) == {(message.id, "mark_read"): ("unknown", True)}
        writer.side_effect = None
        renamed = await mail_rule.update(rule.id, rule.version, name="Read again")
        assert await _preview(renamed) == 0
        assert await _apply(renamed) == NOTHING | {"provider_skipped": 1}
        assert await _drain() == 0
        automatic = await rules.execute_rule_run(renamed.id, renamed.version, message.id, recover=True)
        assert automatic.status == "done" and json.loads(automatic.provider_results_json) == {}
        writer.assert_awaited_once()
        assert await _claims(rule.id) == {(message.id, "mark_read"): ("unknown", True)}
    asyncio.run(run())


@pytest.mark.parametrize("refusal, outcome", [("unsupported", "skipped_unsupported"), ("permission_missing", "skipped_permission")])
def test_proven_pre_dispatch_skip_runs_once_after_capability_is_granted(pg_db, monkeypatch, refusal, outcome):
    async def run():
        from unittest.mock import AsyncMock
        from api.mail import action_capabilities, rules
        from db.accessors import mail_rule, mail_rule_run
        gate = AsyncMock(side_effect=getattr(action_capabilities, refusal)())
        message, label, _, writer = await _setup(monkeypatch, gate=gate)
        rule = await mail_rule.create(name="Read", sender_domain="example.org", label_ids=[label.id], mark_read=True)
        await _apply(rule)
        await _drain()
        assert await _claims(rule.id) == {(message.id, "mark_read"): (outcome, False)}
        # Automatic replays never claim the skip again.
        await rules.execute_rule_run(rule.id, rule.version, message.id)
        assert gate.await_count == 1
        # An explicit apply checks the capability again; while it is missing nothing is sent.
        assert await _preview(rule) == 1
        assert await _apply(rule) == NOTHING | {"matched": 1, "provider_queued": 1}
        await _drain()
        assert gate.await_count == 2
        writer.assert_not_awaited()
        gate.side_effect = None
        renamed = await mail_rule.update(rule.id, rule.version, name="Read after reconnect")
        assert await _apply(renamed) == NOTHING | {"matched": 1, "provider_queued": 1}
        assert await _drain() == 1
        writer.assert_awaited_once_with(message.id, "mark_read")
        assert await _preview(renamed) == 0
        assert await _apply(renamed) == NOTHING | {"provider_skipped": 1}
        assert await _drain() == 0
        writer.assert_awaited_once()
        assert await _claims(rule.id) == {(message.id, "mark_read"): ("done", True)}
        states = {run.rule_version: json.loads(run.provider_results_json) for run in await mail_rule_run.list_for_rule(rule.id)}
        assert states == {1: {"mark_read": outcome}, 2: {"mark_read": "done"}}
    asyncio.run(run())


def test_permission_refusal_after_dispatch_stays_blocked(pg_db, monkeypatch):
    async def run():
        from unittest.mock import AsyncMock
        from api.mail import connector_write_results
        from db.accessors import mail_rule
        writer = AsyncMock(side_effect=connector_write_results.PermissionMissing())
        message, label, _, _ = await _setup(monkeypatch, writer=writer)
        rule = await mail_rule.create(name="Read", sender_domain="example.org", label_ids=[label.id], mark_read=True)
        await _apply(rule)
        await _drain()
        assert await _claims(rule.id) == {(message.id, "mark_read"): ("skipped_permission", True)}
        writer.side_effect = None
        assert await _preview(rule) == 0
        assert await _apply(rule) == NOTHING | {"provider_skipped": 1}
        assert await _drain() == 0
        writer.assert_awaited_once()
    asyncio.run(run())


def test_automatic_new_mail_run_dispatches_once(pg_db, monkeypatch):
    async def run():
        from api.mail import sync
        from api.mail.sync_types import SyncChanges
        from db.accessors import mail_filing, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_account import MailAccount
        _, label, _, writer = await _setup(monkeypatch)
        rule = await mail_rule.create(name="New mail", sender_domain="example.net", label_ids=[label.id], mark_read=True)
        changes = SyncChanges(cursor="next", upserts=[{"folder": "INBOX", "provider_uid": "new", "sender_address": "ada@example.net"}])
        async with get_session_maker().begin() as session:
            account = await session.get(MailAccount, "synthetic")
            ids, _ = await sync._apply_changes(account, changes, session, False, {})
        assert await _drain() == 1
        writer.assert_awaited_once_with(ids[0], "mark_read")
        assert (await mail_filing.for_message(ids[0])).label_ids == [label.id]
        assert await _claims(rule.id) == {(ids[0], "mark_read"): ("done", True)}
        assert await _apply(rule) == NOTHING | {"provider_skipped": 1}
        assert await _drain() == 0
        writer.assert_awaited_once()
    asyncio.run(run())


def test_verified_duplicate_merge_keeps_one_claim(pg_db, monkeypatch):
    async def run():
        from api.mail import rules
        from db.accessors import mail_message, mail_rule, mail_rule_run
        from db.engine import get_session_maker
        keep, label, _, writer = await _setup(monkeypatch)
        duplicate = await mail_message.upsert_message(account_id="synthetic", folder="INBOX", provider_uid="two",
                                                      sender_address="person@example.org")
        rule = await mail_rule.create(name="Read", sender_domain="example.org", label_ids=[label.id], mark_read=True)
        assert (await rules.execute_rule_run(rule.id, rule.version, keep.id)).status == "done"
        renamed = await mail_rule.update(rule.id, rule.version, name="Read copies")
        later = await rules.prepare_rule_run(renamed.id, renamed.version, duplicate.id)
        assert json.loads(later.provider_results_json) == {"mark_read": "pending"}
        async with get_session_maker().begin() as session:
            await mail_rule_run.merge_verified(session, keep.id, duplicate.id)
        assert await _claims(rule.id) == {(keep.id, "mark_read"): ("done", True)}
        result = await rules.dispatch_rule_run(later)
        assert json.loads(result.provider_results_json) == {"mark_read": "skipped_stale"}
        writer.assert_awaited_once_with(keep.id, "mark_read")
    asyncio.run(run())


def test_backfill_collapses_versions_conservatively(pg_db, monkeypatch):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    from db.engine import dispose_engine

    legacy = {
        ("one", 1): ("done", {"mark_read": "done", "archive": "skipped_unsupported", "_folder": "overridden"}),
        ("one", 2): ("unknown", {"mark_read": "skipped_unsupported", "archive": "unknown"}),
        ("one", 3): ("local_done", {"mark_read": "pending"}),
        ("two", 1): ("done", {"mark_read": "skipped_permission", "archive": "skipped_unsupported"}),
        ("two", 2): ("done", {"mark_read": "skipped_unsupported", "archive": "skipped_stale"}),
        ("three", 1): ("local_done", {"mark_read": "dispatching", "archive": "pending"}),
        ("three", 2): ("local_done", {"archive": "pending"}),
        ("four", 1): ("unknown", {"mark_read": "unexpected"}),
    }

    async def seed():
        from db.accessors import mail_filing, mail_message, mail_rule
        from db.engine import get_session_maker
        from db.models.mail_rule_run import MailRuleRun
        message, label, gate, writer = await _setup(monkeypatch)
        mails = {"one": message}
        for uid in ("two", "three", "four"):
            mails[uid] = await mail_message.upsert_message(account_id="synthetic", folder="INBOX", provider_uid=uid,
                                                           sender_address="person@example.org")
        await mail_filing.assign([mail.id for mail in mails.values()], add_labels=[label.id])
        rule = await mail_rule.create(name="Legacy", sender_domain="example.org", label_ids=[label.id], mark_read=True, archive=True)
        rule = await mail_rule.update(rule.id, rule.version, name="Legacy two")
        rule = await mail_rule.update(rule.id, rule.version, name="Legacy three")
        runs = {}
        async with get_session_maker().begin() as session:
            for (uid, version), (status, states) in legacy.items():
                row = MailRuleRun(rule_id=rule.id, rule_version=version, message_id=mails[uid].id, account_id="synthetic",
                                  status=status, provider_results_json=json.dumps(states))
                session.add(row)
                runs[(uid, version)] = row.id
            session.add(MailRuleRun(rule_id=rule.id, rule_version=1, message_id="first-import:synthetic:legacy",
                                    account_id="synthetic", status="skipped_initial_import"))
        await mail_message.set_state(mails["four"].id, folder="ARCHIVE")
        return rule, {uid: mail.id for uid, mail in mails.items()}, runs, writer

    async def verify(rule, mails, runs, writer):
        from sqlalchemy import select
        from api.mail import rules
        from db.engine import get_session_maker
        from db.models.mail_rule_action import MailRuleAction
        from db.models.mail_rule_run import MailRuleRun
        async with get_session_maker()() as session:
            rows = (await session.scalars(select(MailRuleAction).where(MailRuleAction.rule_id == rule.id))).all()
        assert {(row.message_id, row.kind): (row.status, row.dispatched, row.run_id) for row in rows} == {
            (mails["one"], "mark_read"): ("done", True, runs[("one", 1)]),
            (mails["one"], "archive"): ("unknown", True, runs[("one", 2)]),
            (mails["two"], "mark_read"): ("skipped_permission", True, runs[("two", 1)]),
            (mails["two"], "archive"): ("skipped_unsupported", False, runs[("two", 1)]),
            (mails["three"], "mark_read"): ("unknown", True, runs[("three", 1)]),
            (mails["three"], "archive"): ("pending", False, runs[("three", 2)]),
            (mails["four"], "mark_read"): ("unknown", True, runs[("four", 1)]),
        }
        # The proven unsupported skip and the pending claim of an outdated run are claimed again;
        # the ambiguous permission skip and every dispatched outcome stay blocked.
        assert await _apply(rule) == NOTHING | {"matched": 2, "provider_skipped": 4, "provider_queued": 2}
        assert await _drain() == 2
        assert sorted(args.args for args in writer.await_args_list) == sorted([(mails["two"], "archive"), (mails["three"], "archive")])
        # Legacy runs that lost the collapsed claim never dispatch it.
        async with get_session_maker()() as session:
            legacy_runs = {key: await session.get(MailRuleRun, run_id) for key, run_id in runs.items()}
        stale = await rules.dispatch_rule_run(legacy_runs[("one", 3)])
        assert json.loads(stale.provider_results_json) == {"mark_read": "skipped_stale"}
        for key in (("three", 1), ("three", 2)):
            stale = await rules.dispatch_rule_run(legacy_runs[key])
            assert json.loads(stale.provider_results_json)["archive"] == "skipped_stale"
        assert writer.await_count == 2
        assert (await _claims(rule.id))[(mails["three"], "archive")] == ("done", True)

    project = Path(__file__).resolve().parents[1]
    config = Config(str(project / "alembic.ini"))
    config.set_main_option("script_location", str(project / "src/db/migrations"))
    rule, mails, runs, writer = asyncio.run(seed())
    asyncio.run(dispose_engine())
    command.downgrade(config, "10b237a44539")
    asyncio.run(dispose_engine())
    command.upgrade(config, "heads")
    asyncio.run(verify(rule, mails, runs, writer))
