"""Owner-managed classification rules and confirmed application."""

from db.accessors import mail_filing, mail_message, mail_rule, mail_rule_run
from db.models.mail_rule import MailRule
from db.models.mail_rule_run import MailRuleRun
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from api.errors import refuse
from api.mail import rules as rule_engine
from api.mail.rule_schemas import RuleIn, RuleUpdate

router = APIRouter()




class RulePreviewOut(BaseModel):
    matched: int
    already_filed: int
    message_ids: list[str]


class RulePreviewIn(RuleIn):
    saved_rule_id: str | None = None
    expected_version: int | None = Field(default=None, ge=1)
    preview_draft: bool = False


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    confirm_apply: bool = False
    preview_message_ids: list[str] | None = None


class ApplyOut(BaseModel):
    matched: int
    app_changed: int
    provider_skipped: int
    provider_queued: int
    conflicts: int


class RuleFromMessageIn(RuleIn):
    source_message_id: str = Field(min_length=1, max_length=100)
    source_account_id: str = Field(min_length=1, max_length=100)


@router.get("", operation_id="mail_rules_list")
async def list_rules() -> list[MailRule]:
    return await mail_rule.list_rules()


@router.post("", operation_id="mail_rule_create")
async def create_rule(body: RuleIn) -> MailRule:
    try:
        return await mail_rule.create(**body.model_dump())
    except ValueError:
        raise refuse(422, "rule_invalid", "Diese Regel ist nicht gültig.") from None


@router.post('/from-message', operation_id='mail_rule_create_from_message')
async def create_rule_from_message(body: RuleFromMessageIn) -> MailRule:
    try:
        return await mail_rule.create_from_message(body.source_message_id, body.source_account_id,
            **body.model_dump(exclude={'source_message_id', 'source_account_id'}))
    except LookupError:
        raise refuse(404, 'message_not_found', 'Die Mail ist nicht mehr da.') from None
    except ValueError:
        raise refuse(422, 'rule_invalid', 'Diese Regel ist nicht gültig.') from None


@router.patch("/{rule_id}", operation_id="mail_rule_update")
async def update_rule(rule_id: str, body: RuleUpdate) -> MailRule:
    try:
        return await mail_rule.update(rule_id, body.expected_version, **body.model_dump(exclude={"expected_version"}))
    except LookupError:
        raise refuse(404, "rule_not_found", "Die Regel ist nicht mehr da.") from None
    except mail_rule.VersionConflict:
        raise refuse(409, "rule_version_conflict", "Die Regel wurde inzwischen geändert.") from None
    except ValueError:
        raise refuse(422, "rule_invalid", "Diese Regel ist nicht gültig.") from None


@router.delete("/{rule_id}", operation_id="mail_rule_delete")
async def delete_rule(rule_id: str, expected_version: int) -> dict[str, bool]:
    try:
        await mail_rule.delete_rule(rule_id, expected_version)
    except LookupError:
        raise refuse(404, "rule_not_found", "Die Regel ist nicht mehr da.") from None
    except mail_rule.VersionConflict:
        raise refuse(409, "rule_version_conflict", "Die Regel wurde inzwischen geändert.") from None
    return {"ok": True}


async def matching_pages(rule: MailRule):
    before = None
    while True:
        # Manual runs cover indexed provider INBOX, including app-filed messages.
        page = await mail_message.list_page(folder="INBOX", hide_filed=False, before=before, limit=100)
        yield [message for message in page if rule_engine.matches(rule, message)]
        if len(page) < 100:
            break
        before = (page[-1].internal_date, page[-1].id)


async def matching_messages(rule: MailRule):
    async for page in matching_pages(rule):
        for message in page:
            yield message


@router.post("/preview", operation_id="mail_rules_preview", response_model=RulePreviewOut)
async def preview(body: RulePreviewIn) -> dict[str, int | list[str]]:
    from sqlalchemy import select
    from db.engine import get_session_maker

    saved_id = getattr(body, 'saved_rule_id', None)
    draft_changed = False
    if saved_id:
        rule = await mail_rule.get(saved_id)
        if rule is None:
            raise refuse(404, 'rule_not_found', 'Die Regel ist nicht mehr da.')
        if not rule.is_active:
            raise refuse(409, 'rule_inactive', 'Die Regel ist pausiert. Aktiviere sie, um sie anzuwenden.')
        if rule.version != body.expected_version:
            raise refuse(409, 'rule_version_conflict', 'Die Regel wurde inzwischen geändert.')
        if body.preview_draft:
            if not body.is_active:
                raise refuse(409, 'rule_inactive', 'Die Regel ist pausiert. Aktiviere sie, um sie anzuwenden.')
            values = body.model_dump(exclude={'saved_rule_id', 'expected_version', 'preview_draft'})
            draft_changed = any(getattr(rule, key) != value for key, value in values.items())
            rule = MailRule.model_validate(rule.model_dump() | values)
    else:
        if not body.is_active:
            raise refuse(409, 'rule_inactive', 'Die Regel ist pausiert. Aktiviere sie, um sie anzuwenden.')
        rule = MailRule(**body.model_dump(exclude={'saved_rule_id', 'expected_version', 'preview_draft'}))
    active = [item for item in await mail_rule.list_rules() if item.is_active and item.id != rule.id]
    active.append(rule)
    active.sort(key=lambda item: (item.position, item.created_at, item.id))
    matched = already_filed = 0
    message_ids = []
    async for page in matching_pages(rule):
        filings = await mail_filing.for_messages([message.id for message in page])
        async with get_session_maker()() as session:
            existing = set((await session.scalars(select(MailRuleRun.message_id).where(
                MailRuleRun.rule_id == rule.id, MailRuleRun.rule_version == rule.version,
                MailRuleRun.message_id.in_([message.id for message in page])))).all()) if saved_id and not draft_changed else set()
        for message in page:
            filing = filings[message.id]
            _, folder_change, labels = mail_rule_run.local_changes(message, rule, active, filing.folder_id, filing.label_ids, rule_engine.matches)
            provider = any((rule.mark_read, rule.mark_unread, rule.archive)) and message.id not in existing
            if folder_change or labels or provider:
                matched += 1
                already_filed += bool(filing.folder_id)
                message_ids.append(message.id)
    return {"matched": matched, "already_filed": already_filed, "message_ids": message_ids}


@router.post("/{rule_id}/apply", operation_id="mail_rule_apply", response_model=ApplyOut)
async def apply_rule(rule_id: str, body: ApplyIn) -> dict[str, int]:
    if not body.confirm_apply:
        raise refuse(422, "rule_apply_confirmation_required", "Bestätige das Anwenden der Regel.")
    rule = await mail_rule.get(rule_id)
    if rule is None:
        raise refuse(404, "rule_not_found", "Die Regel ist nicht mehr da.")
    if not rule.is_active:
        raise refuse(409, 'rule_inactive', 'Die Regel ist pausiert. Aktiviere sie, um sie anzuwenden.')
    if rule.version != body.expected_version:
        raise refuse(409, "rule_version_conflict", "Die Regel wurde inzwischen geändert.")
    candidates = [message async for message in matching_messages(rule)]
    try:
        return await mail_rule_run.apply_manual(rule.id, body.expected_version, candidates, rule_engine.matches, body.preview_message_ids)
    except LookupError:
        raise refuse(404, "rule_not_found", "Die Regel ist nicht mehr da.") from None
    except mail_rule_run.RuleInactive:
        raise refuse(409, 'rule_inactive', 'Die Regel ist pausiert. Aktiviere sie, um sie anzuwenden.') from None
    except mail_rule.VersionConflict:
        raise refuse(409, "rule_version_conflict", "Die Regel wurde inzwischen geändert.") from None


@router.get("/{rule_id}/runs", operation_id="mail_rule_runs_list")
async def list_runs(rule_id: str) -> list[MailRuleRun]:
    return await mail_rule_run.list_for_rule(rule_id)
