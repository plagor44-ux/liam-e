"""Skill catalog CRUD, validation, and explicit per-turn resolution."""

from __future__ import annotations

import json
import re
from typing import Any

from db.accessors import skill_definition as skill_access
from db.accessors import skill_revision as revision_access
from db.accessors.prompt_template import StaleVersion
from db.models.skill_definition import SkillDefinition
from db.models.skill_revision import SkillRevision

from api.skills.catalog import ensure_builtin_skills
from api.skills.registry import TOOL_REGISTRY
from api.skills.types import (
    SKILL_CREATOR_SLUG,
    ActiveSkill,
    SkillConflictError,
    SkillDisabledError,
    SkillNotFoundError,
    SkillProtectedError,
    SkillRevisionUnavailableError,
    SkillStatus,
    SkillValidationError,
    SkillWorkflowStep,
)

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED_SKILL_SLUGS = frozenset({"tools", SKILL_CREATOR_SLUG})
NAME_CONFLICT_MESSAGE = "Ein Skill mit diesem Namen existiert bereits."
QUOTA_MESSAGE = "Die Grenze für eigene Skills ist erreicht. Entferne zuerst einen gespeicherten Skill."


def parse_workflow_steps(raw: str | None) -> list[SkillWorkflowStep]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    return [SkillWorkflowStep.model_validate(item) for item in items]


def parse_allowed_tools(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        return []
    return items


def validate_skill_payload(
    *,
    slug: str | None = None,
    name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    workflow_steps: list[SkillWorkflowStep] | None = None,
    allowed_tools: list[str] | None = None,
    status: SkillStatus | None = None,
    authoring: bool = False,
) -> dict[str, Any]:
    """Validate and normalize skill fields against the server tool registry.

    ``authoring`` limits tools to those approved for skills drafted in the chat.
    """
    normalized: dict[str, Any] = {}
    if slug is not None:
        cleaned_slug = slug.strip()
        if not SLUG_PATTERN.match(cleaned_slug) or cleaned_slug in RESERVED_SKILL_SLUGS:
            raise SkillValidationError("Prüfe die Kennung: 1–64 Zeichen, Kleinbuchstaben, Ziffern, Bindestrich oder Unterstrich.")
        normalized["slug"] = cleaned_slug
    if name is not None:
        cleaned_name = name.strip()
        if not (1 <= len(cleaned_name) <= 80):
            raise SkillValidationError("Prüfe den Skill-Namen: 1–80 Zeichen.")
        normalized["name"] = cleaned_name
    if description is not None:
        cleaned_desc = description.strip()
        if not (1 <= len(cleaned_desc) <= 400):
            raise SkillValidationError("Prüfe die Beschreibung: 1–400 Zeichen.")
        normalized["description"] = cleaned_desc
    if instructions is not None:
        cleaned_instr = instructions.strip()
        if not (1 <= len(cleaned_instr) <= 8000):
            raise SkillValidationError("Prüfe die Anweisungen: 1–8.000 Zeichen.")
        normalized["instructions"] = cleaned_instr
    if status is not None:
        if status not in ("active", "disabled"):
            raise SkillValidationError()
        normalized["status"] = status
    if allowed_tools is not None:
        normalized["allowed_tools"] = TOOL_REGISTRY.validate_for_definition(allowed_tools, authoring=authoring)
    if workflow_steps is not None:
        if len(workflow_steps) > 20:
            raise SkillValidationError("Höchstens 20 Arbeitsschritte sind möglich.")
        seen_ids: set[str] = set()
        for step in workflow_steps:
            if step.id in seen_ids:
                raise SkillValidationError("Jeder Arbeitsschritt braucht eine eindeutige Kennung.")
            seen_ids.add(step.id)
        normalized["workflow_steps"] = workflow_steps
    if "workflow_steps" in normalized and "allowed_tools" in normalized:
        allowed_set = set(normalized["allowed_tools"])
        for step in normalized["workflow_steps"]:
            if not set(step.tool_names).issubset(allowed_set):
                raise SkillValidationError("Ein Arbeitsschritt verweist auf ein nicht zugeordnetes Werkzeug.")
    return normalized


async def list_skills_with_revisions() -> list[tuple[SkillDefinition, SkillRevision]]:
    """Sync built-in skills and return all catalog entries with their current revision."""
    await ensure_builtin_skills()
    rows = await skill_access.list_skills()
    result: list[tuple[SkillDefinition, SkillRevision]] = []
    for row in rows:
        rev = await revision_access.get_revision(row.revision_id)
        if rev is not None and not rev.removed:
            result.append((row, rev))
    return result


async def get_skill_detail(identifier: str) -> tuple[SkillDefinition, SkillRevision, list[SkillRevision]]:
    """Load one skill, its current revision, and all active revisions (newest first)."""
    await ensure_builtin_skills()
    row = await skill_access.get_skill(identifier)
    if row is None:
        raise SkillNotFoundError
    rev = await revision_access.get_revision(row.revision_id)
    if rev is None or rev.removed:
        raise SkillNotFoundError
    revisions = await revision_access.list_revisions(row.id)
    if not revisions:
        raise SkillNotFoundError
    return row, rev, revisions


async def create_skill(
    *,
    slug: str,
    name: str,
    description: str,
    instructions: str,
    workflow_steps: list[SkillWorkflowStep],
    allowed_tools: list[str],
    status: SkillStatus = "active",
) -> tuple[SkillDefinition, SkillRevision]:
    """Create a custom skill referencing server-registered tools."""
    await ensure_builtin_skills()
    data = validate_skill_payload(
        slug=slug,
        name=name,
        description=description,
        instructions=instructions,
        workflow_steps=workflow_steps,
        allowed_tools=allowed_tools,
        status=status,
    )
    try:
        row = await skill_access.create_custom_skill(
            slug=data["slug"],
            name=data["name"],
            description=data["description"],
            instructions=data["instructions"],
            workflow_steps_json=json.dumps([s.model_dump() for s in data["workflow_steps"]], ensure_ascii=False),
            allowed_tools_json=json.dumps(data["allowed_tools"], ensure_ascii=False),
            status=data["status"],
        )
    except skill_access.DuplicateSkillSlug as error:
        raise SkillConflictError() from error
    except skill_access.DuplicateSkillName as error:
        raise SkillConflictError("skill_name_conflict", NAME_CONFLICT_MESSAGE) from error
    except skill_access.SkillQuotaExceeded as error:
        raise SkillConflictError("skill_quota", QUOTA_MESSAGE) from error
    rev = await revision_access.get_revision(row.revision_id)
    assert rev is not None
    return row, rev


async def update_skill(
    identifier: str,
    expected_version: int,
    *,
    name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    workflow_steps: list[SkillWorkflowStep] | None = None,
    allowed_tools: list[str] | None = None,
    status: SkillStatus | None = None,
) -> tuple[SkillDefinition, SkillRevision]:
    """Update a skill or toggle its active status with optimistic concurrency."""
    await ensure_builtin_skills()
    existing = await skill_access.get_skill(identifier)
    if existing is None:
        raise SkillNotFoundError
    current_rev = await revision_access.get_revision(existing.revision_id)
    if current_rev is None or current_rev.removed:
        raise SkillNotFoundError
    data = validate_skill_payload(
        name=name,
        description=description,
        instructions=instructions,
        workflow_steps=workflow_steps,
        allowed_tools=allowed_tools,
        status=status,
    )
    if "workflow_steps" in data or "allowed_tools" in data:
        effective_tools = set(data.get("allowed_tools", parse_allowed_tools(current_rev.allowed_tools_json)))
        effective_steps = data.get("workflow_steps", parse_workflow_steps(current_rev.workflow_steps_json))
        for step in effective_steps:
            if not set(step.tool_names).issubset(effective_tools):
                raise SkillValidationError("Ein Arbeitsschritt verweist auf ein nicht zugeordnetes Werkzeug.")
    try:
        row = await skill_access.change_skill(
            identifier,
            expected_version,
            name=data.get("name"),
            description=data.get("description"),
            instructions=data.get("instructions"),
            workflow_steps_json=(
                json.dumps([s.model_dump() for s in data["workflow_steps"]], ensure_ascii=False)
                if "workflow_steps" in data
                else None
            ),
            allowed_tools_json=(
                json.dumps(data["allowed_tools"], ensure_ascii=False)
                if "allowed_tools" in data
                else None
            ),
            status=data.get("status"),
        )
    except skill_access.BuiltinSkillProtected as error:
        raise SkillProtectedError() from error
    except skill_access.DuplicateSkillName as error:
        raise SkillConflictError("skill_name_conflict", NAME_CONFLICT_MESSAGE) from error
    except StaleVersion as error:
        raise SkillConflictError("version_conflict", "Der Skill wurde inzwischen geändert. Lade ihn neu.") from error
    if row is None:
        raise SkillNotFoundError
    rev = await revision_access.get_revision(row.revision_id)
    assert rev is not None
    return row, rev


async def delete_skill(identifier: str, expected_version: int) -> None:
    """Delete a custom skill, leaving content-free tombstones and revoking contexts."""
    await ensure_builtin_skills()
    try:
        row = await skill_access.change_skill(identifier, expected_version, delete=True)
    except skill_access.BuiltinSkillProtected as error:
        raise SkillProtectedError() from error
    except StaleVersion as error:
        raise SkillConflictError("version_conflict", "Der Skill wurde inzwischen geändert. Lade ihn neu.") from error
    if row is None:
        raise SkillNotFoundError


def active_skill_from_rows(skill: SkillDefinition, revision: SkillRevision) -> ActiveSkill:
    """Construct an immutable runtime ``ActiveSkill`` and verify all tools exist on server."""
    if skill.status != "active":
        raise SkillDisabledError()
    if revision.removed or revision.skill_id != skill.id:
        raise SkillRevisionUnavailableError()
    allowed_tools = tuple(parse_allowed_tools(revision.allowed_tools_json))
    TOOL_REGISTRY.resolve_for_execution(allowed_tools)
    steps = tuple(parse_workflow_steps(revision.workflow_steps_json))
    return ActiveSkill(
        id=skill.id,
        slug=revision.slug or skill.slug,
        origin=skill.origin,  # type: ignore[arg-type]
        name=revision.name or skill.name,
        description=revision.description or skill.description,
        version=revision.number,
        revision_id=revision.id,
        content_hash=revision.content_hash or "",
        instructions=revision.instructions or "",
        workflow_steps=steps,
        allowed_tools=allowed_tools,
    )


async def load_active_skill(skill_revision_id: str | None) -> ActiveSkill:
    """Load an already-resolved pinned skill revision without syncing catalog rows."""
    if not isinstance(skill_revision_id, str) or not skill_revision_id.strip():
        raise SkillRevisionUnavailableError()
    revision = await revision_access.get_revision(skill_revision_id.strip())
    if revision is None or revision.removed:
        raise SkillRevisionUnavailableError()
    skill = await skill_access.get_skill(revision.skill_id)
    if skill is None:
        raise SkillNotFoundError()
    return active_skill_from_rows(skill, revision)


async def resolve_skill(*, skill_id: str | None = None, skill_revision_id: str | None = None) -> ActiveSkill | None:
    """Resolve and validate an explicitly requested skill or revision without creating a context."""
    if skill_id is None and skill_revision_id is None:
        return None
    await ensure_builtin_skills()
    if skill_revision_id is not None:
        if not isinstance(skill_revision_id, str) or not skill_revision_id.strip():
            raise SkillRevisionUnavailableError()
        revision = await revision_access.get_revision(skill_revision_id.strip())
        if revision is None or revision.removed:
            raise SkillRevisionUnavailableError()
        skill = await skill_access.get_skill(revision.skill_id)
        if skill is None:
            raise SkillNotFoundError()
        if skill_id is not None and skill_id.strip() not in {skill.id, skill.slug}:
            raise SkillRevisionUnavailableError()
        return active_skill_from_rows(skill, revision)
    if not isinstance(skill_id, str) or not skill_id.strip():
        raise SkillNotFoundError()
    skill = await skill_access.get_skill(skill_id.strip())
    if skill is None:
        raise SkillNotFoundError()
    revision = await revision_access.get_revision(skill.revision_id)
    if revision is None or revision.removed:
        raise SkillRevisionUnavailableError()
    return active_skill_from_rows(skill, revision)
