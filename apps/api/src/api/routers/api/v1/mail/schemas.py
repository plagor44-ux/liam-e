"""Response schemas for the indexed inbox."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.mail.actions import ActionKind


class AttachmentOut(BaseModel):
    name: str
    size: int
    content_type: str


class MessageListItemOut(BaseModel):
    id: str
    account_id: str
    color_index: int
    subject: str
    sender_name: str
    sender_address: str
    snippet: str
    internal_date: datetime | None
    is_read: bool
    is_flagged: bool
    has_attachments: bool
    deadline: date | None
    needs_reply: bool
    app_folder_id: str | None = None
    app_label_ids: list[str] = Field(default_factory=list)


class InsightOut(BaseModel):
    points: list[str]
    deadline: date | None
    needs_reply: bool
    followups: list[str]
    model: str
    created_at: datetime


class MessageHeaderOut(BaseModel):
    name: str
    value: str


class MessageOriginalOut(BaseModel):
    headers: list[MessageHeaderOut]
    source: str
    truncated: bool


class MessageHtmlOut(BaseModel):
    html: str | None


class MessageDetailOut(MessageListItemOut):
    local_reference_only: bool = False
    to: list[str]
    cc: list[str]
    body_text: str
    attachments: list[AttachmentOut]
    thread_id: str
    can_unsubscribe: bool
    can_show_original: bool


class MessagePageOut(BaseModel):
    items: list[MessageListItemOut]
    next_cursor: str | None


class MessageActionIn(BaseModel):
    kind: ActionKind


class MessageActionOut(BaseModel):
    action_id: str
    message: MessageListItemOut


class ProviderRateLimitDetail(BaseModel):
    code: Literal["provider_rate_limited"]
    message: str
    retryable: bool


class ProviderRateLimitOut(BaseModel):
    detail: ProviderRateLimitDetail


class BulkMessageActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_ids: list[str] = Field(min_length=1, max_length=100)
    kind: Literal["archive", "trash", "spam", "mark_read", "mark_unread"]

    @field_validator("message_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Duplicate message IDs")
        return value


class BulkMessageActionResult(BaseModel):
    message_id: str
    status: Literal["done", "not_found", "unsupported", "failed", "partial"]
    reconciled: bool = Field(default=False, exclude_if=lambda flag: not flag)
    retryable: bool = Field(default=False, exclude_if=lambda flag: not flag)
    folder: Literal["INBOX", "TRASH", "ARCHIVE", "SPAM"] | None = None
    action_id: str | None = None
    message: MessageListItemOut | None = None
    error_code: str | None = Field(default=None, exclude_if=lambda code: code is None)


class BulkMessageActionOut(BaseModel):
    results: list[BulkMessageActionResult]


class UndoIn(BaseModel):
    action_ids: list[str] = Field(min_length=1, max_length=200)


class UndoOut(BaseModel):
    restored: list[str]
    expired: list[str]
    skipped: list[str]
    failed: list[str]
    error_code: str | None = Field(default=None, exclude_if=lambda code: code is None)
    unsupported: list[str] = Field(default_factory=list, exclude_if=lambda ids: not ids)
    retryable: list[str] = Field(default_factory=list, exclude_if=lambda ids: not ids)
    unconfirmed: list[str] = Field(default_factory=list, exclude_if=lambda ids: not ids)
