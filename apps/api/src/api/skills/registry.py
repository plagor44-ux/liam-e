"""Server-side trusted tool registry for base chat tools and skill-specific tools.

Only executable tools registered in Python application code here may ever be
exposed to the model or dispatched during a chat turn.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from api.skills.types import (
    SkillToolNotAuthorableError,
    SkillToolUnavailableError,
    SkillToolUnknownError,
    StatusCode,
    ToolKind,
    ToolOutcome,
)

ToolHandler = Callable[[dict[str, Any], Any], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class RegisteredTool:
    """Metadata, JSON schema, and optional direct handler for a server-trusted tool.

    ``authorable`` approves a tool for skills drafted in the chat. ``builtin_only``
    reserves a tool for code-owned built-in skills: no custom skill may reference it.
    """

    name: str
    kind: ToolKind
    description: str
    parameters: dict[str, Any]
    skill_only: bool = False
    status_code: StatusCode | None = None
    handler: ToolHandler | None = None
    authorable: bool = False
    builtin_only: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Explicit server-side catalog of registered executable tools."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._bootstrapped = False

    def register(self, tool: RegisteredTool) -> None:
        if not tool.name or not tool.name.strip():
            raise ValueError("Tool name must not be empty")
        if tool.skill_only and tool.handler is None:
            raise ValueError(f"Skill-only tool requires an async handler: {tool.name}")
        if tool.authorable and tool.builtin_only:
            raise ValueError(f"A built-in-only tool cannot be authorable: {tool.name}")
        existing = self._tools.get(tool.name)
        if existing is not None:
            if existing == tool:
                return
            raise ValueError(f"Tool already registered with a different definition: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> RegisteredTool | None:
        self._ensure_bootstrapped()
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def list_tools(self, *, include_skill_only: bool = True) -> list[RegisteredTool]:
        self._ensure_bootstrapped()
        tools = list(self._tools.values())
        if not include_skill_only:
            tools = [tool for tool in tools if not tool.skill_only]
        return sorted(tools, key=lambda tool: (tool.skill_only, tool.name))

    def base_schemas(self) -> list[dict[str, Any]]:
        self._ensure_bootstrapped()
        return [tool.schema() for tool in self._tools.values() if not tool.skill_only]

    def validate_for_definition(
        self,
        names: Iterable[str],
        *,
        authoring: bool = False,
        builtin: bool = False,
    ) -> list[str]:
        """Validate tool names when authoring/updating a skill definition.

        Custom skills never reference built-in-only tools. A skill drafted in the
        chat (``authoring``) may only reference tools approved as authorable.
        """
        self._ensure_bootstrapped()
        unique = sorted(dict.fromkeys(names))
        missing = [name for name in unique if name not in self._tools]
        if missing:
            raise SkillToolUnknownError(missing)
        if not builtin:
            refused = [
                name for name in unique
                if self._tools[name].builtin_only or (authoring and not self._tools[name].authorable)
            ]
            if refused:
                raise SkillToolNotAuthorableError(refused)
        return unique

    def authorable_tools(self) -> list[RegisteredTool]:
        """Tools a chat-drafted skill may reference, sorted by name."""
        self._ensure_bootstrapped()
        return sorted((tool for tool in self._tools.values() if tool.authorable), key=lambda tool: tool.name)

    def resolve_for_execution(self, names: Iterable[str]) -> list[RegisteredTool]:
        """Resolve tool definitions for an active skill turn; fail closed if any is absent."""
        self._ensure_bootstrapped()
        unique = list(dict.fromkeys(names))
        missing = [name for name in unique if name not in self._tools]
        if missing:
            raise SkillToolUnavailableError(missing)
        return [self._tools[name] for name in unique]

    def schemas_for(self, names: Iterable[str]) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.resolve_for_execution(names)]

    def _ensure_bootstrapped(self) -> None:
        if self._bootstrapped:
            return
        self._bootstrapped = True
        if self is TOOL_REGISTRY:
            register_default_tools(self)


TOOL_REGISTRY = ToolRegistry()


def register_default_tools(registry: ToolRegistry) -> None:
    """Populate the default registry with trusted base and skill-specific tools."""
    from api.skills.base_tools import BASE_TOOLS
    from api.skills.briefing import DAILY_BRIEFING_TOOL
    from api.skills.creator import CREATOR_TOOLS

    for tool in BASE_TOOLS:
        registry.register(tool)
    registry.register(DAILY_BRIEFING_TOOL)
    for tool in CREATOR_TOOLS:
        registry.register(tool)
