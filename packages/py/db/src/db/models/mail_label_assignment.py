"""Non-exclusive app-side labels for indexed mail."""

from sqlmodel import Field, SQLModel


class MailLabelAssignment(SQLModel, table=True):
    __tablename__ = "mail_label_assignment"

    message_id: str = Field(primary_key=True)
    label_id: str = Field(primary_key=True, index=True)
