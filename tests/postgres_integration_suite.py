from __future__ import annotations

import hashlib
import http.server
import os
import re
import subprocess
import sys
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parent.parent
BASELINE_REVISION = "d9f9f9ebbced"
HEAD_REVISION = "f4c2a1b8d7e1"
TEST_DATABASE = "kite_integration_test"
TEST_PORT = 55432
SAFETY_MARKER = "KITE_DISPOSABLE_POSTGRES_TEST_ONLY"
RECONCILE_WORKER_CODE = (
    "from backend.app import reconcile_pending_account_deletions; "
    "reconcile_pending_account_deletions()"
)


def start_fake_supabase(block_first_request: bool = False):
    state = {"request_count": 0}
    request_lock = threading.Lock()
    request_started = threading.Event()
    release_first_request = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_DELETE(self):
            with request_lock:
                state["request_count"] += 1
                request_number = state["request_count"]
            request_started.set()
            if block_first_request and request_number == 1:
                if not release_first_request.wait(timeout=15):
                    self.send_error(504)
                    return
            self.send_response(204 if request_number == 1 else 404)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread, state, request_started, release_first_request, base_url


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
            AccountDeletion,
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
            encrypt_account_deletion_subject,
            issue_token,
            reconcile_pending_local_account_deletions,
            validate_runtime_database_role,
            RUNTIME_TABLE_OPERATIONS,
        )
        from backend.db_schema import validate_existing_schema_baseline

        cls.RUNTIME_TABLE_OPERATIONS = RUNTIME_TABLE_OPERATIONS

        cls.Activity = Activity
        cls.AccountDeletion = AccountDeletion
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
        cls.encrypt_account_deletion_subject = staticmethod(
            encrypt_account_deletion_subject,
        )
        cls.reconcile_pending_local_account_deletions = staticmethod(
            reconcile_pending_local_account_deletions,
        )
        cls.validate_runtime_database_role = staticmethod(validate_runtime_database_role)
        cls.validate_existing_schema_baseline = staticmethod(validate_existing_schema_baseline)
        cls.owner_engine = create_engine(cls.owner_url, pool_pre_ping=True)
        cls._ensure_test_runtime_role()
        cls._ensure_default_acl_test_roles()

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
                    "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD "
                    + password_sql
                )
            else:
                connection.exec_driver_sql(
                    "ALTER ROLE kite_runtime WITH LOGIN NOINHERIT NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD "
                    + password_sql
                )
            connection.exec_driver_sql(
                "GRANT CONNECT ON DATABASE kite_integration_test TO kite_runtime"
            )

    @classmethod
    def _ensure_default_acl_test_roles(cls) -> None:
        with cls.owner_engine.begin() as connection:
            for role_name in ("anon", "authenticated", "supabase_admin"):
                exists = connection.execute(text(
                    "SELECT 1 FROM pg_roles WHERE rolname = :role_name"
                ), {"role_name": role_name}).first()
                if exists is None:
                    connection.exec_driver_sql(f'CREATE ROLE "{role_name}"')

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

    def _provider_environment(self, base_url: str) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update({
            "SUPABASE_URL": base_url,
            "SUPABASE_ANON_KEY": "postgres-integration-anon-key",
            "SUPABASE_SERVICE_ROLE_KEY": "postgres-integration-service-role-key",
        })
        return environment

    def _run_reconciliation_process(self, environment: dict[str, str]):
        return subprocess.run(
            [sys.executable, "-c", RECONCILE_WORKER_CODE],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
        )

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
            # Runtime role must have NO access to Alembic metadata
            self.assertFalse(connection.execute(text(
                "SELECT has_table_privilege(current_user, 'public.alembic_version', 'SELECT')"
            )).scalar_one())
            with self.assertRaises(DBAPIError):
                with self.runtime_engine.connect() as metadata_conn:
                    metadata_conn.execute(text("SELECT * FROM public.alembic_version"))

            for table in self.Base.metadata.sorted_tables:
                required = self.RUNTIME_TABLE_OPERATIONS[table.name]
                for operation in required:
                    self.assertTrue(connection.execute(text(
                        "SELECT has_table_privilege(current_user, :table_name, :operation)"
                    ), {"table_name": "public." + table.name, "operation": operation}).scalar_one(),
                    (table.name, operation))
                for disallowed in ("INSERT", "UPDATE", "DELETE"):
                    if disallowed not in required:
                        self.assertFalse(connection.execute(text(
                            "SELECT has_table_privilege(current_user, :table_name, :operation)"
                        ), {"table_name": "public." + table.name, "operation": disallowed}).scalar_one(),
                        (table.name, disallowed))
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
            self.assertEqual(role, (False, False, False, False))
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
            self.assertEqual(set(rls_tables), set())

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
            self.assertEqual(owner_progress.status_code, 200, owner_progress.text)
            self.assertEqual(other_progress.status_code, 200, other_progress.text)
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

    def test_08_partial_unique_index_allows_only_one_active_deletion_per_user(self):
        first_id = uuid.uuid4().hex
        second_id = uuid.uuid4().hex
        with self.SessionLocal() as session:
            user = self.User(
                email=f"postgres-deletion-index-{uuid.uuid4().hex}@example.test",
                name="Deletion Index",
                password_hash="test-hash",
            )
            session.add(user)
            session.flush()
            user_id = user.id
            session.add(self.AccountDeletion(
                id=first_id,
                user_id=user_id,
                state="PENDING_LOCAL",
                provider_reconciled=True,
                local_deleted=False,
            ))
            session.commit()

        with self.SessionLocal() as session:
            session.add(self.AccountDeletion(
                id=second_id,
                user_id=user_id,
                state="PENDING_LOCAL",
                provider_reconciled=True,
                local_deleted=False,
            ))
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()

        with self.SessionLocal() as session:
            completed = session.get(self.AccountDeletion, first_id)
            completed.state = "COMPLETED"
            completed.local_deleted = True
            session.commit()
            session.add(self.AccountDeletion(
                id=second_id,
                user_id=user_id,
                state="PENDING_LOCAL",
                provider_reconciled=True,
                local_deleted=False,
            ))
            session.commit()
            active_count = session.query(self.AccountDeletion).filter_by(
                user_id=user_id,
                local_deleted=False,
            ).count()
            completed_replacement = session.get(self.AccountDeletion, second_id)
            completed_replacement.state = "COMPLETED"
            completed_replacement.local_deleted = True
            session.commit()
        self.assertEqual(active_count, 1)

    def test_09_for_update_blocks_a_second_independent_database_session(self):
        deletion_id = uuid.uuid4().hex
        with self.SessionLocal() as session:
            user = self.User(
                email=f"postgres-row-lock-{uuid.uuid4().hex}@example.test",
                name="Row Lock",
                password_hash="test-hash",
            )
            session.add(user)
            session.flush()
            session.add(self.AccountDeletion(
                id=deletion_id,
                user_id=user.id,
                state="PENDING_LOCAL",
                provider_reconciled=True,
                local_deleted=False,
            ))
            session.commit()

        second_started = threading.Event()
        second_finished = threading.Event()
        second_backend_pid = []
        second_errors = []
        first_session = self.SessionLocal()
        contender = None
        try:
            row = first_session.scalar(
                select(self.AccountDeletion)
                .where(self.AccountDeletion.id == deletion_id)
                .with_for_update()
            )
            self.assertIsNotNone(row)

            def select_locked_row():
                try:
                    with self.SessionLocal() as second_session:
                        second_backend_pid.append(second_session.execute(
                            text("SELECT pg_backend_pid()")
                        ).scalar_one())
                        second_started.set()
                        second_session.scalar(
                            select(self.AccountDeletion)
                            .where(self.AccountDeletion.id == deletion_id)
                            .with_for_update()
                        )
                except Exception as error:
                    second_errors.append(error)
                finally:
                    second_finished.set()

            contender = threading.Thread(target=select_locked_row)
            contender.start()
            self.assertTrue(second_started.wait(timeout=5))

            waiting_on_row_lock = False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with self.owner_engine.connect() as connection:
                    activity = connection.execute(text(
                        "SELECT wait_event_type, query FROM pg_stat_activity "
                        "WHERE pid = :pid"
                    ), {"pid": second_backend_pid[0]}).mappings().one_or_none()
                if (
                    activity is not None
                    and activity["wait_event_type"] == "Lock"
                    and "FOR UPDATE" in activity["query"].upper()
                ):
                    waiting_on_row_lock = True
                    break
                if second_finished.is_set():
                    break
                time.sleep(0.05)
            self.assertTrue(waiting_on_row_lock)
            self.assertFalse(second_finished.is_set())

            first_session.commit()
            self.assertTrue(second_finished.wait(timeout=5))
            self.assertEqual(second_errors, [])
        finally:
            if first_session.in_transaction():
                first_session.rollback()
            first_session.close()
            if contender is not None:
                contender.join(timeout=5)

        with self.SessionLocal() as session:
            completed = session.get(self.AccountDeletion, deletion_id)
            completed.state = "COMPLETED"
            completed.local_deleted = True
            session.commit()

    def test_10_provider_success_cleanup_rollback_recovers_in_a_fresh_process(self):
        deletion_id = uuid.uuid4().hex
        subject = "postgres-restart-" + uuid.uuid4().hex
        target_email = f"postgres-target-{uuid.uuid4().hex}@example.test"
        other_email = f"postgres-other-{uuid.uuid4().hex}@example.test"
        with self.SessionLocal() as session:
            target = self.User(
                email=target_email,
                name="Delete Target",
                password_hash="test-hash",
                auth_subject=subject,
            )
            other = self.User(
                email=other_email,
                name="Other User",
                password_hash="test-hash",
            )
            session.add_all((target, other))
            session.flush()
            target_id = target.id
            other_id = other.id
            session.add_all((
                self.Activity(user_id=target_id, kind="test", item_id="target-row"),
                self.Activity(user_id=other_id, kind="test", item_id="other-row"),
                self.AccountDeletion(
                    id=deletion_id,
                    user_id=target_id,
                    provider_subject_hash=hashlib.sha256(subject.encode()).hexdigest(),
                    provider_subject_ciphertext=self.encrypt_account_deletion_subject(subject),
                    state="PENDING_PROVIDER",
                    provider_reconciled=False,
                    local_deleted=False,
                ),
            ))
            session.commit()

        trigger_name = "kite_integration_fail_user_delete"
        function_name = "kite_integration_fail_user_delete"
        with self.owner_engine.begin() as connection:
            connection.exec_driver_sql(
                f"DROP TRIGGER IF EXISTS {trigger_name} ON public.users"
            )
            connection.exec_driver_sql(
                f"DROP FUNCTION IF EXISTS public.{function_name}()"
            )
            connection.exec_driver_sql(
                f"CREATE FUNCTION public.{function_name}() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'injected account cleanup failure'; "
                "END; $$"
            )
            connection.exec_driver_sql(
                f"CREATE TRIGGER {trigger_name} BEFORE DELETE ON public.users "
                f"FOR EACH ROW EXECUTE FUNCTION public.{function_name}()"
            )

        server, server_thread, provider_state, _, release_request, base_url = (
            start_fake_supabase()
        )
        try:
            with patch.dict(os.environ, self._provider_environment(base_url)):
                self.reconcile_pending_local_account_deletions()

            self.assertEqual(provider_state["request_count"], 1)
            with self.SessionLocal() as session:
                audit = session.get(self.AccountDeletion, deletion_id)
                self.assertEqual(audit.state, "FAILED")
                self.assertFalse(audit.provider_reconciled)
                self.assertFalse(audit.local_deleted)
                self.assertIsNotNone(session.get(self.User, target_id))
                self.assertIsNotNone(session.query(self.Activity).filter_by(
                    user_id=target_id,
                ).one_or_none())
                self.assertIsNotNone(session.get(self.User, other_id))
                self.assertIsNotNone(session.query(self.Activity).filter_by(
                    user_id=other_id,
                ).one_or_none())

            with self.owner_engine.begin() as connection:
                connection.exec_driver_sql(
                    f"DROP TRIGGER IF EXISTS {trigger_name} ON public.users"
                )
                connection.exec_driver_sql(
                    f"DROP FUNCTION IF EXISTS public.{function_name}()"
                )

            result = self._run_reconciliation_process(
                self._provider_environment(base_url),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(provider_state["request_count"], 2)
            with self.SessionLocal() as session:
                audit = session.get(self.AccountDeletion, deletion_id)
                self.assertEqual(audit.state, "COMPLETED")
                self.assertTrue(audit.provider_reconciled)
                self.assertTrue(audit.local_deleted)
                self.assertIsNone(session.get(self.User, target_id))
                self.assertIsNone(session.query(self.Activity).filter_by(
                    user_id=target_id,
                ).one_or_none())
                self.assertIsNotNone(session.get(self.User, other_id))
                self.assertIsNotNone(session.query(self.Activity).filter_by(
                    user_id=other_id,
                ).one_or_none())
        finally:
            release_request.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
            with self.owner_engine.begin() as connection:
                connection.exec_driver_sql(
                    f"DROP TRIGGER IF EXISTS {trigger_name} ON public.users"
                )
                connection.exec_driver_sql(
                    f"DROP FUNCTION IF EXISTS public.{function_name}()"
                )

    def test_11_separate_worker_processes_reconcile_only_once(self):
        deletion_id = uuid.uuid4().hex
        subject = "postgres-workers-" + uuid.uuid4().hex
        with self.SessionLocal() as session:
            user = self.User(
                email=f"postgres-workers-{uuid.uuid4().hex}@example.test",
                name="Concurrent Workers",
                password_hash="test-hash",
                auth_subject=subject,
            )
            session.add(user)
            session.flush()
            user_id = user.id
            session.add_all((
                self.Activity(user_id=user_id, kind="test", item_id="worker-row"),
                self.AccountDeletion(
                    id=deletion_id,
                    user_id=user_id,
                    provider_subject_hash=hashlib.sha256(subject.encode()).hexdigest(),
                    provider_subject_ciphertext=self.encrypt_account_deletion_subject(subject),
                    state="PENDING_PROVIDER",
                    provider_reconciled=False,
                    local_deleted=False,
                ),
            ))
            session.commit()

        (
            server,
            server_thread,
            provider_state,
            request_started,
            release_request,
            base_url,
        ) = start_fake_supabase(block_first_request=True)
        processes = []
        try:
            environment = self._provider_environment(base_url)
            for _ in range(2):
                processes.append(subprocess.Popen(
                    [sys.executable, "-c", RECONCILE_WORKER_CODE],
                    cwd=ROOT,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ))

            self.assertTrue(request_started.wait(timeout=15))
            second_worker_waiting = False
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                with self.owner_engine.connect() as connection:
                    waiting = connection.execute(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() "
                        "AND wait_event_type = 'Lock' "
                        "AND query ILIKE '%account_deletions%' "
                        "AND query ILIKE '%FOR UPDATE%')"
                    )).scalar_one()
                if waiting:
                    second_worker_waiting = True
                    break
                time.sleep(0.05)
            self.assertTrue(
                second_worker_waiting,
                "No second PostgreSQL worker session was observed waiting on the outbox row.",
            )
        finally:
            release_request.set()
            process_results = []
            for process in processes:
                try:
                    process_results.append(process.communicate(timeout=30))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process_results.append(process.communicate(timeout=5))
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

        self.assertEqual(len(processes), 2)
        for process, (stdout, stderr) in zip(processes, process_results):
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(provider_state["request_count"], 1)
        with self.SessionLocal() as session:
            audit = session.get(self.AccountDeletion, deletion_id)
            self.assertEqual(audit.state, "COMPLETED")
            self.assertTrue(audit.provider_reconciled)
            self.assertTrue(audit.local_deleted)
            self.assertIsNone(session.get(self.User, user_id))

    def test_07_downgrade_preserves_audit_data_guard_and_existing_delete_grants(self):
        guard_id = uuid.uuid4().hex
        with self.SessionLocal() as session:
            session.add(self.AccountDeletion(
                id=guard_id,
                user_id=999999999,
                state="FAILED",
                provider_reconciled=False,
                local_deleted=False,
            ))
            session.commit()

        with self.assertRaisesRegex(RuntimeError, "Refusing to downgrade"):
            command.downgrade(self._alembic_config(), "e85bcc461ab0")
        with self.SessionLocal() as session:
            self.assertIsNotNone(session.get(self.AccountDeletion, guard_id))
            session.delete(session.get(self.AccountDeletion, guard_id))
            session.commit()

        with self.owner_engine.connect() as connection:
            before = connection.execute(text(
                "SELECT has_table_privilege('kite_runtime', 'public.activity', 'DELETE'), "
                "has_table_privilege('kite_runtime', 'public.users', 'DELETE'), "
                "has_table_privilege('kite_runtime', 'public.auth_sessions', 'DELETE')"
            )).one()
        self.assertEqual(before, (True, True, True))

        command.downgrade(self._alembic_config(), "e85bcc461ab0")
        self.assertNotIn(
            "account_deletions",
            inspect(self.owner_engine).get_table_names(schema="public"),
        )
        with self.owner_engine.connect() as connection:
            after = connection.execute(text(
                "SELECT has_table_privilege('kite_runtime', 'public.activity', 'DELETE'), "
                "has_table_privilege('kite_runtime', 'public.users', 'DELETE'), "
                "has_table_privilege('kite_runtime', 'public.auth_sessions', 'DELETE')"
            )).one()
        self.assertEqual(after, (False, False, True))

        command.upgrade(self._alembic_config(), "head")
        command.check(self._alembic_config())
        self.assertIn(
            "account_deletions",
            inspect(self.owner_engine).get_table_names(schema="public"),
        )

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
