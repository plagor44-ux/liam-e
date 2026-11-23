"""Owner-managed classification rules and confirmed application."""

from db.accessors import mail_message, mail_rule, mail_rule_run
from db.models.mail_rule import MailRule
from db.models.mail_rule_run import MailRuleRun
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from api.errors import refuse
from api.mail import rules as rule_engine
from api.mail.rule_schemas import RuleIn, RuleUpdate

router = APIRouter()




class ApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    confirm_apply: bool = False


@router.get("", operation_id="mail_rules_list")
async def list_rules() -> list[MailRule]:
    return await mail_rule.list_rules()


@router.post("", operation_id="mail_rule_create")
async def create_rule(body: RuleIn) -> MailRule:
    try:
        return await mail_rule.create(**body.model_dump())
    except ValueError:
        raise refuse(422, "rule_invalid", "Diese Regel ist nicht gültig.") from None


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


async def matching_messages(rule: MailRule):
    before = None
    while True:
        page = await mail_message.list_page(folder="INBOX", hide_filed=True, before=before, limit=100)
        for message in page:
            if rule_engine.matches(rule, message):
                yield message
        if len(page) < 100:
            break
        before = (page[-1].internal_date, page[-1].id)


@router.post("/preview", operation_id="mail_rules_preview")
async def preview(body: RuleIn) -> dict[str, int]:
    rule = MailRule(**(body.model_dump() | {"is_active": True}))
    return {"matched": sum([1 async for _ in matching_messages(rule)])}


@router.post("/{rule_id}/apply", operation_id="mail_rule_apply")
async def apply_rule(rule_id: str, body: ApplyIn) -> dict[str, int]:
    if not body.confirm_apply:
        raise refuse(422, "rule_apply_confirmation_required", "Bestätige das Anwenden der Regel.")
    rule = await mail_rule.get(rule_id)
    if rule is None:
        raise refuse(404, "rule_not_found", "Die Regel ist nicht mehr da.")
    if rule.version != body.expected_version:
        raise refuse(409, "rule_version_conflict", "Die Regel wurde inzwischen geändert.")
    count = 0
    async for message in matching_messages(rule):
        await rule_engine.execute_rule_run(rule.id, rule.version, message.id)
        count += 1
    return {"matched": count}


@router.get("/{rule_id}/runs", operation_id="mail_rule_runs_list")
async def list_runs(rule_id: str) -> list[MailRuleRun]:
    return await mail_rule_run.list_for_rule(rule_id)
