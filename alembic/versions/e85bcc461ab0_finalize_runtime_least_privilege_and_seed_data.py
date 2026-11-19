"""finalize runtime least privilege and seed reference data

Revision ID: e85bcc461ab0
Revises: 9c11c44d3729
Create Date: 2026-11-19 23:00:00.000000

"""
from __future__ import annotations

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from backend.curriculum import ADDITIONAL_LESSONS, FOUNDATION_CORE_LESSONS


# revision identifiers, used by Alembic.
revision: str = 'e85bcc461ab0'
down_revision: Union[str, Sequence[str], None] = '9c11c44d3729'
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

RUNTIME_TABLE_OPERATIONS: dict[str, tuple[str, ...]] = {
    "activity": ("SELECT", "INSERT"),
    "auth_sessions": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "guest_playground_executions": ("SELECT", "INSERT", "DELETE"),
    "lesson_progress": ("SELECT", "INSERT", "UPDATE"),
    "lessons": ("SELECT",),
    "mobile_money_payments": ("SELECT", "INSERT", "UPDATE"),
    "notifications": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "password_resets": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "payment_attempts": ("SELECT", "INSERT", "UPDATE"),
    "payment_provider_configurations": ("SELECT",),
    "payment_status_history": ("SELECT", "INSERT"),
    "payment_transactions": ("SELECT", "INSERT", "UPDATE"),
    "playground_errors": ("SELECT", "INSERT", "UPDATE"),
    "playground_executions": ("SELECT", "INSERT", "DELETE"),
    "quiz_attempts": ("SELECT", "INSERT"),
    "quiz_results": ("SELECT", "INSERT", "UPDATE"),
    "revoked_tokens": ("SELECT", "INSERT", "DELETE"),
    "subscription_plans": ("SELECT",),
    "subscription_registrations": ("SELECT", "INSERT", "UPDATE"),
    "subscriptions": ("SELECT", "INSERT", "UPDATE"),
    "users": ("SELECT", "INSERT", "UPDATE"),
}

SUBSCRIPTION_PLAN_DEFINITIONS = (
    {
        "id": "free",
        "name": "Kite Free",
        "display_price": "Free",
        "amount": "0.00",
        "currency": "ZMW",
        "period": None,
        "premium": False,
        "summary": "Start learning Python at your own pace.",
        "features": ["Core learning path", "Guest quizzes and challenges", "Progress saved on this device"],
        "sort_order": 0,
    },
    {
        "id": "premium_monthly",
        "name": "Kite Premium · Monthly",
        "display_price": "K19.99",
        "amount": "19.99",
        "currency": "ZMW",
        "period": "month",
        "premium": True,
        "summary": "Full access for 30 days, prepaid with no automatic renewal.",
        "features": ["All 12 learning modules", "Premium lessons, quizzes, and challenges", "30 days of access"],
        "sort_order": 1,
    },
    {
        "id": "premium_annual",
        "name": "Kite Premium · Annual",
        "display_price": "K99.99",
        "amount": "99.99",
        "currency": "ZMW",
        "period": "year",
        "premium": True,
        "summary": "Full access for 365 days, prepaid with no automatic renewal.",
        "features": ["All 12 learning modules", "Premium lessons, quizzes, and challenges", "365 days of access"],
        "sort_order": 2,
    },
)


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

    # 1. Seed reference subscription plans (idempotent, safe on live and fresh DBs)
    plan_table = sa.table(
        "subscription_plans",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("display_price", sa.String),
        sa.column("amount", sa.String),
        sa.column("currency", sa.String),
        sa.column("period", sa.String),
        sa.column("premium", sa.Boolean),
        sa.column("summary", sa.String),
        sa.column("features_json", sa.Text),
        sa.column("active", sa.Boolean),
        sa.column("sort_order", sa.Integer),
        sa.column("updated_at", sa.DateTime),
    )
    for plan in SUBSCRIPTION_PLAN_DEFINITIONS:
        bind.execute(sa.text(
            "INSERT INTO subscription_plans "
            "(id, name, display_price, amount, currency, period, premium, summary, features_json, active, sort_order, updated_at) "
            "VALUES (:id, :name, :display_price, :amount, :currency, :period, :premium, :summary, :features_json, :active, :sort_order, current_timestamp) "
            "ON CONFLICT (id) DO NOTHING"
        ), {
            "id": plan["id"],
            "name": plan["name"],
            "display_price": plan["display_price"],
            "amount": plan["amount"],
            "currency": plan["currency"],
            "period": plan["period"],
            "premium": plan["premium"],
            "summary": plan["summary"],
            "features_json": json.dumps(plan["features"]),
            "active": True,
            "sort_order": plan["sort_order"],
        })

    # 2. Seed 101 reference lessons (idempotent, safe on live and fresh DBs)
    all_lessons = list(FOUNDATION_CORE_LESSONS) + list(ADDITIONAL_LESSONS)
    for lesson_row in all_lessons:
        bind.execute(sa.text(
            "INSERT INTO lessons "
            "(id, title, description, content, order_num, premium, xp_reward) "
            "VALUES (:id, :title, :description, :content, :order_num, :premium, :xp_reward) "
            "ON CONFLICT (id) DO NOTHING"
        ), {
            "id": lesson_row[0],
            "title": lesson_row[1],
            "description": lesson_row[2],
            "content": lesson_row[3],
            "order_num": lesson_row[4],
            "premium": lesson_row[5],
            "xp_reward": lesson_row[6],
        })

    if bind.dialect.name != "postgresql":
        return

    # 3. PostgreSQL role security validation
    runtime_role = bind.execute(sa.text(
        "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolcanlogin "
        "FROM pg_roles WHERE rolname = 'kite_runtime'"
    )).mappings().one_or_none()
    if (
        runtime_role is None
        or runtime_role["rolsuper"]
        or runtime_role["rolbypassrls"]
        or runtime_role["rolcreatedb"]
        or runtime_role["rolcreaterole"]
        or not runtime_role["rolcanlogin"]
    ):
        raise RuntimeError(
            "Create kite_runtime with login, no superuser, no DDL, and no RLS-bypass privileges "
            "before applying this migration."
        )

    preparer = bind.dialect.identifier_preparer
    runtime_identifier = preparer.quote("kite_runtime")
    existing_roles = set(bind.execute(sa.text(
        "SELECT rolname FROM pg_roles "
        "WHERE rolname IN ('anon', 'authenticated', 'service_role')"
    )).scalars())
    client_roles = [preparer.quote(role) for role in sorted(existing_roles)]
    grantees = ["PUBLIC"] + client_roles
    all_roles_to_revoke = grantees + [runtime_identifier]
    revoke_target = ", ".join(all_roles_to_revoke)

    # 4. Schema usage and DDL restriction
    bind.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    bind.exec_driver_sql("GRANT USAGE ON SCHEMA public TO " + runtime_identifier)
    bind.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM " + runtime_identifier)

    # 5. Disable RLS and clean up legacy policies
    for table_name in APPLICATION_TABLES:
        table_identifier = "public." + preparer.quote(table_name)
        bind.exec_driver_sql("ALTER TABLE " + table_identifier + " DISABLE ROW LEVEL SECURITY")
        bind.exec_driver_sql("DROP POLICY IF EXISTS kite_" + table_name + "_user_context ON " + table_identifier)
        bind.exec_driver_sql("DROP POLICY IF EXISTS kite_" + table_name + "_public_read ON " + table_identifier)
        bind.exec_driver_sql("DROP POLICY IF EXISTS kite_" + table_name + "_service_write ON " + table_identifier)
        bind.exec_driver_sql("DROP POLICY IF EXISTS kite_" + table_name + "_service_only ON " + table_identifier)
    bind.exec_driver_sql(
        "DROP POLICY IF EXISTS kite_subscription_registrations_context ON public.subscription_registrations"
    )
    bind.exec_driver_sql(
        "DROP POLICY IF EXISTS kite_payment_transactions_context ON public.payment_transactions"
    )
    bind.exec_driver_sql(
        "DROP POLICY IF EXISTS kite_payment_attempts_context ON public.payment_attempts"
    )
    bind.exec_driver_sql(
        "DROP POLICY IF EXISTS kite_payment_status_history_context ON public.payment_status_history"
    )

    # 6. Apply least-privilege matrix to application tables
    for table_name in APPLICATION_TABLES:
        table_identifier = "public." + preparer.quote(table_name)
        bind.exec_driver_sql(
            "REVOKE ALL PRIVILEGES ON TABLE " + table_identifier
            + " FROM " + revoke_target
        )
        operations = ", ".join(RUNTIME_TABLE_OPERATIONS[table_name])
        bind.exec_driver_sql(
            "GRANT " + operations + " ON TABLE " + table_identifier
            + " TO " + runtime_identifier
        )

    # 7. Apply sequence privileges
    application_sequences = bind.execute(sa.text(
        "SELECT seq.relname FROM pg_class AS seq "
        "JOIN pg_namespace AS sequence_schema ON sequence_schema.oid = seq.relnamespace "
        "WHERE seq.relkind = 'S' AND sequence_schema.nspname = 'public'"
    )).scalars().all()
    for sequence_name in application_sequences:
        sequence_identifier = "public." + preparer.quote(sequence_name)
        bind.exec_driver_sql(
            "REVOKE ALL PRIVILEGES ON SEQUENCE " + sequence_identifier
            + " FROM " + revoke_target
        )
        bind.exec_driver_sql(
            "GRANT USAGE, SELECT ON SEQUENCE " + sequence_identifier
            + " TO " + runtime_identifier
        )

    # 8. Revoke Alembic metadata access from runtime and untrusted roles
    if "alembic_version" in inspector.get_table_names(schema="public"):
        bind.exec_driver_sql(
            "REVOKE ALL PRIVILEGES ON TABLE public.alembic_version FROM "
            + revoke_target
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    preparer = bind.dialect.identifier_preparer
    runtime_identifier = preparer.quote("kite_runtime")

    for table_name in APPLICATION_TABLES:
        table_identifier = "public." + preparer.quote(table_name)
        bind.exec_driver_sql("ALTER TABLE " + table_identifier + " ENABLE ROW LEVEL SECURITY")
        bind.exec_driver_sql(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE " + table_identifier
            + " TO " + runtime_identifier
        )
    if "alembic_version" in sa.inspect(bind).get_table_names(schema="public"):
        bind.exec_driver_sql(
            "GRANT SELECT ON TABLE public.alembic_version TO " + runtime_identifier
        )
