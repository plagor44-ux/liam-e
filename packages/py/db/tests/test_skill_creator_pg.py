"""A drafted skill is created only by its confirming PostgreSQL transaction (#97)."""

import asyncio
import json
import os

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("RUN_PG_CONTRACT"), reason="RUN_PG_CONTRACT is not set")


async def creator_card(**overrides):
    from api.mail.proposals import Proposal, create_proposal
    from api.skills.catalog import ensure_builtin_skills
    from api.skills.types import SkillDraft
    from db.accessors import chat_settings, skill_definition
    from db.engine import get_session_maker
    from db.models.chat_context import ChatContext

    await ensure_builtin_skills()
    creator = await skill_definition.get_skill("skill_creator")
    settings = await chat_settings.get()
    async with get_session_maker()() as session:
        context = ChatContext(conversation_id="synthetic", app_prompt_version="test-only",
            mail_scope_version=settings.version, account_scope_json=json.dumps({"selection": None, "account_ids": settings.account_ids}),
            skill_id=creator.id, skill_revision_id=creator.revision_id)
        session.add(context)
        await session.commit()
    draft = SkillDraft(**{"slug": "mail-check", "name": "Mail-Check", "description": "Synthetic description.",
                          "instructions": "Synthetic instructions.", "allowed_tools": ["search_messages"], **overrides})
    return await create_proposal("synthetic", Proposal(kind="skill", reason="Example", items=[], skill=draft), context_id=context.id)


async def custom_skills():
    from db.accessors import skill_definition

    return [row for row in await skill_definition.list_skills() if row.origin == "custom"]


def test_concurrent_confirms_and_builtin_sync_create_exactly_one_skill(pg_db):
    async def run():
        from api.mail import proposals
        from api.skills.catalog import ensure_builtin_skills
        from db.accessors import chat_message, skill_definition

        card = await creator_card()
        outcomes = await asyncio.gather(
            proposals.confirm(card.id, selected_ids=[]),
            proposals.confirm(card.id, selected_ids=[]),
            ensure_builtin_skills(),
            ensure_builtin_skills(),
            return_exceptions=True,
        )
        confirmed = [o for o in outcomes[:2] if not isinstance(o, Exception)]
        assert len(confirmed) == 1
        assert sum(isinstance(o, proposals.ProposalStateError) for o in outcomes[:2]) == 1
        assert not any(isinstance(o, Exception) for o in outcomes[2:])
        created = await custom_skills()
        assert [row.slug for row in created] == ["mail-check"]
        stored = await chat_message.get_message(card.id)
        assert stored.proposal_status == "done"
        assert proposals.proposal_view(stored).result.skill_id == created[0].id
        builtins = [row.slug for row in await skill_definition.list_skills() if row.origin == "builtin"]
        assert sorted(builtins) == ["daily_briefing", "skill_creator"]
    asyncio.run(run())


def test_refused_confirm_rolls_back_the_claim_and_a_corrected_edit_succeeds(pg_db):
    async def run():
        from fastapi import HTTPException

        from api.mail import proposals
        from api.skills.types import SkillEdit
        from db.accessors import chat_message, skill_definition

        card = await creator_card(name="Prüfung")
        await skill_definition.create_custom_skill(slug="other", name="PRÜFUNG", description="d", instructions="i",
                                                   workflow_steps_json="[]", allowed_tools_json="[]")
        with pytest.raises(HTTPException) as refused:
            await proposals.confirm(card.id, selected_ids=[])
        assert refused.value.status_code == 409 and refused.value.detail["code"] == "skill_name_conflict"
        assert (await chat_message.get_message(card.id)).proposal_status == "pending"
        assert len(await custom_skills()) == 1

        with pytest.raises(HTTPException) as slug_taken:
            await proposals.confirm(card.id, selected_ids=[], skill_edit=SkillEdit(slug="other", name="Neu", description="d", instructions="i"))
        assert slug_taken.value.detail["code"] == "skill_conflict"
        assert (await chat_message.get_message(card.id)).proposal_status == "pending"

        done = await proposals.confirm(card.id, selected_ids=[], skill_edit=SkillEdit(slug="pruefung", name="Prüfung neu", description="d", instructions="i"))
        assert done.proposal_status == "done"
        assert sorted(row.slug for row in await custom_skills()) == ["other", "pruefung"]

        # Renames fold case in the database too.
        renamed = next(row for row in await custom_skills() if row.slug == "pruefung")
        with pytest.raises(skill_definition.DuplicateSkillName):
            await skill_definition.change_skill(renamed.id, renamed.version, name="prüfung")
    asyncio.run(run())


def test_disabling_the_creator_revokes_its_card_and_cancel_still_works(pg_db):
    async def run():
        from api.mail import proposals
        from db.accessors import chat_message, skill_definition

        card = await creator_card()
        creator = await skill_definition.get_skill("skill_creator")
        await skill_definition.change_skill(creator.id, creator.version, status="disabled")
        with pytest.raises(proposals.ProposalContextRevokedError):
            await proposals.confirm(card.id, selected_ids=[])
        assert await custom_skills() == []
        assert (await proposals.cancel(card.id)).proposal_status == "cancelled"
        assert (await chat_message.get_message(card.id)).proposal_status == "cancelled"
    asyncio.run(run())


def test_a_failure_after_the_insert_leaves_no_skill_and_a_pending_card(pg_db, monkeypatch):
    async def run():
        from api.mail import proposals
        from db.accessors import chat_message

        card = await creator_card()

        async def broken_finish(session, message_id, *, proposal_json):
            assert len(await custom_skills()) == 0  # The insert is not visible outside the transaction.
            raise RuntimeError("synthetic failure after the insert")

        finish = chat_message.finish_proposal_in_session
        monkeypatch.setattr(chat_message, "finish_proposal_in_session", broken_finish)
        with pytest.raises(RuntimeError):
            await proposals.confirm(card.id, selected_ids=[])
        monkeypatch.setattr(chat_message, "finish_proposal_in_session", finish)
        assert await custom_skills() == []
        assert (await chat_message.get_message(card.id)).proposal_status == "pending"
    asyncio.run(run())
