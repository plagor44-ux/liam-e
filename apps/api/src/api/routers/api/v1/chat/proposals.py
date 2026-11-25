"""Explicit confirmation and cancellation of proposed mail actions."""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from api.preferences.candidates import PreferenceDecision

from api.errors import refuse
from api.mail import proposals
from api.tasks.proposals import TaskCreateCommand
from api.skills.types import SkillEdit
from api.errors import quiet_validation_route
from api.mail.chat_schemas import ChatMessageOut, to_message_out

router = APIRouter(route_class=quiet_validation_route('proposal_invalid', 'Prüfe die Angaben des Vorschlags.'))


class ProposalConfirmIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preference_decision: PreferenceDecision | None = None
    selected_ids: list[str]
    body: str | None = None
    task_edit: TaskCreateCommand | None = None
    skill_edit: SkillEdit | None = None


def _refuse(error: Exception):
    if isinstance(error, proposals.ProposalNotFoundError):
        raise refuse(404, "proposal_not_found", "Der Vorschlag ist nicht mehr da. Lade die Unterhaltung neu.")
    if isinstance(error, proposals.ProposalStateError):
        raise refuse(409, "proposal_not_pending", "Dieser Vorschlag ist schon erledigt oder abgebrochen. Lade die Unterhaltung neu, um den Stand zu sehen.")
    if isinstance(error, proposals.ProposalContextRevokedError):
        raise refuse(409, "proposal_context_revoked", "Dieser Vorschlag stammt aus einem widerrufenen Kontext. Starte eine neue Unterhaltung.")
    if isinstance(error, proposals.ProposalSelectionError):
        text = "Wähle mindestens einen Eintrag aus." if error.code == "selection_empty" else "Die Auswahl passt nicht zum Vorschlag. Lade die Unterhaltung neu."
        raise refuse(422, error.code, text)
    raise error


@router.post("/{proposal_id}/confirm", operation_id="chat_proposal_confirm", description="Führt die ausgewählten Einträge eines offenen Vorschlags nach Bestätigung aus.")
async def confirm(proposal_id: str, body: ProposalConfirmIn) -> ChatMessageOut:
    try:
        return to_message_out(await proposals.confirm(proposal_id, selected_ids=body.selected_ids, body=body.body, task_edit=body.task_edit,
                                                      preference_decision=body.preference_decision, skill_edit=body.skill_edit))
    except (proposals.ProposalNotFoundError, proposals.ProposalStateError, proposals.ProposalSelectionError, proposals.ProposalContextRevokedError) as error:
        _refuse(error)


@router.post("/{proposal_id}/check", operation_id="chat_proposal_check")
async def check(proposal_id: str) -> ChatMessageOut:
    from api.calendar.writes import WriteError
    try:
        return to_message_out(await proposals.recheck_calendar_card(proposal_id))
    except WriteError as error:
        raise refuse(error.status, error.code, "Der Stand konnte nicht geprüft werden. Versuch es später erneut.") from None
    except (proposals.ProposalNotFoundError, proposals.ProposalStateError) as error:
        _refuse(error)


@router.post("/{proposal_id}/cancel", operation_id="chat_proposal_cancel", description="Verwirft einen offenen Vorschlag, ohne seine Aktionen auszuführen.")
async def cancel(proposal_id: str) -> ChatMessageOut:
    try:
        return to_message_out(await proposals.cancel(proposal_id))
    except (proposals.ProposalNotFoundError, proposals.ProposalStateError) as error:
        _refuse(error)
