"""Owner-managed, app-side folders and labels."""

from typing import Literal

from db.accessors import mail_filing
from db.models.mail_folder import MailFolder
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from api.errors import refuse

router = APIRouter()


class DestinationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["folder", "label"]


class RenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)


class FilingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_ids: list[str] = Field(min_length=1, max_length=100)
    folder_id: str | None = None
    add_labels: list[str] = Field(default_factory=list, max_length=100)
    remove_labels: list[str] = Field(default_factory=list, max_length=100)


@router.get("", operation_id="mail_folders_list")
async def list_destinations() -> list[MailFolder]:
    return await mail_filing.list_destinations()


@router.post("", operation_id="mail_folder_create")
async def create_destination(body: DestinationIn) -> MailFolder:
    try:
        return await mail_filing.create_destination(body.name, body.kind)
    except ValueError:
        raise refuse(422, "folder_invalid", "Der Name ist ungültig.") from None
    except IntegrityError:
        raise refuse(409, "folder_exists", "Diesen Namen gibt es schon.") from None


@router.post("/assign", operation_id="mail_filing_assign")
async def assign(body: FilingIn) -> dict[str, bool]:
    kwargs = {"add_labels": body.add_labels, "remove_labels": body.remove_labels}
    if "folder_id" in body.model_fields_set:
        kwargs["folder_id"] = body.folder_id
    try:
        await mail_filing.assign(body.message_ids, **kwargs)
    except LookupError:
        raise refuse(404, "message_not_found", "Die Mail ist nicht mehr da.") from None
    except ValueError:
        raise refuse(422, "filing_invalid", "Diese Zuordnung ist nicht möglich.") from None
    return {"ok": True}


@router.patch("/{destination_id}", operation_id="mail_folder_update")
async def rename_destination(destination_id: str, body: RenameIn) -> MailFolder:
    try:
        return await mail_filing.rename_destination(destination_id, body.name)
    except LookupError:
        raise refuse(404, "folder_not_found", "Der Ordner oder das Label ist nicht mehr da.") from None
    except ValueError:
        raise refuse(422, "folder_invalid", "Der Name ist ungültig.") from None
    except IntegrityError:
        raise refuse(409, "folder_exists", "Diesen Namen gibt es schon.") from None


@router.delete("/{destination_id}", operation_id="mail_folder_delete")
async def delete_destination(destination_id: str, confirm_delete: bool = False) -> dict[str, bool]:
    if not confirm_delete:
        raise refuse(422, "folder_confirmation_required", "Bestätige das Löschen.")
    try:
        await mail_filing.delete_destination(destination_id)
    except LookupError:
        raise refuse(404, "folder_not_found", "Der Ordner oder das Label ist nicht mehr da.") from None
    except ValueError:
        raise refuse(409, "folder_in_use", "Eine Regel verwendet diesen Ordner oder dieses Label.") from None
    return {"ok": True}
