"""Tools of the built-in skill creator: list approved tools and draft a skill card.

Both tools run only inside the built-in ``skill_creator`` skill and are reserved
for it, so no custom skill can create skills. Neither reads mail, calendar or task
data. ``propose_skill`` stores a pending card; the skill itself is created only
when the owner confirms that card (``api.mail.proposals``).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from api.skills.registry import TOOL_REGISTRY, RegisteredTool
from api.skills.types import (
    SKILL_CREATOR_SLUG,
    SkillDraft,
    SkillError,
    SkillToolNotAuthorableError,
    SkillToolUnknownError,
    SkillWorkflowStep,
    ToolOutcome,
)

PROPOSE_SKILL_FIELDS = frozenset({"slug", "name", "description", "instructions", "workflow_steps", "allowed_tools"})


def _outcome(value: dict[str, Any], **kwargs: Any) -> ToolOutcome:
    return ToolOutcome(json.dumps(value, ensure_ascii=False), **kwargs)


async def creator_active(ctx: Any) -> bool:
    """True only while the turn runs the active built-in creator skill."""
    from db.accessors import skill_definition as skill_access

    skill_id = getattr(ctx, "active_skill_id", None)
    if not isinstance(skill_id, str) or not skill_id:
        return False
    skill = await skill_access.get_skill(skill_id)
    return bool(
        skill is not None
        and skill.id == skill_id
        and skill.origin == "builtin"
        and skill.slug == SKILL_CREATOR_SLUG
        and skill.status == "active"
    )


async def list_authorable_tools(_arguments: dict[str, Any], ctx: Any) -> ToolOutcome:
    if not await creator_active(ctx):
        return _outcome({"error": "tool_not_permitted"})
    tools = [
        {"name": tool.name, "kind": tool.kind, "description": tool.description}
        for tool in TOOL_REGISTRY.authorable_tools()
    ]
    return _outcome({
        "tools": tools,
        "note": (
            "Only these tools can be assigned to a new skill. Tools of kind propose change or send "
            "nothing themselves: the owner confirms each of their cards."
        ),
    })


def _draft_from_arguments(arguments: dict[str, Any]) -> SkillDraft:
    tools = arguments.get("allowed_tools") or []
    if not isinstance(tools, list) or not all(isinstance(name, str) for name in tools):
        raise ValueError("allowed_tools")
    steps = arguments.get("workflow_steps") or []
    if not isinstance(steps, list):
        raise ValueError("workflow_steps")
    return SkillDraft.model_validate({
        "slug": arguments.get("slug"),
        "name": arguments.get("name"),
        "description": arguments.get("description"),
        "instructions": arguments.get("instructions"),
        "workflow_steps": [SkillWorkflowStep.model_validate(step) for step in steps],
        "allowed_tools": tools,
    })


async def propose_skill(arguments: dict[str, Any], ctx: Any) -> ToolOutcome:
    from api.mail import proposals
    from api.skills.service import validate_skill_payload
    from db.accessors import chat_context
    from db.accessors import skill_definition as skill_access

    if not await creator_active(ctx):
        return _outcome({"error": "tool_not_permitted"})
    if not ctx.context_id or not await chat_context.is_live(ctx.context_id):
        return _outcome({"error": "context_revoked"})
    if not isinstance(arguments, dict) or set(arguments) - PROPOSE_SKILL_FIELDS:
        return _outcome({"error": "skill_invalid", "note": "Use only the documented fields."})
    try:
        draft = _draft_from_arguments(arguments)
        data = validate_skill_payload(
            slug=draft.slug,
            name=draft.name,
            description=draft.description,
            instructions=draft.instructions,
            workflow_steps=draft.workflow_steps,
            allowed_tools=draft.allowed_tools,
            authoring=True,
        )
    except (ValidationError, ValueError, TypeError):
        return _outcome({"error": "skill_invalid", "note": "Check the field limits and the step format."})
    except SkillToolNotAuthorableError as error:
        return _outcome({"error": error.code, "tools": error.refused, "note": "Choose only tools from list_authorable_tools."})
    except SkillToolUnknownError as error:
        return _outcome({"error": error.code, "tools": error.missing, "note": "Choose only tools from list_authorable_tools."})
    except SkillError as error:
        return _outcome({"error": error.code})
    draft = SkillDraft(
        slug=data["slug"],
        name=data["name"],
        description=data["description"],
        instructions=data["instructions"],
        workflow_steps=data["workflow_steps"],
        allowed_tools=data["allowed_tools"],
    )
    conflicts = await skill_access.creation_conflicts(slug=draft.slug, name=draft.name)
    if "quota" in conflicts:
        return _outcome({"error": "skill_quota"})
    if "slug" in conflicts:
        return _outcome({"error": "skill_conflict", "note": "Another skill uses this command. Suggest a different one."})
    if "name" in conflicts:
        return _outcome({"error": "skill_name_conflict", "note": "Another skill uses this name. Suggest a different one."})
    proposal = proposals.Proposal(
        kind="skill",
        reason=f"Skill-Entwurf „{draft.name}“ (/{draft.slug}) liegt zur Bestätigung vor.",
        items=[],
        skill=draft,
    )
    row = await proposals.create_proposal(ctx.conversation_id, proposal, context_id=ctx.context_id)
    return _outcome(
        {
            "proposal_id": row.id,
            "status": "pending",
            "note": "Der Nutzer muss die Karte bestätigen. Erst dann ist der Skill gespeichert.",
        },
        proposal_message=row,
    )


_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Lowercase step id (a-z, 0-9, - or _), unique in the skill."},
        "title": {"type": "string", "description": "Short step title, at most 120 characters."},
        "description": {"type": "string", "description": "What happens in this step, at most 500 characters."},
        "tool_names": {"type": "array", "items": {"type": "string"}, "description": "Tools used in this step; each must be in allowed_tools."},
    },
    "required": ["id", "title", "description"],
}

CREATOR_TOOLS: tuple[RegisteredTool, ...] = (
    RegisteredTool(
        name="list_authorable_tools",
        kind="read",
        skill_only=True,
        builtin_only=True,
        description="List the server tools a new skill may use, with kind and description. Reads no mail, calendar or task data.",
        parameters={"type": "object", "properties": {}},
        handler=list_authorable_tools,
    ),
    RegisteredTool(
        name="propose_skill",
        kind="propose",
        skill_only=True,
        builtin_only=True,
        status_code="preparing_proposal",
        description=(
            "Propose a complete new skill as a card the owner must confirm; it is not saved before that. "
            "Use only tools from list_authorable_tools."
        ),
        parameters={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Command after /: 1-64 characters, lowercase letters, digits, - or _."},
                "name": {"type": "string", "description": "Display name, 1-80 characters, unique."},
                "description": {"type": "string", "description": "One or two sentences on what the skill does, at most 400 characters."},
                "instructions": {"type": "string", "description": "Working instructions for the assistant, at most 8000 characters."},
                "workflow_steps": {"type": "array", "items": _STEP_SCHEMA, "description": "Optional ordered steps, at most 20."},
                "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "Tool names from list_authorable_tools."},
            },
            "required": ["slug", "name", "description", "instructions", "allowed_tools"],
        },
        handler=propose_skill,
    ),
)
