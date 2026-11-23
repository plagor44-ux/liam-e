"""Bulk trash through the list endpoints with the normal PostgreSQL contract settings.

Set BULK_TEST_WEB_ORIGIN to the built app on loopback to drive /mail with Node Playwright.
BULK_TEST_CHROMIUM optionally selects an existing Chromium binary.
"""

import asyncio
import json
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from api.mail import sync, sync_connector, sync_gmail, sync_imap
from api.routers.api.v1.chat import conversations
from api.routers.api.v1.mail import actions, messages, summary
from api.routers.api.v1.settings import ui
from bulk_mailboxes import UIDS, Mailboxes

pytestmark = pytest.mark.skipif(not os.getenv("RUN_PG_CONTRACT"), reason="RUN_PG_CONTRACT is not set")
MAIL = "/api/v1/mail"
FOLDERS = ("INBOX", "TRASH")


async def snapshot(client):
    result = {}
    for folder in FOLDERS:
        response = await client.get(MAIL + "/messages", params={"folder": folder})
        response.raise_for_status()
        rows = response.json()
        assert rows["next_cursor"] is None
        result[folder] = sorted(row["subject"] for row in rows["items"])
    return result


async def browser_flow(app, web_origin, selected):
    assert urlsplit(web_origin).hostname == "127.0.0.1"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        api_origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError("Local API stopped before startup")
                await asyncio.sleep(.01)
            script = Path(__file__).resolve().parents[4] / "tests/mail_bulk_browser.cjs"
            process = await asyncio.create_subprocess_exec("node", str(script), web_origin, api_origin, json.dumps(selected),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(process.communicate(), timeout=75)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            assert process.returncode == 0, err.decode()
            return json.loads(out)
        finally:
            server.should_exit = True
            await serving


@pytest.mark.parametrize(("names", "variant"), [
    pytest.param(("Yahoo",), "move", id="yahoo-imap-move"),
    pytest.param(("Yahoo",), "uidplus", id="yahoo-imap-uidplus"),
    pytest.param(("Studio",), "bound", id="studio-connector"),
    pytest.param(("Studio",), "inbox-only", id="studio-inbox-only"),
    pytest.param(("OAuth",), "oauth", id="gmail-oauth"),
    pytest.param(("Yahoo", "Studio", "OAuth"), "mixed", id="mixed"),
])
@pytest.mark.parametrize("partial", [False, True], ids=["success", "partial"])
def test_toolbar_bulk_trash_sync_and_group_undo(pg_db, monkeypatch, record_property, names, variant, partial):
    async def run():
        provider = Mailboxes(names, partial, variant)
        await provider.connect(monkeypatch)
        app = FastAPI()
        app.include_router(messages.router, prefix=MAIL + "/messages")
        app.include_router(actions.router, prefix=MAIL + "/actions")
        app.include_router(summary.router, prefix=MAIL + "/summary")
        app.include_router(ui.router, prefix="/api/v1/settings/ui")
        app.include_router(conversations.router, prefix="/api/v1/chat/conversations")
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            rows = (await client.get(MAIL + "/messages")).json()["items"]
            ids = {row["subject"]: row["id"] for row in rows}
            all_subjects = sorted(f"{name} {uid}" for name in names for uid in UIDS)
            assert sorted(ids) == all_subjects
            selected = [s for s in all_subjects if not s.endswith("14")]
            failed = {f"{name} 13" for name in names if partial}
            confirmed = set(selected) - failed
            part_moved = {"Studio 13"} & failed
            # UIDPLUS can leave two actual copies. IMAP sync indexes the surviving source.
            both_folders = part_moved | ({"Yahoo 13"} & failed if variant == "uidplus" else set())
            release = asyncio.Event()
            ready = {}
            modules = {"Studio": sync_connector, "Yahoo": sync_imap, "OAuth": sync_gmail}
            for name in names:
                event = ready[name] = asyncio.Event()

                async def delayed(*args, fetch=modules[name].fetch_changes, fetched=event, **kwargs):
                    changes = await fetch(*args, **kwargs)
                    if not fetched.is_set():
                        assert {row["provider_uid"] for row in changes.upserts} == set(UIDS)
                        fetched.set()
                        await release.wait()
                    return changes

                monkeypatch.setattr(modules[name], "fetch_changes", delayed)

            def poll():
                return asyncio.gather(*(sync.sync_account(account.id) for account in provider.accounts.values()))

            pending = poll()
            states = {}
            upstream = {}

            async def phase(stage):
                if stage == "delayed":
                    release.set()
                    await pending
                elif stage in ("replay", "fresh", "restored"):
                    provider.replay = stage == "replay"
                    assert all(result.status == "ok" for result in await poll())
                else:
                    assert stage in ("immediate", "undone")
                states[stage] = await snapshot(client)
                upstream[stage] = {f"{name} {uid}": sorted(labels & {"INBOX", "TRASH"})
                                   for name, mailbox in provider.labels.items() for uid, labels in mailbox.items()}
                return states[stage]

            try:
                await asyncio.wait_for(asyncio.gather(*(e.wait() for e in ready.values())), 10)
                if web_origin := os.getenv("BULK_TEST_WEB_ORIGIN"):
                    app.post("/_test/phase/{stage}")(phase)
                    outcome = await browser_flow(app, web_origin, selected)
                else:
                    body = {"message_ids": [ids[s] for s in selected], "kind": "trash"}
                    response = await client.post(MAIL + "/messages/actions", json=body)
                    response.raise_for_status()
                    results = response.json()["results"]
                    for stage in ("immediate", "delayed", "replay", "fresh"):
                        await phase(stage)
                    undo_ids = [row["action_id"] for row in results if row["status"] == "done"]
                    undone = await client.post(MAIL + "/actions/undo", json={"action_ids": undo_ids})
                    await phase("undone")
                    await phase("restored")
                    outcome = dict(request=body, results=results, undo_ids=undo_ids, undo_status=undone.status_code,
                                   undo=undone.json() if undone.is_success else {})
            finally:
                release.set()
                await pending

        # Keep all observations, including the actual lists on a failing baseline run.
        subjects_by_id = {local_id: subject for subject, local_id in ids.items()}
        statuses = {subjects_by_id[r["message_id"]]: r["status"] for r in outcome["results"]}
        trace = dict(statuses=statuses, lists=states, upstream=upstream["immediate"],
                     undo=len(outcome["undo"].get("restored", [])), undo_status=outcome["undo_status"],
                     toast=outcome.get("toast"), tone=outcome.get("tone"))
        record_property("flow", json.dumps(trace, ensure_ascii=False))
        assert outcome["request"]["kind"] == "trash"
        submitted = outcome["request"]["message_ids"]
        assert sorted(submitted) == sorted(ids[s] for s in selected)
        assert [r["message_id"] for r in outcome["results"]] == submitted
        assert statuses == {s: "partial" if s in part_moved else "failed" if s in failed else "done" for s in selected}
        for row in outcome["results"]:
            assert bool(row["action_id"]) == (row["status"] == "done")
            assert row["folder"] == ("TRASH" if row["status"] == "partial" else None)
        expected = {subject: ["INBOX"] for subject in all_subjects}
        expected.update({subject: ["INBOX", "TRASH"] for subject in both_folders})
        assert upstream["immediate"] == expected | {subject: ["TRASH"] for subject in confirmed}
        assert upstream["undone"] == upstream["restored"] == expected
        for stage in ("immediate", "delayed", "replay", "fresh", "undone", "restored"):
            trashed = part_moved if stage in ("undone", "restored") else confirmed | part_moved
            assert states[stage] == {"INBOX": sorted(set(all_subjects) - trashed), "TRASH": sorted(trashed)}, stage
        assert outcome["undo_status"] == 200
        tokens = sorted(r["action_id"] for r in outcome["results"] if r["status"] == "done")
        assert sorted(outcome["undo_ids"]) == sorted(outcome["undo"]["restored"]) == tokens
        assert not any(outcome["undo"][key] for key in ("skipped", "expired", "failed"))
        if "toast" in outcome:
            if partial:
                assert f"{len(confirmed)} von {len(selected)} Mails in den Papierkorb gelegt" in outcome["toast"]
                assert "fehlt die Bestätigung" in outcome["toast"]
                if part_moved:
                    noun = "Mail" if len(part_moved) == 1 else "Mails"
                    assert f"Der Anbieter hat {len(part_moved)} {noun} teilweise geändert." in outcome["toast"]
                else:
                    assert "teilweise geändert" not in outcome["toast"]
                assert outcome["tone"] == "alert"
            else:
                assert f"{len(selected)} Mails in den Papierkorb gelegt" in outcome["toast"]
                assert outcome["tone"] == "status"
    asyncio.run(run())
