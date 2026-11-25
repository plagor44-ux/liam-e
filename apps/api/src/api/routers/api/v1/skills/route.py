"""Skill catalog and server tool registry endpoints; skill text is never echoed in errors."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from api.errors import refuse
from api.skills import (
    SkillError,
    SkillWorkflowStep,
    TOOL_REGISTRY,
    create_skill,
    delete_skill,
    get_skill_detail,
    list_skills_with_revisions,
    update_skill,
)
from api.skills.service import parse_allowed_tools, parse_workflow_steps


class PrivateSkillRoute(APIRoute):
    """Validation and DB failures must never echo rejected skill content."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request):
            try:
                return await handler(request)
            except SkillError as error:
                raise refuse(error.status, error.code, error.message) from None
            except SQLAlchemyError:
                raise refuse(503, "skills_unavailable", "Die Skills sind gerade nicht erreichbar. Versuch es gleich noch einmal.") from None
            except RequestValidationError:
                raise refuse(422, "skill_invalid", "Prüfe die Skill-Angaben: Kennung, Name, Beschreibung, Anweisungen, Werkzeuge und eine gültige Version.") from None

        return private_handler


router = APIRouter(
    route_class=PrivateSkillRoute,
    responses={
        403: {"description": "Built-in skill structure cannot be edited or deleted"},
        404: {"description": "Skill unavailable"},
        409: {"description": "Conflict, disabled skill, or stale expected version"},
        422: {"description": "Invalid fields or unregistered tools; content is not echoed"},
    },
)


class RegisteredToolOut(BaseModel):
    name: str
    kind: Literal["read", "propose", "ui"]
    description: str
    skill_only: bool
    authorable: bool = Field(description="Approved for skills drafted in the chat")


class SkillCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    instructions: str = Field(min_length=1, max_length=8000)
    workflow_steps: list[SkillWorkflowStep] = Field(default_factory=list, max_length=20)
    allowed_tools: list[str] = Field(default_factory=list, max_length=32)
    status: Literal["active", "disabled"] = "active"


class SkillUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, min_length=1, max_length=400)
    instructions: str | None = Field(default=None, min_length=1, max_length=8000)
    workflow_steps: list[SkillWorkflowStep] | None = Field(default=None, max_length=20)
    allowed_tools: list[str] | None = Field(default=None, max_length=32)
    status: Literal["active", "disabled"] | None = None

    @model_validator(mode="after")
    def reject_null_fields(self):
        for field in ("name", "description", "instructions", "workflow_steps", "allowed_tools", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError("skill_invalid")
        return self


class SkillRevisionOut(BaseModel):
    id: str
    number: int
    slug: str
    name: str
    description: str
    instructions: str
    workflow_steps: list[SkillWorkflowStep]
    allowed_tools: list[str]
    content_hash: str


class SkillOut(BaseModel):
    id: str
    slug: str
    origin: Literal["builtin", "custom"]
    status: Literal["active", "disabled"]
    name: str
    description: str
    version: int
    revision_id: str
    instructions: str
    workflow_steps: list[SkillWorkflowStep]
    allowed_tools: list[str]
    content_hash: str
    updated_at: datetime | None


class SkillDetailOut(SkillOut):
    revisions: list[SkillRevisionOut]


def _revision_out(revision) -> SkillRevisionOut:
    return SkillRevisionOut(
        id=revision.id,
        number=revision.number,
        slug=revision.slug or "",
        name=revision.name or "",
        description=revision.description or "",
        instructions=revision.instructions or "",
        workflow_steps=parse_workflow_steps(revision.workflow_steps_json),
        allowed_tools=parse_allowed_tools(revision.allowed_tools_json),
        content_hash=revision.content_hash or "",
    )


def _skill_out(skill, revision) -> SkillOut:
    return SkillOut(
        id=skill.id,
        slug=skill.slug,
        origin=skill.origin,
        status=skill.status,
        name=skill.name,
        description=skill.description,
        version=skill.version,
        revision_id=revision.id,
        instructions=revision.instructions or "",
        workflow_steps=parse_workflow_steps(revision.workflow_steps_json),
        allowed_tools=parse_allowed_tools(revision.allowed_tools_json),
        content_hash=revision.content_hash or "",
        updated_at=skill.updated_at,
    )


@router.get("/tools", operation_id="skill_tools_list", summary="List server-registered trusted tools")
async def list_registered_tools() -> list[RegisteredToolOut]:
    return [
        RegisteredToolOut(
            name=tool.name,
            kind=tool.kind,
            description=tool.description,
            skill_only=tool.skill_only,
            authorable=tool.authorable,
        )
        for tool in TOOL_REGISTRY.list_tools()
    ]


@router.get("", operation_id="skills_list", summary="List registered and custom skills")
async def list_skills_route() -> list[SkillOut]:
    return [_skill_out(skill, revision) for skill, revision in await list_skills_with_revisions()]


@router.post("", operation_id="skill_create", status_code=201, summary="Create a custom versioned skill")
async def create_skill_route(body: SkillCreateIn) -> SkillOut:
    skill, revision = await create_skill(
        slug=body.slug,
        name=body.name,
        description=body.description,
        instructions=body.instructions,
        workflow_steps=body.workflow_steps,
        allowed_tools=body.allowed_tools,
        status=body.status,
    )
    return _skill_out(skill, revision)


@router.get("/{skill_id}", operation_id="skill_get", summary="Read one skill and its active revisions")
async def get_skill_route(skill_id: str) -> SkillDetailOut:
    skill, current, revisions = await get_skill_detail(skill_id)
    return SkillDetailOut(
        **_skill_out(skill, current).model_dump(),
        revisions=[_revision_out(rev) for rev in revisions],
    )


@router.patch("/{skill_id}", operation_id="skill_update", summary="Update a skill or change its activation status")
async def update_skill_route(skill_id: str, body: SkillUpdateIn) -> SkillOut:
    skill, revision = await update_skill(
        skill_id,
        body.expected_version,
        name=body.name,
        description=body.description,
        instructions=body.instructions,
        workflow_steps=body.workflow_steps,
        allowed_tools=body.allowed_tools,
        status=body.status,
    )
    return _skill_out(skill, revision)


@router.delete("/{skill_id}", operation_id="skill_delete", status_code=204, summary="Delete a custom skill")
async def delete_skill_route(skill_id: str, expected_version: int = Query(ge=1)) -> Response:
    await delete_skill(skill_id, expected_version)
    return Response(status_code=204)
