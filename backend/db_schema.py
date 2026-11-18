from __future__ import annotations

from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Connection


SAFE_LEGACY_COLUMNS = {
    ("users", "auth_session_version"),
    ("notifications", "event_key"),
    ("notifications", "target_route"),
    ("quiz_attempts", "submission_id"),
    ("quiz_attempts", "request_hash"),
    ("quiz_attempts", "result_json"),
}
SAFE_LEGACY_INDEXES = {
    "uq_notifications_user_event",
    "uq_quiz_attempts_submission",
}


def include_object(
    obj: Any,
    name: str | None,
    object_type: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    if reflected and compare_to is None and object_type in {
        "table", "column", "index", "unique_constraint", "foreign_key_constraint",
    }:
        return False
    return True


def compare_schema(connection: Connection, metadata: Any) -> list[tuple[Any, ...]]:
    context = MigrationContext.configure(connection, opts={
        "compare_type": True,
        "include_object": include_object,
    })
    return compare_metadata(context, metadata)


def validate_existing_schema_baseline(connection: Connection, metadata: Any) -> list[tuple[Any, ...]]:
    schema = "public" if connection.dialect.name == "postgresql" else None
    existing_tables = set(inspect(connection).get_table_names(schema=schema))
    expected_tables = set(metadata.tables)
    present_tables = expected_tables.intersection(existing_tables)
    if not present_tables:
        return []
    missing_tables = expected_tables - present_tables
    if missing_tables:
        raise RuntimeError(
            "Existing database has an incomplete Kite schema; missing tables: "
            + ", ".join(sorted(missing_tables))
        )

    differences = compare_schema(connection, metadata)
    unsafe_differences = []
    for difference in differences:
        if difference[0] == "add_column" and (difference[2], difference[3].name) in SAFE_LEGACY_COLUMNS:
            continue
        if difference[0] == "add_index" and difference[1].name in SAFE_LEGACY_INDEXES:
            continue
        unsafe_differences.append(difference)
    if unsafe_differences:
        raise RuntimeError(
            "Existing database differs from the baseline in unsupported ways; "
            "do not stamp it. Schema differences: " + "; ".join(map(str, unsafe_differences))
        )
    return differences
