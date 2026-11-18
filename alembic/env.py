import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context
from backend.db_schema import include_object

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

hosted = bool(
    os.getenv("RENDER") or os.getenv("VERCEL") or os.getenv("VERCEL_ENV")
    or os.getenv("KITE_ENV", "").strip().lower() == "production"
)
migration_url = os.getenv("MIGRATION_DATABASE_URL", "").strip()
if hosted and not migration_url:
    raise RuntimeError(
        "MIGRATION_DATABASE_URL is required for hosted migration commands. "
        "Run migrations in a separate trusted process, not from the web service."
    )
if migration_url:
    os.environ.setdefault("DATABASE_URL", migration_url)

from backend.app import Base, database_url, validate_postgres_ssl_url

target_metadata = Base.metadata
url = migration_url or database_url()
if url.startswith("postgres://"):
    url = "postgresql+psycopg://" + url[len("postgres://"):]
elif url.startswith("postgresql://"):
    url = "postgresql+psycopg://" + url[len("postgresql://"):]
if migration_url:
    validate_postgres_ssl_url(url, "MIGRATION_DATABASE_URL")
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
