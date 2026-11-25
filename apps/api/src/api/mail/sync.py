"""Apply incremental provider changes to the local inbox index."""

import imaplib
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
from db.accessors import mail_account as account_access
from db.accessors import mail_job
from db.accessors import mail_message as message_access
from db.accessors import mail_rule_run
from db.accessors import mail_sync_state as sync_state_access
from db.models.mail_account import MailAccount, gmail_studio
from db.models.mail_message import IMAP_RETIRED
from env.mail import env as mail_env
from utils.mistral import MistralNotConfiguredError

from api.mail import connector_mailbox, connector_throttle, connectors, netguard, providers, secrets, service, sync_connector, sync_gmail, sync_graph, sync_imap, transport
from api.mail.seed_ids import is_seed_account
from api.mail.sync_types import BODY_VERSION, HTML_VERSION, SyncResult

logger = logging.getLogger(__name__)
# Bodies re-read per run after the text extraction changed; a backlog drains over the next runs.
BODY_REFRESH_BATCH = 25
# Re-reads this process could not finish, by message id: failures so far and when to ask again. A few
# unreadable mails must neither hold up the rest of the list, which is read newest first, nor be asked
# for on every run: the wait doubles per failure, from an hour up to a day. A restart forgets it.
REREAD_WAIT = timedelta(hours=1)
REREAD_WAIT_MAX = timedelta(days=1)
_reread_backoff: dict[str, tuple[int, datetime]] = {}
_html_reread_backoff: dict[str, tuple[int, datetime]] = {}


def _error_code(error: Exception) -> str:
    if isinstance(error, secrets.WorkspaceSecretNotFoundError):
        return "secret_not_found"
    # The app's own Mistral access is missing or refused: no new provider login would help.
    if isinstance(error, MistralNotConfiguredError):
        return "mistral_not_configured"
    if isinstance(error, secrets.WorkspaceSecretAccessDeniedError):
        return "secret_access_denied"
    if isinstance(error, (service.CredentialError, connector_mailbox.MailboxUnavailable)):
        return "auth_expired"
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
        return "auth_expired"
    if isinstance(error, (transport.MailHostNotAllowedError, netguard.BlockedAddressError)):
        return "host_not_allowed"
    if isinstance(error, (OSError, TimeoutError, imaplib.IMAP4.abort, httpx.TimeoutException, httpx.ConnectError)):
        return "unreachable"
    return "provider_error"


async def sync_account(account_id: str) -> SyncResult:
    account = await account_access.get_account(account_id)
    if account is None:
        raise LookupError(f"no mail account with id {account_id}")
    with connector_throttle.sync(account_id if account.auth_type == connectors.CONNECTOR else None):
        return await _sync_account(account)


async def _sync_account(account: MailAccount) -> SyncResult:
    account_id = account.id
    is_connector = account.auth_type == connectors.CONNECTOR
    if is_seed_account(account_id):
        state = await sync_state_access.get_state(account_id)
        return SyncResult("ok", 0, state.cursor if state else "", "")
    previous = await sync_state_access.save_state(account_id=account_id, cursor=None, status="syncing", claim_poll=True)
    cursor = previous.cursor
    try:
        import_state = json.loads(previous.rules_initial_import) if previous.rules_initial_import else {}
        initial_deferred = set(import_state.get('threads', []))
        if account.auth_type == connectors.CONNECTOR:
            provider = sync_connector
        elif account.provider == providers.IMAP:
            provider = sync_imap
        elif account.provider == providers.GOOGLE:
            provider = sync_gmail
        elif account.provider == providers.MICROSOFT:
            provider = sync_graph
        else:
            raise ValueError("Unknown provider")
        options = dict(folders=[f.strip().upper() for f in mail_env.mail_sync_folders.split(",") if f.strip()],
                       limit=mail_env.mail_sync_page_size, max_body_chars=mail_env.mail_sync_max_body_chars)
        native_moves = {}
        checks = []
        proof = previous.write_evidence() if gmail_studio(account) else None
        verified = is_connector and not connector_mailbox.inbox_only(account)
        if verified:
            verified_cursor = previous.connector_cursor if previous and previous.connector_verified else ""
            generation = previous.connector_generation if previous else 0
            checkpoint = None
            known_deferred = previous.connector_deferred_threads if previous.connector_verified else []
            try:
                async with connector_mailbox.bound_mailbox(account, cursor=verified_cursor, generation=generation, observed=previous, bootstrap=True, check_after=True) as (binding, progress):
                    first_import = not progress
                    tracked = set() if first_import else initial_deferred

                    async def before_history(current):
                        nonlocal checkpoint
                        kept, deferred = await _refine_connector_changes(
                            account, current, known_deferred, options, initial_deferred=tracked)
                        checks = await sync_connector.pending_label_checks(account, proof[0], kept) if proof else []
                        await connector_mailbox.verify_bound_mailbox(account, binding, generation=generation, observed=previous)
                        checkpoint = kept, deferred, checks

                    changes, deferred = await _connector_changes(account, progress, known_deferred, options,
                        initial_deferred=tracked, first_import=first_import, before_history=before_history)
                    if proof:
                        checks = await sync_connector.pending_label_checks(account, proof[0], changes)
            except connector_throttle.RateLimited:
                if checkpoint is None:
                    raise
                # The independently verified delta does not depend on the throttled backfill.
                changes, deferred, checks = checkpoint
            stored_progress = connector_mailbox.encode(binding, changes.cursor)
        elif is_connector:
            progress = previous.connector_cursor or cursor
            first_import = not progress
            changes, deferred = await _connector_changes(account, progress,
                previous.connector_deferred_threads, options, initial_deferred=set() if first_import else initial_deferred, first_import=first_import)
            stored_progress = changes.cursor
        else:
            first_import = not cursor
            changes = await provider.fetch_changes(account, cursor=cursor, **options)
            stored_progress = changes.cursor
            native_moves = await _check_native_returns(account, changes)
        async with sync_state_access.sync_transaction(account_id, previous, verified=verified, connector=is_connector) as (session, state):
            import_id = import_state.get('id', '')
            if not import_id and (first_import or (is_connector and progress.startswith('initial:'))):
                import_id = str(previous.connector_generation)
            # Deferred history can outlive the initial scan. Do not suppress unrelated steady-state
            # gaps after that scan has completed merely because a deferred thread is still pending.
            scan_active = is_connector and (first_import or import_state.get('scan_pending', sync_connector.import_scan_pending(progress)))
            # A moving watermark does not prove delivery freshness while the initial scan is active.
            suppress_import_page = bool(import_id) and (not is_connector or scan_active)
            new_ids, invalidated = await _apply_changes(account, changes, session, verified, native_moves,
                initial_import_id=import_id, suppress_import_page=suppress_import_page)
            if checks and proof:
                await sync_connector.apply_label_checks(session, account, proof[0], checks)
            pending_import = (scan_active and (sync_connector.import_scan_pending(changes.cursor) or not changes.cursor)) if is_connector else changes.initial_pending
            tracked_threads = changes.initial_deferred_threads if is_connector else set()
            state.rules_initial_import = json.dumps({'id': import_id, 'threads': sorted(tracked_threads), 'scan_pending': pending_import}) if import_id and (pending_import or tracked_threads or not changes.cursor) else ''
            if is_connector:
                state.connector_deferred_threads = deferred
                if verified:
                    state.connector_proof_revision += 1
                    mail_job.queue_verified_insights(session, account_id, invalidated, mail_env.mail_insight_batch_limit)
                if verified and not state.connector_verified:
                    state.connector_generation += 1
                state.connector_verified = verified
                state.connector_cursor = stored_progress
                state.cursor = sync_connector.incremental_cursor(changes.cursor)
            else:
                state.cursor = changes.cursor
            state.status, state.last_error = "ok", ""
            state.last_sync_at = state.updated_at = datetime.now(UTC)
        try:
            reread = await refresh_bodies(account, provider)
        except (connector_mailbox.MailboxUnavailable, sync_state_access.StaleMailSync):
            raise
        except Exception as error:  # noqa: BLE001 — keep the completed page and retry body refresh
            logger.warning("Body refresh failed: %s", type(error).__name__)
            reread = []
        try:
            await refresh_html_bodies(account, provider)
        except (connector_mailbox.MailboxUnavailable, sync_state_access.StaleMailSync):
            raise
        except Exception as error:  # noqa: BLE001 — retry HTML without losing text refresh results
            logger.warning("HTML refresh failed: %s", type(error).__name__)
        return SyncResult("ok", len(changes.upserts), stored_progress, "", new_ids, bool(invalidated or reread) and not verified)
    except sync_state_access.StaleMailSync:
        return SyncResult("error", 0, cursor, "provider_error")
    except Exception as error:  # noqa: BLE001 — provider failures must not expose credentials
        code = _error_code(error)
        logger.warning("%s", type(error).__name__)
        await sync_state_access.fail_poll(account_id, previous, code)
        return SyncResult("error", 0, cursor, code)


async def _check_native_returns(account, changes):
    uids = {row["provider_uid"] for row in changes.upserts}
    if account.provider == providers.GOOGLE:
        candidates = await message_access.trash_conflicts(account.id, uids, changes.moved_out)
        if candidates:
            labels = await sync_gmail.current_labels(account, set(candidates))
            rows = {row["provider_uid"]: row for row in changes.upserts}
            for uid in candidates:
                changes.observe_current_labels(uid, labels.get(uid), rows.get(uid))
        return {}
    graph = account.provider == providers.MICROSOFT
    origins = await message_access.trash_origins(account.id, uids | set(changes.moved_out), check_existing=graph,
        imap_validity=None if graph else changes.cursor.partition(":")[0])
    if not origins:
        return {}
    if graph:
        confirmed = await sync_graph.confirm_inbox(account, origins)
    else:
        confirmed = await sync_imap.confirm_inbox(account, origins, changes.cursor)
        # Unknown-generation records tell us what to check, never which local ID to merge.
        unbound_absent = {uid for uid, original in origins.items() if original is None and uid not in confirmed}
        changes.moved_out = list((set(changes.moved_out) - confirmed) | unbound_absent)
    changes.upserts = [row for row in changes.upserts if row["provider_uid"] not in origins or row["provider_uid"] in confirmed]
    # Graph's old ID can return to the same row. An IMAP UID in a new UIDVALIDITY
    # identifies an independent message even when the numeric value is identical.
    return {uid: (original, uid in confirmed) for uid, original in origins.items() if original is not None}


async def _connector_changes(account, progress, deferred, options, *, initial_deferred=frozenset(), first_import=False, before_history=None):
    changes = await sync_connector.fetch_changes(account, cursor=progress, **options,
        **({"before_history": before_history} if before_history is not None else {}))
    return await _refine_connector_changes(account, changes, deferred, options,
        initial_deferred=initial_deferred, first_import=first_import)


async def _refine_connector_changes(account, changes, deferred, options, *, initial_deferred, first_import=False):
    pending = list(dict.fromkeys([*deferred, *changes.restored_members]))
    retry_count = min(5, len(pending))
    initial_threads = set(initial_deferred) | changes.initial_deferred_threads
    if first_import:
        initial_threads.update(pending)
        initial_threads.update(changes.deferred_threads)
    changes.initial_history_uids.update(row['provider_uid'] for row in changes.upserts
        if row.get('thread_id') in initial_threads)
    if retry_count:
        recovered = await sync_connector.retry_threads(account, pending[:retry_count], options["max_body_chars"],
                                                       restored_members=changes.restored_members)
        changes.initial_history_uids.update(row['provider_uid'] for row in recovered.upserts
            if row.get('thread_id') in initial_threads)
        sync_connector.merge_observations(changes, recovered)
    excluded = set(changes.moved_out) | changes.unconfirmed_uids
    changes.upserts = [row for row in changes.upserts if row["provider_uid"] not in excluded]
    possible = {row["provider_uid"] for row in changes.upserts} | changes.read_updates.keys() | changes.flag_updates.keys()
    # Studio's date searches omit an old message restored today. Rotate point reads through local trash.
    candidates = await message_access.trash_conflicts(account.id, possible, changes.moved_out, check_unlisted=True)
    if candidates:
        await sync_connector.confirm_trash_conflicts(account, changes, candidates)
    remaining = list(dict.fromkeys([*pending[retry_count:], *changes.deferred_threads]))
    changes.initial_deferred_threads = initial_threads.intersection(remaining)
    return changes, remaining


async def _apply_changes(account, changes, session, verified, native_moves, *, initial_import_id='', suppress_import_page=False):
    account_id = account.id
    connector = account.auth_type == connectors.CONNECTOR
    label_folders = connector or account.provider == providers.GOOGLE
    unverified = connector and not verified
    invalidated = []
    known = await message_access.known_uids(account_id, "INBOX", session=session)
    moved = set(changes.moved_out) - changes.confirmed_inbox
    graph = account.provider == providers.MICROSOFT
    imap_validity = changes.cursor.partition(":")[0] if account.provider == providers.IMAP and not connector else None
    restored_ids = {}
    for uid, (original, in_inbox) in native_moves.items():
        if await message_access.reconcile_native_trash(session, account_id, uid, original, in_inbox=in_inbox,
                                                      graph=graph, imap_validity=imap_validity):
            moved.discard(uid)
    imap_moves = {}
    imap_reset = False
    if changes.full_rebuild:
        retained = changes.confirmed_inbox | moved if label_folders else {uid for uid, (_, present) in native_moves.items() if graph and present}
        # Keep only IDs proven to identify the same delivery across this rebuild.
        candidates = {row["provider_uid"] for row in changes.upserts}
        if (not connector and account.provider in (providers.GOOGLE, providers.MICROSOFT)) or (connector and verified):
            retained.update(candidates)
        elif imap_validity:
            retained.update(await message_access.rebuild_imap_uids(session, account_id, candidates, imap_validity))
            imap_reset = await message_access.imap_generation_reset(session, account_id, imap_validity)
        if imap_validity and known:
            imap_moves = await message_access.imap_inbox_moves_in_generation(session, account_id, imap_validity)
            retained.update(imap_moves)
        await message_access.delete_folder(account_id, "INBOX", session=session, exclude_uids=retained)
        known = {uid: local_id for uid, local_id in known.items() if uid in retained}
    for row in changes.imap_new_hidden:
        # A restore can publish this very delivery before the first page in its new generation.
        uid = row["provider_uid"].removeprefix(f"{imap_validity}:")
        if uid in imap_moves:
            changes.imap_hidden[uid] = imap_moves[uid]
        else:
            await message_access.upsert_message(account_id=account_id, **row, body_version=BODY_VERSION, session=session)
    if changes.imap_hidden or changes.imap_retired:
        await message_access.apply_imap_hidden(session, account_id, imap_validity, changes.imap_hidden, changes.imap_retired)
        moved.difference_update(changes.imap_hidden)
    if label_folders:
        changes.upserts = [row for row in changes.upserts if row["provider_uid"] not in moved]
        blocked, restored_ids = await message_access.reconcile_label_folders(session, account_id,
            {row["provider_uid"] for row in changes.upserts} | changes.confirmed_inbox, moved,
            verified=not unverified, restored=changes.confirmed_inbox, trashed=changes.trashed)
        changes.upserts = [row for row in changes.upserts if row["provider_uid"] not in blocked]
    # Rows this run rewrites that still hold text from an older extraction.
    old_texts = await message_access.stale_body_texts(account_id, "INBOX", BODY_VERSION, [parsed["provider_uid"] for parsed in changes.upserts], session=session)
    new_ids = []
    rule_ids = []
    for parsed in changes.upserts:
        uid = parsed["provider_uid"]
        if uid not in known and (local_id := changes.imap_undeleted.get(uid)):
            if await message_access.restore_imap_flagged(session, account_id, uid, local_id, imap_validity):
                known[uid] = local_id
        if connector:
            if uid in changes.read_updates:
                parsed["is_read"] = changes.read_updates[uid]
            if uid in changes.flag_updates:
                parsed["is_flagged"] = changes.flag_updates[uid]
        outdated = uid in old_texts and insight_outdated(old_texts[uid], parsed.get("body_text", ""))
        row = await message_access.upsert_message(account_id=account_id, **parsed, body_version=BODY_VERSION, drop_insight=outdated,
                                                  unverified_connector=unverified, invalidated_insights=invalidated, session=session)
        if uid not in known and not imap_reset:
            new_ids.append(row.id)
            if not suppress_import_page and uid not in changes.initial_history_uids:
                rule_ids.append(row.id)
        if changes.full_rebuild or label_folders:
            known[parsed["provider_uid"]] = row.id
    known.update(restored_ids)
    for uid in set(changes.read_updates) | set(changes.flag_updates) | moved:
        if uid not in known:
            continue
        updates = {}
        if uid in changes.read_updates:
            updates["is_read"] = changes.read_updates[uid]
        if uid in changes.flag_updates:
            updates["is_flagged"] = changes.flag_updates[uid]
        if imap_validity and updates:
            updates["imap_uidvalidity"] = imap_validity
        if uid in moved and not label_folders:
            updates["folder"] = IMAP_RETIRED if imap_validity else "ARCHIVE"
            if imap_validity:
                updates["provider_uid"] = message_access.imap_archive_uid(imap_validity, uid)
        if updates:
            if connector:
                updates["connector_labels"] = True
            await message_access.set_state(known[uid], **updates, session=session)
    if imap_reset and changes.upserts:
        await mail_rule_run.record_imap_rebuild(session, account_id, imap_validity)
    if initial_import_id:
        await mail_rule_run.record_import_notice(session, account_id,
            f'first-import:{account_id}:{initial_import_id}', 'skipped_initial_import')
    await mail_job.queue_mail_rules(session, account_id, rule_ids)
    return new_ids, invalidated


def insight_outdated(old_text: str, new_text: str) -> bool:
    """An insight summarizes the stored text: it goes when the text changes, and when there is no text
    at all (the model then only had the subject and made things up; without text none is computed)."""
    return new_text != old_text or not new_text.strip()


async def refresh_bodies(account, provider) -> list[str]:
    """Re-read INBOX bodies stored by an older text extraction; returns the ids whose insight was dropped."""
    now = datetime.now(UTC)
    waiting = [message_id for message_id, (_, retry_at) in _reread_backoff.items() if retry_at > now]
    stale = await message_access.stale_bodies(account.id, "INBOX", BODY_VERSION, limit=BODY_REFRESH_BATCH, exclude=waiting)
    if not stale:
        return []
    if account.auth_type == connectors.CONNECTOR and not connector_mailbox.inbox_only(account):
        state = await sync_state_access.get_state(account.id)
        if state is None or not state.connector_verified:
            raise connector_mailbox.MailboxUnavailable()
        async with connector_mailbox.bound_mailbox(account, cursor=state.connector_cursor, generation=state.connector_generation, observed=state, check_after=True):
            found = await provider.fetch_bodies(account, stale, max_body_chars=mail_env.mail_sync_max_body_chars)
        async with sync_state_access.sync_transaction(account.id, state, verified=True) as (session, _):
            dropped = await _apply_bodies(stale, found, now, session=session)
            mail_job.queue_verified_insights(session, account.id, dropped, mail_env.mail_insight_batch_limit)
            return dropped
    found = await provider.fetch_bodies(account, stale, max_body_chars=mail_env.mail_sync_max_body_chars)
    return await _apply_bodies(stale, found, now)


async def refresh_html_bodies(account, provider) -> None:
    """Backfill HTML independently of text; archive rows remain pending until INBOX."""
    now = datetime.now(UTC)
    waiting = [key for key, (_, retry_at) in _html_reread_backoff.items() if retry_at > now]
    stale = await message_access.stale_html_bodies(account.id, HTML_VERSION, limit=BODY_REFRESH_BATCH, exclude=waiting)
    if not stale:
        return
    if account.auth_type == connectors.CONNECTOR and not connector_mailbox.inbox_only(account):
        state = await sync_state_access.get_state(account.id)
        if state is None or not state.connector_verified:
            raise connector_mailbox.MailboxUnavailable()
        async with connector_mailbox.bound_mailbox(account, cursor=state.connector_cursor, generation=state.connector_generation, observed=state, check_after=True):
            found = await provider.fetch_bodies(account, stale, max_body_chars=mail_env.mail_sync_max_body_chars)
        async with sync_state_access.sync_transaction(account.id, state, verified=True) as (session, _):
            await _apply_html_bodies(stale, found, now, session=session)
    else:
        found = await provider.fetch_bodies(account, stale, max_body_chars=mail_env.mail_sync_max_body_chars)
        await _apply_html_bodies(stale, found, now)


async def _apply_html_bodies(stale, found, now, *, session=None):
    for message in stale:
        if message.provider_uid not in found or (found[message.provider_uid] is not None and found[message.provider_uid].get("html_version") is None):
            failures = _html_reread_backoff.get(message.id, (0, now))[0] + 1
            _html_reread_backoff[message.id] = (failures, now + min(REREAD_WAIT * 2 ** min(failures - 1, 5), REREAD_WAIT_MAX))
            continue
        _html_reread_backoff.pop(message.id, None)
        parsed = found[message.provider_uid]
        if parsed is not None and message.rfc_message_id and parsed.get("rfc_message_id") and parsed["rfc_message_id"] != message.rfc_message_id:
            parsed = None
        status = parsed.get("html_status", "absent") if parsed is not None else "unavailable"
        html = parsed.get("body_html") if parsed is not None else None
        await message_access.set_html(message.id, expected_uid=message.provider_uid, expected_message_id=message.rfc_message_id, expected_uidvalidity=message.imap_uidvalidity, version=HTML_VERSION, status=status, html=html, **({"session": session} if session is not None else {}))


async def _apply_bodies(stale, found, now, *, session=None):
    transaction = {"session": session} if session is not None else {}
    dropped = []
    for message in stale:
        if message.provider_uid not in found:
            failures = _reread_backoff.get(message.id, (0, now))[0] + 1
            _reread_backoff[message.id] = (failures, now + min(REREAD_WAIT * 2 ** min(failures - 1, 5), REREAD_WAIT_MAX))
            continue
        _reread_backoff.pop(message.id, None)
        parsed = found[message.provider_uid]
        # Another Message-ID means the provider id now names a different mail (renumbered IMAP UIDs).
        if parsed is not None and message.rfc_message_id and parsed.get("rfc_message_id") and parsed["rfc_message_id"] != message.rfc_message_id:
            parsed = None
        text = message.body_text if parsed is None else parsed["body_text"]
        outdated = insight_outdated(message.body_text, text)
        if outdated:
            dropped.append(message.id)
        if parsed is None:
            # Gone or not this mail: the stored text stays and the row is not asked for again.
            await message_access.set_body(message.id, body_version=BODY_VERSION, drop_insight=outdated, **transaction)
        else:
            await message_access.set_body(message.id, body_version=BODY_VERSION, body_text=text, snippet=parsed["snippet"], drop_insight=outdated, **transaction)
    return dropped
