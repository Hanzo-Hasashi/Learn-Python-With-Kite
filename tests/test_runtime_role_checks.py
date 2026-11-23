import os
from types import SimpleNamespace
import unittest
from time import perf_counter
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

_ORIGINAL_ENVIRONMENT = os.environ.copy()
os.environ["KITE_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite://"

from sqlalchemy.exc import SQLAlchemyError

from backend.app import (
    app,
    database_engine_options,
    startup,
    validate_runtime_database_role,
)

os.environ.clear()
os.environ.update(_ORIGINAL_ENVIRONMENT)


class RuntimeRoleCheckTests(unittest.TestCase):
    @staticmethod
    def fake_connection(permission_rows=None, missing_sequences=None):
        role_result = MagicMock()
        role_result.mappings.return_value.one.return_value = {
            "role_name": "kite_runtime",
            "rolsuper": False,
            "rolbypassrls": False,
            "rolcreatedb": False,
            "rolcreaterole": False,
            "can_create_database": False,
            "can_create_schema": False,
        }
        elevated_result = MagicMock()
        elevated_result.mappings.return_value.one.return_value = {
            "has_role_memberships": False,
            "can_create_in_schema": False,
            "owns_application_objects": False,
        }
        permissions_result = MagicMock()
        permissions_result.mappings.return_value.all.return_value = permission_rows or []
        alembic_result = MagicMock()
        alembic_result.scalar_one.return_value = False
        sequences_result = MagicMock()
        sequences_result.scalars.return_value.all.return_value = missing_sequences or []

        connection = MagicMock()
        connection.execute.side_effect = [
            role_result,
            elevated_result,
            permissions_result,
            alembic_result,
            sequences_result,
        ]
        return connection

    def test_permission_validation_batches_catalog_queries(self):
        connection = self.fake_connection()
        engine = SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"),
            connect=MagicMock(),
        )
        engine.connect.return_value.__enter__.return_value = connection

        with patch("backend.app.engine", engine), patch(
            "backend.app.is_hosted_environment", return_value=True
        ):
            validate_runtime_database_role()

        self.assertEqual(connection.execute.call_count, 5)
        privilege_query = connection.execute.call_args_list[2]
        self.assertIn("unnest", str(privilege_query.args[0]))
        self.assertEqual(
            len(privilege_query.args[1]["table_names"]),
            len(privilege_query.args[1]["operations"]),
        )

    def test_missing_batched_table_permission_fails_startup_validation(self):
        connection = self.fake_connection([{
            "table_name": "account_deletions",
            "operation": "DELETE",
        }])
        engine = SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"),
            connect=MagicMock(),
        )
        engine.connect.return_value.__enter__.return_value = connection

        with patch("backend.app.engine", engine), patch(
            "backend.app.is_hosted_environment", return_value=True
        ):
            with self.assertRaisesRegex(RuntimeError, "DELETE for account_deletions"):
                validate_runtime_database_role()

        self.assertEqual(connection.execute.call_count, 3)

    def test_missing_sequence_permission_fails_startup_validation(self):
        connection = self.fake_connection(missing_sequences=["users_id_seq"])
        engine = SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"),
            connect=MagicMock(),
        )
        engine.connect.return_value.__enter__.return_value = connection

        with patch("backend.app.engine", engine), patch(
            "backend.app.is_hosted_environment", return_value=True
        ):
            with self.assertRaisesRegex(RuntimeError, "users_id_seq"):
                validate_runtime_database_role()

        self.assertEqual(connection.execute.call_count, 5)

    def test_liveness_endpoint_is_quick_and_does_not_open_a_database_connection(self):
        started = perf_counter()
        with patch("backend.app.engine.connect", side_effect=AssertionError("database access")):
            response = TestClient(app).get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertLess(perf_counter() - started, 1)

    def test_postgres_connection_timeout_is_bounded_for_render_and_vercel(self):
        render_options = database_engine_options(
            "postgresql+psycopg://user:pass@db.example.test/postgres?sslmode=require",
            serverless=False,
        )
        vercel_options = database_engine_options(
            "postgresql+psycopg://user:pass@db.example.test/postgres?sslmode=require",
            serverless=True,
        )
        self.assertEqual(render_options["connect_args"]["connect_timeout"], 5)
        self.assertEqual(vercel_options["connect_args"]["connect_timeout"], 5)
        self.assertIsNone(vercel_options["connect_args"]["prepare_threshold"])
        self.assertEqual(vercel_options["pool_size"], 1)
        self.assertEqual(vercel_options["max_overflow"], 0)

    def test_startup_reuses_one_connection_for_database_validation(self):
        connection = MagicMock()
        engine = SimpleNamespace(
            dialect=SimpleNamespace(name="sqlite"),
            connect=MagicMock(),
        )
        engine.connect.return_value.__enter__.return_value = connection

        with patch("backend.app.engine", engine), patch(
            "backend.app.validate_runtime_configuration"
        ), patch("backend.app.assert_database_migrations_applied") as migrations, patch(
            "backend.app.assert_database_schema_matches_models"
        ) as schema, patch(
            "backend.app.validate_runtime_database_role"
        ) as role, patch(
            "backend.app.start_account_deletion_reconciliation_worker"
        ), patch.dict(os.environ, {"KITE_SECRET_KEY": "test-key"}):
            startup()

        engine.connect.assert_called_once_with()
        migrations.assert_called_once_with(connection)
        schema.assert_called_once_with(connection)
        role.assert_called_once_with(connection)

    def test_readiness_failure_returns_503_without_logging_connection_details(self):
        session = MagicMock()
        session.execute.side_effect = SQLAlchemyError("postgresql://private-user:private-pass@db/private")
        with patch("backend.app.SessionLocal", return_value=session):
            with self.assertLogs("kite", level="WARNING") as captured:
                response = TestClient(app).get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "The service is temporarily unavailable. Please try again shortly.",
        )
        self.assertNotIn("private-user", "\n".join(captured.output))
        self.assertNotIn("private-pass", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
