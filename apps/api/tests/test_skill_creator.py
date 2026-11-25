"""Contracts for drafting custom skills in the chat and saving them only after confirmation (#97)."""

from dataclasses import replace
from datetime import date
import json

from fastapi.testclient import TestClient
import pytest

from api.mail import agent, agent_tools, proposals
from api.skills import TOOL_REGISTRY, check_tool_allowed
from api.skills.catalog import BUILTIN_SKILLS, ensure_builtin_skills
from api.skills.types import SkillDraft, SkillWorkflowStep
from conftest import build_app
from db.accessors import chat_message, skill_definition
from db.models.chat_context import ChatContext
from support.fake_mistral import FakeMistral
from test_chat_agent import chunk
from test_skill_chat_integration import skill_runtime  # noqa: F401  (shared synthetic store)

pytestmark = pytest.mark.anyio

CREATOR_TOOLS = {"list_authorable_tools", "propose_skill", "suggest_followups"}
APPROVED = {
    "collect_daily_briefing", "create_draft", "find_newsletters", "get_calendar_occurrence", "get_insights",
    "get_message", "get_task", "list_accounts", "list_folders", "propose_action", "propose_calendar_change",
    "propose_filing", "propose_task", "search_calendar", "search_messages", "search_tasks", "show_mails",
    "suggest_followups",
}


def draft_args(**overrides):
    args = {
        "slug": "mail-check",
        "name": "Mail-Check",
        "description": "Prüft ungelesene Mails und fasst sie kurz zusammen.",
        "instructions": "Suche ungelesene Mails und fasse sie in drei Punkten zusammen.",
        "workflow_steps": [{"id": "search", "title": "Suchen", "description": "Ungelesene Mails suchen.", "tool_names": ["search_messages"]}],
        "allowed_tools": ["search_messages", "show_mails"],
    }
    args.update(overrides)
    return args


class FakeTransaction:
    """Session stand-in: unless committed, leaving it restores cards and skills like a rollback."""

    def __init__(self, store):
        self.store = store
        self.committed = False

    async def __aenter__(self):
        self.saved = (
            {m.id: (m.proposal_status, m.proposal_json) for m in self.store.messages},
            dict(self.store.skills),
            dict(self.store.revisions),
        )
        return self

    async def __aexit__(self, *exc):
        if not self.committed:
            statuses, skills, revisions = self.saved
            for message in self.store.messages:
                if message.id in statuses:
                    message.proposal_status, message.proposal_json = statuses[message.id]
            self.store.skills.clear()
            self.store.skills.update(skills)
            self.store.revisions.clear()
            self.store.revisions.update(revisions)
        return False

    async def commit(self):
        self.committed = True


@pytest.fixture
def creator_store(skill_runtime, monkeypatch):  # noqa: F811
    monkeypatch.setattr(proposals, "get_session_maker", lambda: (lambda: FakeTransaction(skill_runtime)))
    return skill_runtime


async def creator_context(store, *, slug="skill_creator"):
    await ensure_builtin_skills()
    skill = next(s for s in store.skills.values() if s.slug == slug)
    ctx = ChatContext(conversation_id="conv-creator", app_prompt_version="test",
                      account_scope_json='{"account_ids":[],"selection":null}', mail_scope_version=1,
                      skill_id=skill.id, skill_revision_id=skill.revision_id)
    store.contexts[ctx.id] = ctx
    return skill, ctx


def turn(skill, ctx):
    return agent_tools.TurnContext(conversation_id=ctx.conversation_id, account_id=None, today=date(2026, 11, 21),
                                   context_id=ctx.id, allowed_tools=tuple(json.loads(skill_revision_tools(skill))),
                                   active_skill_id=skill.id, active_skill_revision_id=skill.revision_id)


def skill_revision_tools(skill):
    spec = next(s for s in BUILTIN_SKILLS if s.slug == skill.slug)
    return json.dumps(list(spec.allowed_tools))


async def pending_card(store, ctx, **overrides):
    args = draft_args(**overrides)
    draft = SkillDraft(**{**args, "workflow_steps": [SkillWorkflowStep(**s) for s in args["workflow_steps"]]})
    return await proposals.create_proposal(ctx.conversation_id, proposals.Proposal(kind="skill", reason="Entwurf", items=[], skill=draft), context_id=ctx.id)


def custom_skills(store):
    return [s for s in store.skills.values() if s.origin == "custom"]


def test_approved_tools_match_owner_decision_and_creator_tools_stay_reserved():
    assert {tool.name for tool in TOOL_REGISTRY.authorable_tools()} == APPROVED
    for name in ("propose_rule", "propose_preference", "list_authorable_tools", "propose_skill"):
        assert not TOOL_REGISTRY.get(name).authorable
    assert {t.name for t in TOOL_REGISTRY.list_tools() if t.builtin_only} == {"list_authorable_tools", "propose_skill"}
    creator = next(spec for spec in BUILTIN_SKILLS if spec.slug == "skill_creator")
    # No mail, calendar or task reads: drafts never see foreign content.
    assert set(creator.allowed_tools) == CREATOR_TOOLS
    with pytest.raises(ValueError):
        from api.skills.registry import ToolRegistry
        ToolRegistry().register(replace(TOOL_REGISTRY.get("propose_skill"), authorable=True))


def test_rest_skills_cannot_reference_creator_tools(creator_store):
    client = TestClient(build_app())
    secret = "synthetic-creator-instructions"
    for tool in ("propose_skill", "list_authorable_tools"):
        res = client.post("/api/v1/skills", json={"slug": f"x-{tool}", "name": f"X {tool}", "description": "d",
                                                  "instructions": secret, "allowed_tools": [tool]})
        assert res.status_code == 422 and res.json()["detail"]["code"] == "skill_tool_not_authorable"
        assert secret not in res.text
    reserved = client.post("/api/v1/skills", json={"slug": "skill_creator", "name": "Fake", "description": "d",
                                                   "instructions": "i", "allowed_tools": []})
    assert reserved.status_code == 422 and reserved.json()["detail"]["code"] == "skill_invalid"
    # The owner's REST editor keeps its existing tool rules; only chat drafts are limited to approved tools.
    rest_rule = client.post("/api/v1/skills", json={"slug": "rules", "name": "Regeln", "description": "d",
                                                    "instructions": "i", "allowed_tools": ["propose_rule"]})
    assert rest_rule.status_code == 201
    duplicate_name = client.post("/api/v1/skills", json={"slug": "rules-2", "name": "REGELN", "description": "d",
                                                         "instructions": "i", "allowed_tools": []})
    assert duplicate_name.status_code == 409 and duplicate_name.json()["detail"]["code"] == "skill_name_conflict"
    renamed = client.patch(f"/api/v1/skills/{rest_rule.json()['id']}", json={"expected_version": 1, "name": "tagesüberblick"})
    assert renamed.status_code == 409 and renamed.json()["detail"]["code"] == "skill_name_conflict"


async def test_creator_turn_offers_only_creator_tools_and_drafts_a_pending_card(creator_store, monkeypatch):
    fake = FakeMistral(streams=[
        [chunk("list_authorable_tools", {})],
        [chunk("propose_skill", draft_args(), id="call2")],
        ["Der Entwurf liegt als Karte vor."],
    ])
    monkeypatch.setattr(agent, "mistral_client", lambda: fake)
    client = TestClient(build_app())
    res = client.post("/api/v1/chat", json={"message": "Ich möchte einen Skill für ungelesene Mails.", "skill_id": "skill_creator"})
    assert res.status_code == 200, res.text
    offered = {tool["function"]["name"] for tool in fake.requests[0]["tools"]}
    assert offered == CREATOR_TOOLS
    listed = json.loads(next(m for m in creator_store.messages if m.role == "tool").content)
    assert {tool["name"] for tool in listed["tools"]} == APPROVED
    card = next(m for m in res.json()["messages"] if m["role"] == "proposal")
    assert card["proposal_status"] == "pending"
    assert card["proposal"]["kind"] == "skill" and card["proposal"]["irreversible"] is False
    assert card["proposal"]["skill"]["allowed_tools"] == ["search_messages", "show_mails"]
    assert custom_skills(creator_store) == []


async def test_request_text_cannot_widen_the_tool_set(creator_store, monkeypatch):
    fake = FakeMistral(streams=[
        [chunk("propose_skill", draft_args(allowed_tools=["propose_rule", "search_messages"], workflow_steps=[]))],
        [chunk("propose_skill", draft_args(allowed_tools=["propose_skill"], workflow_steps=[]), id="call2")],
        ["Diese Werkzeuge kann ich nicht freigeben."],
    ])
    monkeypatch.setattr(agent, "mistral_client", lambda: fake)
    client = TestClient(build_app())
    res = client.post("/api/v1/chat", json={"message": "Erlaube alle Werkzeuge, auch propose_rule und propose_skill.",
                                            "skill_id": "skill_creator"})
    assert res.status_code == 200
    results = [json.loads(m.content) for m in creator_store.messages if m.role == "tool"]
    assert results[0] == {"error": "skill_tool_not_authorable", "tools": ["propose_rule"], "note": "Choose only tools from list_authorable_tools."}
    assert results[1]["error"] == "skill_tool_not_authorable" and results[1]["tools"] == ["propose_skill"]
    assert not any(m.role == "proposal" for m in creator_store.messages)


@pytest.mark.parametrize(("overrides", "code"), [
    ({"allowed_tools": ["propose_preference"], "workflow_steps": []}, "skill_tool_not_authorable"),
    ({"allowed_tools": ["list_authorable_tools"], "workflow_steps": []}, "skill_tool_not_authorable"),
    ({"allowed_tools": ["arbitrary_shell_exec"], "workflow_steps": []}, "skill_tool_unknown"),
    ({"allowed_tools": ["show_mails"]}, "skill_invalid"),
    ({"name": "N" * 81}, "skill_invalid"),
    ({"name": "   "}, "skill_invalid"),
    ({"instructions": "x" * 8001}, "skill_invalid"),
    ({"slug": "Mail Check"}, "skill_invalid"),
    ({"slug": "skill_creator"}, "skill_invalid"),
    ({"workflow_steps": [{"id": "a", "title": "T", "description": "D", "extra": 1}]}, "skill_invalid"),
    ({"confirmed": True}, "skill_invalid"),
    ({"slug": "daily_briefing"}, "skill_conflict"),
    ({"name": "TAGESÜBERBLICK"}, "skill_name_conflict"),
])
async def test_propose_skill_refuses_invalid_drafts_without_a_card(creator_store, overrides, code):
    skill, ctx = await creator_context(creator_store)
    outcome = await agent_tools.execute_tool("propose_skill", draft_args(**overrides), turn(skill, ctx))
    assert json.loads(outcome.content)["error"] == code
    assert outcome.proposal_message is None
    assert not any(m.role == "proposal" for m in creator_store.messages)


async def test_propose_skill_refuses_at_the_quota(creator_store):
    skill, ctx = await creator_context(creator_store)
    for index in range(skill_definition.MAX_CUSTOM_SKILLS):
        await skill_definition.create_custom_skill(slug=f"s{index}", name=f"S{index}", description="d", instructions="i",
                                                   workflow_steps_json="[]", allowed_tools_json="[]", status="active")
    outcome = await agent_tools.execute_tool("propose_skill", draft_args(), turn(skill, ctx))
    assert json.loads(outcome.content) == {"error": "skill_quota"}


async def test_creator_tools_need_the_live_builtin_creator(creator_store):
    # Outside creator mode the tools are not offered and calls are refused before any handler runs.
    assert check_tool_allowed("propose_skill", None) == "tool_not_permitted"
    briefing = next(s for s in BUILTIN_SKILLS if s.slug == "daily_briefing")
    assert check_tool_allowed("propose_skill", briefing.allowed_tools) == "tool_not_permitted"

    skill, ctx = await creator_context(creator_store)
    briefing_skill, briefing_ctx = await creator_context(creator_store, slug="daily_briefing")
    # Even a direct handler call outside the creator stays refused.
    wrong = turn(briefing_skill, briefing_ctx)
    for name in ("propose_skill", "list_authorable_tools"):
        assert json.loads((await TOOL_REGISTRY.get(name).handler(draft_args(), wrong)).content) == {"error": "tool_not_permitted"}

    ctx.revoked = True
    assert json.loads((await agent_tools.execute_tool("propose_skill", draft_args(), turn(skill, ctx))).content) == {"error": "context_revoked"}
    ctx.revoked = False
    skill.status = "disabled"
    assert json.loads((await agent_tools.execute_tool("propose_skill", draft_args(), turn(skill, ctx))).content) == {"error": "tool_not_permitted"}
    assert not any(m.role == "proposal" for m in creator_store.messages)


async def test_confirm_applies_card_edits_creates_once_and_the_skill_is_usable(creator_store, monkeypatch):
    _, ctx = await creator_context(creator_store)
    card = await pending_card(creator_store, ctx)
    client = TestClient(build_app())
    edit = {"slug": "inbox-digest", "name": "Posteingang kurz", "description": "Kurzer Überblick.", "instructions": "Fasse ungelesene Mails zusammen."}
    res = client.post(f"/api/v1/chat/proposals/{card.id}/confirm", json={"selected_ids": [], "skill_edit": edit})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["proposal_status"] == "done"
    created = custom_skills(creator_store)
    assert len(created) == 1 and body["proposal"]["result"]["skill_id"] == created[0].id
    assert (created[0].slug, created[0].name) == ("inbox-digest", "Posteingang kurz")
    revision = creator_store.revisions[created[0].revision_id]
    assert json.loads(revision.allowed_tools_json) == ["search_messages", "show_mails"]
    assert body["proposal"]["skill"]["name"] == "Posteingang kurz"

    again = client.post(f"/api/v1/chat/proposals/{card.id}/confirm", json={"selected_ids": [], "skill_edit": edit})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "proposal_not_pending"
    assert len(custom_skills(creator_store)) == 1

    # "Use now": the next turn runs the saved skill with exactly its tools.
    fake = FakeMistral(streams=[["Drei ungelesene Mails."]])
    monkeypatch.setattr(agent, "mistral_client", lambda: fake)
    used = client.post("/api/v1/chat", json={"message": "Los", "skill_id": "inbox-digest"})
    assert used.status_code == 200
    assert {tool["function"]["name"] for tool in fake.requests[0]["tools"]} == {"search_messages", "show_mails"}


@pytest.mark.parametrize(("existing", "code"), [
    ({"slug": "mail-check", "name": "Anderer Name"}, "skill_conflict"),
    ({"slug": "anderer-befehl", "name": "mail-check"}, "skill_name_conflict"),
])
async def test_confirm_conflict_keeps_the_card_pending_until_corrected(creator_store, existing, code):
    _, ctx = await creator_context(creator_store)
    card = await pending_card(creator_store, ctx)
    await skill_definition.create_custom_skill(**existing, description="d", instructions="i", workflow_steps_json="[]",
                                               allowed_tools_json="[]", status="active")
    client = TestClient(build_app())
    res = client.post(f"/api/v1/chat/proposals/{card.id}/confirm", json={"selected_ids": []})
    assert res.status_code == 409 and res.json()["detail"]["code"] == code
    assert card.proposal_status == "pending" and len(custom_skills(creator_store)) == 1
    edit = {"slug": "mail-check-2", "name": "Mail-Check 2", "description": "d", "instructions": "i"}
    fixed = client.post(f"/api/v1/chat/proposals/{card.id}/confirm", json={"selected_ids": [], "skill_edit": edit})
    assert fixed.status_code == 200 and fixed.json()["proposal_status"] == "done"


async def test_confirm_rechecks_quota_and_tools_withdrawn_since_the_draft(creator_store, monkeypatch):
    _, ctx = await creator_context(creator_store)
    card = await pending_card(creator_store, ctx)
    client = TestClient(build_app())
    approved = TOOL_REGISTRY.get("show_mails")
    monkeypatch.setitem(TOOL_REGISTRY._tools, "show_mails", replace(approved, authorable=False))
    res = client.post(f"/api/v1/chat/proposals/{card.id}/confirm", json={"selected_ids": []})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "skill_tool_not_authorable"
    assert card.proposal_status == "pending" and custom_skills(creator_store) == []
    monkeypatch.setitem(TOOL_REGISTRY._tools, "show_mails", approved)

    for index in range(skill_definition.MAX_CUSTOM_SKILLS):
        await skill_definition.create_custom_skill(slug=f"s{index}", name=f"S{index}", description="d", instructions="i",
                                                   workflow_steps_json="[]", allowed_tools_json="[]", status="active")
    quota = client.post(f"/api/v1/chat/proposals/{card.id}/confirm", json={"selected_ids": []})
    assert quota.status_code == 409 and quota.json()["detail"]["code"] == "skill_quota"
    assert card.proposal_status == "pending"
    assert len(custom_skills(creator_store)) == skill_definition.MAX_CUSTOM_SKILLS


async def test_disabled_creator_blocks_confirm_but_cancel_still_works(creator_store):
    skill, ctx = await creator_context(creator_store)
    card = await pending_card(creator_store, ctx)
    client = TestClient(build_app())
    assert client.patch(f"/api/v1/skills/{skill.id}", json={"expected_version": 1, "status": "disabled"}).status_code == 200
    res = client.post(f"/api/v1/chat/proposals/{card.id}/confirm", json={"selected_ids": []})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "proposal_context_revoked"
    assert custom_skills(creator_store) == []
    cancelled = client.post(f"/api/v1/chat/proposals/{card.id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["proposal_status"] == "cancelled"


async def test_skill_card_outside_the_creator_context_is_never_saved(creator_store):
    _, briefing_ctx = await creator_context(creator_store, slug="daily_briefing")
    card = await pending_card(creator_store, briefing_ctx)
    res = TestClient(build_app()).post(f"/api/v1/chat/proposals/{card.id}/confirm", json={"selected_ids": []})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "proposal_context_revoked"
    assert card.proposal_status == "pending" and custom_skills(creator_store) == []


async def test_skill_confirm_selection_rules_and_quiet_validation(creator_store):
    _, ctx = await creator_context(creator_store)
    card = await pending_card(creator_store, ctx)
    client = TestClient(build_app())
    picked = client.post(f"/api/v1/chat/proposals/{card.id}/confirm", json={"selected_ids": ["x"]})
    assert picked.status_code == 422 and picked.json()["detail"]["code"] == "selection_invalid"
    secret = "synthetic-overlong-instructions"
    too_long = client.post(f"/api/v1/chat/proposals/{card.id}/confirm", json={"selected_ids": [], "skill_edit": {
        "slug": "ok", "name": "N" * 81, "description": "d", "instructions": secret}})
    assert too_long.status_code == 422 and too_long.json()["detail"]["code"] == "proposal_invalid"
    assert secret not in too_long.text
    # Only text fields are editable on the card: tools and steps need a new draft.
    widened = client.post(f"/api/v1/chat/proposals/{card.id}/confirm", json={"selected_ids": [], "skill_edit": {
        "slug": "ok", "name": "Ok", "description": "d", "instructions": "i", "allowed_tools": ["propose_rule"]}})
    assert widened.status_code == 422 and widened.json()["detail"]["code"] == "proposal_invalid"
    assert card.proposal_status == "pending" and custom_skills(creator_store) == []

    send = await chat_message.add_message(conversation_id=ctx.conversation_id, role="proposal", content="", proposal_status="pending",
                                          context_id=ctx.id, proposal_json=proposals.Proposal(kind="send", reason="", items=[], draft=proposals.SendDraft(
                                              account_id="acc", mode="new", to=["x@example.org"], cc=[], subject="s", body="b", quote="",
                                              reply_to_message_id=None)).model_dump_json())
    foreign = client.post(f"/api/v1/chat/proposals/{send.id}/confirm", json={"selected_ids": [], "skill_edit": {
        "slug": "ok", "name": "Ok", "description": "d", "instructions": "i"}})
    assert foreign.status_code == 422 and foreign.json()["detail"]["code"] == "selection_invalid"
    assert send.proposal_status == "pending"


async def test_creator_only_runs_in_a_conversation_that_never_left_creator_mode(creator_store, monkeypatch):
    fake = FakeMistral(streams=[["Drei ungelesene Mails."], ["Worum soll es gehen?"]])
    monkeypatch.setattr(agent, "mistral_client", lambda: fake)
    client = TestClient(build_app())
    plain = client.post("/api/v1/chat", json={"message": "Was ist neu?"})
    assert plain.status_code == 200
    conversation_id = next(m for m in plain.json()["messages"] if m["role"] == "assistant")["conversation_id"]
    # Earlier plain turns could hold mail text: the creator refuses before any model call.
    mixed = client.post("/api/v1/chat", json={"message": "Neuer Skill", "conversation_id": conversation_id, "skill_id": "skill_creator"})
    assert mixed.status_code == 409 and mixed.json()["detail"]["code"] == "context_revoked"
    assert len(fake.requests) == 1
    fresh = client.post("/api/v1/chat", json={"message": "Neuer Skill", "skill_id": "skill_creator"})
    assert fresh.status_code == 200
    follow_up = client.post("/api/v1/chat", json={"message": "Für Mails", "skill_id": "skill_creator",
                                                  "conversation_id": next(m for m in fresh.json()["messages"] if m["role"] == "assistant")["conversation_id"]})
    assert follow_up.status_code != 409
