"""Alembic environment configured for async SQLAlchemy (asyncpg) + SQLModel.

The connection URL is taken from db settings (``DATABASE_URL``, normalized
to the asyncpg driver) rather than alembic.ini, and ``target_metadata`` is
``SQLModel.metadata`` populated by importing every model module. Autogenerate only diffs the
tables those models declare (``db.schema_filter``), so tables a library or extension owns are left
alone.

The migration set is one independent branch per capability that owns a table (each rooted at
``down_revision = None`` and labelled with the capability id), so it may have several heads:
upgrade to ``heads``, never ``head``.
"""

import asyncio
from logging.config import fileConfig

import db.models  # noqa: F401  (import populates SQLModel.metadata)
from alembic import context
from alembic.autogenerate import comparators, rewriter
from alembic.operations import ops
from db import metadata as target_metadata
from db.migrations import row_triggers  # noqa: F401 (register the model trigger comparator and renderer)
from db.schema_filter import include_object
from env.db import env
from sqlalchemy import CheckConstraint, inspect, pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Several processes can upgrade at once, such as API instances that each migrate as they start. This
# Postgres advisory lock makes them take turns, so the later ones find the database already upgraded.
MIGRATION_LOCK_KEY = int.from_bytes(b"migrate")

config = context.config
config.set_main_option("sqlalchemy.url", env.async_database_url)

# Not `fileConfig`'s default, which disables every logger that exists already: `cli migrations` and
# the migration tests run this in a process whose own loggers would then go silent.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


@comparators.dispatch_for("table")
def compare_removed_checks(autogen_context, modify_ops, schema, name, reflected, model):
    """Autogenerate misses removed CHECKs; this repo forbids hand-edited migrations.

    Only a table whose model declares no CHECK at all gets drops. Removing one of several
    CHECKs from a model is not detected: unnamed constraints cannot be matched safely.
    """
    if (model is None or reflected is None
            or not include_object(reflected, name, "table", True, model)
            or any(isinstance(c, CheckConstraint) for c in model.constraints)):
        return
    for check in inspect(autogen_context.connection).get_check_constraints(name, schema=schema):
        constraint = CheckConstraint(check["sqltext"], name=check["name"])
        reflected.append_constraint(constraint)
        modify_ops.ops.append(ops.DropConstraintOp.from_constraint(constraint))


mail_downgrade = rewriter.Rewriter()


@mail_downgrade.rewrites(ops.DropColumnOp)
def rewrite_data_handoffs(context, revision, operation):
    """Generate data handoffs for rule labels and the IMAP downgrade."""
    if (operation.table_name, operation.column_name) == ("mail_rule", "label_id"):
        return [ops.ExecuteSQLOp("UPDATE mail_rule SET label_ids = jsonb_build_array(label_id) WHERE label_id IS NOT NULL"), operation]
    if (operation.table_name, operation.column_name) == ("mail_rule", "label_ids"):
        return [ops.ExecuteSQLOp("DO $$ BEGIN IF EXISTS (SELECT 1 FROM mail_rule WHERE jsonb_array_length(label_ids) > 1) THEN RAISE EXCEPTION 'Cannot downgrade rules with multiple labels'; END IF; END $$"),
                ops.ExecuteSQLOp("UPDATE mail_rule SET label_id = label_ids->>0"), operation]
    if (operation.table_name, operation.column_name) != ("mail_message", "imap_uidvalidity"):
        return operation
    # Take the column drop's lock before reading the rows. An in-flight insight writer
    # holds a share lock on its mail until its new cache entry commits.
    return [ops.ExecuteSQLOp("LOCK TABLE mail_message IN ACCESS EXCLUSIVE MODE"), ops.ExecuteSQLOp("""
        UPDATE mail_message AS hidden
        SET folder = 'INBOX', provider_uid = split_part(hidden.provider_uid, ':', 2)
        FROM mail_sync_state AS state JOIN mail_account AS account ON account.id = state.account_id
        WHERE account.provider = 'imap' AND hidden.account_id = account.id AND hidden.folder = 'IMAP_DELETED'
          AND hidden.provider_uid ~ '^[0-9]+:[0-9]+$'
          AND hidden.imap_uidvalidity = split_part(hidden.provider_uid, ':', 1)
          AND hidden.imap_uidvalidity = split_part(state.cursor, ':', 1)
          AND NOT EXISTS (SELECT 1 FROM mail_message AS current
                          WHERE current.account_id = hidden.account_id AND current.folder = 'INBOX'
                            AND current.provider_uid = split_part(hidden.provider_uid, ':', 2))
    """), ops.ExecuteSQLOp("""
        WITH archived AS (
            DELETE FROM mail_message AS mail USING mail_account AS account
            WHERE mail.account_id = account.id AND account.provider = 'imap'
              AND (mail.folder = 'IMAP_RETIRED' OR (mail.folder = 'ARCHIVE'
                   AND (mail.provider_uid LIKE 'retired:%' OR mail.provider_uid LIKE 'inbox:%')))
            RETURNING mail.id, mail.account_id, mail.connector_alias_ids, mail.subject, mail.sender,
                      mail.sender_address, mail.recipients_json, mail.cc_json, mail.snippet, mail.body_text,
                      mail.internal_date, mail.is_read, mail.is_flagged, mail.attachments_json, mail.has_attachments
        ), links AS (
            SELECT link.id, archived.account_id, (to_jsonb(archived) - ARRAY['id','account_id','connector_alias_ids'])::text AS content
            FROM archived CROSS JOIN LATERAL unnest(array_prepend(archived.id, archived.connector_alias_ids)) AS link(id)
        ), cleared AS (
            DELETE FROM mail_insight WHERE message_id IN (SELECT id FROM links)
        )
        INSERT INTO mail_reference_snapshot (id, account_id, content_json)
        SELECT id, account_id, content FROM links
        ON CONFLICT (id) DO NOTHING
    """), operation]


def run_migrations_offline() -> None:
    context.configure(
        url=env.async_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
        process_revision_directives=mail_downgrade,
    )
    with context.begin_transaction():
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
