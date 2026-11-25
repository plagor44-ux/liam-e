"""Sync Studio inboxes and parse full-content messages without headers.

The Gmail connector returns no Reply-To field, so ``reply_to_json`` stays empty and replies go to
the sender.

Cursors are Unix seconds: newest, optionally followed by :gap_after:gap_before
for an unfinished window. Window boundaries overlap by one second.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from db.accessors import mail_job
from db.accessors import mail_message as message_access
from db.models.mail_account import MailAccount
from db.models.mail_job import MailJob
from db.models.mail_message import MailMessage

from api.mail import connectors
from api.mail.sync_types import HTML_VERSION, Reread, SyncChanges, attachments, body_text, html_body, common, inbox_labels


logger = logging.getLogger(__name__)


async def _thread_page(provider: str, args: dict) -> tuple[list[dict], str]:
    page = await connectors.call_read_tool_json(provider, "search_threads", args)
    threads = page.get("threads") if isinstance(page, dict) else None
    if not isinstance(threads, list) or not all(isinstance(row, dict) and isinstance(row.get("id"), str)
                                               and row["id"] for row in threads):
        raise ValueError("Incomplete connector thread listing")
    token = page.get("nextPageToken")
    if token is None:
        token = ""
    if not isinstance(token, str):
        raise ValueError("Invalid connector page token")
    return threads, token


async def _search(provider: str, query: str) -> list[dict[str, Any]]:
    threads = []
    seen = set()
    token = ""
    while True:
        args: dict[str, Any] = {"query": query, "pageSize": 50}
        if token:
            args["pageToken"] = token
        rows, token = await _thread_page(provider, args)
        threads.extend(rows)
        if not token:
            return threads
        if token in seen:
            raise ValueError("Repeated connector page token")
        seen.add(token)


def incremental_cursor(progress: str) -> str:
    """A legacy worker can always resume the numeric new-mail cursor."""
    if progress.startswith("initial:"):
        return str(json.loads(progress.removeprefix("initial:"))[0])
    return progress or "0"


def import_scan_pending(progress: str) -> bool:
    """An initial history page or a bounded overlapping new-mail scan remains unfinished."""
    parts = progress.split(':')
    return progress.startswith('initial:') or (len(parts) == 3 and all(part.isdigit() for part in parts))


async def fetch_changes(
    account: MailAccount, *, cursor: str, folders: list[str], limit: int, max_body_chars: int,
    before_history: Callable[[SyncChanges], Awaitable[None]] | None = None,
) -> SyncChanges:
    if not cursor.startswith("initial:"):
        return await _fetch_page(account, cursor=cursor, limit=limit, max_body_chars=max_body_chars)
    delta, previous_token = json.loads(cursor.removeprefix("initial:"))
    current = await _fetch_page(account, cursor=str(delta), limit=limit, max_body_chars=max_body_chars)
    if before_history is not None and (current.upserts or current.cursor != str(delta)):
        checkpoint = deepcopy(current)
        checkpoint.cursor = "initial:" + json.dumps([current.cursor, previous_token])
        await before_history(checkpoint)
    try:
        history = await _history_page(account, cursor, limit, max_body_chars)
    except (ValueError, KeyError, TypeError, httpx.HTTPError) as error:
        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
            raise
        # Keep this exact historical page for retry. The caller still verifies the mailbox
        # identity after this return and fences publication of the independent delta.
        logger.warning("Connector history page deferred: %s", type(error).__name__)
        current.cursor = "initial:" + json.dumps([current.cursor, previous_token])
        return current
    # A UID seen in the historical page remains ambiguous even if the overlapping delta also lists it.
    current.initial_history_uids.update(row['provider_uid'] for row in history.upserts)
    current.history_observed_uids.update(
        current.initial_history_uids, history.moved_out, history.unconfirmed_uids,
        history.read_updates, history.flag_updates, history.confirmed_inbox,
    )
    for members in history.restored_members.values():
        current.history_observed_uids.update(members)
    current.initial_deferred_threads.update(history.deferred_threads)
    current.initial_deferred_threads.update(history.restored_members)
    merge_observations(current, history)
    if history.cursor.startswith("initial:"):
        _, token = json.loads(history.cursor.removeprefix("initial:"))
        current.cursor = "initial:" + json.dumps([current.cursor, token])
    return current


def merge_observations(current: SyncChanges, later: SyncChanges) -> None:
    """Combine successive reads of the same mailbox. The caller owns pagination."""
    excluded = set(later.moved_out) | later.unconfirmed_uids
    rows = {row["provider_uid"]: row for row in current.upserts if row["provider_uid"] not in excluded}
    returned = set()
    for row in later.upserts:
        uid = row["provider_uid"]
        if uid in excluded:
            continue
        previous = rows.get(uid, {})
        if row["labels_json"] is None:
            # A singleton inbox match proves the folder but says nothing about the flags.
            row = {**row, "labels_json": previous.get("labels_json"),
                   "is_read": current.read_updates.get(uid, previous.get("is_read")),
                   "is_flagged": current.flag_updates.get(uid, previous.get("is_flagged"))}
        else:
            current.read_updates.pop(uid, None)
            current.flag_updates.pop(uid, None)
        rows[uid] = row
        returned.add(uid)
    current.upserts = list(rows.values())
    current.moved_out = list(dict.fromkeys([uid for uid in current.moved_out if uid not in returned] + later.moved_out))
    current.trashed.difference_update(returned | set(later.moved_out))
    current.trashed.update(later.trashed)
    current.unconfirmed_uids.difference_update(returned)
    current.unconfirmed_uids.update(later.unconfirmed_uids)
    current.read_updates.update(later.read_updates)
    current.flag_updates.update(later.flag_updates)
    current.deferred_threads.extend(later.deferred_threads)
    for thread, labels in later.restored_members.items():
        current.restored_members.setdefault(thread, {}).update(labels)


async def _thread_messages(provider: str, thread_id: str):
    try:
        thread = await connectors.call_read_tool_json(provider, "get_thread",
            {"threadId": thread_id, "messageFormat": "FULL_CONTENT"})
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (401, 403):
            raise
        return [] if error.response.status_code == 404 else None
    except (ValueError, TypeError, httpx.TransportError):
        return None
    if not isinstance(thread, dict) or thread.get("id", thread_id) != thread_id:
        return None
    messages = thread.get("messages")
    return messages if isinstance(messages, list) else None


async def confirm_trash_conflicts(account, changes: SyncChanges, candidates: dict[str, str]) -> None:
    """Search and history can replay an old folder after a move in either direction."""
    rows = {row["provider_uid"]: row for row in changes.upserts}
    by_thread: dict[str, set[str]] = {}
    uncertain = set()
    for uid, thread in candidates.items():
        by_thread.setdefault(rows.get(uid, {}).get("thread_id") or thread, set()).add(uid)
    for thread, uids in by_thread.items():
        try:
            latest = await connectors.thread_labels(account.provider, thread, uids) if thread else {}
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                latest = dict.fromkeys(uids, [])
            elif error.response.status_code in (400, 410, 422):
                latest = {}
            else:
                raise
        except json.JSONDecodeError:
            # A text tool error for this thread does not invalidate the other inbox observations.
            latest = {}
        for uid in uids:
            if uid in latest:
                changes.observe_current_labels(uid, latest[uid], rows.get(uid))
            else:
                uncertain.add(uid)
                changes.read_updates.pop(uid, None)
                changes.flag_updates.pop(uid, None)
    if uncertain:
        # A missing member in an incomplete thread cannot settle a legacy conflict.
        changes.upserts = [row for row in changes.upserts if row["provider_uid"] not in uncertain]
        changes.moved_out = [uid for uid in changes.moved_out if uid not in uncertain]
        changes.trashed.difference_update(uncertain)


type LabelCheck = tuple[str, dict[str, str], list[str] | None]


def _exclude_label_observations(changes: SyncChanges, uids: set[str]) -> None:
    if not uids:
        return
    # A stale page cannot replace an indexed delivery or trigger rules for it while
    # a pending point read owns its folder and flags.
    changes.upserts = [row for row in changes.upserts if row["provider_uid"] not in uids]
    changes.moved_out = [uid for uid in changes.moved_out if uid not in uids]
    changes.confirmed_inbox.difference_update(uids)
    changes.trashed.difference_update(uids)
    for uid in uids:
        changes.read_updates.pop(uid, None)
        changes.flag_updates.pop(uid, None)


def protect_pending_label_checks(changes: SyncChanges, jobs: Sequence[MailJob], binding: str) -> None:
    """Preserve current rows when publishing a checkpoint before optional point reads."""
    targets = (json.loads(job.payload_json) for job in jobs)
    _exclude_label_observations(changes, {target["provider_uid"] for target in targets if target["binding"] == binding})


async def pending_label_checks(
    account: MailAccount, binding: str, changes: SyncChanges, *,
    prior_checks: Sequence[LabelCheck] = (), jobs: Sequence[MailJob] | None = None,
) -> list[LabelCheck]:
    """Read exact pending members and reuse checkpoint results unaffected by history."""
    reusable = {job_id: labels for job_id, target, labels in prior_checks
                if target["provider_uid"] not in changes.history_observed_uids}
    checked = []
    jobs = await mail_job.connector_label_checks(account.id) if jobs is None else jobs
    for job in jobs:
        target = json.loads(job.payload_json)
        if target["binding"] != binding:
            checked.append((job.id, target, None))
            continue
        uid = target["provider_uid"]
        if job.id in reusable:
            labels = reusable[job.id]
        else:
            try:
                labels = (await connectors.thread_labels(
                    account.provider, target["thread_id"], {uid}, minimal_only=True,
                )).get(uid)
            except httpx.HTTPStatusError as error:
                if error.response.status_code in (401, 403):
                    raise
                labels = [] if error.response.status_code == 404 else None
            except (ValueError, httpx.TransportError):
                labels = None
        checked.append((job.id, target, labels))
    protect_pending_label_checks(changes, jobs, binding)
    return checked


async def apply_label_checks(session, account: MailAccount, binding: str, checked: Sequence[LabelCheck]) -> None:
    completed, pending = [], []
    for job_id, target, labels in checked:
        if target["binding"] != binding:
            completed.append(job_id)
            continue
        row = await message_access.get_message(target["message_id"], session)
        if (row is not None and row.account_id == account.id and not row.unverified_connector
                and row.provider_uid == target["provider_uid"] and row.thread_id == target["thread_id"]):
            if labels is None:
                pending.append(job_id)
                continue
            await message_access.commit_trash_move(row, {
                "folder": connectors.observed_folder(labels), "labels_json": json.dumps(sorted(labels)),
                "is_read": "UNREAD" not in labels, "is_flagged": "STARRED" in labels,
            }, sync_session=session)
        completed.append(job_id)
    await mail_job.finish_connector_label_checks(session, completed, pending)


async def _history_page(account, cursor, limit, max_body_chars):
    try:
        return await _fetch_page(account, cursor=cursor, limit=limit, max_body_chars=max_body_chars)
    except httpx.HTTPStatusError as error:
        if error.response.status_code not in (400, 410):
            raise
        # Restart expired provider pagination independently of the new-mail watermark.
        return await _fetch_page(account, cursor="", limit=limit, max_body_chars=max_body_chars)


async def _fetch_page(account: MailAccount, *, cursor: str, limit: int, max_body_chars: int) -> SyncChanges:
    parts = cursor.split(":")
    valid = len(parts) in (1, 3) and all(part.isdigit() for part in parts)
    newest = int(parts[0]) if valid else 0
    page_token = ""
    if cursor.startswith("initial:"):
        _, page_token = json.loads(cursor.removeprefix("initial:"))
    gap = (int(parts[1]), int(parts[2])) if valid and len(parts) == 3 else None
    # Provider UIDs keep their local IDs, including legacy rows referenced by tasks and history.
    changes = SyncChanges()
    if connectors.mailbox_credential.get() is not None:
        known = await message_access.known_uids(account.id, "INBOX", verified_only=True)
    else:
        known = await message_access.known_uids(account.id, "INBOX")
    seen = set(known) if valid else set()

    async def scan(query: str, lower: int | None, upper: int | None) -> tuple[bool, int | None, int | None]:
        nonlocal page_token
        token = page_token if not valid else ""
        frontier = latest = None
        for _ in range(10):
            args: dict[str, Any] = {"query": query, "pageSize": 50 if valid else min(50, limit)}
            if token:
                args["pageToken"] = token
            threads, next_token = await _thread_page(account.provider, args)
            for thread in threads:
                if valid and len(changes.upserts) >= limit and frontier is not None:
                    return False, frontier, latest
                thread_id = thread["id"]
                messages = await _thread_messages(account.provider, thread_id)
                if messages is None:
                    changes.deferred_threads.append(thread_id)
                    continue
                thread_latest = None
                members = _member_labels(thread)
                for message in messages:
                    parsed = _indexed_message(message, thread_id, max_body_chars)
                    if parsed is None:
                        if thread_id not in changes.deferred_threads:
                            changes.deferred_threads.append(thread_id)
                        continue
                    stamp = int(parsed["internal_date"].timestamp())
                    thread_latest = stamp if thread_latest is None else max(thread_latest, stamp)
                    if (lower is not None and stamp <= lower) or (upper is not None and stamp >= upper):
                        continue
                    latest = stamp if latest is None else max(latest, stamp)
                    uid = parsed["provider_uid"]
                    belongs = _apply_inbox_evidence(parsed, message.get("labelIds"), len(messages), members)
                    if belongs is None:
                        changes.deferred_threads.append(thread_id)
                        changes.unconfirmed_uids.add(uid)
                    elif belongs and uid not in seen:
                        changes.upserts.append(parsed)
                        seen.add(uid)
                    elif not belongs:
                        changes.moved_from_inbox(uid, json.loads(parsed["labels_json"] or "[]"))
                if thread_latest is not None:
                    frontier = min(thread_latest, upper) if upper is not None else thread_latest
            token = next_token
            if not valid:
                page_token = token
            if not token:
                return True, frontier, latest
            if not valid and len(changes.upserts) >= limit:
                return False, frontier, latest
        return False, frontier, latest

    old_newest = newest
    done, frontier, latest = await scan(f"in:inbox after:{newest - 1}" if valid else "in:inbox", newest - 1 if valid else None, None)
    if latest is not None:
        newest = max(newest, latest)
    if not valid and not done:
        # Finish the current page before saving its opaque successor. Equal timestamps do not skip rows.
        changes.cursor = "initial:" + json.dumps([newest, page_token])
        return changes
    if valid and not done and frontier is not None:
        gap = (gap[0] if gap else old_newest, frontier)
    if gap and gap[1] < gap[0]:
        gap = None
    if valid and gap and len(changes.upserts) < limit:
        done, frontier, _ = await scan(f"in:inbox after:{gap[0] - 1} before:{gap[1] + 1}", gap[0] - 1, gap[1] + 1)
        if done:
            gap = None
        elif frontier is not None:
            gap = (gap[0], frontier)
        if gap and gap[1] < gap[0]:
            gap = None
    changes.cursor = str(newest) + (f":{gap[0]}:{gap[1]}" if gap else "")
    if valid:
        threads = await _search(account.provider, "in:inbox newer_than:14d")
        present = {thread["id"] for thread in threads}
        members_by_thread = {thread["id"]: _member_labels(thread) for thread in threads}
        listed_members = {uid: (thread_id, labels) for thread_id, members in members_by_thread.items()
                          for uid, labels in (members or {}).items()}
        indexed = set(known) | {row["provider_uid"] for row in changes.upserts}
        candidates = [uid for uid, (_, labels) in listed_members.items()
                      if uid not in indexed and (not isinstance(labels, list) or inbox_labels(labels))]
        bare_threads = [thread_id for thread_id, members in members_by_thread.items() if members == {}]
        cutoff = datetime.now(UTC) - timedelta(days=14)
        for uid, stored_thread in await message_access.archived_connector_members(account.id, candidates, bare_threads, cutoff):
            thread_id, labels = listed_members.get(uid, (stored_thread, None))
            if uid not in indexed:
                evidence = changes.restored_members.setdefault(thread_id, {})
                if isinstance(labels, list) and inbox_labels(labels):
                    evidence[uid] = labels
        fallback = {}
        for uid, local_id in known.items():
            message = await message_access.get_message(local_id)
            if message is None:
                continue
            date = message.internal_date
            if date is not None and (date.replace(tzinfo=UTC) if date.tzinfo is None else date) < cutoff:
                continue
            if uid in listed_members and isinstance(listed_members[uid][1], list):
                _, labels = listed_members[uid]
                if not inbox_labels(labels):
                    changes.moved_from_inbox(uid, labels)
                else:
                    changes.read_updates[uid] = "UNREAD" not in labels
                    changes.flag_updates[uid] = "STARRED" in labels
            elif message.thread_id not in present:
                changes.moved_out.append(uid)
            else:
                fallback[uid] = message.thread_id
        if fallback:
            unread = {thread["id"] for thread in await _search(account.provider, "is:unread in:inbox newer_than:14d")}
            changes.read_updates.update({uid: thread_id not in unread for uid, thread_id in fallback.items()})
    return changes


def _indexed_message(row, thread_id, max_body_chars):
    if not isinstance(row, dict) or not row.get("id"):
        return None
    try:
        parsed = parse_message({"htmlBody": None, **row}, thread_id=thread_id, folder="INBOX", max_body_chars=max_body_chars)
    except (KeyError, TypeError, ValueError):
        return None
    return parsed if parsed["internal_date"] is not None else None


def _member_labels(thread: dict) -> dict[str, Any] | None:
    rows = thread.get("messages")
    if rows is None:
        return {}
    if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"] for row in rows):
        return None
    return {row["id"]: row.get("labelIds") for row in rows}


def _label_fields(labels: list) -> dict[str, Any]:
    return {"is_read": "UNREAD" not in labels, "is_flagged": "STARRED" in labels, "labels_json": json.dumps(labels)}


def _apply_inbox_evidence(message, labels, thread_size, members) -> bool | None:
    if isinstance(labels, list):
        return inbox_labels(labels)
    if members is None:
        return None
    uid = message["provider_uid"]
    matched_labels = members.get(uid)
    if isinstance(matched_labels, list):
        message.update(_label_fields(matched_labels))
        return inbox_labels(matched_labels)
    if thread_size == 1 and (not members or set(members) == {uid}):
        message.update(labels_json=None, is_read=None, is_flagged=None)
        return True
    # The search matched the thread, but a sibling may be its only inbox message.
    return None


async def _inbox_evidence(provider: str, thread_id: str, stamp: int):
    """Check the current inbox near the message date, even for mail outside the recent window."""
    token = ""
    query = f"in:inbox after:{max(0, stamp - 1)} before:{stamp + 2}"
    for _ in range(10):
        args = {"query": query, "pageSize": 50}
        if token:
            args["pageToken"] = token
        threads, token = await _thread_page(provider, args)
        for item in threads:
            if item["id"] != thread_id:
                continue
            return True, _member_labels(item)
        if not token:
            return False, {}
    return None, {}


async def _message_in_inbox(provider, thread_id, message, labels, thread_size, evidence, restored) -> bool | None:
    if isinstance(labels, list):
        return inbox_labels(labels)
    stamp = int(message["internal_date"].timestamp())
    if stamp not in evidence:
        try:
            evidence[stamp] = await _inbox_evidence(provider, thread_id, stamp)
        except httpx.HTTPStatusError as error:
            if error.response.status_code in (401, 403):
                raise
            evidence[stamp] = (None, {})
        except (ValueError, TypeError, httpx.TransportError):
            evidence[stamp] = (None, {})
    listed, members = evidence[stamp]
    if listed is not True:
        return listed
    uid = message["provider_uid"]
    belongs = _apply_inbox_evidence(message, labels, thread_size, members)
    if belongs is True and message["labels_json"] is None and uid in restored:
        message.update(_label_fields(restored[uid]))
    return belongs


async def retry_threads(account: MailAccount, thread_ids: list[str], max_body_chars: int, *, restored_members=None) -> SyncChanges:
    """Direct reads by thread ID need current inbox evidence before restoring messages."""
    recovered = SyncChanges()
    recheck = {}
    for thread_id in thread_ids:
        messages = await _thread_messages(account.provider, thread_id)
        pending = messages is None
        evidence = {}
        restored = (restored_members or {}).get(thread_id, {})
        for row in messages or []:
            parsed = _indexed_message(row, thread_id, max_body_chars)
            if parsed is None:
                pending = True
                continue
            uid = parsed["provider_uid"]
            in_inbox = await _message_in_inbox(account.provider, thread_id, parsed, row.get("labelIds"), len(messages), evidence, restored)
            if in_inbox is None:
                if restored:
                    recheck[thread_id, uid] = parsed
                    continue
                recovered.unconfirmed_uids.add(uid)
                pending = True
            elif in_inbox:
                recovered.upserts.append(parsed)
            else:
                recovered.moved_from_inbox(uid, json.loads(parsed["labels_json"] or "[]"))
        if pending:
            recovered.deferred_threads.append(thread_id)
    if recheck:
        # Some connectors expose message labels only in the recent listing. Reread it
        # after the full threads. A sibling or an earlier positive is never sufficient.
        try:
            threads = await _search(account.provider, "in:inbox newer_than:14d")
        except httpx.HTTPStatusError as error:
            if error.response.status_code in (401, 403):
                raise
            threads = []
        except (ValueError, TypeError, httpx.TransportError):
            threads = []
        labels_by_item = {(thread["id"], uid): labels for thread in threads
                          for uid, labels in (_member_labels(thread) or {}).items() if isinstance(labels, list)}
        for (thread_id, uid), parsed in recheck.items():
            labels = labels_by_item.get((thread_id, uid))
            if labels is None:
                recovered.unconfirmed_uids.add(uid)
                recovered.deferred_threads.append(thread_id)
            elif inbox_labels(labels):
                parsed.update(_label_fields(labels))
                recovered.upserts.append(parsed)
            else:
                recovered.moved_from_inbox(uid, labels)
    return recovered


async def fetch_bodies(account: MailAccount, messages: list[MailMessage], *, max_body_chars: int) -> Reread:
    """Re-read messages thread by thread; see `Reread` for the result."""
    wanted: dict[str, list[str]] = {}
    for message in messages:
        wanted.setdefault(message.thread_id, []).append(message.provider_uid)
    found: Reread = {}
    for thread_id, uids in wanted.items():
        try:
            full = await connectors.call_read_tool_json(account.provider, "get_thread", {"threadId": thread_id, "messageFormat": "FULL_CONTENT"})
        except ValueError:
            # Text instead of JSON: an unknown thread, but also a tool error or rate limit. Not answered,
            # so the mail is asked again later, each time after a longer wait (sync.refresh_bodies).
            continue
        except httpx.HTTPError:
            break  # connector unreachable or refusing: the rest waits for the next run
        items = {item.get("id"): item for item in full.get("messages", []) if isinstance(item, dict)} if isinstance(full, dict) else {}
        for uid in uids:
            found[uid] = parse_message({"htmlBody": None, **items[uid]}, thread_id=thread_id, folder="INBOX", max_body_chars=max_body_chars) if uid in items else None
    return found


def parse_message(message: dict[str, Any], *, thread_id: str, folder: str, max_body_chars: int) -> dict[str, Any]:
    body = body_text([message.get('plaintextBody') or ''], [message.get('htmlBody') or ''])[:max_body_chars]
    html, html_status = html_body([message.get('htmlBody') or ''])
    labels = message.get('labelIds') or []
    sender = message.get('sender') or ''
    try:
        date = datetime.fromisoformat(message['date']) if message.get('date') else None
    except ValueError:
        date = None
    files = attachments(message.get('attachments') or [])
    return {**common(sender, {}), 'folder': folder, 'provider_uid': message['id'],
            'thread_id': thread_id, 'subject': message.get('subject') or '', 'sender': sender,
            'recipients_json': json.dumps(message.get('toRecipients') or []), 'cc_json': json.dumps(message.get('ccRecipients') or []),
            'snippet': ' '.join((message.get('snippet') or body).split())[:500], 'body_text': body, 'body_html': html, 'html_status': html_status if 'htmlBody' in message else 'pending', 'html_version': HTML_VERSION if 'htmlBody' in message else None,
            'internal_date': date, 'has_attachments': bool(message.get('attachmentIds') or files),
            'attachments_json': json.dumps(files), **_label_fields(labels)}
