from __future__ import annotations

import hashlib
import os
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parent.parent
BASELINE_REVISION = "d9f9f9ebbced"
HEAD_REVISION = "9c11c44d3729"
TEST_DATABASE = "kite_integration_test"
TEST_PORT = 55432
SAFETY_MARKER = "KITE_DISPOSABLE_POSTGRES_TEST_ONLY"


def validated_test_urls() -> tuple[str, str, str]:
    if os.getenv("KITE_POSTGRES_INTEGRATION_TESTS") != "1":
        raise RuntimeError("Run these tests only through tests\\run_postgres_integration.ps1.")
    if os.getenv("KITE_TEST_SAFETY_MARKER") != SAFETY_MARKER:
        raise RuntimeError("The explicit PostgreSQL integration-test safety marker is missing.")
    if (
        any(os.getenv(name) for name in ("RENDER", "VERCEL", "VERCEL_ENV"))
        or os.getenv("KITE_ENV", "").strip().lower() == "production"
    ):
        raise RuntimeError("Hosted environment variables are forbidden for PostgreSQL integration tests.")
    owner_url = os.getenv("KITE_TEST_DATABASE_URL", "")
    runtime_url = os.getenv("KITE_TEST_RUNTIME_DATABASE_URL", "")
    if not owner_url or not runtime_url:
        raise RuntimeError("Explicit owner and runtime test database URLs are required.")

    urls = (make_url(owner_url), make_url(runtime_url))
    expected_users = ("kite_test_owner", "kite_runtime")
    for url, expected_user in zip(urls, expected_users):
        if (
            url.drivername != "postgresql+psycopg"
            or url.host != "127.0.0.1"
            or url.port != TEST_PORT
            or url.database != TEST_DATABASE
            or url.username != expected_user
            or url.query.get("sslmode") != "disable"
        ):
            raise RuntimeError(
                "Integration tests may connect only to the localhost Compose database "
                "kite_integration_test on port 55432."
            )
    if os.getenv("DATABASE_URL") != runtime_url:
        raise RuntimeError("DATABASE_URL must be the restricted runtime test URL.")
    if os.getenv("MIGRATION_DATABASE_URL") != owner_url:
        raise RuntimeError("MIGRATION_DATABASE_URL must be the test-only owner URL.")
    if make_url(runtime_url).password != os.getenv("KITE_TEST_RUNTIME_PASSWORD"):
        raise RuntimeError("The runtime test password does not match the test-only URL.")
    return owner_url, runtime_url, os.getenv("KITE_TEST_RUNTIME_PASSWORD", "")


class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.owner_url, cls.runtime_url, cls.runtime_password = validated_test_urls()

        from backend.app import (
            Activity,
            Base,
            Lesson,
            LessonProgress,
            MobileMoneyPayment,
            Notice,
            PlaygroundExecution,
            QuizAttempt,
            QuizResult,
            SessionLocal,
            Subscription,
            SubscriptionPlan,
            SubscriptionRegistration,
            User,
            app,
            engine,
            issue_token,
            validate_runtime_database_role,
        )
        from backend.db_schema import validate_existing_schema_baseline

        cls.Activity = Activity
        cls.Base = Base
        cls.Lesson = Lesson
        cls.LessonProgress = LessonProgress
        cls.MobileMoneyPayment = MobileMoneyPayment
        cls.Notice = Notice
        cls.PlaygroundExecution = PlaygroundExecution
        cls.QuizAttempt = QuizAttempt
        cls.QuizResult = QuizResult
        cls.SessionLocal = SessionLocal
        cls.Subscription = Subscription
        cls.SubscriptionPlan = SubscriptionPlan
        cls.SubscriptionRegistration = SubscriptionRegistration
        cls.User = User
        cls.app = app
        cls.runtime_engine = engine
        cls.issue_token = staticmethod(issue_token)
        cls.validate_runtime_database_role = staticmethod(validate_runtime_database_role)
        cls.validate_existing_schema_baseline = staticmethod(validate_existing_schema_baseline)
        cls.owner_engine = create_engine(cls.owner_url, pool_pre_ping=True)
        cls._ensure_test_runtime_role()

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "owner_engine"):
            cls.owner_engine.dispose()
        if hasattr(cls, "runtime_engine"):
            cls.runtime_engine.dispose()

    @classmethod
    def _ensure_test_runtime_role(cls) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,}", cls.runtime_password):
            raise RuntimeError("The test-only runtime password must be a non-empty safe test value.")
        with cls.owner_engine.begin() as connection:
            database = connection.execute(text(
                "SELECT current_database() AS database_name, current_user AS role_name, "
                "roles.rolsuper AS is_superuser "
                "FROM pg_roles AS roles WHERE roles.rolname = current_user"
            )).mappings().one()
            if (
                database["database_name"] != TEST_DATABASE
                or database["role_name"] != "kite_test_owner"
                or not database["is_superuser"]
            ):
                raise RuntimeError("Destructive setup requires the test-only Compose owner database.")

            existing = connection.execute(text(
                "SELECT 1 FROM pg_roles WHERE rolname = 'kite_runtime'"
            )).first()
            password_sql = "'" + cls.runtime_password + "'"
            if existing is None:
                connection.exec_driver_sql(
                    "CREATE ROLE kite_runtime WITH LOGIN NOINHERIT NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS PASSWORD "
                    + password_sql
                )
            else:
                connection.exec_driver_sql(
                    "ALTER ROLE kite_runtime WITH LOGIN NOINHERIT NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS PASSWORD "
                    + password_sql
                )
            connection.exec_driver_sql(
                "GRANT CONNECT ON DATABASE kite_integration_test TO kite_runtime"
            )

    @classmethod
    def _reset_public_schema(cls) -> None:
        with cls.owner_engine.begin() as connection:
            database = connection.execute(text(
                "SELECT current_database(), current_user"
            )).one()
            if database != (TEST_DATABASE, "kite_test_owner"):
                raise RuntimeError("Refusing to reset a database outside the explicit test target.")
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
            connection.exec_driver_sql(
                "CREATE SCHEMA public AUTHORIZATION kite_test_owner"
            )

    @classmethod
    def _alembic_config(cls) -> Config:
        return Config(str(ROOT / "alembic.ini"))

    def test_00_postgresql_server_version_matches_the_pinned_compose_image(self):
        with self.owner_engine.connect() as connection:
            version = connection.execute(text(
                "SELECT current_setting('server_version')"
            )).scalar_one()
            version_number = connection.execute(text(
                "SELECT current_setting('server_version_num')::integer"
            )).scalar_one()
        self.assertRegex(version, r"^17\.12(?:\s|$)")
        self.assertEqual(version_number, 170012)

    def test_01_fresh_migration_creates_the_complete_postgresql_schema(self):
        self._reset_public_schema()
        command.upgrade(self._alembic_config(), "head")
        command.check(self._alembic_config())

        tables = set(inspect(self.owner_engine).get_table_names(schema="public"))
        self.assertTrue(set(self.Base.metadata.tables).issubset(tables))
        with self.owner_engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM public.alembic_version")
            ).scalar_one()
        self.assertEqual(revision, HEAD_REVISION)

    def test_02_repeated_migration_is_idempotent(self):
        command.upgrade(self._alembic_config(), "head")
        command.upgrade(self._alembic_config(), "head")
        command.check(self._alembic_config())
        with self.owner_engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    text("SELECT version_num FROM public.alembic_version")
                ).scalar_one(),
                HEAD_REVISION,
            )

    def test_03_existing_schema_adoption_preserves_data_and_repairs_only_known_fields(self):
        self._reset_public_schema()
        self.Base.metadata.create_all(self.owner_engine)
        with Session(self.owner_engine) as session:
            session.add(self.User(
                email="preserved@example.test",
                name="Preserved",
                password_hash="test-hash",
            ))
            session.commit()

        with self.owner_engine.begin() as connection:
            connection.exec_driver_sql("DROP INDEX public.uq_notifications_user_event")
            connection.exec_driver_sql("DROP INDEX public.uq_quiz_attempts_submission")
            for table, column in (
                ("users", "auth_session_version"),
                ("notifications", "event_key"),
                ("notifications", "target_route"),
                ("quiz_attempts", "submission_id"),
                ("quiz_attempts", "request_hash"),
                ("quiz_attempts", "result_json"),
            ):
                connection.exec_driver_sql(
                    f"ALTER TABLE public.{table} DROP COLUMN {column}"
                )

        with self.owner_engine.connect() as connection:
            differences = self.validate_existing_schema_baseline(
                connection, self.Base.metadata,
            )
        self.assertTrue(differences)
        command.stamp(self._alembic_config(), BASELINE_REVISION)
        command.upgrade(self._alembic_config(), "head")
        command.check(self._alembic_config())

        with Session(self.owner_engine) as session:
            user = session.query(self.User).filter_by(email="preserved@example.test").one()
            self.assertEqual(user.name, "Preserved")
            self.assertEqual(user.auth_session_version, 0)
        columns = {
            column["name"]
            for column in inspect(self.owner_engine).get_columns(
                "quiz_attempts", schema="public",
            )
        }
        self.assertTrue({"submission_id", "request_hash", "result_json"}.issubset(columns))

    def test_04_runtime_role_has_required_access_and_cannot_change_schema(self):
        with self.runtime_engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("SELECT current_user")).scalar_one(),
                "kite_runtime",
            )
            self.assertEqual(
                connection.execute(
                    text("SELECT version_num FROM public.alembic_version")
                ).scalar_one(),
                HEAD_REVISION,
            )
            for table in self.Base.metadata.sorted_tables:
                has_dml = connection.execute(text(
                    "SELECT has_table_privilege(current_user, :table_name, 'SELECT') "
                    "AND has_table_privilege(current_user, :table_name, 'INSERT') "
                    "AND has_table_privilege(current_user, :table_name, 'UPDATE') "
                    "AND has_table_privilege(current_user, :table_name, 'DELETE')"
                ), {"table_name": "public." + table.name}).scalar_one()
                self.assertTrue(has_dml, table.name)
            sequences = connection.execute(text(
                "SELECT sequence.relname FROM pg_class AS sequence "
                "JOIN pg_namespace AS namespace ON namespace.oid = sequence.relnamespace "
                "JOIN pg_depend AS dependency ON dependency.objid = sequence.oid "
                "AND dependency.deptype IN ('a', 'i') "
                "JOIN pg_class AS application_table ON application_table.oid = dependency.refobjid "
                "WHERE sequence.relkind = 'S' AND namespace.nspname = 'public' "
                "AND application_table.relname = ANY(:table_names)"
            ), {"table_names": list(self.Base.metadata.tables)}).scalars().all()
            self.assertTrue(sequences)
            for sequence_name in sequences:
                allowed = connection.execute(text(
                    "SELECT has_sequence_privilege(current_user, :sequence_name, 'USAGE') "
                    "AND has_sequence_privilege(current_user, :sequence_name, 'SELECT')"
                ), {"sequence_name": "public." + sequence_name}).scalar_one()
                self.assertTrue(allowed, sequence_name)
            role = connection.execute(text(
                "SELECT rolbypassrls, rolsuper, rolcreatedb, rolcreaterole "
                "FROM pg_roles WHERE rolname = current_user"
            )).one()
            self.assertEqual(role, (True, False, False, False))
            self.assertFalse(connection.execute(text(
                "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
            )).scalar_one())
            self.assertFalse(connection.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_auth_members "
                "WHERE member = (SELECT oid FROM pg_roles WHERE rolname = current_user))"
            )).scalar_one())
            self.assertFalse(connection.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_class WHERE relnamespace = "
                "'public'::regnamespace AND relowner = "
                "(SELECT oid FROM pg_roles WHERE rolname = current_user))"
            )).scalar_one())
            rls_tables = connection.execute(text(
                "SELECT relname FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace "
                "AND relname = ANY(:table_names) AND relkind = 'r' "
                "AND relrowsecurity"
            ), {"table_names": list(self.Base.metadata.tables)}).scalars().all()
            self.assertEqual(set(rls_tables), set(self.Base.metadata.tables))
            self.assertFalse(connection.execute(text(
                "SELECT row_security_active('public.users'::regclass)"
            )).scalar_one())

        with self.SessionLocal() as session:
            user = self.User(
                email="runtime-write@example.test",
                name="Runtime Write",
                password_hash="test-hash",
            )
            session.add(user)
            session.flush()
            session.add(self.Activity(
                user_id=user.id,
                kind="integration",
                item_id=uuid.uuid4().hex,
                xp=1,
            ))
            session.commit()
            self.assertIsNotNone(user.id)

        forbidden_ddl = (
            "CREATE TABLE public.integration_forbidden (id integer)",
            "ALTER TABLE public.users ADD COLUMN integration_forbidden integer",
            "DROP TABLE public.users",
        )
        for statement in forbidden_ddl:
            with self.subTest(statement=statement):
                with self.assertRaises(DBAPIError):
                    with self.runtime_engine.begin() as connection:
                        connection.exec_driver_sql(statement)

    def test_05_fastapi_startup_runs_with_the_restricted_runtime_role(self):
        from fastapi.testclient import TestClient

        with TestClient(self.app) as client:
            response = client.get("/health")
            self.assertEqual(response.status_code, 200, response.text)
        self.validate_runtime_database_role()
        with self.runtime_engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("SELECT current_user")).scalar_one(),
                "kite_runtime",
            )

    def test_06_api_user_data_isolation_for_progress_quiz_notifications_subscriptions_and_executions(self):
        from fastapi.testclient import TestClient

        with TestClient(self.app) as client:
            owner_headers, owner_id = self._register_user(client, "Owner")
            other_headers, other_id = self._register_user(client, "Other")
            now = datetime.now(timezone.utc)
            registration_key = "registration-capability-" + uuid.uuid4().hex
            registration_id = str(uuid.uuid4())
            execution_id = uuid.uuid4().hex
            payment_id = str(uuid.uuid4())
            with self.SessionLocal() as session:
                plan = session.query(self.SubscriptionPlan).filter_by(
                    period="month",
                ).first()
                self.assertIsNotNone(plan)
                session.add_all([
                    self.LessonProgress(
                        user_id=owner_id,
                        lesson_id="python-variables",
                        completed=True,
                        score=100,
                    ),
                    self.QuizResult(
                        user_id=owner_id,
                        quiz_id="python-basics",
                        best_score=10,
                        total_questions=10,
                    ),
                    self.QuizAttempt(
                        user_id=owner_id,
                        quiz_id="python-basics",
                        score=10,
                        total_questions=10,
                        percentage=100,
                        passed=True,
                        feedback_json="[]",
                        result_json="{}",
                    ),
                    self.Notice(
                        user_id=owner_id,
                        title="Private integration notification",
                        body="Owner-only notification",
                        category="updates",
                    ),
                    self.Subscription(
                        user_id=owner_id,
                        product_id="kite_premium_monthly",
                        token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                        token_ciphertext="test-only",
                        state="SUBSCRIPTION_STATE_ACTIVE",
                        expires_at=now + timedelta(days=30),
                        auto_renew=False,
                    ),
                    self.SubscriptionRegistration(
                        id=registration_id,
                        user_id=owner_id,
                        plan_id=plan.id,
                        idempotency_key=str(uuid.uuid4()),
                        request_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                        access_key_hash=hashlib.sha256(registration_key.encode()).hexdigest(),
                        phone_ciphertext="test-only",
                        phone_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                        phone_last4="1234",
                        status="PENDING_PAYMENT",
                    ),
                    self.MobileMoneyPayment(
                        id=payment_id,
                        user_id=owner_id,
                        idempotency_key=str(uuid.uuid4()),
                        request_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                        plan_id=plan.id,
                        amount=plan.amount,
                        provider="test",
                        status="FAILED",
                    ),
                    self.PlaygroundExecution(
                        execution_id=execution_id,
                        user_id=owner_id,
                    ),
                ])
                session.commit()

            with self.runtime_engine.connect() as connection:
                visible_test_users = connection.execute(text(
                    "SELECT count(*) FROM public.users WHERE id IN (:owner_id, :other_id)"
                ), {"owner_id": owner_id, "other_id": other_id}).scalar_one()
                self.assertEqual(visible_test_users, 2)

            owner_progress = client.get("/api/progress", headers=owner_headers)
            other_progress = client.get("/api/progress", headers=other_headers)
            self.assertGreater(owner_progress.json()["completion_percent"], 0)
            self.assertEqual(other_progress.json()["completion_percent"], 0)

            owner_quiz = next(
                item for item in client.get("/api/quizzes", headers=owner_headers).json()
                if item["id"] == "python-basics"
            )
            other_quiz = next(
                item for item in client.get("/api/quizzes", headers=other_headers).json()
                if item["id"] == "python-basics"
            )
            self.assertEqual(owner_quiz["attempt_count"], 1)
            self.assertEqual(other_quiz["attempt_count"], 0)
            self.assertEqual(other_quiz["attempts"], [])

            owner_notifications = client.get(
                "/api/notifications", headers=owner_headers,
            ).json()
            other_notifications = client.get(
                "/api/notifications", headers=other_headers,
            ).json()
            private_notice = next(
                item for item in owner_notifications
                if item["title"] == "Private integration notification"
            )
            self.assertFalse(any(
                item["title"] == private_notice["title"] for item in other_notifications
            ))
            self.assertEqual(
                client.post(
                    f"/api/notifications/{private_notice['id']}/read",
                    headers=other_headers,
                ).status_code,
                404,
            )

            self.assertTrue(
                client.get("/api/subscription", headers=owner_headers).json()["premium"]
            )
            self.assertFalse(
                client.get("/api/subscription", headers=other_headers).json()["premium"]
            )
            registration_url = f"/api/subscription/registrations/{registration_id}"
            self.assertEqual(
                client.get(registration_url, headers=other_headers).status_code,
                404,
            )
            self.assertEqual(
                client.get(registration_url, headers=owner_headers).status_code,
                200,
            )
            payment_url = f"/api/subscription/mobile-money/{payment_id}"
            self.assertEqual(
                client.get(payment_url, headers=other_headers).status_code,
                404,
            )
            self.assertEqual(
                client.get(payment_url, headers=owner_headers).status_code,
                200,
            )

            with patch(
                "backend.app.cancel_runner_execution", new_callable=AsyncMock,
            ) as cancel:
                cancel.return_value = {"cancelled": True}
                denied = client.post(
                    "/api/execution/cancel",
                    headers=other_headers,
                    json={"execution_id": execution_id},
                )
                self.assertEqual(denied.status_code, 404)
                cancel.assert_not_awaited()
                allowed = client.post(
                    "/api/execution/cancel",
                    headers=owner_headers,
                    json={"execution_id": execution_id},
                )
                self.assertEqual(allowed.status_code, 200, allowed.text)
                cancel.assert_awaited_once_with(execution_id)

    def _register_user(self, client, label: str) -> tuple[dict[str, str], int]:
        email = f"postgres-{label.lower()}-{uuid.uuid4().hex}@example.com"
        response = client.post("/api/auth/register", json={
            "name": label,
            "email": email,
            "password": "secure-example-password",
        })
        self.assertEqual(response.status_code, 201, response.text)
        user_id = response.json()["user"]["id"]
        with self.SessionLocal() as session:
            user = session.get(self.User, user_id)
            self.assertIsNotNone(user)
            token = self.issue_token(user, False)[0]
        client.cookies.clear()
        return {"Authorization": "Bearer " + token}, user_id


if __name__ == "__main__":
    unittest.main(verbosity=2)
