from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, inspect


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "preflight":
        print("Usage: python -m backend.migration_tools preflight", file=sys.stderr)
        return 2

    migration_url = os.getenv("MIGRATION_DATABASE_URL", "").strip()
    hosted = bool(
        os.getenv("RENDER") or os.getenv("VERCEL") or os.getenv("VERCEL_ENV")
        or os.getenv("KITE_ENV", "").strip().lower() == "production"
    )
    if hosted and not migration_url:
        print("Set MIGRATION_DATABASE_URL in this trusted migration process.", file=sys.stderr)
        return 2
    if migration_url:
        os.environ.setdefault("DATABASE_URL", migration_url)

    from backend.app import Base, database_engine_options, database_url, validate_postgres_ssl_url
    from backend.db_schema import validate_existing_schema_baseline

    url = migration_url or database_url()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if migration_url:
        validate_postgres_ssl_url(url, "MIGRATION_DATABASE_URL")
    engine = create_engine(url, **database_engine_options(url, serverless=False))
    try:
        with engine.connect() as connection:
            schema = "public" if connection.dialect.name == "postgresql" else None
            existing_tables = set(inspect(connection).get_table_names(schema=schema))
            if not set(Base.metadata.tables).intersection(existing_tables):
                print("No Kite application tables found; use 'alembic upgrade head'. Do not stamp a baseline.")
                return 0
            differences = validate_existing_schema_baseline(connection, Base.metadata)
        if not differences:
            print("Existing schema matches the Alembic baseline; it is safe to stamp the baseline revision.")
        else:
            print(
                "Only known additive compatibility columns/indexes are missing; "
                "the next security revision will add them without dropping data."
            )
            for difference in differences:
                print("  " + str(difference))
        return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
