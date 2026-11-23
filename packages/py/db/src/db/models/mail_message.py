"""One synced mail message of one account — the searchable index the chat agent works on.

``provider_uid`` is the provider's own id (Gmail message id, IMAP UID per folder, Graph id), unique
per (account, folder). Confirmed IMAP trash moves store the destination as UIDVALIDITY:UID. ``body_text`` is capped at ``MAIL_SYNC_MAX_BODY_CHARS`` during sync so one
huge mail cannot dominate the index. ``recipients_json``/``cc_json``/``reply_to_json``/``labels_json``
are JSON arrays of strings.
``IMAP_DELETED`` holds a reversibly flagged INBOX delivery under UIDVALIDITY:UID until
an IMAP poll observes its flag cleared or the delivery expunged.
``IMAP_RETIRED`` is a local archive reference without a known physical provider folder.
"""

from datetime import datetime

from sqlalchemy import Column, Computed, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlmodel import Field, SQLModel

from db.models._common import new_id, utc_datetime_column, utcnow

IMAP_RETIRED = "IMAP_RETIRED"


class MailMessage(SQLModel, table=True):
    __tablename__ = "mail_message"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "folder",
            "provider_uid",
            name="uq_mail_message_account_folder_uid",
        ),
        Index("ix_mail_message_account_date", "account_id", "internal_date"),
        Index("ix_mail_message_folder_date", "folder", "internal_date", "id"),
        Index("ix_mail_message_search_tsv", "search_tsv", postgresql_using="gin"),
        Index("ix_mail_message_connector_alias_ids", "connector_alias_ids", postgresql_using="gin",
              postgresql_where=text("cardinality(connector_alias_ids) > 0")),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    account_id: str = Field(index=True)
    folder: str = "INBOX"
    provider_uid: str
    # IMAP UIDs identify a delivery only within one mailbox generation.
    imap_uidvalidity: str | None = None
    thread_id: str = ""
    subject: str = ""
    sender: str = ""
    sender_address: str = Field(default="", index=True)
    list_unsubscribe: str = ""
    list_unsubscribe_post: str = ""
    rfc_message_id: str = ""
    references: str = ""
    cc_json: str = "[]"
    # Server default keeps inserts from older code working while the column is NOT NULL.
    reply_to_json: str = Field(default="[]", sa_column_kwargs={"server_default": "[]"})
    attachments_json: str = "[]"
    is_flagged: bool = False
    search_tsv: str | None = Field(default=None, sa_column=Column(TSVECTOR, Computed("to_tsvector('german', coalesce(subject,'') || ' ' || coalesce(sender,'') || ' ' || coalesce(body_text,''))", persisted=True)))
    recipients_json: str = "[]"
    snippet: str = ""
    body_text: str = ""
    body_html: str | None = Field(default=None, sa_column=Column(String(200_000), nullable=True))
    html_version: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    html_status: str = Field(default="pending", sa_column_kwargs={"server_default": "pending"})
    # Version of the text extraction that produced body_text (api.mail.sync_types.BODY_VERSION);
    # sync re-reads older bodies from the provider. 0 marks rows from before versioning.
    # Legacy Studio rows keep their ordinary inbox identity until a bound provider read verifies them.
    unverified_connector: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    # Previous local UUIDs of verified copies of this same provider message and mailbox.
    connector_alias_ids: list[str] = Field(default_factory=list, sa_column=Column(
        ARRAY(String), nullable=False, server_default=text("'{}'")))
    body_version: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    internal_date: datetime | None = Field(
        default=None, sa_column=utc_datetime_column(nullable=True)
    )
    has_attachments: bool = False
    is_read: bool = False
    labels_json: str = "[]"
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=utc_datetime_column()
    )
