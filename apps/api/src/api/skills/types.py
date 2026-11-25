"""Shared types and domain exceptions for the server-side skill runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from db.models.chat_message import ChatMessage

SKILL_CREATOR_SLUG = "skill_creator"

ToolKind = Literal["read", "propose", "ui"]
SkillOrigin = Literal["builtin", "custom"]
SkillStatus = Literal["active", "disabled"]
StatusCode = Literal[
    "thinking",
    "searching",
    "reading",
    "checking_deadlines",
    "finding_newsletters",
    "preparing_proposal",
]


@dataclass
class ToolOutcome:
    """Result returned by any agent or skill tool execution."""

    content: str
    mails: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    proposal_message: ChatMessage | None = None
    calendars: list[dict[str, str]] = field(default_factory=list)


class SkillWorkflowStep(BaseModel):
    """One documented, inspectable step inside a skill workflow."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    tool_names: list[str] = Field(default_factory=list, max_length=20)


class SkillDraft(BaseModel):
    """A custom skill drafted in the chat; nothing is stored until the owner confirms its card."""

    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    instructions: str = Field(min_length=1, max_length=8000)
    workflow_steps: list[SkillWorkflowStep] = Field(default_factory=list, max_length=20)
    allowed_tools: list[str] = Field(default_factory=list, max_length=32)


class SkillEdit(BaseModel):
    """Text fields the owner may change on a skill card before saving.

    Tools and steps change only through a new card drafted in the chat.
    """

    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    instructions: str = Field(min_length=1, max_length=8000)


class SkillExecutionRef(BaseModel):
    """Content-free reference identifying the skill revision executed in a turn."""

    model_config = ConfigDict(extra="forbid")
    id: str
    slug: str
    revision_id: str
    version: int
    origin: SkillOrigin


@dataclass(frozen=True)
class ActiveSkill:
    """Server-resolved, immutable skill binding for one chat turn."""

    id: str
    slug: str
    origin: SkillOrigin
    name: str
    description: str
    version: int
    revision_id: str
    content_hash: str
    instructions: str
    workflow_steps: tuple[SkillWorkflowStep, ...]
    allowed_tools: tuple[str, ...]

    def execution_ref(self) -> SkillExecutionRef:
        return SkillExecutionRef(
            id=self.id,
            slug=self.slug,
            revision_id=self.revision_id,
            version=self.version,
            origin=self.origin,
        )


class SkillError(Exception):
    """Base error for skill catalog, validation, or activation failures."""

    def __init__(self, code: str, message: str, *, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(code)


class SkillNotFoundError(SkillError):
    def __init__(self) -> None:
        super().__init__(
            "skill_not_found",
            "Dieser Skill existiert nicht mehr. Wähle einen anderen.",
            status=404,
        )


class SkillRevisionUnavailableError(SkillError):
    def __init__(self) -> None:
        super().__init__(
            "skill_revision_unavailable",
            "Diese Skill-Version ist nicht mehr verfügbar.",
            status=404,
        )


class SkillDisabledError(SkillError):
    def __init__(self) -> None:
        super().__init__(
            "skill_disabled",
            "Dieser Skill ist deaktiviert. Aktiviere ihn zuerst oder wähle einen anderen.",
            status=409,
        )


class SkillToolUnknownError(SkillError):
    def __init__(self, missing: list[str] | None = None) -> None:
        self.missing = sorted(missing or [])
        super().__init__(
            "skill_tool_unknown",
            "Mindestens ein ausgewähltes Werkzeug ist im Server nicht registriert.",
            status=422,
        )


class SkillToolUnavailableError(SkillError):
    def __init__(self, missing: list[str] | None = None) -> None:
        self.missing = sorted(missing or [])
        super().__init__(
            "skill_tool_unavailable",
            "Ein für diesen Skill benötigtes Werkzeug steht in dieser Version nicht zur Verfügung.",
            status=422,
        )


class SkillToolNotAuthorableError(SkillError):
    def __init__(self, refused: list[str] | None = None) -> None:
        self.refused = sorted(refused or [])
        super().__init__(
            "skill_tool_not_authorable",
            "Mindestens ein ausgewähltes Werkzeug ist für eigene Skills nicht freigegeben.",
            status=422,
        )


class SkillProtectedError(SkillError):
    def __init__(self) -> None:
        super().__init__(
            "skill_builtin_protected",
            "Integrierte Skills können nur aktiviert oder deaktiviert werden.",
            status=403,
        )


class SkillConflictError(SkillError):
    def __init__(self, code: str = "skill_conflict", message: str = "Ein Skill mit dieser Kennung existiert bereits.") -> None:
        super().__init__(code, message, status=409)


class SkillValidationError(SkillError):
    def __init__(self, message: str = "Prüfe die Skill-Angaben.") -> None:
        super().__init__("skill_invalid", message, status=422)
