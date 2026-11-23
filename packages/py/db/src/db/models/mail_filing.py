"""Exclusive app-side folder assignment for an indexed mail."""

from sqlmodel import Field, SQLModel


class MailFiling(SQLModel, table=True):
    __tablename__ = "mail_filing"

    message_id: str = Field(primary_key=True)
    folder_id: str | None = Field(default=None, index=True)
