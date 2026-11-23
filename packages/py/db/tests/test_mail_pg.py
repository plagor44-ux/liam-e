"""Live Postgres contracts for the mail index and job queue."""

import asyncio
import os
from datetime import UTC, date, datetime, timedelta

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("RUN_PG_CONTRACT"), reason="RUN_PG_CONTRACT is not set")


def test_reclaimed_job_fences_old_completion(pg_db):
    async def run():
        from db.accessors import mail_job

        now = datetime.now(UTC) + timedelta(seconds=1)
        job = await mail_job.enqueue("fence-test", {}, dedupe_key="fence-test")
        first = await mail_job.claim_next(now, stale_after=timedelta(minutes=1))
        assert first.id == job.id
        second = await mail_job.claim_next(now + timedelta(minutes=2), stale_after=timedelta(minutes=1))
        assert second.id == job.id and second.attempts == first.attempts + 1
        assert not await mail_job.finish(job.id, first.attempts)
        assert not await mail_job.fail(job.id, first.attempts, "Error", retry_at=None)
        assert await mail_job.finish(job.id, second.attempts)
        assert not await mail_job.fail(job.id, second.attempts, "Error", retry_at=None)

    asyncio.run(run())


def test_account_lookup_ignores_case_and_selects_first_deterministically(pg_db):
    async def run():
        from db.accessors import mail_account as a

        first = await a.create_account(provider="imap", auth_type="imap_password", email_address="Max@example.org")
        assert (await a.get_account_by_email("max@example.org")).id == first.id
        assert (await a.get_account(first.id)).email_address == "Max@example.org"
        second = await a.create_account(provider="imap", auth_type="imap_password", email_address="max@example.org")
        second.created_at = first.created_at
        await a.touch_account(second)
        assert (await a.get_account_by_email("MAX@EXAMPLE.ORG")).id == min(first.id, second.id)
        assert await a.get_account_by_email("missing@example.org") is None

    asyncio.run(run())


def test_reply_to_json_round_trips_and_old_inserts_get_the_server_default(pg_db):
    async def run():
        from db.accessors import mail_message as m
        from db.engine import get_session_maker
        from sqlalchemy import text

        row = await m.upsert_message(account_id="a", folder="INBOX", provider_uid="1", reply_to_json='["help@example.org"]')
        assert (await m.get_message(row.id)).reply_to_json == '["help@example.org"]'
        row = await m.upsert_message(account_id="a", folder="INBOX", provider_uid="1")
        assert (await m.get_message(row.id)).reply_to_json == "[]"
        async with get_session_maker()() as session:
            await session.execute(text("INSERT INTO mail_message (id, account_id, folder, provider_uid, thread_id, subject, sender, sender_address, list_unsubscribe, list_unsubscribe_post, rfc_message_id, \"references\", cc_json, attachments_json, is_flagged, recipients_json, snippet, body_text, has_attachments, is_read, labels_json, created_at) VALUES ('legacy', 'a', 'INBOX', '2', '', '', '', '', '', '', '', '', '[]', '[]', false, '[]', '', '', false, false, '[]', now())"))
            await session.commit()
        assert (await m.get_message("legacy")).reply_to_json == "[]"
    asyncio.run(run())


def test_stale_bodies_are_found_rewritten_and_searchable(pg_db):
    async def run():
        from db.accessors import mail_insight as i
        from db.accessors import mail_message as m
        from db.engine import get_session_maker
        from sqlalchemy import text

        now = datetime.now(UTC)
        old = await m.upsert_message(account_id="a", folder="INBOX", provider_uid="1", body_text="96 p{ margin:10px }", internal_date=now - timedelta(days=2))
        new = await m.upsert_message(account_id="a", folder="INBOX", provider_uid="2", body_text="Waffeltag", internal_date=now, body_version=1)
        await m.upsert_message(account_id="a", folder="Sent", provider_uid="3")
        await m.upsert_message(account_id="b", folder="INBOX", provider_uid="4")
        undated = await m.upsert_message(account_id="a", folder="INBOX", provider_uid="5")
        async with get_session_maker()() as session:
            # A row written before the column existed gets version 0 from the server default.
            await session.execute(text("INSERT INTO mail_message (id, account_id, folder, provider_uid, thread_id, subject, sender, sender_address, list_unsubscribe, list_unsubscribe_post, rfc_message_id, \"references\", cc_json, reply_to_json, attachments_json, is_flagged, recipients_json, snippet, body_text, has_attachments, is_read, labels_json, created_at) VALUES ('legacy', 'a', 'INBOX', '6', '', '', '', '', '', '', '', '', '[]', '[]', '[]', false, '[]', '', '', false, false, '[]', now())"))
            await session.commit()

        uids = ["1", "2", "3", "4", "5", "6", "unknown"]
        assert await m.stale_body_texts("a", "INBOX", 1, uids) == {"1": "96 p{ margin:10px }", "5": "", "6": ""}
        assert await m.stale_body_texts("a", "INBOX", 1, ["2", "5"]) == {"5": ""}
        assert await m.stale_body_texts("a", "INBOX", 0, uids) == {}
        assert await m.stale_body_texts("a", "INBOX", 1, []) == {}
        # Newest first, undated rows last; only the requested account and folder.
        assert [row.provider_uid for row in await m.stale_bodies("a", "INBOX", 1, limit=10)][0] == "1"
        assert {row.id for row in await m.stale_bodies("a", "INBOX", 1, limit=10)} == {old.id, undated.id, "legacy"}
        assert len(await m.stale_bodies("a", "INBOX", 1, limit=2)) == 2
        # Rows that wait for a later try are left out.
        assert {row.id for row in await m.stale_bodies("a", "INBOX", 1, limit=10, exclude=[old.id, "unknown"])} == {undated.id, "legacy"}

        for message_id in (old.id, new.id):
            await i.store_if_missing(message_id, points=["x"], deadline=None, needs_reply=False, followups=[], model="m")
        await m.set_body(old.id, body_version=1, body_text="Kuchenbasar am Freitag", snippet="Kuchenbasar", drop_insight=True)
        await m.set_body(undated.id, body_version=1)
        await m.set_body("missing", body_version=1)
        rewritten, untouched = await m.get_message(old.id), await m.get_message(undated.id)
        assert (rewritten.body_text, rewritten.snippet, rewritten.body_version) == ("Kuchenbasar am Freitag", "Kuchenbasar", 1)
        assert (untouched.body_text, untouched.body_version) == ("", 1)
        assert await m.stale_body_texts("a", "INBOX", 1, uids) == {"6": ""}
        # The generated fulltext column follows the rewritten text.
        assert [row.id for row in await m.list_page(query="Kuchenbasar", limit=10)] == [old.id]
        # The new text took the old insight with it, in the same write; other insights stay.
        assert await i.get(old.id) is None
        assert await i.get(new.id) is not None
        await m.upsert_message(account_id="a", folder="INBOX", provider_uid="2", body_text="Waffeltag fällt aus", internal_date=now, body_version=1, drop_insight=True)
        assert await i.get(new.id) is None

        # A summary is stored only while the mail still has the text the model read.
        assert await i.store_if_missing(old.id, points=["alt"], deadline=None, needs_reply=False, followups=[], model="m", summarized="96 p{ margin:10px }") is None
        assert await i.get(old.id) is None
        assert await i.store_if_missing("missing", points=["x"], deadline=None, needs_reply=False, followups=[], model="m", summarized="") is None
        stored = await i.store_if_missing(old.id, points=["neu"], deadline=None, needs_reply=False, followups=[], model="m", summarized="Kuchenbasar am Freitag")
        assert stored is not None and (await i.get(old.id)).points_json == '["neu"]'
    asyncio.run(run())


def test_unread_without_insight_can_exclude_accounts(pg_db):
    async def run():
        from db.accessors import mail_message as m

        now = datetime.now(UTC)
        for account in ("keep", "skip"):
            await m.upsert_message(account_id=account, folder="INBOX", provider_uid="1", internal_date=now)
        since = now - timedelta(days=1)
        assert {row.account_id for row in await m.unread_without_insight(since=since, limit=10)} == {"keep", "skip"}
        assert [row.account_id for row in await m.unread_without_insight(since=since, limit=10, exclude_account_ids=frozenset({"skip"}))] == ["keep"]
    asyncio.run(run())


def test_spam_excluded_from_search_and_insight_backlog(pg_db):
    async def run():
        from db.accessors import mail_message as m

        now = datetime.now(UTC)
        for folder in ("INBOX", "ARCHIVE", "TRASH", "SPAM"):
            await m.upsert_message(account_id="a", folder=folder, provider_uid=folder, subject="needle", internal_date=now)
        expected = {"INBOX", "ARCHIVE", "TRASH"}
        assert {row.folder for row in await m.search_messages(query="needle")} == expected
        assert {row.folder for row in await m.unread_without_insight(since=now - timedelta(days=1), limit=10)} == expected
        assert {row.folder for row in await m.list_page()} == {"INBOX"}
        assert await m.count_unread() == {"a": 1}
    asyncio.run(run())


def test_delete_duplicate_only_merges_same_identified_mail(pg_db):
    async def run():
        from db.accessors import mail_message as m
        from sqlalchemy.exc import IntegrityError

        keep = await m.upsert_message(account_id="a", folder="INBOX", provider_uid="old", rfc_message_id="<same>")
        copy = await m.upsert_message(account_id="a", folder="INBOX", provider_uid="new", rfc_message_id="<same>")
        with pytest.raises(IntegrityError):
            await m.set_state(keep.id, provider_uid="new")
        for account, folder, uid, keep_id, rfc in [
            ("a", "INBOX", "new", keep.id, ""),
            ("a", "INBOX", "new", keep.id, "<other>"),
            ("b", "INBOX", "new", keep.id, "<same>"),
            ("a", "ARCHIVE", "new", keep.id, "<same>"),
            ("a", "INBOX", "new", copy.id, "<same>"),
        ]:
            assert not await m.delete_duplicate(account, folder, uid, keep_id=keep_id, rfc_message_id=rfc)
            assert await m.get_message(copy.id) is not None
        assert await m.delete_duplicate("a", "INBOX", "new", keep_id=keep.id, rfc_message_id="<same>")
        assert await m.get_message(copy.id) is None
        assert (await m.set_state(keep.id, provider_uid="new")).provider_uid == "new"
    asyncio.run(run())


def test_list_page_pages_with_cursor_without_gaps(pg_db):
    async def run():
        from db.accessors import mail_message as m
        now = datetime.now(UTC)
        rows = [await m.upsert_message(account_id="a", folder="INBOX", provider_uid=str(i), internal_date=now - timedelta(days=i // 2)) for i in range(5)]
        pages = []
        before = None
        for _ in range(3):
            page = await m.list_page(before=before, limit=2)
            pages.extend(page)
            before = (page[-1].internal_date, page[-1].id)
        assert [x.id for x in pages] == [x.id for x in sorted(rows, key=lambda x: (x.internal_date, x.id), reverse=True)]
    asyncio.run(run())


def test_list_page_pages_through_null_dates(pg_db):
    async def run():
        from db.accessors import mail_message as m
        now = datetime.now(UTC)
        rows = [await m.upsert_message(account_id="a", folder="INBOX", provider_uid=str(i), internal_date=now - timedelta(days=i) if i < 2 else None) for i in range(5)]
        pages = []
        before = None
        while page := await m.list_page(before=before, limit=2):
            pages.extend(page)
            before = (page[-1].internal_date, page[-1].id)
        expected = sorted(rows[:2], key=lambda x: (x.internal_date, x.id), reverse=True) + sorted(rows[2:], key=lambda x: x.id, reverse=True)
        assert [x.id for x in pages] == [x.id for x in expected]
    asyncio.run(run())


def test_list_page_filters(pg_db):
    async def run():
        from db.accessors import mail_message as m, mail_insight as ins
        rows = [await m.upsert_message(account_id="b" if i == 4 else "a", folder="INBOX", provider_uid=str(i), is_read=i == 4, has_attachments=i == 3, is_flagged=i == 0) for i in range(5)]
        for i in (1, 2):
            await ins.store_if_missing(rows[i].id, points=[], deadline=date.today() if i == 1 else None, needs_reply=i == 2, followups=[], model="m")
        assert {x.id for x in await m.list_page(important_only=True)} == {x.id for x in rows[:3]}
        assert {x.id for x in await m.list_page(unread_only=True)} == {x.id for x in rows[:4]}
        assert [x.id for x in await m.list_page(with_attachments=True)] == [rows[3].id]
        assert [x.id for x in await m.list_page(account_ids=["b"])] == [rows[4].id]
    asyncio.run(run())


def test_fulltext_matches_german_stems(pg_db):
    async def run():
        from db.accessors import mail_message as m
        row = await m.upsert_message(account_id="a", folder="INBOX", provider_uid="1", body_text="Die Rechnungen kommen morgen")
        assert [x.id for x in await m.list_page(query="Rechnung")] == [row.id]
    asyncio.run(run())


def test_enqueue_dedupes_open_jobs(pg_db):
    async def run():
        from db.accessors import mail_job as j
        job = await j.enqueue("sync", {}, dedupe_key="a")
        assert await j.enqueue("sync", {}, dedupe_key="a") is None
        claim = await j.claim_next(datetime.now(UTC) + timedelta(seconds=1), stale_after=timedelta(minutes=5))
        assert await j.finish(job.id, claim.attempts)
        assert await j.enqueue("sync", {}, dedupe_key="a") is not None
    asyncio.run(run())


def test_claim_next_skips_locked_rows(pg_db):
    async def run():
        from db.accessors import mail_job as j
        await j.enqueue("sync", {}, dedupe_key="a")
        now = datetime.now(UTC)
        result = await asyncio.gather(*(j.claim_next(now, stale_after=timedelta(minutes=5)) for _ in range(2)))
        assert sum(x is not None for x in result) == 1
    asyncio.run(run())


def test_claim_next_reclaims_stale_running_job(pg_db):
    async def run():
        from db.accessors import mail_job as j
        await j.enqueue("sync", {}, dedupe_key="a")
        now = datetime.now(UTC)
        first = await j.claim_next(now, stale_after=timedelta(minutes=5))
        second = await j.claim_next(now + timedelta(minutes=6), stale_after=timedelta(minutes=5))
        assert second.id == first.id and second.attempts == 2
    asyncio.run(run())


def test_mark_undone_is_single_use(pg_db):
    async def run():
        from db.accessors import mail_action as a
        action = await a.record(message_id="m", account_id="a", kind="read", previous_json="{}")
        assert await a.mark_undone(action.id, datetime.now(UTC)) is True
        assert await a.mark_undone(action.id, datetime.now(UTC)) is False
    asyncio.run(run())


def test_action_release_and_latest_open(pg_db):
    async def run():
        from db.accessors import mail_action as a
        from db.engine import get_session_maker
        from db.models.mail_action import MailAction
        from sqlalchemy import update

        first = await a.record(message_id="m", account_id="a", kind="archive", previous_json="{}")
        second = await a.record(message_id="m", account_id="a", kind="restore", previous_json="{}")
        await a.record(message_id="other", account_id="a", kind="archive", previous_json="{}")
        assert (await a.latest_open("m")).id == second.id
        assert await a.latest_open("missing") is None
        assert await a.mark_undone(second.id, datetime.now(UTC))
        assert (await a.latest_open("m")).id == first.id
        await a.release(second.id)
        assert (await a.latest_open("m")).id == second.id
        assert (await a.get_many([second.id]))[0].undone_at is None
        # Equal timestamps use descending IDs, independently of insertion order.
        async with get_session_maker()() as session:
            await session.execute(update(MailAction).where(MailAction.message_id == "m").values(created_at=first.created_at))
            await session.commit()
        assert (await a.latest_open("m")).id == max(first.id, second.id)
        assert await a.mark_undone(second.id, datetime.now(UTC))
        assert await a.mark_undone(first.id, datetime.now(UTC))
        assert await a.latest_open("m") is None
        await a.release("missing")
    asyncio.run(run())


def test_newsletter_senders_takes_headers_from_newest_mail(pg_db):
    async def run():
        from db.accessors import mail_message as m
        from db.engine import get_session_maker
        from sqlalchemy import event

        await m.upsert_message(account_id="a", folder="INBOX", provider_uid="old", sender_address="news@example.com", sender="Old", list_unsubscribe="old")
        await m.upsert_message(account_id="a", folder="INBOX", provider_uid="new", sender_address="news@example.com", sender="New", internal_date=datetime.now(UTC), list_unsubscribe="new", list_unsubscribe_post="post", labels_json='["new"]')
        statements = []
        engine = get_session_maker().kw["bind"].sync_engine
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", capture)
        try:
            rows = await m.newsletter_senders()
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert len(rows) == 1
        assert (rows[0].sender_display, rows[0].list_unsubscribe, rows[0].list_unsubscribe_post, rows[0].labels_json) == ("New", "new", "post", '["new"]')
        assert len(statements) == 1
        assert "body_text" not in statements[0].lower()
        assert "count(" in statements[0].lower()
    asyncio.run(run())


def test_newsletter_senders_aggregates_counts(pg_db):
    async def run():
        from db.accessors import mail_message as m
        now = datetime.now(UTC)
        for i in range(3):
            await m.upsert_message(account_id="a", folder="INBOX", provider_uid=str(i), sender_address="news@example.com", sender="News", internal_date=now - timedelta(days=i), is_read=i != 0, list_unsubscribe="latest" if i == 0 else "old")
        rows = await m.newsletter_senders()
        assert len(rows) == 1
        assert (rows[0].total, rows[0].unread, rows[0].last_read_at, rows[0].list_unsubscribe) == (3, 1, now - timedelta(days=1), "latest")
    asyncio.run(run())


def test_existing_insight_wins_across_concurrent_insight_requests(pg_db):
    async def run():
        from db.accessors import mail_insight, mail_message

        message = await mail_message.upsert_message(account_id="account", folder="INBOX", provider_uid="message", body_text="Synthetic test email")
        languages = ["Existing summary", "Andere Zusammenfassung"]
        results = await asyncio.gather(*(
            mail_insight.store_if_missing(
                message.id, points=[point], deadline=None, needs_reply=False, followups=[],
                model="test-model", summarized=message.body_text,
            )
            for point in languages
        ))
        assert results[0] is not None and results[1] is not None
        assert results[0].id == results[1].id
        assert results[0].points_json == results[1].points_json == (await mail_insight.get(message.id)).points_json

    asyncio.run(run())


def test_insight_language_change_replaces_only_matching_body(pg_db):
    async def run():
        from db.accessors import mail_insight, mail_message
        message = await mail_message.upsert_message(account_id='account', folder='INBOX', provider_uid='language', body_text='Synthetic')
        async def store(locale, text, body='Synthetic'):
            return await mail_insight.store_if_missing(message.id, points=[text], deadline=None, needs_reply=False, followups=[], model='test', summarized=body, locale=locale)
        de = await store('de', 'Deutsch')
        en = await store('en', 'English')
        assert de.locale == 'de' and en.locale == 'en'
        assert (await mail_insight.get(message.id)).points_json == '["English"]'
        assert await store('de', 'stale', body='wrong body') is None
        assert (await mail_insight.get(message.id)).locale == 'en'
    asyncio.run(run())


def test_html_backfill_is_independent_bounded_and_does_not_touch_text(pg_db):
    async def run():
        from db.accessors import mail_message as m
        from sqlalchemy.exc import DBAPIError
        initial = await m.upsert_message(account_id='a', folder='INBOX', provider_uid='html', body_text='plain', body_version=1)
        archive = await m.upsert_message(account_id='a', folder='ARCHIVE', provider_uid='archive', body_text='old', body_version=1)
        assert {row.id for row in await m.stale_html_bodies('a', 1, limit=10)} == {initial.id}
        await m.set_html(initial.id, expected_uid='wrong', expected_message_id='', expected_uidvalidity=None, version=1, status='available', html='<p>wrong</p>')
        assert (await m.get_message(initial.id)).html_version == 0
        await m.set_html(initial.id, expected_uid='html', expected_message_id='', expected_uidvalidity=None, version=1, status='available', html='<p>ok</p>')
        current = await m.get_message(initial.id)
        assert (current.body_html, current.html_status, current.body_text, current.body_version) == ('<p>ok</p>', 'available', 'plain', 1)
        assert (await m.get_message(archive.id)).html_status == 'pending'
        with pytest.raises(DBAPIError):
            await m.upsert_message(account_id='a', folder='INBOX', provider_uid='too-long', body_html='x' * 200001, html_version=1, html_status='available')
    asyncio.run(run())
