"""Non-streaming compatibility endpoint for one chat turn."""
from db.accessors import chat_message as message_access
from fastapi import APIRouter
from typing import Literal
from pydantic import BaseModel, Field
from api.errors import quiet_validation_route, refuse
from api.locales import Locale
from api.mail import agent
from api.mail.models_catalog import list_chat_models, require_available_model
from . import schemas

router = APIRouter(route_class=quiet_validation_route('chat_invalid', 'Prüfe die Nachricht: 1–8.000 Zeichen.'))


class ChatTurnRequest(BaseModel):
    conversation_id: str = Field(default='', description='Leer lassen für eine neue Unterhaltung')
    message: str = Field(min_length=1, max_length=8000)
    expected_mail_scope_version: int | None = Field(default=None, ge=1)
    account_id: str | None = None
    prompt_revision_id: str | None = None
    skill_id: str | None = Field(default=None, max_length=100)
    skill_revision_id: str | None = Field(default=None, max_length=100)
    without_preferences: bool = False
    locale: Locale | None = Field(default=None, description="Browser language when the saved setting follows the browser")
    model: str | None = None
    context_message_id: str | None = None
    context_occurrence_id: str | None = None
    calendar_source_ids: list[str] | None = Field(default=None, description="Omit to include every currently readable calendar. An explicit list narrows this turn.")
    calendar_timezone: str = Field(default='Europe/Berlin', max_length=100)
    task_source: Literal['none', 'all', 'unlinked', 'account', 'account_and_unlinked'] = 'none'
    task_source_account_id: str | None = Field(default=None, max_length=200)


@router.post('', operation_id='chat_turn', summary='Send a message to the mail agent')
async def chat_turn(body: ChatTurnRequest) -> schemas.ChatTurnResponse:
    """Run a complete chat turn and return its visible messages."""
    if not body.conversation_id and body.model is not None:
        require_available_model(body.model, await list_chat_models())
    conversation_id = ''
    user_id = ''
    try:
        async for event in agent.run_turn_events(conversation_id=body.conversation_id or None, text=body.message, account_id=body.account_id, expected_mail_scope_version=body.expected_mail_scope_version, prompt_revision_id=body.prompt_revision_id, skill_id=body.skill_id, skill_revision_id=body.skill_revision_id, model=body.model, context_message_id=body.context_message_id, context_occurrence_id=body.context_occurrence_id, calendar_source_ids=body.calendar_source_ids, calendar_timezone=body.calendar_timezone, task_source=body.task_source, task_source_account_id=body.task_source_account_id, without_preferences=body.without_preferences, locale=body.locale):
            if event.event == 'start':
                conversation_id, user_id = event.conversation_id, event.user_message_id
            elif event.event == 'error':
                if event.code == 'calendar_reference_unavailable':
                    raise refuse(404, event.code, 'Dieser Termin ist für den Chat nicht mehr verfügbar.')
                if event.code == 'context_revoked':
                    raise refuse(409, 'context_revoked', 'Die Vorlage, Präferenzen oder die Auswahl von Konten, Kalendern oder Aufgaben sind nicht mehr gültig. Starte eine neue Unterhaltung.')
                if event.code == 'preferences_unavailable':
                    raise refuse(503, 'preferences_unavailable', 'Die Präferenzen konnten nicht geladen werden. Du kannst ausdrücklich ohne Präferenzen fortfahren.')
                if event.code == 'model_unavailable':
                    raise refuse(503, 'model_unavailable', 'Das Modell antwortet gerade nicht. Versuch es gleich noch mal.')
                raise refuse(500, 'internal', 'Die Antwort konnte nicht erstellt werden. Versuch es gleich noch mal.')
    except LookupError as error:
        raise refuse(404, 'conversation_not_found', 'Diese Unterhaltung gibt es nicht mehr. Starte eine neue Unterhaltung.') from error
    rows = await message_access.list_messages(conversation_id)
    index = next((i for i, row in enumerate(rows) if row.id == user_id), len(rows))
    return schemas.ChatTurnResponse(conversation_id=conversation_id, messages=[schemas.to_message_out(m) for m in agent.visible_messages(rows[index + 1:])])
