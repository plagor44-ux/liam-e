"""Serialized skill mutations, builtin catalog sync, and context revocation.

Advisory lock ``380084`` serializes builtin catalog seeding/upgrades, custom
creations and skill changes (quota, slug and case-insensitive name checks). It is
always taken before per-row ``FOR UPDATE`` locks. A proposal confirmation takes it
after its claim, which already holds the preference lock ``380038``.
"""

from collections.abc import Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.accessors.prompt_template import StaleVersion
from db.engine import get_session_maker
from db.models.chat_context import ChatContext
from db.models.prompt_template import OWNER_SCOPE
from db.models.skill_definition import SkillDefinition
from db.models.skill_revision import SkillRevision

MAX_CUSTOM_SKILLS = 50
BUILTIN_SYNC_LOCK_KEY = 380084


class DuplicateSkillSlug(Exception):
    """Another skill already uses this slug in the installation."""


class DuplicateSkillName(Exception):
    """Another skill already uses this name, compared case-insensitively."""


class BuiltinSkillProtected(Exception):
    """Built-in skills cannot be deleted or structurally altered via user CRUD."""


class SkillQuotaExceeded(Exception):
    """Maximum number of stored custom skills has been reached."""


def _parse_json_list(raw: str, field_name: str) -> list[object]:
    """Validate and decode a serialized JSON array field before DB writes."""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be valid JSON") from error
    if not isinstance(parsed, list):
        raise ValueError(f"{field_name} must be a JSON array")
    return parsed


def _validate_skill_json(workflow_steps_json: str | None, allowed_tools_json: str | None) -> None:
    """Ensure serialized workflow step and tool lists have valid array/element types."""
    if workflow_steps_json is not None:
        steps = _parse_json_list(workflow_steps_json, "workflow_steps_json")
        if not all(isinstance(step, dict) for step in steps):
            raise ValueError("workflow_steps_json entries must be JSON objects")
    if allowed_tools_json is not None:
        tools = _parse_json_list(allowed_tools_json, "allowed_tools_json")
        if not all(isinstance(name, str) and bool(name.strip()) for name in tools):
            raise ValueError("allowed_tools_json entries must be non-empty strings")


def compute_content_hash(
    *,
    slug: str,
    name: str,
    description: str,
    instructions: str,
    workflow_steps_json: str,
    allowed_tools_json: str,
) -> str:
    """Compute a stable SHA-256 digest over the canonical skill definition."""
    _validate_skill_json(workflow_steps_json, allowed_tools_json)
    payload = json.dumps(
        {
            "slug": slug,
            "name": name,
            "description": description,
            "instructions": instructions,
            "workflow_steps": json.loads(workflow_steps_json),
            "allowed_tools": json.loads(allowed_tools_json),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def build_revision(
    *,
    skill_id: str,
    number: int,
    slug: str,
    name: str,
    description: str,
    instructions: str,
    workflow_steps_json: str,
    allowed_tools_json: str,
) -> SkillRevision:
    """Construct an immutable skill revision with its canonical content hash."""
    return SkillRevision(
        skill_id=skill_id,
        number=number,
        slug=slug,
        name=name,
        description=description,
        instructions=instructions,
        workflow_steps_json=workflow_steps_json,
        allowed_tools_json=allowed_tools_json,
        content_hash=compute_content_hash(
            slug=slug,
            name=name,
            description=description,
            instructions=instructions,
            workflow_steps_json=workflow_steps_json,
            allowed_tools_json=allowed_tools_json,
        ),
    )


async def _revoke_contexts_in_session(
    session: AsyncSession,
    skill_id: str,
    *,
    clear_hash: bool = False,
) -> None:
    """Revoke any turn contexts bound to revisions of ``skill_id``."""
    skill_column = getattr(ChatContext, "skill_revision_id", None)
    if skill_column is None:
        return
    revision_ids = select(SkillRevision.id).where(SkillRevision.skill_id == skill_id)
    values: dict[str, object] = {"revoked": True}
    if clear_hash and hasattr(ChatContext, "skill_hash"):
        values["skill_hash"] = None
    await session.execute(
        update(ChatContext).where(skill_column.in_(revision_ids)).values(**values)
    )


async def context_skill_in_session(session: AsyncSession, context: ChatContext | None) -> SkillDefinition | None:
    """Return the active, unremoved skill a turn context is bound to, or ``None``."""
    if context is None or not context.skill_revision_id:
        return None
    revision = await session.get(SkillRevision, context.skill_revision_id)
    if revision is None or revision.removed or revision.owner_scope != OWNER_SCOPE:
        return None
    skill = await session.get(SkillDefinition, revision.skill_id)
    if skill is None or skill.owner_scope != OWNER_SCOPE or skill.status != "active":
        return None
    return skill


async def list_skills() -> list[SkillDefinition]:
    """List all skill definitions in the installation scope ordered by origin and name."""
    async with get_session_maker()() as session:
        rows = (await session.execute(
            select(SkillDefinition)
            .where(SkillDefinition.owner_scope == OWNER_SCOPE)
            .order_by(SkillDefinition.origin, SkillDefinition.name, SkillDefinition.slug)
        )).scalars()
        return list(rows)


async def get_skill(identifier: str) -> SkillDefinition | None:
    """Look up a skill by UUID id or stable slug."""
    async with get_session_maker()() as session:
        row = await session.get(SkillDefinition, identifier)
        if row and row.owner_scope == OWNER_SCOPE:
            return row
        stmt = select(SkillDefinition).where(
            SkillDefinition.owner_scope == OWNER_SCOPE,
            SkillDefinition.slug == identifier,
        )
        return (await session.execute(stmt)).scalar_one_or_none()


async def lock_catalog(session: AsyncSession) -> None:
    """Take the transaction-scoped catalog lock; re-entrant within one transaction."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": BUILTIN_SYNC_LOCK_KEY})


async def _custom_count(session: AsyncSession) -> int:
    return int((await session.execute(
        select(func.count()).select_from(SkillDefinition).where(
            SkillDefinition.owner_scope == OWNER_SCOPE,
            SkillDefinition.origin == "custom",
        )
    )).scalar_one())


async def _slug_taken(session: AsyncSession, slug: str) -> bool:
    stmt = select(SkillDefinition.id).where(SkillDefinition.owner_scope == OWNER_SCOPE, SkillDefinition.slug == slug)
    return (await session.execute(stmt.limit(1))).first() is not None


async def _name_taken(session: AsyncSession, name: str, *, exclude_id: str | None = None) -> bool:
    """Compare names case-insensitively in the database so every caller folds case the same way."""
    stmt = select(SkillDefinition.id).where(
        SkillDefinition.owner_scope == OWNER_SCOPE,
        func.lower(SkillDefinition.name) == func.lower(name),
    )
    if exclude_id is not None:
        stmt = stmt.where(SkillDefinition.id != exclude_id)
    return (await session.execute(stmt.limit(1))).first() is not None


async def creation_conflicts(*, slug: str, name: str) -> list[str]:
    """Report ``slug``, ``name`` and ``quota`` conflicts for a draft without locking.

    Advisory only: confirmation checks again under the catalog lock.
    """
    async with get_session_maker()() as session:
        conflicts: list[str] = []
        if await _slug_taken(session, slug):
            conflicts.append("slug")
        if await _name_taken(session, name):
            conflicts.append("name")
        if await _custom_count(session) >= MAX_CUSTOM_SKILLS:
            conflicts.append("quota")
        return conflicts


async def create_custom_skill_in_session(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    description: str,
    instructions: str,
    workflow_steps_json: str,
    allowed_tools_json: str,
    status: str = "active",
) -> SkillDefinition:
    """Create a custom skill and its first revision in the caller's transaction; never commit.

    The catalog lock serializes quota, slug and name checks with other creations and
    the built-in sync until the caller's transaction ends.
    """
    _validate_skill_json(workflow_steps_json, allowed_tools_json)
    await lock_catalog(session)
    if await _custom_count(session) >= MAX_CUSTOM_SKILLS:
        raise SkillQuotaExceeded
    if await _slug_taken(session, slug):
        raise DuplicateSkillSlug
    if await _name_taken(session, name):
        raise DuplicateSkillName
    row = SkillDefinition(
        slug=slug,
        origin="custom",
        status=status,
        name=name,
        description=description,
        version=1,
        revision_id="",
        updated_at=datetime.now(timezone.utc),
    )
    rev = build_revision(
        skill_id=row.id,
        number=1,
        slug=slug,
        name=name,
        description=description,
        instructions=instructions,
        workflow_steps_json=workflow_steps_json,
        allowed_tools_json=allowed_tools_json,
    )
    row.revision_id = rev.id
    session.add_all([row, rev])
    try:
        await session.flush()
    except IntegrityError as error:
        raise DuplicateSkillSlug from error
    return row


async def create_custom_skill(
    *,
    slug: str,
    name: str,
    description: str,
    instructions: str,
    workflow_steps_json: str,
    allowed_tools_json: str,
    status: str = "active",
) -> SkillDefinition:
    """Create a new custom skill and its initial immutable revision."""
    async with get_session_maker()() as session:
        row = await create_custom_skill_in_session(
            session,
            slug=slug,
            name=name,
            description=description,
            instructions=instructions,
            workflow_steps_json=workflow_steps_json,
            allowed_tools_json=allowed_tools_json,
            status=status,
        )
        await session.commit()
        return row


async def change_skill(
    identifier: str,
    expected_version: int,
    *,
    name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    workflow_steps_json: str | None = None,
    allowed_tools_json: str | None = None,
    status: str | None = None,
    delete: bool = False,
) -> SkillDefinition | None:
    """Update, toggle status, or delete a skill with optimistic concurrency and revocation."""
    if not delete:
        _validate_skill_json(workflow_steps_json, allowed_tools_json)
    async with get_session_maker()() as session:
        # Catalog lock first, then the row, as in the built-in sync: it serializes the
        # case-insensitive name check with creations.
        await lock_catalog(session)
        row = (await session.execute(
            select(SkillDefinition).where(
                SkillDefinition.owner_scope == OWNER_SCOPE,
                (SkillDefinition.id == identifier) | (SkillDefinition.slug == identifier),
            ).with_for_update()
        )).scalar_one_or_none()
        if row is None:
            return None
        if row.version != expected_version:
            raise StaleVersion
        if row.origin == "builtin":
            if delete or any(
                value is not None
                for value in (name, description, instructions, workflow_steps_json, allowed_tools_json)
            ):
                raise BuiltinSkillProtected
            if status is None or status == row.status:
                return row
            previous = await session.get(SkillRevision, row.revision_id)
            assert previous is not None
            row.version += 1
            row.status = status
            row.updated_at = datetime.now(timezone.utc)
            if status == "disabled":
                await _revoke_contexts_in_session(session, row.id)
            await session.commit()
            return row

        if delete:
            await _revoke_contexts_in_session(session, row.id, clear_hash=True)
            await session.execute(
                update(SkillRevision).where(SkillRevision.skill_id == row.id).values(
                    slug=None,
                    name=None,
                    description=None,
                    instructions=None,
                    workflow_steps_json=None,
                    allowed_tools_json=None,
                    content_hash=None,
                    removed=True,
                )
            )
            await session.delete(row)
            await session.commit()
            return row

        previous = await session.get(SkillRevision, row.revision_id)
        assert previous is not None
        next_name = row.name if name is None else name
        next_description = row.description if description is None else description
        next_instructions = previous.instructions if instructions is None else instructions
        next_steps = previous.workflow_steps_json if workflow_steps_json is None else workflow_steps_json
        next_tools = previous.allowed_tools_json if allowed_tools_json is None else allowed_tools_json
        next_status = row.status if status is None else status
        if next_name != row.name and await _name_taken(session, next_name, exclude_id=row.id):
            raise DuplicateSkillName

        content_changed = (
            next_name != row.name
            or next_description != row.description
            or next_instructions != previous.instructions
            or next_steps != previous.workflow_steps_json
            or next_tools != previous.allowed_tools_json
        )
        status_changed = next_status != row.status
        if not content_changed and not status_changed:
            return row

        old_tools = set(json.loads(previous.allowed_tools_json or "[]"))
        new_tools = set(json.loads(next_tools or "[]"))
        tools_narrowed = bool(old_tools - new_tools)

        row.version += 1
        row.updated_at = datetime.now(timezone.utc)
        row.name = next_name
        row.description = next_description
        row.status = next_status
        if content_changed:
            max_number = int((await session.execute(
                select(func.coalesce(func.max(SkillRevision.number), 0)).where(SkillRevision.skill_id == row.id)
            )).scalar_one())
            rev = build_revision(
                skill_id=row.id,
                number=max_number + 1,
                slug=row.slug,
                name=next_name,
                description=next_description,
                instructions=next_instructions or "",
                workflow_steps_json=next_steps or "[]",
                allowed_tools_json=next_tools or "[]",
            )
            row.revision_id = rev.id
            session.add(rev)
        if next_status == "disabled" or tools_narrowed:
            await _revoke_contexts_in_session(session, row.id)
        await session.commit()
        return row


async def sync_builtin_skills(specs: Sequence[dict[str, str]]) -> list[SkillDefinition]:
    """Idempotently create or upgrade built-in skills while preserving owner status."""
    if not specs:
        return []
    synced: list[SkillDefinition] = []
    async with get_session_maker()() as session:
        await lock_catalog(session)
        now = datetime.now(timezone.utc)
        for spec in specs:
            slug = spec["slug"]
            target_hash = compute_content_hash(
                slug=slug,
                name=spec["name"],
                description=spec["description"],
                instructions=spec["instructions"],
                workflow_steps_json=spec["workflow_steps_json"],
                allowed_tools_json=spec["allowed_tools_json"],
            )
            existing = (await session.execute(
                select(SkillDefinition).where(
                    SkillDefinition.owner_scope == OWNER_SCOPE,
                    SkillDefinition.slug == slug,
                ).with_for_update()
            )).scalar_one_or_none()
            if existing is None:
                row = SkillDefinition(
                    slug=slug,
                    origin="builtin",
                    status="active",
                    name=spec["name"],
                    description=spec["description"],
                    version=1,
                    revision_id="",
                    updated_at=now,
                )
                rev = build_revision(
                    skill_id=row.id,
                    number=1,
                    slug=slug,
                    name=spec["name"],
                    description=spec["description"],
                    instructions=spec["instructions"],
                    workflow_steps_json=spec["workflow_steps_json"],
                    allowed_tools_json=spec["allowed_tools_json"],
                )
                row.revision_id = rev.id
                session.add_all([row, rev])
                synced.append(row)
                continue

            if existing.origin != "builtin":
                synced.append(existing)
                continue
            current_rev = await session.get(SkillRevision, existing.revision_id)
            if current_rev and not current_rev.removed and current_rev.content_hash == target_hash:
                synced.append(existing)
                continue

            old_tools = set(json.loads(current_rev.allowed_tools_json or "[]")) if current_rev else set()
            new_tools = set(json.loads(spec["allowed_tools_json"]))
            if old_tools - new_tools:
                await _revoke_contexts_in_session(session, existing.id)
            max_number = int((await session.execute(
                select(func.coalesce(func.max(SkillRevision.number), 0)).where(SkillRevision.skill_id == existing.id)
            )).scalar_one())
            existing.version += 1
            existing.name = spec["name"]
            existing.description = spec["description"]
            existing.updated_at = now
            rev = build_revision(
                skill_id=existing.id,
                number=max_number + 1,
                slug=slug,
                name=spec["name"],
                description=spec["description"],
                instructions=spec["instructions"],
                workflow_steps_json=spec["workflow_steps_json"],
                allowed_tools_json=spec["allowed_tools_json"],
            )
            existing.revision_id = rev.id
            session.add(rev)
            synced.append(existing)
        await session.commit()
    return synced
