"""Validated rule inputs shared by the browser and chat."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    account_id: str | None = None
    sender_address: str = Field(default="", max_length=320)
    sender_domain: str = Field(default="", max_length=255)
    subject_contains: str = Field(default="", max_length=200)
    recipient: str = Field(default="", max_length=320)
    folder_id: str | None = None
    label_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=20)
    mark_read: bool = False
    mark_unread: bool = False
    archive: bool = False
    position: int = 0
    is_active: bool = True

    @model_validator(mode="after")
    def check_rule(self):
        self.label_ids = list(dict.fromkeys(self.label_ids))
        self.name = self.name.strip()
        self.sender_address = self.sender_address.strip().casefold()
        self.sender_domain = self.sender_domain.strip().lstrip("@").casefold()
        self.subject_contains = self.subject_contains.strip()
        self.recipient = self.recipient.strip().casefold()
        if not self.name or not any((self.account_id, self.sender_address, self.sender_domain, self.subject_contains, self.recipient)):
            raise ValueError("A name and at least one condition are required")
        if not (self.folder_id or self.label_ids):
            raise ValueError("A folder or label is required")
        if self.mark_read and self.mark_unread:
            raise ValueError("Choose one read action")
        if self.sender_address and ("@" not in self.sender_address or any(c.isspace() for c in self.sender_address)):
            raise ValueError("Invalid sender")
        if self.sender_domain and ("@" in self.sender_domain or any(c.isspace() for c in self.sender_domain)):
            raise ValueError("Invalid domain")
        if self.recipient and ("@" not in self.recipient or any(c.isspace() for c in self.recipient)):
            raise ValueError("Invalid recipient")
        return self


class RuleUpdate(RuleIn):
    expected_version: int = Field(ge=1)
