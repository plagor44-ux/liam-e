"""Owner-managed classification rules and confirmed application."""

from db.accessors import mail_filing, mail_job, mail_message, mail_rule, mail_rule_run
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


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    confirm_apply: bool = False


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
async def preview(body: RuleIn) -> dict[str, int]:
    rule = MailRule(**(body.model_dump() | {"is_active": True}))
    matched = already_filed = 0
    async for page in matching_pages(rule):
        matched += len(page)
        filings = await mail_filing.for_messages([message.id for message in page])
        already_filed += sum(bool(filings[message.id].folder_id) for message in page)
    return {"matched": matched, "already_filed": already_filed}


@router.post("/{rule_id}/apply", operation_id="mail_rule_apply")
async def apply_rule(rule_id: str, body: ApplyIn) -> dict[str, int]:
    if not body.confirm_apply:
        raise refuse(422, "rule_apply_confirmation_required", "Bestätige das Anwenden der Regel.")
    rule = await mail_rule.get(rule_id)
    if rule is None:
        raise refuse(404, "rule_not_found", "Die Regel ist nicht mehr da.")
    if rule.version != body.expected_version:
        raise refuse(409, "rule_version_conflict", "Die Regel wurde inzwischen geändert.")
    snapshot = [[item.id, item.version] for item in await mail_rule.list_rules() if item.is_active]
    count = 0
    async for message in matching_messages(rule):
        await mail_job.enqueue("mail_rule_apply", {"account_id": message.account_id, "message_id": message.id,
            "rules": [[rule.id, rule.version]], "snapshot": snapshot},
            dedupe_key=f"mail-rule-manual:{rule.id}:{rule.version}:{message.id}")
        count += 1
    return {"matched": count}


@router.get("/{rule_id}/runs", operation_id="mail_rule_runs_list")
async def list_runs(rule_id: str) -> list[MailRuleRun]:
    return await mail_rule_run.list_for_rule(rule_id)
