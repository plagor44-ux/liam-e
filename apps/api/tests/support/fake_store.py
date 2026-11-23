"""In-memory accessor replacement for mail tests, scoped to each fixture invocation."""

import json
from contextlib import asynccontextmanager
from sqlalchemy.exc import IntegrityError
from db.accessors import mail_account, mail_action, mail_credential, mail_filing, mail_insight, mail_sync_state, mail_job, mail_message, mail_reference_snapshot, mail_rule_run
from db.models.mail_action import MailAction
from db.models.mail_job import MailJob
from db.models.mail_account import MailAccount
from db.models.mail_sync_state import MailSyncState


class FakeStore:
    def __init__(self, monkeypatch):
        self.accounts = {}
        self.credentials = {}
        self.states = {}
        self.jobs = {}
        self.unread = {}
        self.messages = {}
        self.actions = {}
        self.insights = {}
        async def no_filings(message_ids):
            return {}
        monkeypatch.setattr(mail_filing, "for_messages", no_filings)
        for name in ("record", "get_many", "mark_undone", "release", "latest_open", "delete_for_account"):
            monkeypatch.setattr(mail_action, name, getattr(self, "action_" + name))
        monkeypatch.setattr(mail_message, "get_message", self.get_message)
        monkeypatch.setattr(mail_message, "known_uids", self.known_uids)
        monkeypatch.setattr(mail_message, "legacy_imap_uidvalidity", self.legacy_imap_uidvalidity)
        async def imap_generation_reset(session, account_id, generation):
            state = self.states.get(account_id)
            return bool(state and state.cursor) and state.cursor.partition(':')[0] != generation
        monkeypatch.setattr(mail_message, "imap_generation_reset", imap_generation_reset)
        monkeypatch.setattr(mail_message, "commit_trash_move", self.commit_trash_move)
        monkeypatch.setattr(mail_sync_state, "sync_transaction", self.sync_transaction)
        monkeypatch.setattr(mail_message, "resolve_aliases", self.resolve_aliases)
        monkeypatch.setattr(mail_message, "has_messages_from_accounts", self.has_messages_from_accounts)
        async def no_snapshot(message_id):
            return None
        monkeypatch.setattr(mail_reference_snapshot, "read", no_snapshot)
        monkeypatch.setattr(mail_message, "set_state", self.set_message_state)
        monkeypatch.setattr(mail_message, "delete_duplicate", self.delete_duplicate)
        monkeypatch.setattr(mail_message, "stale_body_texts", self.stale_body_texts)
        monkeypatch.setattr(mail_message, "stale_bodies", self.stale_bodies)
        monkeypatch.setattr(mail_message, "set_body", self.set_body)
        for name in ("enqueue", "claim_next", "finish", "fail", "latest_for_account", "delete_for_account"):
            monkeypatch.setattr(mail_job, name, getattr(self, "job_" + name))
        async def no_rules(session, account_id, message_ids):
            return None
        monkeypatch.setattr(mail_job, "queue_mail_rules", no_rules)
        async def no_rebuild_notice(session, account_id, generation):
            return None
        monkeypatch.setattr(mail_rule_run, "record_imap_rebuild", no_rebuild_notice)
        monkeypatch.setattr(mail_message, "count_unread", self.count_unread)
        monkeypatch.setattr(mail_message, "count_for_account", self.count_for_account)
        async def newsletter_senders(*, account_ids=None, chat_only=False):
            return []
        monkeypatch.setattr(mail_message, "newsletter_senders", newsletter_senders)
        for module, names in (
            (mail_account, ("list_accounts", "get_account", "get_account_by_email", "create_account", "touch_account", "update_display_name", "delete_account")),
            (mail_credential, ("load_credential", "store_credential")),
            (mail_sync_state, ("get_state", "save_state", "fail_poll")),
        ):
            for name in names:
                monkeypatch.setattr(module, name, getattr(self, name))
        async def delete_credentials(account_id):
            for key in list(self.credentials):
                if key[0] == account_id:
                    del self.credentials[key]
        async def delete_state(account_id):
            self.states.pop(account_id, None)
        monkeypatch.setattr(mail_credential, "delete_for_account", delete_credentials)
        monkeypatch.setattr(mail_sync_state, "delete_for_account", delete_state)

    async def get_message(self, message_id):
        row = self.messages.get(message_id)
        if row is None:
            target = (await self.resolve_aliases([message_id])).get(message_id)
            row = self.messages.get(target)
        return row.model_copy() if row else None

    async def known_uids(self, account_id, folder, *, verified_only=False, session=None):
        return {row.provider_uid: row.id for row in self.messages.values()
                if row.account_id == account_id and row.folder == folder and (not verified_only or not row.unverified_connector)}

    async def legacy_imap_uidvalidity(self, message):
        row = self.messages.get(message.id)
        state = self.states.get(message.account_id)
        if row is None or state is None or (row.account_id, row.folder, row.provider_uid, row.rfc_message_id) != (
            message.account_id, "INBOX", message.provider_uid, message.rfc_message_id
        ):
            return None
        validity, _, position = state.cursor.partition(":")
        return validity if validity.isdigit() and position.partition(":")[0].isdigit() else None

    @asynccontextmanager
    async def sync_transaction(self, account_id, previous, *, verified, connector=True):
        yield None, self.states[account_id]

    async def commit_trash_move(self, message, updates, *, action=None, undo_id=None):
        updates = updates.copy()
        validity = updates.pop("imap_source_uidvalidity", None)
        if message.folder == "INBOX" and updates.get("folder") == "TRASH" and self.accounts[message.account_id].provider in ("imap", "microsoft"):
            record = action or self.actions[undo_id]
            record.inbox_source_uid = message.provider_uid
            record.imap_source_uidvalidity = validity
        await self.set_message_state(message.id, **updates)
        if "imap_uidvalidity" in updates:
            self.messages[message.id].imap_uidvalidity = updates["imap_uidvalidity"]
        if action:
            self.actions[action.id] = action

    async def has_messages_from_accounts(self, message_ids, account_ids):
        for message_id in message_ids:
            row = await self.get_message(message_id)
            if row and row.account_id in account_ids:
                return True
        return False

    async def resolve_aliases(self, message_ids):
        return {alias: row.id for row in self.messages.values() if not row.unverified_connector
                for alias in set(message_ids).intersection(row.connector_alias_ids)}

    async def set_message_state(self, message_id, **kwargs):
        target = (await self.resolve_aliases([message_id])).get(message_id, message_id)
        row = self.messages.get(target)
        if row:
            folder = kwargs.get("folder", row.folder)
            uid = kwargs.get("provider_uid", row.provider_uid)
            if any(other.id != row.id and (other.account_id, other.folder, other.provider_uid) == (row.account_id, folder, uid) for other in self.messages.values()):
                raise IntegrityError("unique", {}, Exception())
            for key, value in kwargs.items():
                if value is not None:
                    setattr(row, key, value)
            return row.model_copy()
        return None

    def _stale(self, account_id, folder, version):
        return [row for row in self.messages.values() if row.account_id == account_id and row.folder == folder and row.body_version < version]

    async def stale_body_texts(self, account_id, folder, version, uids, *, session=None):
        return {row.provider_uid: row.body_text for row in self._stale(account_id, folder, version) if row.provider_uid in uids}

    async def stale_bodies(self, account_id, folder, version, *, limit, exclude=()):
        return [row.model_copy() for row in self._stale(account_id, folder, version) if row.id not in exclude][:limit]

    async def set_body(self, message_id, *, body_version, body_text=None, snippet=None, drop_insight=False):
        row = self.messages[message_id]
        row.body_version = body_version
        if body_text is not None:
            row.body_text = body_text
        if snippet is not None:
            row.snippet = snippet
        if drop_insight:
            self.insights.pop(message_id, None)

    async def delete_duplicate(self, account_id, folder, provider_uid, *, keep_id, rfc_message_id):
        for key, row in list(self.messages.items()):
            if key != keep_id and rfc_message_id and (row.account_id, row.folder, row.provider_uid, row.rfc_message_id) == (account_id, folder, provider_uid, rfc_message_id):
                del self.messages[key]
                return True
        return False

    async def action_record(self, **kwargs):
        row = MailAction(**kwargs)
        self.actions[row.id] = row
        return row

    async def action_get_many(self, ids):
        return [row for key, row in self.actions.items() if key in ids]

    async def action_mark_undone(self, action_id, at):
        row = self.actions.get(action_id)
        if row is None or row.undone_at is not None:
            return False
        row.undone_at = at
        return True

    async def action_release(self, action_id):
        if action_id in self.actions:
            self.actions[action_id].undone_at = None

    async def action_latest_open(self, message_id):
        return max((row for row in self.actions.values() if row.message_id == message_id and row.undone_at is None), key=lambda row: (row.created_at, row.id), default=None)

    async def action_delete_for_account(self, account_id):
        self.actions = {key: row for key, row in self.actions.items() if row.account_id != account_id}

    async def count_for_account(self, account_id):
        return sum(row.account_id == account_id for row in self.messages.values())

    async def count_unread(self, *, folder="INBOX", account_ids=None, chat_only=False):
        return {id: count for id, count in self.unread.items() if account_ids is None or id in account_ids}

    async def job_enqueue(self, kind, payload, *, dedupe_key, run_after=None):
        if any(j.dedupe_key == dedupe_key and j.status in ("queued", "running") for j in self.jobs.values()):
            return None
        job = MailJob(kind=kind, payload_json=json.dumps(payload), dedupe_key=dedupe_key)
        if run_after is not None:
            job.run_after = run_after
        self.jobs[job.id] = job
        return job

    enqueue = job_enqueue

    async def job_claim_next(self, now, *, stale_after):
        eligible = [j for j in self.jobs.values() if (j.status == "queued" and j.run_after <= now) or (j.status == "running" and j.locked_at and j.locked_at < now - stale_after)]
        if not eligible:
            return None
        job = min(eligible, key=lambda j: (j.run_after, j.id))
        job.status, job.locked_at, job.attempts = "running", now, job.attempts + 1
        return job

    async def job_finish(self, job_id, attempt):
        job = self.jobs[job_id]
        if job.status != "running" or job.attempts != attempt:
            return False
        job.status = "done"
        job.locked_at = None
        return True

    async def job_fail(self, job_id, attempt, error_code, *, retry_at):
        job = self.jobs[job_id]
        if job.status != "running" or job.attempts != attempt:
            return False
        job.status = "queued" if retry_at else "failed"
        job.last_error = error_code
        job.locked_at = None
        if retry_at:
            job.run_after = retry_at
        return True

    async def job_latest_for_account(self, account_id, *, kind=None, open_only=False):
        rows = [j for j in self.jobs.values() if json.loads(j.payload_json).get("account_id") == account_id and (kind is None or j.kind == kind) and (not open_only or j.status in ("queued", "running"))]
        return max(rows, key=lambda j: (j.created_at, j.id), default=None)

    async def job_delete_for_account(self, account_id):
        for job in list(self.jobs.values()):
            if json.loads(job.payload_json).get("account_id") == account_id:
                del self.jobs[job.id]

    async def list_accounts(self, session=None):
        return list(self.accounts.values())

    async def get_account(self, account_id):
        return self.accounts.get(account_id)

    async def get_account_by_email(self, email_address):
        return next((a for a in sorted(self.accounts.values(), key=lambda a: (a.created_at, a.id)) if a.email_address.lower() == email_address.lower()), None)

    async def create_account(self, **kwargs):
        return await self.touch_account(MailAccount(**kwargs))

    async def touch_account(self, account):
        self.accounts[account.id] = account
        return account

    async def update_display_name(self, account_id, display_name):
        account = self.accounts.get(account_id)
        if account:
            account.display_name = display_name
        return account

    async def delete_account(self, account_id):
        return self.accounts.pop(account_id, None) is not None

    async def load_credential(self, account_id, kind):
        return self.credentials.get((account_id, kind))

    async def store_credential(self, *, account_id, kind, encrypted_payload):
        self.credentials[account_id, kind] = encrypted_payload

    async def get_state(self, account_id):
        return self.states.get(account_id)

    async def fail_poll(self, account_id, previous, code):
        current = self.states.get(account_id)
        if current and mail_sync_state.same_connector_attempt(current, previous):
            current.status, current.last_error = "error", code

    async def save_state(self, *, account_id, **kwargs):
        claim_poll = kwargs.pop("claim_poll", False)
        previous = self.states.get(account_id)
        for key in ("connector_verified", "connector_cursor", "connector_generation", "connector_proof_revision", "connector_deferred_threads", "cursor"):
            if kwargs.get(key) is None:
                kwargs.pop(key, None)
                if previous:
                    kwargs[key] = getattr(previous, key)
        state = MailSyncState(account_id=account_id, **kwargs)
        if claim_poll:
            state.connector_generation += 1
        self.states[account_id] = state
        return state
