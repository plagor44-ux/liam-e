"""Synthetic end-to-end contracts for the 'Heute fällig' quick action flow (#77)."""

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from conftest import build_app
from db.models.calendar_occurrence import CalendarOccurrence
from db.models.calendar_source import CalendarSource
from db.models.calendar_sync_state import CalendarSyncState
from db.models.mail_insight import MailInsight
from db.models.mail_job import MailJob
from db.models.mail_message import MailMessage
from db.models.mail_sync_state import MailSyncState
from fastapi.testclient import TestClient
from support.fake_mistral import FakeMistral
from test_chat_agent import chunk, store  # noqa: F401

from api.mail import agent, calendar_tools
from api.mail.agent_prompt import APP_PROMPT_VERSION, system_prompt
from api.mail.agent_tools import TurnContext, execute_tool
from api.tasks import chat as task_chat

pytestmark = pytest.mark.anyio


def _install_calendars(monkeypatch, *, entries, occurrences, default_source_id):
    async def catalog(*, ids=None):
        return [entry for entry in entries if ids is None or entry[0].id in ids]

    monkeypatch.setattr(calendar_tools.calendar_source, "catalog", catalog)
    monkeypatch.setattr(
        calendar_tools.calendar_settings,
        "get",
        AsyncMock(return_value=NS(default_source_id=default_source_id)),
    )
    monkeypatch.setattr(
        calendar_tools.calendar_occurrence,
        "page_visible",
        AsyncMock(return_value=occurrences),
    )


async def test_due_today_turn_separates_mail_calendar_tasks_and_enforces_grants(store, monkeypatch):
    today = date(2026, 11, 21)
    now = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
    granted_ok = store.account
    granted_error = await store.mail.create_account(
        provider="imap", auth_type="imap_password", email_address="work@example.org", display_name="Arbeit",
    )
    excluded = await store.mail.create_account(
        provider="imap", auth_type="imap_password", email_address="hidden@example.org", display_name="Privat-Ausgeschlossen",
    )
    store.mail.states[granted_ok.id] = MailSyncState(account_id=granted_ok.id, status="ok", last_sync_at=now)
    store.mail.states[granted_error.id] = MailSyncState(
        account_id=granted_error.id, status="error", last_error="auth_expired", last_sync_at=None,
    )

    due_mail = MailMessage(
        account_id=granted_ok.id, provider_uid="1", subject="Rechnung fällig",
        sender="Buchhaltung <billing@example.org>", internal_date=now - timedelta(hours=2), is_flagged=True,
    )
    hidden_mail = MailMessage(
        account_id=excluded.id, provider_uid="2", subject="Geheime Frist",
        sender="Secret <secret@example.org>", internal_date=now, is_flagged=True,
    )
    store.mail.messages[due_mail.id] = due_mail
    store.mail.messages[hidden_mail.id] = hidden_mail

    insights = {
        due_mail.id: MailInsight(
            message_id=due_mail.id, points_json='["Heute zahlen"]', deadline=today, needs_reply=True, followups_json="[]",
        )
    }

    async def list_page(**kwargs):
        assert set(kwargs["account_ids"]) <= {granted_ok.id, granted_error.id}
        if kwargs.get("important_only"):
            return [due_mail]
        return [due_mail]

    monkeypatch.setattr(agent.mail_message, "list_page", list_page)
    monkeypatch.setattr("api.mail.agent_tools.mail_message.list_page", list_page)
    monkeypatch.setattr("api.mail.agent_tools.mail_insight.get_for_messages", AsyncMock(return_value=insights))
    monkeypatch.setattr(agent.mail_insight, "get_for_messages", AsyncMock(return_value=insights))

    cal_work = CalendarSource(
        id="cal-work", provider="google", connection_ref="synthetic-work", external_id="work",
        name="Arbeit", timezone="Europe/Berlin", capabilities_json='["read","write"]', is_active=True,
    )
    cal_paused = CalendarSource(
        id="cal-paused", provider="ics", connection_ref="synthetic-paused", external_id="paused",
        name="Pausiert", timezone="Europe/Berlin", capabilities_json='["read"]', is_active=False,
    )
    cal_disconnected = CalendarSource(
        id="cal-disc", provider="ics", connection_ref="synthetic-disc", external_id="disc",
        name="Getrennt", timezone="Europe/Berlin", capabilities_json='["read"]', is_active=True,
    )
    cal_occ = CalendarOccurrence(
        id="occ-1", source_id=cal_work.id, event_id="evt-1", external_id="e1", title="Abgabetermin",
        start_at=datetime(2026, 11, 21, 14, 0, tzinfo=UTC), end_at=datetime(2026, 11, 21, 15, 0, tzinfo=UTC),
        timezone="Europe/Berlin",
    )
    _install_calendars(
        monkeypatch,
        entries=[
            (cal_work, CalendarSyncState(
                source_id=cal_work.id,
                coverage_start=now - timedelta(days=30),
                coverage_end=now + timedelta(days=60),
                last_success_at=now,
                incomplete=False,
            )),
            (cal_paused, CalendarSyncState(source_id=cal_paused.id)),
            (cal_disconnected, CalendarSyncState(source_id=cal_disconnected.id, last_error="reconnect_required")),
        ],
        occurrences=[cal_occ],
        default_source_id=cal_work.id,
    )

    task_scope = {"source": "account_and_unlinked", "account_id": granted_ok.id}

    async def fake_task_read(name, args, scope, account_ids):
        assert scope == task_scope
        assert account_ids == [granted_ok.id, granted_error.id]
        return {
            "read_only": True,
            "window": {"from": args["from"], "to": args["to"]},
            "source": scope,
            "next_cursor": None,
            "untrusted_task_data": {
                "series": [
                    {
                        "series_id": "t-1",
                        "title": "Belege abheften",
                        "archived": False,
                        "source_account_id": None,
                        "source_message_id": None,
                        "complete": True,
                        "occurrences": [{"id": "to-1", "due_local": today.isoformat(), "status": "open"}],
                        "next_occurrence_cursor": None,
                    }
                ]
            },
        }

    monkeypatch.setattr(task_chat, "read", fake_task_read)

    conversation = await store.create()
    snapshot = store.snapshot(
        "due-today-ctx",
        account_scope_json=json.dumps({"account_ids": [granted_ok.id, granted_error.id], "selection": None}),
        calendar_scope_json=json.dumps([cal_work.id]),
        task_scope_json=json.dumps(task_scope),
        revoked=False,
    )
    monkeypatch.setattr(agent.chat_context, "get_contexts", AsyncMock(return_value={snapshot.id: snapshot}))

    streams = [
        [
            chunk("list_accounts", {}, id="c1", index=0),
            chunk("search_messages", {"important_only": True, "limit": 50}, id="c2", index=1),
            chunk("search_calendar", {"from": "2026-11-21", "to": "2026-11-22"}, id="c3", index=2),
            chunk("search_tasks", {"from": "2026-11-21", "to": "2026-11-21"}, id="c4", index=3),
            chunk("show_mails", {"message_ids": [hidden_mail.id]}, id="c5a", index=4),
            chunk("show_mails", {"message_ids": [due_mail.id]}, id="c5b", index=5),
            chunk("suggest_followups", {"texts": ["Antwort entwerfen"]}, id="c6", index=6),
        ],
        [
            "Mailfristen\n- Rechnung fällig (Frist heute). Arbeit ist gerade nicht erreichbar.\n\n"
            "Kalendertermine\n- Abgabetermin (Arbeit). Pausiert/Getrennt ausgeschlossen.\n\n"
            "Aufgaben\n- Belege abheften (heute)."
        ],
    ]
    fake = FakeMistral(streams=streams)
    monkeypatch.setattr(agent, "mistral_client", lambda: fake)

    events = [
        event async for event in agent.run_turn_events(
            conversation_id=conversation.id,
            text="Was ist heute fällig?",
            calendar_timezone="Europe/Berlin",
            task_source="account_and_unlinked",
            task_source_account_id=granted_ok.id,
            resolved_context=(conversation, snapshot, None),
        )
    ]

    assert events[-1].event == "done"
    system_msg = fake.requests[0]["messages"][0]["content"]
    assert "Mailumfang: 2 freigegebene Konten." in system_msg
    assert "Arbeit: auth_expired" in system_msg
    assert "Kalenderumfang: 1 lesbare Kalender" in system_msg
    assert "Aufgabenstatus: Freigegeben (account_and_unlinked" in system_msg

    # Verify excluded account never reaches tool payloads or emitted mail refs.
    second_turn_payload = json.dumps(fake.requests[1]["messages"], ensure_ascii=False)
    assert "Geheime Frist" not in second_turn_payload
    assert hidden_mail.account_id not in second_turn_payload

    tool_rows = {row.tool_name: json.loads(row.content) for row in store.rows if row.role == "tool"}
    show_rows = [json.loads(row.content) for row in store.rows if row.role == "tool" and row.tool_name == "show_mails"]
    assert show_rows == [{"error": "message_not_found"}, {"ok": True}]
    assert {a["id"]: a["state"] for a in tool_rows["list_accounts"]} == {
        granted_ok.id: "ok",
        granted_error.id: "auth_expired",
    }
    assert tool_rows["search_messages"]["untrusted_mails"][0]["deadline"] == "2026-11-21"
    assert tool_rows["search_messages"]["missing_insight_count"] == 0
    assert tool_rows["search_calendar"]["unavailable_source_counts"] == {"paused": 1, "disconnected": 1}
    assert tool_rows["search_calendar"]["write_default_status"] == "configured"
    assert tool_rows["search_calendar"]["write_default_source_id"] == cal_work.id

    mails_event = next(e for e in events if e.event == "mails")
    assert [m.id for m in mails_event.mails] == [due_mail.id]
    calendars_event = next(e for e in events if e.event == "calendars")
    assert [c.occurrence_id for c in calendars_event.calendars] == [cal_occ.id]


async def test_due_today_excluded_empty_paused_unreachable_incomplete_and_missing_default(store, monkeypatch):
    today = date(2026, 11, 21)
    now = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)

    # 1) Empty mail grant + empty task scope + 0 readable calendars (paused + disconnected) + missing write default
    cal_paused = CalendarSource(
        id="p1", provider="ics", connection_ref="c1", external_id="p1", name="Pausiert",
        timezone="UTC", capabilities_json='["read"]', is_active=False,
    )
    cal_disc = CalendarSource(
        id="d1", provider="ics", connection_ref="c2", external_id="d1", name="Getrennt",
        timezone="UTC", capabilities_json='["read"]', is_active=True,
    )
    _install_calendars(
        monkeypatch,
        entries=[
            (cal_paused, CalendarSyncState(source_id=cal_paused.id)),
            (cal_disc, CalendarSyncState(source_id=cal_disc.id, last_error="provider_unavailable", access_blocked=True)),
        ],
        occurrences=[],
        default_source_id=None,
    )

    empty_ctx = TurnContext("conv", None, today, account_ids=[], calendar_source_ids=[], task_scope={"source": "none"})
    assert json.loads((await execute_tool("list_accounts", {}, empty_ctx)).content) == []
    mail_empty = json.loads((await execute_tool("search_messages", {}, empty_ctx)).content)
    assert mail_empty["mail_scope_empty"] is True and mail_empty["untrusted_mails"] == []
    cal_empty = json.loads((await execute_tool("search_calendar", {"from": "2026-11-21", "to": "2026-11-22"}, empty_ctx)).content)
    assert cal_empty["source_count"] == 0
    assert cal_empty["index_complete_for_window"] is False
    assert cal_empty["unavailable_source_counts"] == {"paused": 1, "disconnected": 1}
    assert cal_empty["write_default_status"] == "missing"
    assert json.loads((await execute_tool("search_tasks", {"from": "2026-11-21", "to": "2026-11-21"}, empty_ctx)).content) == {
        "error": "task_out_of_scope"
    }

    # 2) Granted mail with syncing/never states + filtered important_only on unanalyzed inbox
    acc_syncing = store.account
    acc_never = await store.mail.create_account(
        provider="imap", auth_type="imap_password", email_address="never@example.org", display_name="Neu",
    )
    store.mail.jobs["j1"] = MailJob(kind="sync", status="running", payload_json=json.dumps({"account_id": acc_syncing.id}))
    plain_mail = MailMessage(account_id=acc_syncing.id, provider_uid="p1", subject="Info", internal_date=now)
    store.mail.messages[plain_mail.id] = plain_mail

    async def mixed_list_page(**kwargs):
        return [] if kwargs.get("important_only") else [plain_mail]

    monkeypatch.setattr("api.mail.agent_tools.mail_message.list_page", mixed_list_page)
    monkeypatch.setattr("api.mail.agent_tools.mail_insight.get_for_messages", AsyncMock(return_value={}))

    active_ctx = TurnContext("conv", None, today, account_ids=[acc_syncing.id, acc_never.id])
    important_res = json.loads((await execute_tool("search_messages", {"important_only": True}, active_ctx)).content)
    assert important_res["inbox_has_other_mails"] is True
    assert "note" in important_res
    assert {s["id"]: s["state"] for s in important_res["account_statuses"]} == {
        acc_syncing.id: "syncing",
        acc_never.id: "never",
    }
    plain_res = json.loads((await execute_tool("search_messages", {}, active_ctx)).content)
    assert plain_res["missing_insight_count"] == 1
    assert plain_res["untrusted_mails"][0]["has_insight"] is False

    # 3) Readable calendar with transient error + old state + unavailable write default
    cal_ro = CalendarSource(
        id="ro", provider="ics", connection_ref="c3", external_id="ro", name="Verein",
        timezone="UTC", capabilities_json='["read"]', is_active=True,
    )
    _install_calendars(
        monkeypatch,
        entries=[
            (cal_ro, CalendarSyncState(
                source_id=cal_ro.id,
                coverage_start=now - timedelta(days=5),
                coverage_end=now + timedelta(days=5),
                last_success_at=now - timedelta(days=3),
                last_error="provider_unavailable",
                incomplete=False,
            )),
        ],
        occurrences=[],
        default_source_id="ro",
    )
    ro_ctx = TurnContext("conv", None, today, calendar_source_ids=["ro"])
    ro_res = json.loads((await execute_tool("search_calendar", {"from": "2026-11-21", "to": "2026-11-22"}, ro_ctx)).content)
    assert ro_res["index_complete_for_window"] is False
    assert ro_res["untrusted_calendar_data"]["sources"][0]["stale"] is True
    assert ro_res["untrusted_calendar_data"]["sources"][0]["last_error"] == "provider_unavailable"
    assert ro_res["write_default_status"] == "unavailable"


async def test_due_today_settings_change_refuses_stale_conversation_and_succeeds_fresh(store, monkeypatch):
    from api.mail import context_resolver
    from api.routers.api.v1.chat import stream

    monkeypatch.setattr(stream.chat_conversation, "get_conversation", AsyncMock(return_value=object()))
    monkeypatch.setattr(context_resolver.chat_context, "resolve", AsyncMock(side_effect=context_resolver.chat_context.ScopeChange()))
    client = TestClient(build_app())
    resp = client.post(
        "/api/v1/chat/stream",
        json={"conversation_id": "c-existing", "text": "Was ist heute fällig?", "task_source": "all"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "conversation_scope_change_requires_new"

    monkeypatch.setattr(context_resolver.chat_context, "resolve", AsyncMock(side_effect=context_resolver.chat_settings.GrantsChanged()))
    resp_grant = client.post(
        "/api/v1/chat/stream",
        json={"text": "Was ist heute fällig?", "expected_mail_scope_version": 1},
    )
    assert resp_grant.status_code == 409
    assert resp_grant.json()["detail"]["code"] == "chat_settings_changed"

    # Empty scopes in system_prompt context note still execute cleanly when starting a fresh conversation.
    conversation = await store.create()
    empty_snapshot = store.snapshot(
        "empty-all",
        account_scope_json=json.dumps({"account_ids": [], "selection": None}),
        calendar_scope_json="[]",
        task_scope_json='{"source":"none"}',
    )
    monkeypatch.setattr(agent.chat_context, "get_contexts", AsyncMock(return_value={empty_snapshot.id: empty_snapshot}))
    fake = FakeMistral(streams=[["Mailfristen, Kalendertermine und Aufgaben sind alle ausgeschlossen oder nicht freigegeben."]])
    monkeypatch.setattr(agent, "mistral_client", lambda: fake)
    events = [
        e async for e in agent.run_turn_events(
            conversation_id=conversation.id,
            text="Was ist heute fällig?",
            resolved_context=(conversation, empty_snapshot, None),
        )
    ]
    assert events[-1].event == "done"
    sys_content = fake.requests[0]["messages"][0]["content"]
    assert "Mailumfang: Keine Mailkonten freigegeben" in sys_content
    assert "Kalenderumfang: Keine lesbaren Kalender freigegeben" in sys_content
    assert "Aufgabenstatus: Ausgeschlossen" in sys_content


def test_due_today_prompt_enforces_separation_and_no_false_empty_claims():
    from api.mail.agent_tools import tools_for_context
    from api.skills.registry import TOOL_REGISTRY

    prompt = system_prompt(today=date(2026, 11, 21), account_scope=None, locale="de")
    assert APP_PROMPT_VERSION == "20"
    assert "Aktiver Skill:" in prompt
    for phrase in (
        "streng getrennt",
        "Mailfristen, Kalenderterminen und Aufgaben",
        "important_only=true",
        "write_default_status",
        "complete=false",
        "niemals ‚nichts fällig‘",
    ):
        assert phrase in prompt

    by_name = {entry["function"]["name"]: entry["function"] for entry in TOOL_REGISTRY.base_schemas()}
    assert "sync status" in by_name["list_accounts"]["description"]
    assert by_name["search_messages"]["parameters"]["properties"]["important_only"] == {"type": "boolean"}
    scoped = tools_for_context(TurnContext("conv", None, date(2026, 11, 21), allowed_tools=("list_accounts", "search_messages")))
    assert [entry["function"]["name"] for entry in scoped] == ["list_accounts", "search_messages"]


async def test_due_today_skill_scope_blocks_disallowed_tool_while_keeping_handlers(store, monkeypatch):
    today = date(2026, 11, 21)
    store.mail.states[store.account.id] = MailSyncState(account_id=store.account.id, status="error", last_error="auth_expired")
    monkeypatch.setattr("api.mail.agent_tools.mail_message.list_page", AsyncMock(return_value=[]))
    ctx = TurnContext("conv", None, today, account_ids=[store.account.id], allowed_tools=("list_accounts", "search_messages"))

    assert json.loads((await execute_tool("search_calendar", {"from": "2026-11-21", "to": "2026-11-22"}, ctx)).content) == {
        "error": "tool_not_permitted"
    }
    accounts_out = json.loads((await execute_tool("list_accounts", {}, ctx)).content)
    assert accounts_out[0]["state"] == "auth_expired"
    msgs_out = json.loads((await execute_tool("search_messages", {"important_only": True}, ctx)).content)
    assert msgs_out["account_statuses"][0]["state"] == "auth_expired"
    assert msgs_out["inbox_has_other_mails"] is False
