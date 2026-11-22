import os

os.environ.setdefault("KITE_ENV", "development")
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from backend.app import Base, User
from backend.db_schema import validate_existing_schema_baseline


ROOT = Path(__file__).parent.parent
BASELINE_REVISION = "d9f9f9ebbced"
HEAD_REVISION = "f4c2a1b8d7e1"


def migration_environment(database_url: str) -> dict[str, str]:
    environment = os.environ.copy()
    for key in ("RENDER", "VERCEL", "VERCEL_ENV", "KITE_ENV", "MIGRATION_DATABASE_URL"):
        environment.pop(key, None)
    environment["DATABASE_URL"] = database_url
    environment["MIGRATION_DATABASE_URL"] = database_url
    return environment


def alembic_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


class MigrationTests(unittest.TestCase):
    def test_production_migrations_require_a_separate_owner_url(self):
        with patch.dict(os.environ, {
            "KITE_ENV": "production",
            "DATABASE_URL": (
                "postgresql+psycopg://kite_runtime:test-password@127.0.0.1:5432/"
                "kite?sslmode=require"
            ),
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MIGRATION_DATABASE_URL is required"):
                command.upgrade(alembic_config(), "head")

    def test_fresh_database_upgrade_is_repeatable_and_matches_models(self):
        with tempfile.TemporaryDirectory() as directory:
            database_url = "sqlite:///" + (Path(directory) / "fresh.db").as_posix()
            with patch.dict(os.environ, migration_environment(database_url), clear=True):
                config = alembic_config()
                command.upgrade(config, "head")
                command.upgrade(config, "head")
                command.check(config)

            engine = create_engine(database_url)
            try:
                tables = set(inspect(engine).get_table_names())
                self.assertTrue(set(Base.metadata.tables).issubset(tables))
                with engine.connect() as connection:
                    revision = connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                self.assertEqual(revision, HEAD_REVISION)
            finally:
                engine.dispose()

    def test_account_deletion_migration_downgrades_and_reupgrades_on_isolated_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database_url = "sqlite:///" + (Path(directory) / "upgrade-downgrade.db").as_posix()
            with patch.dict(os.environ, migration_environment(database_url), clear=True):
                config = alembic_config()
                command.upgrade(config, "head")
                command.downgrade(config, "e85bcc461ab0")

                engine = create_engine(database_url)
                try:
                    self.assertNotIn("account_deletions", inspect(engine).get_table_names())
                    with engine.connect() as connection:
                        revision = connection.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalar_one()
                    self.assertEqual(revision, "e85bcc461ab0")
                finally:
                    engine.dispose()

                command.upgrade(config, "head")
                command.check(config)

    def test_account_deletion_downgrade_refuses_to_drop_persisted_audit_records(self):
        with tempfile.TemporaryDirectory() as directory:
            database_url = "sqlite:///" + (Path(directory) / "preserve-audit.db").as_posix()
            with patch.dict(os.environ, migration_environment(database_url), clear=True):
                config = alembic_config()
                command.upgrade(config, "head")
                engine = create_engine(database_url)
                try:
                    with engine.begin() as connection:
                        connection.execute(text(
                            "INSERT INTO account_deletions "
                            "(id, user_id, state, provider_reconciled, local_deleted, created_at, updated_at) "
                            "VALUES ('audit-preserved', 42, 'COMPLETED', 1, 1, "
                            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                        ))

                    with self.assertRaisesRegex(RuntimeError, "audit/outbox records exist"):
                        command.downgrade(config, "e85bcc461ab0")

                    with engine.connect() as connection:
                        audit_count = connection.execute(
                            text("SELECT count(*) FROM account_deletions")
                        ).scalar_one()
                        revision = connection.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalar_one()
                    self.assertEqual(audit_count, 1)
                    self.assertEqual(revision, HEAD_REVISION)

                    with engine.begin() as connection:
                        connection.execute(text("DELETE FROM account_deletions"))
                    command.downgrade(config, "e85bcc461ab0")
                finally:
                    engine.dispose()

    def test_existing_schema_can_be_stamped_without_losing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            database_url = "sqlite:///" + (Path(directory) / "existing.db").as_posix()
            engine = create_engine(database_url)
            try:
                Base.metadata.create_all(engine)
                with Session(engine) as session:
                    session.add(User(
                        email="existing@example.test",
                        name="Existing",
                        password_hash="hash",
                    ))
                    session.commit()

                with patch.dict(os.environ, migration_environment(database_url), clear=True):
                    config = alembic_config()
                    command.stamp(config, BASELINE_REVISION)
                    command.upgrade(config, "head")
                    command.check(config)

                with engine.connect() as connection:
                    email = connection.execute(
                        text("SELECT email FROM users WHERE id = 1")
                    ).scalar_one()
                    revision = connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                self.assertEqual(email, "existing@example.test")
                self.assertEqual(revision, HEAD_REVISION)
            finally:
                engine.dispose()

    def test_existing_legacy_schema_gets_only_known_additive_repairs(self):
        with tempfile.TemporaryDirectory() as directory:
            database_url = "sqlite:///" + (Path(directory) / "legacy.db").as_posix()
            engine = create_engine(database_url)
            try:
                Base.metadata.create_all(engine)
                with Session(engine) as session:
                    session.add(User(
                        email="legacy@example.test",
                        name="Legacy",
                        password_hash="hash",
                    ))
                    session.commit()
                with engine.begin() as connection:
                    connection.execute(text("DROP INDEX uq_notifications_user_event"))
                    connection.execute(text("DROP INDEX uq_quiz_attempts_submission"))
                    connection.execute(text("ALTER TABLE users DROP COLUMN auth_session_version"))
                    connection.execute(text("ALTER TABLE notifications DROP COLUMN event_key"))
                    connection.execute(text("ALTER TABLE notifications DROP COLUMN target_route"))
                    connection.execute(text("ALTER TABLE quiz_attempts DROP COLUMN submission_id"))
                    connection.execute(text("ALTER TABLE quiz_attempts DROP COLUMN request_hash"))
                    connection.execute(text("ALTER TABLE quiz_attempts DROP COLUMN result_json"))

                with patch.dict(os.environ, migration_environment(database_url), clear=True):
                    with engine.connect() as connection:
                        differences = validate_existing_schema_baseline(connection, Base.metadata)
                    self.assertEqual(len(differences), 8)
                    config = alembic_config()
                    command.stamp(config, BASELINE_REVISION)
                    command.upgrade(config, "head")
                    command.check(config)

                with Session(engine) as session:
                    user = session.query(User).filter_by(email="legacy@example.test").one()
                    self.assertEqual(user.name, "Legacy")
                    self.assertEqual(user.auth_session_version, 0)
                columns = {column["name"] for column in inspect(engine).get_columns("quiz_attempts")}
                self.assertTrue({"submission_id", "request_hash", "result_json"}.issubset(columns))
            finally:
                engine.dispose()

    def test_partial_existing_schema_is_refused_without_creating_application_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            database_url = "sqlite:///" + (Path(directory) / "partial.db").as_posix()
            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))

                with patch.dict(os.environ, migration_environment(database_url), clear=True):
                    with self.assertRaisesRegex(RuntimeError, "Application tables already exist"):
                        command.upgrade(alembic_config(), "head")

                tables = set(inspect(engine).get_table_names())
                self.assertEqual(tables.intersection(Base.metadata.tables), {"users"})
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
