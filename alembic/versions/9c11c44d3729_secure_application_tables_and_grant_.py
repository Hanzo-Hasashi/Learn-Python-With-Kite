"""secure application tables and grant runtime role

Revision ID: 9c11c44d3729
Revises: d9f9f9ebbced
Create Date: 2026-11-18 00:07:46.598211

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9c11c44d3729'
down_revision: Union[str, Sequence[str], None] = 'd9f9f9ebbced'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APPLICATION_TABLES = (
    "activity",
    "auth_sessions",
    "guest_playground_executions",
    "lesson_progress",
    "lessons",
    "mobile_money_payments",
    "notifications",
    "password_resets",
    "payment_attempts",
    "payment_provider_configurations",
    "payment_status_history",
    "payment_transactions",
    "playground_errors",
    "playground_executions",
    "quiz_attempts",
    "quiz_results",
    "revoked_tokens",
    "subscription_plans",
    "subscription_registrations",
    "subscriptions",
    "users",
)
LEGACY_COLUMNS = {
    "users": (
        sa.Column("auth_session_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    ),
    "notifications": (
        sa.Column("event_key", sa.String(length=160), nullable=True),
        sa.Column("target_route", sa.String(length=40), nullable=True),
    ),
    "quiz_attempts": (
        sa.Column("submission_id", sa.String(length=64), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
    ),
}


def upgrade() -> None:
    bind = op.get_bind()
    schema = "public" if bind.dialect.name == "postgresql" else None
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names(schema=schema))
    missing_tables = set(APPLICATION_TABLES) - existing_tables
    if missing_tables:
        raise RuntimeError(
            "The database is missing Kite application tables: "
            + ", ".join(sorted(missing_tables))
        )

    for table_name, columns in LEGACY_COLUMNS.items():
        existing_columns = {column["name"] for column in inspector.get_columns(table_name, schema=schema)}
        for column in columns:
            if column.name not in existing_columns:
                op.add_column(table_name, column, schema=schema)
                existing_columns.add(column.name)

    existing_indexes = {
        table_name: {index["name"] for index in inspector.get_indexes(table_name, schema=schema)}
        for table_name in ("notifications", "quiz_attempts")
    }
    if "uq_notifications_user_event" not in existing_indexes["notifications"]:
        op.create_index(
            "uq_notifications_user_event", "notifications", ["user_id", "event_key"],
            unique=True, schema=schema,
        )
    if "uq_quiz_attempts_submission" not in existing_indexes["quiz_attempts"]:
        op.create_index(
            "uq_quiz_attempts_submission", "quiz_attempts",
            ["user_id", "quiz_id", "submission_id"], unique=True,
            sqlite_where=sa.text("submission_id IS NOT NULL"),
            postgresql_where=sa.text("submission_id IS NOT NULL"),
            schema=schema,
        )

    if bind.dialect.name != "postgresql":
        return
    runtime_role = bind.execute(sa.text(
        "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
        "FROM pg_roles WHERE rolname = 'kite_runtime'"
    )).mappings().one_or_none()
    if (
        runtime_role is None or runtime_role["rolsuper"] or not runtime_role["rolbypassrls"]
        or runtime_role["rolcreatedb"] or runtime_role["rolcreaterole"]
    ):
        raise RuntimeError(
            "Create the restricted kite_runtime role before applying this migration."
        )

    preparer = bind.dialect.identifier_preparer
    runtime_identifier = preparer.quote("kite_runtime")
    grantees = ["PUBLIC"]
    existing_roles = set(bind.execute(sa.text(
        "SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')"
    )).scalars())
    client_roles = [preparer.quote(role) for role in sorted(existing_roles)]
    grantees.extend(client_roles)

    bind.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    bind.exec_driver_sql("GRANT USAGE ON SCHEMA public TO " + runtime_identifier)
    bind.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM " + runtime_identifier)
    bind.exec_driver_sql(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC"
    )
    bind.exec_driver_sql(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC"
    )
    for role in sorted(existing_roles):
        bind.exec_driver_sql(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM "
            + preparer.quote(role)
        )
        bind.exec_driver_sql(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM "
            + preparer.quote(role)
        )
    bind.exec_driver_sql(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO " + runtime_identifier
    )
    bind.exec_driver_sql(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO " + runtime_identifier
    )
    for table_name in APPLICATION_TABLES:
        table_identifier = "public." + preparer.quote(table_name)
        bind.exec_driver_sql("ALTER TABLE " + table_identifier + " ENABLE ROW LEVEL SECURITY")
        bind.exec_driver_sql(
            "REVOKE ALL PRIVILEGES ON TABLE " + table_identifier
            + " FROM " + ", ".join(grantees)
        )
        bind.exec_driver_sql(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE " + table_identifier
            + " TO " + runtime_identifier
        )

    application_sequences = bind.execute(sa.text(
        "SELECT seq.relname FROM pg_class AS seq "
        "JOIN pg_namespace AS sequence_schema ON sequence_schema.oid = seq.relnamespace "
        "JOIN pg_depend AS dependency ON dependency.objid = seq.oid "
        "AND dependency.deptype IN ('a', 'i') "
        "JOIN pg_class AS application_table ON application_table.oid = dependency.refobjid "
        "WHERE seq.relkind = 'S' AND sequence_schema.nspname = 'public' "
        "AND application_table.relname = ANY(:table_names)"
    ), {"table_names": list(APPLICATION_TABLES)}).scalars()
    for sequence_name in application_sequences:
        sequence_identifier = "public." + preparer.quote(sequence_name)
        bind.exec_driver_sql(
            "REVOKE ALL PRIVILEGES ON SEQUENCE " + sequence_identifier
            + " FROM " + ", ".join(grantees)
        )
        bind.exec_driver_sql(
            "GRANT USAGE, SELECT ON SEQUENCE " + sequence_identifier
            + " TO " + runtime_identifier
        )
    if "alembic_version" in sa.inspect(bind).get_table_names(schema="public"):
        bind.exec_driver_sql(
            "REVOKE ALL PRIVILEGES ON TABLE public.alembic_version FROM "
            + ", ".join(grantees)
        )
        bind.exec_driver_sql(
            "GRANT SELECT ON TABLE public.alembic_version TO " + runtime_identifier
        )


def downgrade() -> None:
    raise RuntimeError("The security baseline cannot be downgraded automatically.")
