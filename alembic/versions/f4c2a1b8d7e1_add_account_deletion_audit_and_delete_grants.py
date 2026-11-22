"""add account deletion reconciliation and runtime delete grants

Revision ID: f4c2a1b8d7e1
Revises: e85bcc461ab0
Create Date: 2026-11-21

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4c2a1b8d7e1"
down_revision: Union[str, Sequence[str], None] = "e85bcc461ab0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACCOUNT_DELETION_DELETE_TABLES = (
    "activity",
    "lesson_progress",
    "mobile_money_payments",
    "payment_attempts",
    "payment_status_history",
    "payment_transactions",
    "playground_errors",
    "quiz_attempts",
    "quiz_results",
    "subscription_registrations",
    "subscriptions",
    "users",
)


def _runtime_role_name() -> str:
    import os

    role = os.getenv("KITE_RUNTIME_DB_ROLE", "kite_runtime").strip() or "kite_runtime"
    if role != "kite_runtime":
        raise RuntimeError("Account-deletion grants must target the kite_runtime role.")
    return role


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    schema = "public" if bind.dialect.name == "postgresql" else None
    table_names = set(inspector.get_table_names(schema=schema))
    if "account_deletions" not in table_names:
        op.create_table(
            "account_deletions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("provider_subject_hash", sa.String(length=64), nullable=True),
            sa.Column("provider_subject_ciphertext", sa.Text(), nullable=True),
            sa.Column("state", sa.String(length=24), nullable=False),
            sa.Column("provider_reconciled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("local_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    else:
        existing_columns = {
            column["name"]
            for column in inspector.get_columns("account_deletions", schema=schema)
        }
        if "provider_subject_ciphertext" not in existing_columns:
            op.add_column(
                "account_deletions",
                sa.Column("provider_subject_ciphertext", sa.Text(), nullable=True),
                schema=schema,
            )

    indexes = {
        index["name"]: index
        for index in inspector.get_indexes("account_deletions", schema=schema)
    }
    if indexes.get("ix_account_deletions_user_id", {}).get("unique"):
        op.drop_index(
            "ix_account_deletions_user_id",
            table_name="account_deletions",
            schema=schema,
        )
        indexes.pop("ix_account_deletions_user_id")
    if "ix_account_deletions_user_id" not in indexes:
        op.create_index(
            "ix_account_deletions_user_id",
            "account_deletions",
            ["user_id"],
            unique=False,
            schema=schema,
        )
    if "uq_account_deletions_active_user" not in indexes:
        op.create_index(
            "uq_account_deletions_active_user",
            "account_deletions",
            ["user_id"],
            unique=True,
            schema=schema,
            postgresql_where=sa.text("local_deleted = false"),
            sqlite_where=sa.text("local_deleted = false"),
        )

    if op.get_bind().dialect.name == "postgresql":
        role = _runtime_role_name()
        bind = op.get_bind()
        preparer = bind.dialect.identifier_preparer
        role_identifier = preparer.quote(role)
        client_roles = set(bind.execute(sa.text(
            "SELECT rolname FROM pg_roles "
            "WHERE rolname IN ('anon', 'authenticated', 'service_role')"
        )).scalars())
        revoke_target = ", ".join(
            ["PUBLIC"] + [preparer.quote(client_role) for client_role in sorted(client_roles)]
        )
        op.execute("ALTER TABLE public.account_deletions DISABLE ROW LEVEL SECURITY")
        op.execute(
            "REVOKE ALL PRIVILEGES ON TABLE public.account_deletions FROM "
            + revoke_target
        )
        op.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.account_deletions "
            "TO " + role_identifier
        )
        for table_name in ACCOUNT_DELETION_DELETE_TABLES:
            table_identifier = "public." + preparer.quote(table_name)
            op.execute(
                "GRANT DELETE ON TABLE " + table_identifier
                + " TO " + role_identifier
            )


def downgrade() -> None:
    bind = op.get_bind()
    schema = "public" if bind.dialect.name == "postgresql" else None
    if "account_deletions" in sa.inspect(bind).get_table_names(schema=schema):
        table = sa.Table(
            "account_deletions",
            sa.MetaData(),
            schema=schema,
            autoload_with=bind,
        )
        if bind.execute(sa.select(sa.func.count()).select_from(table)).scalar_one():
            raise RuntimeError(
                "Refusing to downgrade while account-deletion audit/outbox records exist."
            )

    if bind.dialect.name == "postgresql":
        role = _runtime_role_name()
        preparer = bind.dialect.identifier_preparer
        role_identifier = preparer.quote(role)
        op.execute(
            "REVOKE ALL PRIVILEGES ON TABLE public.account_deletions "
            "FROM " + role_identifier
        )
        for table_name in ACCOUNT_DELETION_DELETE_TABLES:
            table_identifier = "public." + preparer.quote(table_name)
            op.execute(
                "REVOKE DELETE ON TABLE " + table_identifier
                + " FROM " + role_identifier
            )
    op.drop_index(
        "ix_account_deletions_user_id",
        table_name="account_deletions",
        schema=schema,
    )
    op.drop_index(
        "uq_account_deletions_active_user",
        table_name="account_deletions",
        schema=schema,
    )
    op.drop_table("account_deletions", schema=schema)
