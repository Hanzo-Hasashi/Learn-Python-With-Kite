import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import HTTPException

DB_DIR = tempfile.TemporaryDirectory()
logging.disable(logging.CRITICAL)
os.environ["DATABASE_URL"] = "sqlite:///" + Path(DB_DIR.name, "test.db").as_posix()
os.environ.pop("MIGRATION_DATABASE_URL", None)
os.environ["KITE_SECRET_KEY"] = "test-only-secret-key-with-at-least-32-characters"
os.environ["KITE_ALLOW_LOCAL_AUTH"] = "true"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_ANON_KEY"] = ""
os.environ["RUNNER_SHARED_SECRET"] = "test-runner-secret"

from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.app import (
    Activity, AuthSession, Base, CHALLENGES, GuestPlaygroundExecution, LESSONS, Lesson, LessonProgress,
    MobileMoneyPayment, PaymentAttempt, PaymentProviderConfiguration, PaymentStatusHistory,
    PaymentTransaction, PlaygroundError, PlaygroundExecution, Subscription, MODULES, QUIZZES,
    QUIZ_METADATA, QuizAttempt, SessionLocal, SubscriptionRegistration, User, app, engine, issue_token,
    cookie_settings, database_engine_options, database_url, decrypt_subscription_phone, startup,
    validate_runtime_configuration,
    validate_postgres_ssl_url,
    auth_attempts,
)
from backend.curriculum import NEW_CHALLENGE_DATA, NEW_QUIZ_DATA
from backend.payments import PaymentStatus, ProviderPaymentResult, normalize_zambian_phone


class KiteApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        command.upgrade(Config(str(Path(__file__).parent.parent / "alembic.ini")), "head")
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        Base.metadata.drop_all(engine)
        engine.dispose()
        DB_DIR.cleanup()

    def setUp(self):
        auth_attempts.clear()
        self.email = f"user-{uuid.uuid4().hex}@example.com"
        response = self.client.post("/api/auth/register", json={
            "name": "Kite Learner",
            "email": self.email,
            "password": "secure-example-password",
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.token = self.issue_token(self.email)
        self.headers = {"Authorization": "Bearer " + self.token}
        self.client.cookies.clear()

    @staticmethod
    def issue_token(email):
        with SessionLocal() as db:
            user = db.query(User).filter_by(email=email).one()
            return issue_token(user, False)[0]

    def test_registration_login_and_persisted_profile(self):
        response = self.client.get("/api/me", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Kite Learner")
        login = self.client.post("/api/auth/login", json={
            "email": self.email,
            "password": "secure-example-password",
        })
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["user"]["email"], self.email)

    def test_local_auth_requires_explicit_opt_in_and_is_disabled_on_vercel(self):
        registration_email = f"disabled-{uuid.uuid4().hex}@example.com"
        disabled_auth = {
            "KITE_ALLOW_LOCAL_AUTH": "false",
            "SUPABASE_URL": "",
            "SUPABASE_ANON_KEY": "",
        }
        with patch.dict(os.environ, disabled_auth):
            registration = self.client.post("/api/auth/register", json={
                "name": "Unverified Learner",
                "email": registration_email,
                "password": "secure-example-password",
            })
            self.assertEqual(registration.status_code, 503)
            login = self.client.post("/api/auth/login", json={
                "email": self.email,
                "password": "secure-example-password",
            })
            self.assertEqual(login.status_code, 503)
            self.assertEqual(self.client.get("/api/me", headers=self.headers).status_code, 401)
            with SessionLocal() as db:
                self.assertIsNone(db.query(User.id).filter_by(email=registration_email).first())

        with patch.dict(os.environ, {
            **disabled_auth,
            "KITE_ALLOW_LOCAL_AUTH": "true",
            "VERCEL": "1",
            "KITE_CORS_ORIGINS": "https://learn.example.com",
        }):
            hosted_registration = self.client.post("/api/auth/register", json={
                "name": "Unverified Learner",
                "email": registration_email,
                "password": "secure-example-password",
            }, headers={"Origin": "https://learn.example.com"})
            self.assertEqual(hosted_registration.status_code, 503)

    def test_hosted_database_configuration_requires_postgres(self):
        with patch.dict(os.environ, {"RENDER": "true"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL is required"):
                database_url()
            os.environ["DATABASE_URL"] = "sqlite:///local.db"
            with self.assertRaisesRegex(RuntimeError, "require a PostgreSQL"):
                database_url()
            os.environ["DATABASE_URL"] = "postgres://user:password@db.example.test/postgres?sslmode=require"
            self.assertEqual(
                database_url(),
                "postgresql+psycopg://user:password@db.example.test/postgres?sslmode=require",
            )
            os.environ["DATABASE_URL"] = "postgresql://user:password@db.example.test/postgres"
            with self.assertRaisesRegex(RuntimeError, "must specify sslmode"):
                database_url()
            os.environ["DATABASE_URL"] = "postgresql://user:password@db.example.test/postgres?sslmode=disable"
            with self.assertRaisesRegex(RuntimeError, "must specify sslmode"):
                database_url()

    def test_sqlite_fallback_requires_explicit_development_mode(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "KITE_ENV=development"):
                database_url()

        with patch.dict(os.environ, {"KITE_ENV": "development"}, clear=True):
            self.assertEqual(database_url(), "sqlite:///./kite.db")

        with patch.dict(os.environ, {"KITE_ENV": "production"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL is required"):
                database_url()
            os.environ["DATABASE_URL"] = "sqlite:///local.db"
            with self.assertRaisesRegex(RuntimeError, "require a PostgreSQL"):
                database_url()

    def test_ssl_disabled_is_allowed_only_for_marked_local_migration_tests(self):
        test_url = (
            "postgresql+psycopg://kite_test_owner:test-password@127.0.0.1:55432/"
            "kite_integration_test?sslmode=disable"
        )
        test_environment = {
            "KITE_POSTGRES_INTEGRATION_TESTS": "1",
            "KITE_TEST_SAFETY_MARKER": "KITE_DISPOSABLE_POSTGRES_TEST_ONLY",
            "KITE_TEST_DATABASE_URL": test_url,
        }
        with patch.dict(os.environ, test_environment, clear=True):
            validate_postgres_ssl_url(test_url, "MIGRATION_DATABASE_URL")

        unsafe_url = test_url.replace("127.0.0.1", "db.example.test")
        with patch.dict(os.environ, {
            **test_environment,
            "KITE_TEST_DATABASE_URL": unsafe_url,
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "must specify sslmode"):
                validate_postgres_ssl_url(unsafe_url, "MIGRATION_DATABASE_URL")

    def test_hosted_runtime_requires_supabase_secure_cookies_and_exact_cors(self):
        hosted_config = {
            "RENDER": "true",
            "DATABASE_URL": "postgresql://kite_runtime:password@db.example.test/postgres?sslmode=require",
            "KITE_SECRET_KEY": "test-only-secret-key-with-at-least-32-characters",
            "SUPABASE_URL": "https://kite-project.supabase.co",
            "SUPABASE_ANON_KEY": "sb_publishable_test",
            "PUBLIC_APP_URL": "https://learn.example.com",
            "PUBLIC_API_URL": "https://api.example.com",
            "KITE_COOKIE_SITE": "example.com",
            "KITE_CORS_ORIGINS": "https://learn.example.com",
            "KITE_COOKIE_SECURE": "true",
            "KITE_COOKIE_SAMESITE": "lax",
            "KITE_ALLOW_LOCAL_AUTH": "false",
        }
        with patch.dict(os.environ, hosted_config, clear=True):
            validate_runtime_configuration()

            os.environ["KITE_CORS_ORIGINS"] = "*"
            with self.assertRaisesRegex(RuntimeError, "exact origins"):
                validate_runtime_configuration()

            os.environ["KITE_CORS_ORIGINS"] = hosted_config["KITE_CORS_ORIGINS"]
            os.environ["KITE_COOKIE_SECURE"] = "false"
            with self.assertRaisesRegex(RuntimeError, "KITE_COOKIE_SECURE=true"):
                validate_runtime_configuration()

            os.environ["KITE_COOKIE_SECURE"] = "true"
            os.environ["SUPABASE_ANON_KEY"] = ""
            with self.assertRaisesRegex(RuntimeError, "both SUPABASE_URL and SUPABASE_ANON_KEY"):
                validate_runtime_configuration()

            os.environ["SUPABASE_ANON_KEY"] = hosted_config["SUPABASE_ANON_KEY"]
            os.environ["PUBLIC_API_URL"] = "https://kite.onrender.com"
            with self.assertRaisesRegex(RuntimeError, "must use subdomains"):
                validate_runtime_configuration()

            os.environ["PUBLIC_API_URL"] = hosted_config["PUBLIC_API_URL"]
            os.environ["KITE_COOKIE_SAMESITE"] = "none"
            with self.assertRaisesRegex(RuntimeError, "KITE_COOKIE_SAMESITE=lax"):
                validate_runtime_configuration()

    def test_hosted_runtime_accepts_same_origin_vercel_proxy_with_secret(self):
        proxy_config = {
            "RENDER": "true",
            "DATABASE_URL": "postgresql://kite_runtime:password@db.example.test/postgres?sslmode=require",
            "KITE_SECRET_KEY": "test-only-secret-key-with-at-least-32-characters",
            "SUPABASE_URL": "https://kite-project.supabase.co",
            "SUPABASE_ANON_KEY": "sb_publishable_test",
            "PUBLIC_APP_URL": "https://learnpythonwithkite.vercel.app",
            "PUBLIC_API_URL": "https://learnpythonwithkite.vercel.app",
            "KITE_VERCEL_PROXY": "true",
            "KITE_PROXY_SECRET": "proxy-secret-for-tests-that-is-at-least-32",
            "KITE_CORS_ORIGINS": "https://learnpythonwithkite.vercel.app",
            "KITE_COOKIE_SECURE": "true",
            "KITE_COOKIE_SAMESITE": "lax",
            "KITE_ALLOW_LOCAL_AUTH": "false",
        }
        with patch.dict(os.environ, proxy_config, clear=True):
            validate_runtime_configuration()

            os.environ["KITE_PROXY_SECRET"] = "too-short"
            with self.assertRaisesRegex(RuntimeError, "KITE_PROXY_SECRET"):
                validate_runtime_configuration()

            os.environ["KITE_PROXY_SECRET"] = proxy_config["KITE_PROXY_SECRET"]
            os.environ["KITE_PROXY_SECRET_PREVIOUS"] = "short"
            with self.assertRaisesRegex(RuntimeError, "KITE_PROXY_SECRET_PREVIOUS"):
                validate_runtime_configuration()

            os.environ["KITE_PROXY_SECRET_PREVIOUS"] = proxy_config["KITE_PROXY_SECRET"]
            with self.assertRaisesRegex(RuntimeError, "different value"):
                validate_runtime_configuration()

            os.environ.pop("KITE_PROXY_SECRET_PREVIOUS")
            os.environ["PUBLIC_API_URL"] = "https://kite.onrender.com"
            with self.assertRaisesRegex(RuntimeError, "match PUBLIC_APP_URL"):
                validate_runtime_configuration()

    def test_vercel_proxy_blocks_direct_api_requests_and_keeps_health_public(self):
        secret = "proxy-secret-for-tests-that-is-at-least-32"
        with patch.dict(os.environ, {
            "KITE_VERCEL_PROXY": "true",
            "KITE_PROXY_SECRET": secret,
            "KITE_PROXY_SECRET_PREVIOUS": "",
        }, clear=False):
            direct = self.client.get("/api/auth/session")
            self.assertEqual(direct.status_code, 404)
            self.assertEqual(direct.headers["Cache-Control"], "private, no-store")

            proxied = self.client.get(
                "/api/auth/session",
                headers={"X-Kite-Proxy-Secret": secret},
            )
            self.assertEqual(proxied.status_code, 401)
            self.assertEqual(proxied.headers["Cache-Control"], "private, no-store")

            health = self.client.get("/health")
            self.assertEqual(health.status_code, 200)

    def test_proxy_secret_rotation_accepts_current_and_previous_secret(self):
        current = "proxy-secret-current-for-tests-at-least-32"
        previous = "proxy-secret-previous-for-tests-at-least-32"
        with patch.dict(os.environ, {
            "KITE_VERCEL_PROXY": "true",
            "KITE_PROXY_SECRET": current,
            "KITE_PROXY_SECRET_PREVIOUS": previous,
        }, clear=False):
            for secret in (current, previous):
                response = self.client.get(
                    "/api/auth/session",
                    headers={"X-Kite-Proxy-Secret": secret},
                )
                self.assertEqual(response.status_code, 401)

            rejected = self.client.get(
                "/api/auth/session",
                headers={"X-Kite-Proxy-Secret": "unrecognized-secret"},
            )
            self.assertEqual(rejected.status_code, 404)

    def test_proxy_rate_limit_uses_validated_visitor_ip(self):
        secret = "proxy-secret-for-tests-that-is-at-least-32"
        email = f"proxy-limit-{uuid.uuid4().hex}@example.com"
        payload = {
            "name": "Proxy Rate Limit",
            "email": email,
            "password": "secure-example-password",
        }
        with patch.dict(os.environ, {
            "KITE_VERCEL_PROXY": "true",
            "KITE_PROXY_SECRET": secret,
            "KITE_PROXY_SECRET_PREVIOUS": "",
        }, clear=False):
            headers = {
                "X-Kite-Proxy-Secret": secret,
                "X-Real-IP": "203.0.113.10",
            }
            for _ in range(5):
                self.client.post("/api/auth/register", json=payload, headers=headers)
            limited = self.client.post("/api/auth/register", json=payload, headers=headers)
            self.assertEqual(limited.status_code, 429)

            other_visitor = self.client.post(
                "/api/auth/register",
                json=payload,
                headers={
                    "X-Kite-Proxy-Secret": secret,
                    "X-Real-IP": "203.0.113.11",
                },
            )
            self.assertNotEqual(other_visitor.status_code, 429)

    def test_hosted_unsafe_api_requests_require_an_allowed_origin(self):
        with patch.dict(os.environ, {
            "KITE_ENV": "production",
            "KITE_VERCEL_PROXY": "false",
            "KITE_PROXY_SECRET": "",
            "KITE_CORS_ORIGINS": "https://learn.example.com",
            "SUPABASE_URL": "",
            "SUPABASE_ANON_KEY": "",
            "KITE_ALLOW_LOCAL_AUTH": "false",
        }, clear=False):
            payload = {
                "name": "Origin Check",
                "email": f"origin-{uuid.uuid4().hex}@example.com",
                "password": "secure-example-password",
            }
            missing_origin = self.client.post("/api/auth/register", json=payload)
            self.assertEqual(missing_origin.status_code, 403)
            self.assertEqual(missing_origin.headers["Cache-Control"], "private, no-store")

            untrusted_origin = self.client.post(
                "/api/auth/register",
                json=payload,
                headers={"Origin": "https://attacker.example"},
            )
            self.assertEqual(untrusted_origin.status_code, 403)

            trusted_origin = self.client.post(
                "/api/auth/register",
                json=payload,
                headers={"Origin": "https://learn.example.com"},
            )
            self.assertEqual(trusted_origin.status_code, 503)

    def test_cookie_policy_uses_secure_same_site_settings_in_hosted_environment(self):
        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/auth/login",
            "raw_path": b"/api/auth/login",
            "query_string": b"",
            "headers": [],
            "server": ("api.onrender.com", 80),
            "client": ("127.0.0.1", 12345),
        })
        with patch.dict(os.environ, {
            "RENDER": "true",
            "KITE_COOKIE_SECURE": "true",
            "KITE_COOKIE_SAMESITE": "lax",
        }, clear=True):
            settings = cookie_settings(request, True)
        self.assertTrue(settings["httponly"])
        self.assertTrue(settings["secure"])
        self.assertEqual(settings["samesite"], "lax")

        with patch.dict(os.environ, {}, clear=True):
            local_settings = cookie_settings(request, False)
        self.assertFalse(local_settings["secure"])
        self.assertEqual(local_settings["samesite"], "lax")

    def test_local_password_recovery_requires_auth_provider_or_local_opt_in(self):
        with patch.dict(os.environ, {
            "KITE_ALLOW_LOCAL_AUTH": "false",
            "SUPABASE_URL": "",
            "SUPABASE_ANON_KEY": "",
        }):
            requested = self.client.post("/api/auth/password-reset", json={"email": self.email})
            completed = self.client.post("/api/auth/password-reset/complete", json={
                "token": "not-a-valid-reset-token",
                "password": "another-secure-password",
            })
        self.assertEqual(requested.status_code, 503)
        self.assertEqual(completed.status_code, 503)

    def test_incorrect_login_credentials_do_not_create_a_session(self):
        response = self.client.post("/api/auth/login", json={
            "email": self.email,
            "password": "incorrect-example-password",
        })
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["detail"], "Email or password is incorrect.")
        self.assertFalse(any(
            value.startswith("kite_access=") for value in response.headers.get_list("set-cookie")
        ))

    def test_cookie_session_is_http_only_csrf_protected_and_restorable(self):
        email = f"remembered-{uuid.uuid4().hex}@example.com"
        response = self.client.post("/api/auth/register", json={
            "name": "Remembered Learner",
            "email": email,
            "password": "secure-example-password",
            "remember_me": True,
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("access_token", response.json())
        cookies = response.headers.get_list("set-cookie")
        access_cookie = next(value for value in cookies if value.startswith("kite_access="))
        self.assertIn("httponly", access_cookie.lower())
        self.assertIn("max-age=2592000", access_cookie.lower())

        restored = self.client.get("/api/auth/session")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertTrue(restored.json()["remember_me"])
        self.assertEqual(restored.json()["user"]["email"], email)
        self.assertEqual(self.client.patch("/api/me", json={"name": "Changed Name"}).status_code, 403)
        csrf = restored.json()["csrf_token"]
        updated = self.client.patch(
            "/api/me", headers={"X-Kite-CSRF": csrf}, json={"name": "Changed Name"},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        logged_out = self.client.post("/api/auth/logout", headers={"X-Kite-CSRF": csrf})
        self.assertEqual(logged_out.status_code, 200, logged_out.text)
        self.assertEqual(self.client.get("/api/auth/session").status_code, 401)

    def test_nonremembered_cookie_session_expires_with_browser_session(self):
        email = f"session-{uuid.uuid4().hex}@example.com"
        response = self.client.post("/api/auth/register", json={
            "name": "Session Learner",
            "email": email,
            "password": "secure-example-password",
        })
        self.assertEqual(response.status_code, 201, response.text)
        access_cookie = next(
            value for value in response.headers.get_list("set-cookie")
            if value.startswith("kite_access=")
        )
        self.assertNotIn("max-age=", access_cookie.lower())
        self.assertFalse(self.client.get("/api/auth/session").json()["remember_me"])

    def test_logout_revokes_local_access_token(self):
        response = self.client.post("/api/auth/logout", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/me", headers=self.headers).status_code, 401)

    def test_supabase_logout_uses_refreshed_access_token_and_clears_session_cookies(self):
        env = {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
        }
        provider_user = {
            "id": "supabase-logout-" + uuid.uuid4().hex,
            "email": self.email,
            "email_confirmed_at": "2025-01-01T00:00:00Z",
        }
        expired_access = "expired-access-" + uuid.uuid4().hex
        valid_refresh = "valid-refresh-" + uuid.uuid4().hex
        with patch.dict(os.environ, env), patch(
            "backend.app.supabase_request", return_value=provider_user,
        ):
            linked = self.client.post("/api/auth/session", json={
                "access_token": expired_access,
                "refresh_token": valid_refresh,
                "remember_me": True,
            })
        self.assertEqual(linked.status_code, 200, linked.text)
        csrf = linked.json()["csrf_token"]
        session_id = self.client.cookies.get("kite_session")
        original_cookies = dict(self.client.cookies)

        def supabase_request(path, *, method="GET", data=None, access_token=None):
            if path == "/user" and access_token == expired_access:
                raise HTTPException(status_code=401, detail="Expired access token.")
            if path == "/token?grant_type=refresh_token":
                self.assertEqual(data, {"refresh_token": valid_refresh})
                return {
                    "access_token": "refreshed-access-token",
                    "refresh_token": "rotated-refresh-token",
                    "user": provider_user,
                }
            if path == "/logout?scope=local":
                self.assertEqual(method, "POST")
                self.assertEqual(access_token, "refreshed-access-token")
                return {}
            self.fail(f"Unexpected Supabase Auth request: {path}")

        with patch.dict(os.environ, env), patch(
            "backend.app.supabase_request", side_effect=supabase_request,
        ) as auth_request:
            response = self.client.post(
                "/api/auth/logout", headers={"X-Kite-CSRF": csrf},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ok": True})
        paths = [call.args[0] for call in auth_request.call_args_list]
        self.assertEqual(
            paths,
            ["/user", "/token?grant_type=refresh_token", "/logout?scope=local"],
        )
        self.assertEqual(
            self.client.cookies.get("kite_access"), None,
            response.headers.get_list("set-cookie"),
        )
        self.assertEqual(self.client.cookies.get("kite_refresh"), None)
        self.assertEqual(self.client.cookies.get("kite_session"), None)

        cleared_cookies = response.headers.get_list("set-cookie")
        for name in ("kite_access", "kite_refresh", "kite_session"):
            cookie = next(value for value in cleared_cookies if value.startswith(name + "="))
            self.assertIn("path=/", cookie.lower())
            self.assertIn("httponly", cookie.lower())
            self.assertIn("max-age=0", cookie.lower())
            self.assertIn("samesite=lax", cookie.lower())

        with SessionLocal() as db:
            session = db.get(AuthSession, session_id)
            self.assertIsNotNone(session.revoked_at)

        cookie_header = "; ".join(
            name + "=" + value for name, value in original_cookies.items()
        )
        repeated = self.client.post(
            "/api/auth/logout",
            headers={"Cookie": cookie_header, "X-Kite-CSRF": csrf},
        )
        self.assertEqual(repeated.status_code, 401)
        protected = self.client.get("/api/me", headers={"Cookie": cookie_header})
        self.assertEqual(protected.status_code, 401)

    def test_supabase_logout_failure_does_not_report_success_or_revoke_locally(self):
        env = {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
        }
        provider_user = {
            "id": "supabase-logout-failure-" + uuid.uuid4().hex,
            "email": self.email,
            "email_confirmed_at": "2025-01-01T00:00:00Z",
        }
        access_token = "logout-failure-access-" + uuid.uuid4().hex
        with patch.dict(os.environ, env), patch(
            "backend.app.supabase_request", return_value=provider_user,
        ):
            linked = self.client.post("/api/auth/session", json={
                "access_token": access_token,
                "refresh_token": "logout-failure-refresh-" + uuid.uuid4().hex,
            })
        self.assertEqual(linked.status_code, 200, linked.text)
        session_id = self.client.cookies.get("kite_session")
        csrf = linked.json()["csrf_token"]

        def fail_logout(path, **kwargs):
            if path == "/user":
                return provider_user
            if path == "/logout?scope=local":
                raise HTTPException(status_code=503, detail="Authentication service is unavailable.")
            self.fail(f"Unexpected Supabase Auth request: {path}")

        with patch.dict(os.environ, env), patch(
            "backend.app.supabase_request", side_effect=fail_logout,
        ):
            response = self.client.post(
                "/api/auth/logout", headers={"X-Kite-CSRF": csrf},
            )

        self.assertEqual(response.status_code, 503)
        self.assertNotEqual(response.json(), {"ok": True})
        self.assertTrue(self.client.cookies.get("kite_access"))
        with SessionLocal() as db:
            self.assertIsNone(db.get(AuthSession, session_id).revoked_at)

    def test_failed_logout_after_token_rotation_preserves_session_for_retry(self):
        env = {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
        }
        provider_user = {
            "id": "supabase-logout-rotation-" + uuid.uuid4().hex,
            "email": self.email,
            "email_confirmed_at": "2025-01-01T00:00:00Z",
        }
        expired_access = "logout-rotation-access-" + uuid.uuid4().hex
        refresh_token = "logout-rotation-refresh-" + uuid.uuid4().hex
        with patch.dict(os.environ, env), patch(
            "backend.app.supabase_request", return_value=provider_user,
        ):
            linked = self.client.post("/api/auth/session", json={
                "access_token": expired_access,
                "refresh_token": refresh_token,
                "remember_me": True,
            })
        self.assertEqual(linked.status_code, 200, linked.text)
        csrf = linked.json()["csrf_token"]
        session_id = self.client.cookies.get("kite_session")
        rotated_access = "logout-rotation-access-refreshed-" + uuid.uuid4().hex
        rotated_refresh = "logout-rotation-refresh-refreshed-" + uuid.uuid4().hex
        logout_attempts = 0

        def supabase_request(path, *, method="GET", data=None, access_token=None):
            nonlocal logout_attempts
            if path == "/user" and access_token == expired_access:
                raise HTTPException(status_code=401, detail="Expired access token.")
            if path == "/user" and access_token == rotated_access:
                return provider_user
            if path == "/token?grant_type=refresh_token":
                self.assertEqual(data, {"refresh_token": refresh_token})
                return {
                    "access_token": rotated_access,
                    "refresh_token": rotated_refresh,
                    "user": provider_user,
                }
            if path == "/logout?scope=local":
                self.assertEqual(method, "POST")
                self.assertEqual(access_token, rotated_access)
                logout_attempts += 1
                if logout_attempts == 1:
                    raise HTTPException(
                        status_code=503, detail="Authentication service is unavailable.",
                    )
                return {}
            self.fail(f"Unexpected Supabase Auth request: {path}")

        with patch.dict(os.environ, env), patch(
            "backend.app.supabase_request", side_effect=supabase_request,
        ):
            failed = self.client.post(
                "/api/auth/logout", headers={"X-Kite-CSRF": csrf},
            )
            self.assertEqual(failed.status_code, 503)
            self.assertEqual(self.client.cookies.get("kite_access"), rotated_access)
            self.assertEqual(self.client.cookies.get("kite_refresh"), rotated_refresh)
            with SessionLocal() as db:
                session = db.get(AuthSession, session_id)
                self.assertEqual(
                    session.token_hash,
                    hashlib.sha256(rotated_access.encode()).hexdigest(),
                )
                self.assertIsNone(session.revoked_at)

            retried = self.client.post(
                "/api/auth/logout", headers={"X-Kite-CSRF": csrf},
            )

        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(logout_attempts, 2)
        self.assertIsNone(self.client.cookies.get("kite_access"))
        with SessionLocal() as db:
            self.assertIsNotNone(db.get(AuthSession, session_id).revoked_at)

    def test_structured_validation_error_does_not_expose_internal_details(self):
        response = self.client.post("/api/auth/login", json={"email": "not-an-email"})
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertIn("detail", body)
        self.assertIn("error", body)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertIn("X-Kite-Request-ID", response.headers)
        combined = json.dumps(body).lower()
        for forbidden in ("database", "sql", "supabase", "traceback", "stack trace", "http 422"):
            self.assertNotIn(forbidden, combined)

    def test_structured_not_found_error_does_not_expose_paths(self):
        response = self.client.get("/api/this-route-does-not-exist")
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["error"]["code"], "not_found")
        self.assertNotIn("/api/this-route-does-not-exist", body["detail"])
        self.assertIn("X-Kite-Request-ID", response.headers)

    def test_unexpected_exception_returns_generic_safe_response(self):
        async def explode():
            raise RuntimeError("database password=secret and SQL SELECT * leaked")

        app.add_api_route(
            "/api/test-only-unexpected-error",
            explode,
            methods=["GET"],
        )
        try:
            response = self.client.get("/api/test-only-unexpected-error")
        finally:
            app.router.routes[:] = [
                route for route in app.router.routes
                if getattr(route, "path", None) != "/api/test-only-unexpected-error"
            ]
        self.assertEqual(response.status_code, 500)
        body = response.json()
        combined = json.dumps(body).lower()
        self.assertEqual(body["error"]["code"], "internal_error")
        self.assertNotIn("database password", combined)
        self.assertNotIn("sql", combined)
        self.assertIn("X-Kite-Request-ID", response.headers)

    def test_supabase_logout_rejects_expired_access_and_refresh_credentials(self):
        env = {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
        }
        provider_user = {
            "id": "supabase-expired-logout-" + uuid.uuid4().hex,
            "email": self.email,
            "email_confirmed_at": "2025-01-01T00:00:00Z",
        }
        access_token = "expired-access-" + uuid.uuid4().hex
        with patch.dict(os.environ, env), patch(
            "backend.app.supabase_request", return_value=provider_user,
        ):
            linked = self.client.post("/api/auth/session", json={
                "access_token": access_token,
                "refresh_token": "expired-refresh-" + uuid.uuid4().hex,
            })
        self.assertEqual(linked.status_code, 200, linked.text)
        csrf = linked.json()["csrf_token"]

        def expired_credentials(path, **kwargs):
            if path in {"/user", "/token?grant_type=refresh_token"}:
                raise HTTPException(status_code=401, detail="Session expired.")
            self.fail(f"Unexpected Supabase Auth request: {path}")

        with patch.dict(os.environ, env), patch(
            "backend.app.supabase_request", side_effect=expired_credentials,
        ) as auth_request:
            response = self.client.post(
                "/api/auth/logout", headers={"X-Kite-CSRF": csrf},
            )

        self.assertEqual(response.status_code, 401)
        self.assertNotEqual(response.json(), {"ok": True})
        self.assertFalse(any(
            call.args[0].startswith("/logout") for call in auth_request.call_args_list
        ))
        self.assertIsNone(self.client.cookies.get("kite_access"))
        self.assertIsNone(self.client.cookies.get("kite_refresh"))

    def test_tampered_local_auth_token_is_rejected(self):
        token = self.issue_token(self.email)
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        response = self.client.get("/api/me", headers={"Authorization": "Bearer " + tampered})
        self.assertEqual(response.status_code, 401)

    def test_profile_email_password_and_settings_changes_persist(self):
        profile = self.client.patch("/api/me", headers=self.headers, json={"name": "Updated Kite Learner"})
        self.assertEqual(profile.status_code, 200, profile.text)
        self.assertEqual(profile.json()["name"], "Updated Kite Learner")

        email = f"updated-{uuid.uuid4().hex}@example.com"
        changed_email = self.client.patch(
            "/api/me/email", headers=self.headers,
            json={"email": email, "password": "secure-example-password"},
        )
        self.assertEqual(changed_email.status_code, 200, changed_email.text)
        self.assertEqual(changed_email.json()["email"], email)

        changed_password = self.client.patch(
            "/api/me/password", headers=self.headers,
            json={"current_password": "secure-example-password", "new_password": "another-secure-password"},
        )
        self.assertEqual(changed_password.status_code, 200, changed_password.text)
        self.assertEqual(self.client.post("/api/auth/login", json={
            "email": email, "password": "secure-example-password",
        }).status_code, 401)
        login = self.client.post("/api/auth/login", json={
            "email": email, "password": "another-secure-password",
        })
        self.assertEqual(login.status_code, 200, login.text)

        settings = self.client.patch(
            "/api/settings", headers=self.headers, json={"theme": "dark", "notifications": False},
        )
        self.assertEqual(settings.status_code, 200, settings.text)
        me = self.client.get("/api/me", headers=self.headers).json()
        self.assertEqual(me["settings"]["theme"], "dark")
        self.assertFalse(me["settings"]["notifications"])

    def test_notification_read_clear_and_ownership_flows(self):
        notifications = self.client.get("/api/notifications", headers=self.headers)
        self.assertEqual(notifications.status_code, 200, notifications.text)
        self.assertEqual(len(notifications.json()), 1)
        notice_id = notifications.json()[0]["id"]

        marked = self.client.post(f"/api/notifications/{notice_id}/read", headers=self.headers)
        self.assertEqual(marked.status_code, 200)
        self.assertTrue(self.client.get("/api/notifications", headers=self.headers).json()[0]["read"])
        self.assertEqual(self.client.post("/api/notifications/read-all", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.delete("/api/notifications", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/notifications", headers=self.headers).json(), [])
        self.assertEqual(self.client.post("/api/notifications/999999/read", headers=self.headers).status_code, 404)

    def test_activity_notifications_are_real_deduplicated_and_preference_aware(self):
        complete_url = "/api/lessons/python-variables/complete"
        self.assertEqual(self.client.post(complete_url, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post(complete_url, headers=self.headers).status_code, 200)
        notices = self.client.get("/api/notifications", headers=self.headers).json()
        lesson_notices = [
            item for item in notices
            if item["title"] == "Lesson completed: " + LESSONS[0][1]
        ]
        self.assertEqual(len(lesson_notices), 1)
        self.assertEqual(lesson_notices[0]["category"], "learning")
        self.assertEqual(lesson_notices[0]["target_route"], "learn")
        self.assertEqual(
            self.client.get("/api/notifications/unread-count", headers=self.headers).json()["count"],
            len(notices),
        )

        self.client.patch("/api/settings", headers=self.headers, json={"notifications": False})
        self.assertEqual(
            self.client.post("/api/lessons/python-types/complete", headers=self.headers).status_code,
            200,
        )
        notices = self.client.get("/api/notifications", headers=self.headers).json()
        self.assertFalse(any(item["title"] == "Lesson completed: Data types" for item in notices))
        self.assertEqual(
            self.client.get("/api/notifications/unread-count", headers=self.headers).json()["count"],
            len(notices),
        )

    def test_progress_statistics_are_derived_from_persisted_records(self):
        initial = self.client.get("/api/progress", headers=self.headers).json()
        self.assertEqual(initial["total_lessons"], len(LESSONS))
        self.assertEqual(initial["completion_percent"], 0)
        self.assertEqual(initial["quiz_count"], 0)
        modules = self.client.get("/api/modules", headers=self.headers).json()
        self.assertEqual(
            initial["total_quizzes"],
            sum(module["quiz_count"] for module in modules if not module["locked"]),
        )
        self.assertEqual(initial["challenge_count"], 0)
        self.assertEqual(initial["total_challenges"], len(CHALLENGES))
        self.assertEqual(len(initial["weekly_activity"]), 7)
        self.assertEqual(initial["active_days_7"], 0)

        self.client.post("/api/lessons/python-variables/complete", headers=self.headers)
        result = self.client.get("/api/progress", headers=self.headers).json()
        self.assertEqual(result["completion_percent"], round(1 / len(LESSONS) * 100))
        self.assertEqual(result["xp"], 20)
        self.assertEqual(result["streak"], 1)
        self.assertEqual(result["active_days_7"], 1)
        self.assertEqual(sum(day["xp"] for day in result["weekly_activity"]), 20)

    def test_curriculum_has_twelve_modules_and_preserves_existing_lessons(self):
        response = self.client.get("/api/modules", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        modules = response.json()
        self.assertEqual(len(modules), 12)
        self.assertEqual([module["order"] for module in modules], list(range(1, 13)))
        self.assertEqual(
            [module["lessons"][0]["id"] for module in modules[:6]],
            [row[0] for row in LESSONS[:6]],
        )
        self.assertTrue(all(module["lesson_count"] == 8 for module in modules[:11]))
        self.assertEqual(modules[11]["lesson_count"], 13)
        self.assertEqual(sum(module["lesson_count"] for module in modules), 101)
        self.assertEqual(
            [module["quiz_count"] for module in modules],
            [4, 2, 3, 3, 3, 4, 3, 3, 2, 3, 3, 5],
        )
        self.assertEqual([module["challenge_count"] for module in modules[:6]], [2, 3, 3, 4, 3, 3])
        self.assertTrue(all(module["challenge_count"] == 10 for module in modules[6:11]))
        self.assertEqual(modules[11]["challenge_count"], 12)
        lesson_ids = [
            lesson["id"] for module in modules for lesson in module["lessons"]
        ]
        self.assertEqual(len(lesson_ids), len(set(lesson_ids)))
        self.assertTrue(all(lesson["locked"] for module in modules[6:] for lesson in module["lessons"]))
        quizzes = self.client.get("/api/quizzes", headers=self.headers).json()
        challenges = self.client.get("/api/challenges", headers=self.headers).json()
        self.assertEqual(len(quizzes), len(QUIZZES))
        self.assertEqual(len(challenges), len(CHALLENGES))
        self.assertTrue(all(item["locked"] for item in quizzes if item["module_id"] == modules[6]["id"]))
        self.assertTrue(all(item["locked"] for item in challenges if item["module_id"] == modules[6]["id"]))

    def test_startup_preserves_existing_lesson_data_without_runtime_ddl_or_writes(self):
        source = LESSONS[0]
        with SessionLocal() as db:
            lesson = db.get(Lesson, source[0])
            self.assertIsNotNone(lesson)
            lesson.title = "Custom Admin Title"
            db.commit()

        startup()

        with SessionLocal() as db:
            lesson = db.get(Lesson, source[0])
            self.assertEqual(lesson.title, "Custom Admin Title")
            lesson.title = source[1]
            db.commit()

    def test_registration_access_key_is_allowed_by_cross_origin_cors(self):
        response = self.client.options(
            "/api/subscription/registrations/test-registration",
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-kite-registration-key",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost")
        allowed_headers = response.headers.get("access-control-allow-headers", "").lower()
        self.assertIn("x-kite-registration-key", allowed_headers)

    def test_credentials_cors_does_not_allow_unlisted_origins(self):
        response = self.client.options(
            "/api/me",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "x-kite-csrf",
            },
        )
        self.assertNotEqual(
            response.headers.get("access-control-allow-origin"),
            "https://attacker.example",
        )

    def test_confirmed_production_frontend_origin_is_allowed_by_cors(self):
        response = self.client.options(
            "/api/me",
            headers={
                "Origin": "https://learnpythonwithkite.vercel.app",
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "x-kite-csrf",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "https://learnpythonwithkite.vercel.app",
        )
        self.assertEqual(
            response.headers.get("access-control-allow-credentials"),
            "true",
        )
        allowed_headers = response.headers.get("access-control-allow-headers", "").lower()
        self.assertIn("x-kite-csrf", allowed_headers)

    def test_hosted_runtime_rejects_localhost_cors_origin(self):
        hosted_config = {
            "RENDER": "true",
            "DATABASE_URL": "postgresql://kite_runtime:password@db.example.test/postgres?sslmode=require",
            "KITE_SECRET_KEY": "test-only-secret-key-with-at-least-32-characters",
            "SUPABASE_URL": "https://kite-project.supabase.co",
            "SUPABASE_ANON_KEY": "sb_publishable_test",
            "PUBLIC_APP_URL": "https://learnpythonwithkite.vercel.app",
            "PUBLIC_API_URL": "https://learnpythonwithkite.vercel.app",
            "KITE_VERCEL_PROXY": "true",
            "KITE_PROXY_SECRET": "proxy-secret-for-tests-that-is-at-least-32",
            "KITE_CORS_ORIGINS": "https://learnpythonwithkite.vercel.app,https://localhost",
            "KITE_COOKIE_SECURE": "true",
            "KITE_COOKIE_SAMESITE": "lax",
            "KITE_ALLOW_LOCAL_AUTH": "false",
        }
        with patch.dict(os.environ, hosted_config, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Localhost origins are restricted"):
                validate_runtime_configuration()

    def test_health_and_readiness_endpoints(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})

        ready = self.client.get("/health/ready")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json(), {"status": "ready", "database": "connected"})

    def test_serverless_postgres_pool_is_compatible_with_transaction_pooling(self):
        options = database_engine_options(
            "postgresql+psycopg://user:password@pooler.example.test/database",
            serverless=True,
        )
        self.assertIsNone(options["connect_args"]["prepare_threshold"])
        self.assertEqual(options["pool_size"], 1)
        self.assertEqual(options["max_overflow"], 0)
        self.assertEqual(
            database_engine_options("sqlite:///kite.db", serverless=True)["connect_args"],
            {"check_same_thread": False},
        )

    def test_new_curriculum_examples_and_assessments_are_complete(self):
        required_sections = (
            "Learning objectives\n", "Core concept\n", "Why it matters\n", "How it works\n",
            "Syntax\n", "Worked example\n", "Step-by-step walkthrough\n", "Expected output\n",
            "Understand the result\n", "Practical application\n", "Common mistakes\n",
            "Practice exercises\n", "Knowledge check\n",
        )
        for lesson in LESSONS:
            self.assertGreater(len(lesson[3]), 1800, lesson[0])
            for section in required_sections:
                self.assertIn(section, lesson[3], lesson[0])
            example = lesson[3].split("Worked example\n", 1)[1].split(
                "\n\nStep-by-step walkthrough\n", 1,
            )[0]
            output = io.StringIO()
            disabled_logging = logging.root.manager.disable
            logging.disable(logging.NOTSET)
            try:
                with contextlib.redirect_stdout(output):
                    exec(compile(example, lesson[0], "exec"), {})
            finally:
                logging.disable(disabled_logging)
            expected = lesson[3].split("\n\nExpected output\n", 1)[1].split(
                "\n\nUnderstand the result\n", 1,
            )[0]
            self.assertEqual(output.getvalue().rstrip("\n"), expected, lesson[0])
        for quiz in NEW_QUIZ_DATA.values():
            self.assertEqual(len(quiz["questions"]), 5)
            for question in quiz["questions"]:
                self.assertTrue(question["explanation"])
                self.assertIn(question["type"], {
                    "multiple_choice", "true_false", "output_prediction",
                    "code_interpretation", "debugging", "error_identification",
                })
                self.assertGreaterEqual(question["answer"], 0)
                self.assertLess(question["answer"], len(question["options"]))
        self.assertIn("true_false", {
            question.get("type", "multiple_choice")
            for questions in QUIZZES.values() for question in questions
        })
        self.assertIn("output_prediction", {
            question["type"] for quiz in NEW_QUIZ_DATA.values() for question in quiz["questions"]
        })
        for quiz in QUIZZES.values():
            self.assertTrue(all(question.get("explanation") for question in quiz))
        self.assertEqual(len(QUIZZES), 25)
        self.assertEqual(len(CHALLENGES), 80)
        for challenge in NEW_CHALLENGE_DATA.values():
            self.assertTrue(challenge["description"])
            self.assertTrue(challenge["hint"])
            self.assertGreaterEqual(len(challenge["tests"]), 1)

    def test_repeated_outputs_have_topic_specific_result_explanations(self):
        lesson_by_id = {lesson[0]: lesson for lesson in LESSONS}
        first = lesson_by_id["python-functions"][3].split(
            "\n\nUnderstand the result\n", 1,
        )[1].split("\n\nPractical application\n", 1)[0]
        second = lesson_by_id["python-errors-try-except"][3].split(
            "\n\nUnderstand the result\n", 1,
        )[1].split("\n\nPractical application\n", 1)[0]
        self.assertNotEqual(first, second)
        self.assertIn("define, call, and return from functions", first)
        self.assertIn("handle expected exceptions", second)

    def test_new_lesson_completion_updates_persisted_progress(self):
        with patch("backend.app.premium_entitled", return_value=True):
            completed = self.client.post(
                "/api/lessons/python-strings-literals/complete", headers=self.headers,
            )
        self.assertEqual(completed.status_code, 200, completed.text)
        progress = self.client.get("/api/progress", headers=self.headers).json()
        self.assertIn("python-strings-literals", progress["completed_lessons"])
        self.assertEqual(progress["xp"], 30)
        self.assertEqual(progress["total_lessons"], len(LESSONS))

    def test_new_quiz_submission_returns_explanations_and_persists_score(self):
        with patch("backend.app.premium_entitled", return_value=True):
            quiz = self.client.get("/api/quizzes/strings-reading", headers=self.headers)
            self.assertEqual(quiz.status_code, 200, quiz.text)
            self.assertEqual(len(quiz.json()["questions"]), 5)
            result = self.client.post(
                "/api/quizzes/strings-reading/submit",
                headers=self.headers,
                json={"answers": [0, 0, 1, 0, 1]},
            )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["score"], 5)
        self.assertTrue(all(item["correct"] for item in result.json()["feedback"]))
        self.assertTrue(all(item["explanation"] for item in result.json()["feedback"]))
        with patch("backend.app.premium_entitled", return_value=True):
            progress = self.client.get("/api/progress", headers=self.headers).json()
            modules = self.client.get("/api/modules", headers=self.headers).json()
        self.assertEqual(progress["quiz_count"], 1)
        self.assertEqual(progress["quiz_attempt_count"], 1)
        self.assertEqual(progress["average_quiz_score"], 100)
        module = next(
            item for item in modules
            if item["id"] == next(
                quiz["module_id"] for quiz in QUIZ_METADATA if quiz["id"] == "strings-reading"
            )
        )
        self.assertEqual(module["completed_quizzes"], 1)
        self.assertEqual(module["quiz_attempt_count"], 1)
        self.assertEqual(module["average_quiz_score"], 100)
        strings_quiz = next(item for item in module["quizzes"] if item["id"] == "strings-reading")
        self.assertTrue(strings_quiz["completed"])
        self.assertEqual(strings_quiz["attempt_count"], 1)

    def test_guest_can_submit_free_quizzes_without_account_or_server_history(self):
        catalog = self.client.get("/api/quizzes")
        self.assertEqual(catalog.status_code, 200, catalog.text)
        quiz = self.client.get("/api/quizzes/python-basics")
        self.assertEqual(quiz.status_code, 200, quiz.text)
        self.assertIn("type", quiz.json()["questions"][0])
        self.assertNotIn("answer", quiz.json()["questions"][0])
        self.assertNotIn("explanation", quiz.json()["questions"][0])

        locked = self.client.get("/api/quizzes/advanced-iteration")
        self.assertEqual(locked.status_code, 402)
        locked_submit = self.client.post(
            "/api/quizzes/advanced-iteration/submit",
            json={"answers": [0, 0, 0, 0, 0]},
        )
        self.assertEqual(locked_submit.status_code, 402)

        answers = [0, 1, 1, 2, 1, 1, 2, 2, 1, 1]
        result = self.client.post("/api/quizzes/python-basics/submit", json={"answers": answers})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["guest"])
        self.assertEqual(result.json()["score"], 10)
        self.assertEqual(result.json()["percentage"], 100)
        self.assertTrue(result.json()["passed"])
        self.assertEqual(result.json()["xp_awarded"], 0)
        self.assertIsNone(result.json()["total_xp"])
        after = self.client.get("/api/quizzes").json()
        basics = next(item for item in after if item["id"] == "python-basics")
        self.assertFalse(basics["completed"])
        self.assertEqual(basics["attempts"], [])

        invalid = self.client.post(
            "/api/quizzes/python-basics/submit",
            json={"answers": [99] + answers[1:]},
        )
        self.assertEqual(invalid.status_code, 400)

    def test_quiz_catalog_maps_topics_to_their_module_lessons_and_challenges(self):
        catalog = self.client.get("/api/quizzes").json()
        modules = self.client.get("/api/modules").json()
        challenges = self.client.get("/api/challenges").json()
        lessons_by_module = {
            module["id"]: {lesson["id"] for lesson in module["lessons"]}
            for module in modules
        }
        challenges_by_id = {challenge["id"]: challenge for challenge in challenges}
        self.assertEqual(len(catalog), len(QUIZ_METADATA))
        for quiz in catalog:
            self.assertTrue(quiz["topic"])
            self.assertTrue(quiz["difficulty"])
            if not quiz["locked"]:
                detail = self.client.get("/api/quizzes/" + quiz["id"])
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(quiz["question_count"], len(detail.json()["questions"]))
            self.assertTrue(set(quiz["lesson_ids"]).issubset(lessons_by_module[quiz["module_id"]]))
            challenge = challenges_by_id.get(quiz["practice_challenge_id"])
            self.assertIsNotNone(challenge)
            self.assertEqual(challenge["module_id"], quiz["module_id"])

    def test_every_lesson_has_a_contextual_assessment_without_changing_quiz_catalog(self):
        catalog = self.client.get("/api/quizzes").json()
        self.assertEqual(len(catalog), 25)
        self.assertFalse(any(item.get("lesson_only") for item in catalog))
        lessons = self.client.get("/api/lessons").json()
        self.assertEqual(len(lessons), 101)

        for lesson in lessons:
            response = self.client.get(
                "/api/quizzes", params={"lesson_id": lesson["id"]},
            )
            self.assertEqual(response.status_code, 200, response.text)
            assessments = response.json()
            self.assertTrue(assessments, lesson["id"])
            for assessment in assessments:
                self.assertIn(lesson["id"], assessment["lesson_ids"])
                self.assertEqual(assessment["module_id"], lesson["module_id"])
                if assessment["locked"]:
                    detail = self.client.get(
                        "/api/quizzes/" + assessment["id"],
                        params={"lesson_id": lesson["id"]},
                    )
                    self.assertEqual(detail.status_code, 402, detail.text)
                    continue

                detail = self.client.get(
                    "/api/quizzes/" + assessment["id"],
                    params={"lesson_id": lesson["id"]},
                )
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(
                    len(detail.json()["questions"]), assessment["question_count"],
                )
                self.assertNotIn("answer", detail.json()["questions"][0])

        self.assertEqual(
            self.client.get("/api/quizzes/lesson-first-errors").status_code, 404,
        )
        self.assertEqual(
            self.client.post(
                "/api/quizzes/lesson-first-errors/submit",
                json={"answers": [0, 0, 0, 1]},
            ).status_code,
            404,
        )

    def test_lesson_only_quiz_attempt_persists_against_its_lesson(self):
        lesson_id = "python-foundations-first-errors"
        quiz_id = "lesson-first-errors"
        result = self.client.post(
            "/api/quizzes/" + quiz_id + "/submit",
            params={"lesson_id": lesson_id},
            headers=self.headers,
            json={"answers": [0, 0, 0, 1]},
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["score"], 4)
        self.assertTrue(result.json()["passed"])
        self.assertEqual(result.json()["review_lesson_ids"], [lesson_id])

        history = self.client.get(
            "/api/quizzes", params={"lesson_id": lesson_id}, headers=self.headers,
        )
        assessment = next(item for item in history.json() if item["id"] == quiz_id)
        self.assertEqual(assessment["attempt_count"], 1)
        self.assertEqual(assessment["best_score"], 4)
        self.assertEqual(len(assessment["attempts"]), 1)
        self.assertFalse(any(
            item["id"] == quiz_id for item in self.client.get("/api/quizzes").json()
        ))
        progress = self.client.get("/api/progress", headers=self.headers).json()
        self.assertEqual(progress["quiz_count"], 1)
        self.assertEqual(progress["quiz_attempt_count"], 1)
        module = next(
            item for item in self.client.get("/api/modules", headers=self.headers).json()
            if item["id"] == "python-foundations"
        )
        self.assertEqual(module["completed_quizzes"], 1)
        self.assertEqual(module["quiz_attempt_count"], 1)
        self.assertEqual(module["completed_lessons"], 0)
        self.assertTrue(any(item["id"] == quiz_id for item in module["quizzes"]))

    def test_quiz_attempt_history_is_persisted_per_account(self):
        answers = [0, 1, 1, 2, 1, 1, 2, 2, 1, 1]
        for _ in range(2):
            result = self.client.post(
                "/api/quizzes/python-basics/submit",
                headers=self.headers,
                json={"answers": answers},
            )
            self.assertEqual(result.status_code, 200, result.text)
        quiz = next(
            item for item in self.client.get("/api/quizzes", headers=self.headers).json()
            if item["id"] == "python-basics"
        )
        self.assertEqual(quiz["attempt_count"], 2)
        self.assertEqual(len(quiz["attempts"]), 2)
        self.assertEqual(quiz["best_score"], 10)
        with SessionLocal() as db:
            user = db.query(User).filter(User.email == self.email).one()
            self.assertEqual(
                db.query(QuizAttempt).filter(
                    QuizAttempt.user_id == user.id,
                    QuizAttempt.quiz_id == "python-basics",
                ).count(),
                2,
            )

        other_email = f"other-{uuid.uuid4().hex}@example.com"
        other = self.client.post("/api/auth/register", json={
            "name": "Other Learner",
            "email": other_email,
            "password": "secure-example-password",
        })
        other_headers = {"Authorization": "Bearer " + self.issue_token(other_email)}
        self.client.cookies.clear()
        other_quiz = next(
            item for item in self.client.get("/api/quizzes", headers=other_headers).json()
            if item["id"] == "python-basics"
        )
        self.assertEqual(other_quiz["attempt_count"], 0)
        self.assertEqual(other_quiz["attempts"], [])

    def test_quiz_submission_idempotency_replays_saved_result_without_duplicate_attempt(self):
        answers = [0, 1, 1, 2, 1, 1, 2, 2, 1, 1]
        payload = {"answers": answers, "submission_id": uuid.uuid4().hex}
        first = self.client.post(
            "/api/quizzes/python-basics/submit", headers=self.headers, json=payload,
        )
        replay = self.client.post(
            "/api/quizzes/python-basics/submit", headers=self.headers, json=payload,
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(first.json()["attempt_number"], 1)
        self.assertEqual(first.json()["submission_id"], payload["submission_id"])

        changed_answers = self.client.post(
            "/api/quizzes/python-basics/submit",
            headers=self.headers,
            json={"answers": [-1] + answers[1:], "submission_id": payload["submission_id"]},
        )
        self.assertEqual(changed_answers.status_code, 409)

        quiz = next(
            item for item in self.client.get("/api/quizzes", headers=self.headers).json()
            if item["id"] == "python-basics"
        )
        self.assertEqual(quiz["attempt_count"], 1)
        notices = [
            item for item in self.client.get("/api/notifications", headers=self.headers).json()
            if item["category"] == "learning" and item["target_route"] == "quiz"
        ]
        self.assertEqual(len(notices), 1)

    def test_new_challenge_uses_runner_tests_and_awards_xp_once(self):
        with patch("backend.app.premium_entitled", return_value=True), \
                patch("backend.app.call_runner", new_callable=AsyncMock) as run:
            run.side_effect = [
                {"stdout": "Kite learns Python\n", "stderr": "", "error": None},
                {"stdout": "one two\n", "stderr": "", "error": None},
            ]
            result = self.client.post(
                "/api/challenges/python-strings-normalize-space/submit",
                headers=self.headers,
                json={"code": 'print(" ".join(text.split()))'},
            )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["passed"])
        self.assertEqual(result.json()["xp_awarded"], 100)
        self.assertEqual(run.await_count, 2)

    def test_leaderboard_periods_and_invalid_period(self):
        for period in ("week", "month", "all"):
            result = self.client.get(f"/api/leaderboard?period={period}", headers=self.headers)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["period"], period)
            self.assertIn("rank", result.json()["current"])
        invalid = self.client.get("/api/leaderboard?period=year", headers=self.headers)
        self.assertEqual(invalid.status_code, 400)

    def test_guest_can_view_leaderboard_without_personal_rank(self):
        response = self.client.get("/api/leaderboard?period=week")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["period"], "week")
        self.assertIsNone(response.json()["current"])
        self.assertIsInstance(response.json()["leaders"], list)

    def test_python_pro_and_top_ten_achievements_use_saved_activity(self):
        with SessionLocal() as db:
            user = db.query(User).filter(User.email == self.email).one()
            for lesson in LESSONS:
                db.add(LessonProgress(user_id=user.id, lesson_id=lesson[0], completed=True, score=100))
            for challenge_id in CHALLENGES:
                db.add(Activity(
                    user_id=user.id, kind="challenge", item_id=challenge_id, xp=10000,
                ))
            db.commit()
        result = self.client.get("/api/progress", headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("Python Pro", result.json()["achievements"])
        self.assertIn("Top 10", result.json()["achievements"])

    def test_bug_hunter_tracks_resolved_playground_errors(self):
        error_result = {"stdout": "", "stderr": "Traceback", "error": "Code exited with an error."}
        success_result = {"stdout": "2\n", "stderr": "", "error": None}
        with patch("backend.app.call_runner", new_callable=AsyncMock) as run:
            run.side_effect = [result for _ in range(10) for result in (error_result, success_result)]
            for _ in range(10):
                failed = self.client.post("/api/execution/run", headers=self.headers, json={"code": "bad code"})
                fixed = self.client.post("/api/execution/run", headers=self.headers, json={"code": "print(2)"})
                self.assertEqual(failed.status_code, 200, failed.text)
                self.assertEqual(fixed.status_code, 200, fixed.text)
        with SessionLocal() as db:
            fixed_count = db.query(PlaygroundError).filter(
                PlaygroundError.user_id == self.client.get("/api/me", headers=self.headers).json()["id"],
                PlaygroundError.fixed_at.is_not(None),
            ).count()
        self.assertEqual(fixed_count, 10)
        progress = self.client.get("/api/progress", headers=self.headers).json()
        self.assertIn("Bug Hunter", progress["achievements"])

    def test_subscription_verification_and_refresh_update_entitlement(self):
        product_id = "kite_premium_monthly"
        purchase_token = "test-google-play-purchase-token"
        expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        active = {
            "state": "SUBSCRIPTION_STATE_ACTIVE",
            "expires_at": expires_at,
            "auto_renew": True,
        }
        with patch.dict(os.environ, {"KITE_PLAY_MONTHLY_PRODUCT_ID": product_id}), \
                patch("backend.app.fetch_google_purchase", return_value=active) as verify:
            response = self.client.post(
                "/api/subscription/google/verify",
                headers=self.headers,
                json={"product_id": product_id, "purchase_token": purchase_token},
            )
            duplicate = self.client.post(
                "/api/subscription/google/verify",
                headers=self.headers,
                json={"product_id": product_id, "purchase_token": purchase_token},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertTrue(response.json()["premium"])
        self.assertEqual(verify.call_count, 2)
        verify.assert_called_with(product_id, purchase_token)
        confirmed = [
            item for item in self.client.get("/api/notifications", headers=self.headers).json()
            if item["category"] == "subscription"
        ]
        self.assertEqual(len(confirmed), 1)
        self.assertTrue(self.client.get("/api/subscription", headers=self.headers).json()["premium"])
        lessons = {lesson["id"]: lesson for lesson in self.client.get("/api/lessons", headers=self.headers).json()}
        self.assertFalse(lessons["python-loops"]["locked"])
        unlocked_quizzes = {
            item["id"]: item for item in self.client.get("/api/quizzes", headers=self.headers).json()
        }
        self.assertFalse(unlocked_quizzes["strings-reading"]["locked"])
        self.assertEqual(
            self.client.get("/api/quizzes/strings-reading", headers=self.headers).status_code, 200,
        )

        expired = {
            "state": "SUBSCRIPTION_STATE_EXPIRED",
            "expires_at": datetime.now(timezone.utc) - timedelta(days=1),
            "auto_renew": False,
        }
        with patch.dict(os.environ, {"KITE_PLAY_MONTHLY_PRODUCT_ID": product_id}), \
                patch("backend.app.fetch_google_purchase", return_value=expired):
            refreshed = self.client.post("/api/subscription/refresh", headers=self.headers)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertFalse(refreshed.json()["premium"])
        relocked_quizzes = {
            item["id"]: item for item in self.client.get("/api/quizzes", headers=self.headers).json()
        }
        self.assertTrue(relocked_quizzes["strings-reading"]["locked"])
        self.assertEqual(
            self.client.get("/api/quizzes/strings-reading", headers=self.headers).status_code, 402,
        )
        self.assertTrue(any(
            item["title"] == "Kite Premium has expired"
            for item in self.client.get("/api/notifications", headers=self.headers).json()
        ))

    def test_registration_rejects_password_shorter_than_eight_characters(self):
        response = self.client.post("/api/auth/register", json={
            "name": "Short Password",
            "email": f"short-{uuid.uuid4().hex}@example.com",
            "password": "short7",
        })
        self.assertEqual(response.status_code, 422)

    def test_registration_rate_limit_returns_retry_after(self):
        email = f"limited-{uuid.uuid4().hex}@example.com"
        payload = {"name": "Rate Limited", "email": email, "password": "secure-example-password"}
        for _ in range(5):
            self.client.post("/api/auth/register", json=payload)
        limited = self.client.post("/api/auth/register", json=payload)
        self.assertEqual(limited.status_code, 429, limited.text)
        self.assertGreater(int(limited.headers["Retry-After"]), 0)

    def test_unverified_supabase_registration_does_not_create_local_account(self):
        email = f"verify-{uuid.uuid4().hex}@example.com"
        provider_user = {"id": "unverified-provider-user", "email": email}
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
        }), patch("backend.app.supabase_request", return_value={"user": provider_user}):
            response = self.client.post("/api/auth/register", json={
                "name": "Needs Verification",
                "email": email,
                "password": "secure-example-password",
            })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("access_token", response.json())
        with SessionLocal() as db:
            self.assertIsNone(db.query(User).filter_by(email=email).one_or_none())

    def test_unverified_provider_session_cannot_be_linked(self):
        email = f"unverified-{uuid.uuid4().hex}@example.com"
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
        }), patch("backend.app.supabase_request", return_value={"id": "unverified-session", "email": email}):
            response = self.client.post("/api/auth/session", json={"access_token": "provider-token"})
        self.assertEqual(response.status_code, 403, response.text)
        with SessionLocal() as db:
            self.assertIsNone(db.query(User).filter_by(email=email).one_or_none())

    def test_verified_provider_session_links_once_without_replacing_identity(self):
        env = {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
        }
        verified_user = {
            "id": "verified-provider-user",
            "email": self.email,
            "email_confirmed_at": "2025-01-01T00:00:00Z",
            "user_metadata": {"name": "Kite Learner"},
        }
        with patch.dict(os.environ, env), patch(
            "backend.app.supabase_request", return_value=verified_user,
        ):
            linked = self.client.post("/api/auth/session", json={"access_token": "provider-token"})
        self.assertEqual(linked.status_code, 200, linked.text)
        self.assertNotIn("access_token", linked.json())
        self.assertEqual(linked.json()["user"]["email"], self.email)

        conflicting_user = dict(verified_user, id="different-provider-user")
        with patch.dict(os.environ, env), patch(
            "backend.app.supabase_request", return_value=conflicting_user,
        ):
            conflict = self.client.post("/api/auth/session", json={"access_token": "other-provider-token"})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertNotIn(self.email, conflict.text)

    def test_verification_resend_uses_provider_and_generic_response(self):
        env = {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
            "PUBLIC_APP_URL": "https://kite.example.app",
        }
        with patch.dict(os.environ, env), patch("backend.app.supabase_request", return_value={}) as request:
            response = self.client.post("/api/auth/resend-verification", json={"email": self.email})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["message"],
            "If the address needs verification, an email will be sent shortly.",
        )
        self.assertEqual(request.call_args.args[0], "/resend?redirect_to=https%3A%2F%2Fkite.example.app%2F")
        self.assertEqual(request.call_args.kwargs["data"]["type"], "signup")

    def test_supabase_password_reset_uses_reset_redirect_and_pkce_challenge(self):
        env = {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
            "PUBLIC_APP_URL": "https://learnpythonwithkite.vercel.app",
        }
        challenge = "A" * 43
        with patch.dict(os.environ, env), patch("backend.app.supabase_request", return_value={}) as request:
            response = self.client.post("/api/auth/password-reset", json={
                "email": self.email,
                "code_challenge": challenge,
                "code_challenge_method": "s256",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            request.call_args.args[0],
            "/recover?redirect_to=https%3A%2F%2Flearnpythonwithkite.vercel.app%2F%23reset",
        )
        self.assertEqual(request.call_args.kwargs["data"]["email"], self.email)
        self.assertEqual(request.call_args.kwargs["data"]["code_challenge"], challenge)
        self.assertEqual(request.call_args.kwargs["data"]["code_challenge_method"], "s256")

    def test_supabase_recovery_code_exchange_returns_reset_token(self):
        env = {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
        }
        provider_result = {
            "access_token": "recovery-access-token",
            "refresh_token": "recovery-refresh-token",
            "user": {"id": "supabase-recovery-user", "email": self.email},
        }
        with patch.dict(os.environ, env), patch(
            "backend.app.supabase_request", return_value=provider_result
        ) as request:
            response = self.client.post("/api/auth/recovery/exchange", json={
                "code": "recovery-code",
                "code_verifier": "V" * 43,
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["access_token"], "recovery-access-token")
        self.assertEqual(request.call_args.args[0], "/token?grant_type=pkce")
        self.assertEqual(request.call_args.kwargs["data"]["auth_code"], "recovery-code")

    def test_local_password_recovery_fails_closed_without_smtp(self):
        with patch.dict(os.environ, {
            "SUPABASE_URL": "",
            "SUPABASE_ANON_KEY": "",
            "SMTP_HOST": "",
        }):
            response = self.client.post("/api/auth/password-reset", json={"email": self.email})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(self.email, response.text)

    def test_local_password_reset_token_is_one_time_and_changes_password(self):
        env = {
            "SUPABASE_URL": "",
            "SUPABASE_ANON_KEY": "",
            "SMTP_HOST": "smtp.example",
        }
        with patch.dict(os.environ, env), patch("backend.app.send_reset_email") as send_email:
            requested = self.client.post("/api/auth/password-reset", json={"email": self.email})
            self.assertEqual(requested.status_code, 200, requested.text)
            reset_token = send_email.call_args.args[1]
            new_password = "different-secure-Password-92!"
            completed = self.client.post("/api/auth/password-reset/complete", json={
                "token": reset_token,
                "password": new_password,
            })
            self.assertEqual(completed.status_code, 200, completed.text)
            reused = self.client.post("/api/auth/password-reset/complete", json={
                "token": reset_token,
                "password": new_password,
            })
            self.assertEqual(reused.status_code, 400)
            old_login = self.client.post("/api/auth/login", json={
                "email": self.email,
                "password": "secure-example-password",
            })
            new_login = self.client.post("/api/auth/login", json={
                "email": self.email,
                "password": new_password,
            })
        self.assertEqual(old_login.status_code, 401)
        self.assertEqual(new_login.status_code, 200, new_login.text)

    def test_lesson_completion_persists_and_awards_xp_once(self):
        first = self.client.post("/api/lessons/python-variables/complete", headers=self.headers)
        second = self.client.post("/api/lessons/python-variables/complete", headers=self.headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["xp"], 20)
        self.assertEqual(second.json()["xp"], 20)
        progress = self.client.get("/api/progress", headers=self.headers).json()
        self.assertIn("python-variables", progress["completed_lessons"])

    def test_guest_lesson_progress_import_is_free_only_and_idempotent(self):
        payload = {
            "started_lessons": ["python-types"],
            "completed_lessons": ["python-variables"],
        }
        imported = self.client.post("/api/progress/import", headers=self.headers, json=payload)
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()["imported_started"], 1)
        self.assertEqual(imported.json()["imported_completed"], 1)
        self.assertEqual(imported.json()["xp_awarded"], 20)

        lessons = {lesson["id"]: lesson for lesson in self.client.get(
            "/api/lessons", headers=self.headers,
        ).json()}
        self.assertTrue(lessons["python-variables"]["completed"])
        self.assertTrue(lessons["python-types"]["started"])
        progress = self.client.get("/api/progress", headers=self.headers).json()
        self.assertIn("python-variables", progress["completed_lessons"])
        self.assertIn("python-types", progress["started_lessons"])

        repeated = self.client.post("/api/progress/import", headers=self.headers, json=payload)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["imported_started"], 0)
        self.assertEqual(repeated.json()["imported_completed"], 0)
        self.assertEqual(repeated.json()["xp_awarded"], 0)

        premium = self.client.post(
            "/api/progress/import", headers=self.headers,
            json={"completed_lessons": ["python-loops"]},
        )
        self.assertEqual(premium.status_code, 402)

    def test_guest_progress_import_requires_authentication(self):
        response = self.client.post(
            "/api/progress/import",
            json={"completed_lessons": ["python-variables"]},
        )
        self.assertEqual(response.status_code, 401)

    def test_premium_content_is_locked_for_free_account(self):
        listing = self.client.get("/api/lessons", headers=self.headers)
        premium = next(item for item in listing.json() if item["id"] == "python-loops")
        self.assertTrue(premium["locked"])
        locked = self.client.get("/api/lessons/python-loops", headers=self.headers)
        self.assertEqual(locked.status_code, 402)

    def test_authenticated_runner_is_unavailable_without_runner_service(self):
        response = self.client.post("/api/execution/run", headers=self.headers, json={"code": "print('hi')"})
        self.assertEqual(response.status_code, 503)

    def test_empty_playground_code_is_rejected(self):
        response = self.client.post(
            "/api/execution/run", headers=self.headers, json={"code": ""},
        )
        self.assertEqual(response.status_code, 422)

    def test_guest_can_run_code_without_an_account(self):
        code = 'for number in range(1, 4):\n    print(number)'
        execution_id = uuid.uuid4().hex
        guest_token = uuid.uuid4().hex + uuid.uuid4().hex
        with patch("backend.app.call_runner", new_callable=AsyncMock) as run:
            run.return_value = {"stdout": "1\n2\n3\n", "stderr": "", "error": None}
            response = self.client.post("/api/execution/run", json={
                "code": code,
                "execution_id": execution_id,
                "guest_token": guest_token,
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"stdout": "1\n2\n3\n", "stderr": "", "error": None})
        run.assert_awaited_once_with(code, execution_id=execution_id)
        with SessionLocal() as db:
            self.assertIsNone(db.get(GuestPlaygroundExecution, execution_id))

    def test_invalid_session_is_treated_as_guest_for_playground_execution(self):
        execution_id = uuid.uuid4().hex
        guest_token = uuid.uuid4().hex + uuid.uuid4().hex
        with patch("backend.app.call_runner", new_callable=AsyncMock) as run:
            run.return_value = {"stdout": "guest output\n", "stderr": "", "error": None}
            response = self.client.post(
                "/api/execution/run",
                headers={"Authorization": "Bearer expired-or-invalid"},
                json={
                    "code": 'print("guest output")',
                    "execution_id": execution_id,
                    "guest_token": guest_token,
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["stdout"], "guest output\n")
        run.assert_awaited_once_with('print("guest output")', execution_id=execution_id)

    def test_guest_execution_cancellation_requires_its_capability_token(self):
        execution_id = uuid.uuid4().hex
        guest_token = uuid.uuid4().hex + uuid.uuid4().hex
        with SessionLocal() as db:
            db.add(GuestPlaygroundExecution(
                execution_id=execution_id,
                token_hash=hashlib.sha256(guest_token.encode()).hexdigest(),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            ))
            db.commit()
        with patch("backend.app.cancel_runner_execution", new_callable=AsyncMock) as cancel:
            cancel.return_value = {"cancelled": True}
            rejected = self.client.post("/api/execution/cancel", json={
                "execution_id": execution_id,
                "guest_token": uuid.uuid4().hex + uuid.uuid4().hex,
            })
            accepted = self.client.post("/api/execution/cancel", json={
                "execution_id": execution_id,
                "guest_token": guest_token,
            })
        self.assertEqual(rejected.status_code, 404)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertTrue(accepted.json()["cancelled"])
        cancel.assert_awaited_once_with(execution_id)
        with SessionLocal() as db:
            db.delete(db.get(GuestPlaygroundExecution, execution_id))
            db.commit()

    def test_execution_cancellation_is_limited_to_the_owning_user(self):
        other_email = f"other-{uuid.uuid4().hex}@example.com"
        other = self.client.post("/api/auth/register", json={
            "name": "Other Learner",
            "email": other_email,
            "password": "secure-example-password",
        })
        self.assertEqual(other.status_code, 201, other.text)
        execution_id = uuid.uuid4().hex
        other_id = other.json()["user"]["id"]
        with SessionLocal() as db:
            db.add(PlaygroundExecution(execution_id=execution_id, user_id=other_id))
            db.commit()
        with patch("backend.app.cancel_runner_execution", new_callable=AsyncMock) as cancel:
            cancel.return_value = {"cancelled": True}
            rejected = self.client.post(
                "/api/execution/cancel",
                headers=self.headers,
                json={"execution_id": execution_id},
            )
            accepted = self.client.post(
                "/api/execution/cancel",
                headers={"Authorization": "Bearer " + self.issue_token(other_email)},
                json={"execution_id": execution_id},
            )
        self.assertEqual(rejected.status_code, 404)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        cancel.assert_awaited_once_with(execution_id)
        with SessionLocal() as db:
            db.delete(db.get(PlaygroundExecution, execution_id))
            db.commit()

    def test_cancelled_execution_does_not_count_as_a_playground_error(self):
        execution_id = uuid.uuid4().hex
        with patch("backend.app.call_runner", new_callable=AsyncMock) as run:
            run.return_value = {"stdout": "", "stderr": "", "error": "Execution cancelled."}
            response = self.client.post(
                "/api/execution/run",
                headers=self.headers,
                json={"code": "while True: pass", "execution_id": execution_id},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["error"], "Execution cancelled.")
        run.assert_awaited_once_with("while True: pass", execution_id=execution_id)
        with SessionLocal() as db:
            self.assertIsNone(db.get(PlaygroundExecution, execution_id))
            self.assertEqual(db.query(PlaygroundError).filter(
                PlaygroundError.user_id == self.client.get("/api/me", headers=self.headers).json()["id"],
                PlaygroundError.fixed_at.is_(None),
            ).count(), 0)

    def test_unauthenticated_progress_is_rejected(self):
        response = self.client.get("/api/progress")
        self.assertEqual(response.status_code, 401)

    def test_guest_progress_inputs_are_public_without_exposing_account_progress(self):
        modules = self.client.get("/api/modules")
        self.assertEqual(modules.status_code, 200, modules.text)
        self.assertEqual(len(modules.json()), 12)

        lesson = self.client.get("/api/lessons/python-variables")
        self.assertEqual(lesson.status_code, 200, lesson.text)
        self.assertEqual(lesson.json()["xp_reward"], 20)
        self.assertEqual(self.client.get("/api/progress").status_code, 401)

    def test_subscription_plans_show_requested_prices_and_annual_value(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/api/plans")
        self.assertEqual(response.status_code, 200)
        plans = {plan["id"]: plan for plan in response.json()["plans"]}
        self.assertEqual(plans["premium_monthly"]["price"], "K19.99")
        self.assertEqual(plans["premium_monthly"]["period"], "month")
        self.assertEqual(plans["premium_monthly"]["product_id"], "kite_premium_monthly")
        self.assertEqual(plans["premium_annual"]["price"], "K99.99")
        self.assertEqual(plans["premium_annual"]["period"], "year")
        self.assertEqual(plans["premium_annual"]["product_id"], "kite_premium_annual")
        self.assertEqual(plans["premium_annual"]["savings"], "K139.89 annually")
        self.assertEqual(plans["premium_annual"]["percentage_savings"], "58.3%")
        self.assertTrue(plans["premium_annual"]["best_value"])

    def test_zambian_mobile_number_normalization_and_validation(self):
        for supplied in ("+260 97 123 4567", "097-123-4567", "00260971234567"):
            with self.subTest(supplied=supplied):
                self.assertEqual(normalize_zambian_phone(supplied), "+260971234567")
        for supplied in ("+260 97 123", "+260 12 123 4567", "+260 97abc1234567"):
            with self.subTest(supplied=supplied):
                with self.assertRaises(ValueError):
                    normalize_zambian_phone(supplied)

    def test_guest_subscription_registration_persists_encrypted_phone_and_is_idempotent(self):
        payload = {
            "plan_id": "premium_monthly",
            "phone_number": "097 123 4567",
            "idempotency_key": str(uuid.uuid4()),
            "access_key": secrets.token_urlsafe(32),
        }
        created = self.client.post("/api/subscription/registrations", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(body["subscription_status"], "PENDING_PAYMENT")
        self.assertEqual(body["payment"]["status"], "NOT_INITIATED")
        self.assertEqual(body["payment"]["amount"], "19.99")
        self.assertFalse(body["premium_active"])
        self.assertEqual(body["phone_number"], "••••4567")
        self.assertIn("not active", body["message"])

        repeated = self.client.post("/api/subscription/registrations", json=payload)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["registration_id"], body["registration_id"])
        mismatched = {**payload, "phone_number": "+260 96 765 4321"}
        self.assertEqual(
            self.client.post("/api/subscription/registrations", json=mismatched).status_code,
            409,
        )
        self.assertEqual(
            self.client.get("/api/subscription/registrations/" + body["registration_id"]).status_code,
            404,
        )
        fetched = self.client.get(
            "/api/subscription/registrations/" + body["registration_id"],
            headers={"X-Kite-Registration-Key": payload["access_key"]},
        )
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json()["payment"]["status"], "NOT_INITIATED")

        with SessionLocal() as db:
            registration = db.get(SubscriptionRegistration, body["registration_id"])
            self.assertEqual(decrypt_subscription_phone(registration.phone_ciphertext), "+260971234567")
            self.assertNotIn("+260971234567", registration.phone_ciphertext)
            self.assertEqual(
                db.query(PaymentTransaction).filter_by(registration_id=registration.id).count(),
                1,
            )
            transaction = db.query(PaymentTransaction).filter_by(registration_id=registration.id).one()
            self.assertEqual(transaction.status, "NOT_INITIATED")
            self.assertEqual(db.query(PaymentAttempt).filter_by(transaction_id=transaction.id).count(), 0)

    def test_subscription_registration_rejects_invalid_numbers_without_writing(self):
        with SessionLocal() as db:
            registration_count = db.query(SubscriptionRegistration).count()
        for phone in ("+260 97 123", "+260 12 123 4567", "+260 97abc1234567"):
            with self.subTest(phone=phone):
                response = self.client.post("/api/subscription/registrations", json={
                    "plan_id": "premium_monthly",
                    "phone_number": phone,
                    "idempotency_key": str(uuid.uuid4()),
                    "access_key": secrets.token_urlsafe(32),
                })
                self.assertEqual(response.status_code, 422, response.text)
        with SessionLocal() as db:
            self.assertEqual(db.query(SubscriptionRegistration).count(), registration_count)

    def test_guest_registration_reports_missing_secure_storage_key(self):
        payload = {
            "plan_id": "premium_monthly",
            "phone_number": "097 123 4567",
            "idempotency_key": str(uuid.uuid4()),
            "access_key": secrets.token_urlsafe(32),
        }
        with patch.dict(os.environ, {"KITE_SECRET_KEY": ""}):
            response = self.client.post("/api/subscription/registrations", json=payload)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "The service is temporarily unavailable. Please try again shortly.",
        )

    def test_registration_cancel_and_unconfigured_provider_do_not_activate_premium(self):
        registration_key = secrets.token_urlsafe(32)
        registration = self.client.post("/api/subscription/registrations", json={
            "plan_id": "premium_annual",
            "phone_number": "0971234567",
            "idempotency_key": str(uuid.uuid4()),
            "access_key": registration_key,
        })
        self.assertEqual(registration.status_code, 201, registration.text)
        registration_id = registration.json()["registration_id"]
        headers = {"X-Kite-Registration-Key": registration_key}
        initiation = self.client.post(
            "/api/subscription/registrations/" + registration_id + "/payments",
            headers=headers,
            json={"provider_id": "not-configured", "idempotency_key": str(uuid.uuid4())},
        )
        self.assertEqual(initiation.status_code, 200, initiation.text)
        self.assertEqual(initiation.json()["payment"]["status"], "NOT_INITIATED")
        cancelled = self.client.post(
            "/api/subscription/registrations/" + registration_id + "/cancel",
            headers=headers,
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["subscription_status"], "CANCELLED")
        self.assertEqual(cancelled.json()["payment"]["status"], "CANCELLED")
        self.assertFalse(cancelled.json()["premium_active"])

    def test_verified_provider_webhook_activates_linked_subscription_once(self):
        registration_key = secrets.token_urlsafe(32)
        registered = self.client.post(
            "/api/subscription/registrations",
            json={
                "plan_id": "premium_monthly",
                "phone_number": "+260 97 123 4567",
                "idempotency_key": str(uuid.uuid4()),
                "access_key": registration_key,
            },
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        registration = registered.json()
        claimed = self.client.post(
            "/api/subscription/registrations/" + registration["registration_id"] + "/claim",
            headers=self.headers,
            json={"access_key": registration_key},
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        with SessionLocal() as db:
            db.add(PaymentProviderConfiguration(
                provider_id="test-wallet",
                enabled=True,
                environment="sandbox",
                options_json="{}",
            ))
            db.commit()

        class MockProvider:
            provider_id = "test-wallet"
            display_name = "Test wallet"

            def initiate(self, reference, amount, currency, phone_number):
                self.initiated = (reference, amount, currency, phone_number)
                return ProviderPaymentResult(
                    status=PaymentStatus.SUCCESSFUL,
                    verified=True,
                    transaction_reference=reference,
                    provider_reference="sandbox-payment-1",
                    amount=amount,
                    currency=currency,
                )

            def verify(self, reference, provider_reference):
                return ProviderPaymentResult(
                    status=PaymentStatus.SUCCESSFUL,
                    verified=False,
                    transaction_reference=reference,
                    provider_reference=provider_reference,
                    amount="19.99",
                    currency="ZMW",
                )

            def verify_webhook(self, body, headers):
                self.webhook_body = body
                return ProviderPaymentResult(
                    status=PaymentStatus.SUCCESSFUL,
                    verified=True,
                    transaction_reference=self.initiated[0],
                    provider_reference="sandbox-payment-1",
                    amount="19.99",
                    currency="ZMW",
                )

        provider = MockProvider()
        with patch("backend.app.get_mobile_money_provider", return_value=provider):
            initiation = self.client.post(
                "/api/subscription/registrations/" + registration["registration_id"] + "/payments",
                headers=self.headers,
                json={"provider_id": "test-wallet", "idempotency_key": str(uuid.uuid4())},
            )
            self.assertEqual(initiation.status_code, 200, initiation.text)
            self.assertEqual(initiation.json()["payment"]["status"], "PROCESSING")
            self.assertEqual(provider.initiated[3], "+260971234567")
            self.assertEqual(
                self.client.get("/api/subscription", headers=self.headers).json()["premium"],
                False,
            )

            unverified = self.client.post(
                "/api/subscription/registrations/" + registration["registration_id"] + "/verify",
                headers=self.headers,
            )
            self.assertEqual(unverified.json()["payment"]["status"], "PROCESSING")
            self.assertFalse(self.client.get("/api/subscription", headers=self.headers).json()["premium"])

            webhook = self.client.post("/api/payment-webhooks/test-wallet", content=b"signed mock event")
            self.assertEqual(webhook.status_code, 200, webhook.text)
            self.assertEqual(webhook.json()["status"], "SUCCESSFUL")
            repeated_webhook = self.client.post(
                "/api/payment-webhooks/test-wallet", content=b"signed duplicate mock event",
            )
            self.assertEqual(repeated_webhook.status_code, 200, repeated_webhook.text)

        status = self.client.get(
            "/api/subscription/registrations/" + registration["registration_id"],
            headers=self.headers,
        )
        self.assertEqual(status.json()["subscription_status"], "ACTIVE")
        self.assertTrue(self.client.get("/api/subscription", headers=self.headers).json()["premium"])
        with SessionLocal() as db:
            transaction = db.query(PaymentTransaction).filter_by(
                registration_id=registration["registration_id"],
            ).one()
            self.assertEqual(transaction.status, "SUCCESSFUL")
            self.assertIsNotNone(transaction.subscription_id)
            self.assertEqual(
                db.query(PaymentAttempt).filter_by(transaction_id=transaction.id).count(), 1,
            )
            history = db.query(PaymentStatusHistory).filter_by(transaction_id=transaction.id).all()
            self.assertEqual(
                [item.status for item in history],
                ["NOT_INITIATED", "PROCESSING", "SUCCESSFUL"],
            )

    def test_mobile_money_checkout_is_disabled_without_provider_credentials(self):
        with patch.dict(os.environ, {
            "KITE_PAWAPAY_ENABLED": "false",
            "KITE_PAWAPAY_ENV": "sandbox",
            "KITE_PAWAPAY_API_TOKEN": "",
        }):
            plans = self.client.get("/api/plans")
            checkout = self.client.post(
                "/api/subscription/mobile-money",
                headers=self.headers,
                json={
                    "plan_id": "premium_monthly",
                    "provider": "ZAMTEL_ZMB",
                    "phone_number": "+260763456789",
                    "idempotency_key": str(uuid.uuid4()),
                },
            )
        self.assertFalse(plans.json()["mobile_money"]["enabled"])
        self.assertEqual(plans.json()["mobile_money"]["providers"], [])
        self.assertEqual(checkout.status_code, 503)

    def test_lenco_stays_hidden_and_unavailable_while_collection_flags_are_off(self):
        with SessionLocal() as db:
            db.merge(PaymentProviderConfiguration(
                provider_id="lenco_airtel",
                enabled=True,
                environment="sandbox",
                options_json="{}",
            ))
            user = db.query(User).filter_by(email=self.email).one()
            registration_id = str(uuid.uuid4())
            db.add(SubscriptionRegistration(
                id=registration_id,
                user_id=user.id,
                plan_id="premium_monthly",
                idempotency_key=str(uuid.uuid4()),
                request_hash="a" * 64,
                access_key_hash="b" * 64,
                phone_ciphertext="unused",
                phone_hash="c" * 64,
                phone_last4="4567",
                status="PENDING_PAYMENT",
            ))
            db.add(PaymentTransaction(
                id=str(uuid.uuid4()),
                registration_id=registration_id,
                reference="KITE-SUB-" + uuid.uuid4().hex[:20].upper(),
                amount="19.99",
                currency="ZMW",
                status="NOT_INITIATED",
            ))
            db.commit()

        env = {
            "KITE_LENCO_BASE_URL": "https://api.lenco.co/access/v2",
            "KITE_LENCO_API_TOKEN": "test-only-lenco-token",
            "KITE_LENCO_PUBLIC_KEY": "test-only-lenco-public-key",
            "KITE_LENCO_COLLECTION_ENABLED": "false",
            "KITE_LENCO_ZMW_COLLECTION_CONFIRMED": "false",
            "KITE_PAWAPAY_ENABLED": "false",
        }
        try:
            with patch.dict(os.environ, env, clear=False):
                plans = self.client.get("/api/plans")
                self.assertTrue(all(plan["currency"] == "ZMW" for plan in plans.json()["plans"]))
                self.assertNotIn(env["KITE_LENCO_API_TOKEN"], plans.text)
                self.assertNotIn(env["KITE_LENCO_PUBLIC_KEY"], plans.text)
                self.assertNotIn(
                    "lenco",
                    [provider["id"] for provider in plans.json()["payment_providers"]],
                )

                started = self.client.post(
                    "/api/subscription/registrations/" + registration_id + "/payments",
                    headers=self.headers,
                    json={
                        "provider_id": "lenco_airtel",
                        "idempotency_key": str(uuid.uuid4()),
                    },
                )
                self.assertEqual(started.status_code, 200, started.text)
                self.assertEqual(started.json()["payment"]["status"], "NOT_INITIATED")
                self.assertFalse(started.json()["premium_active"])

                webhook = self.client.post(
                    "/api/payment-webhooks/lenco",
                    content=b'{"event":"transaction.successful"}',
                    headers={"X-Lenco-Signature": "invalid"},
                )
                self.assertEqual(webhook.status_code, 400)

                with SessionLocal() as db:
                    transaction = db.query(PaymentTransaction).filter_by(
                        registration_id=registration_id,
                    ).one()
                    self.assertEqual(transaction.currency, "ZMW")
                    self.assertEqual(transaction.status, "NOT_INITIATED")
                    self.assertEqual(
                        db.query(PaymentAttempt).filter_by(transaction_id=transaction.id).count(),
                        0,
                    )
        finally:
            with SessionLocal() as db:
                db.query(PaymentProviderConfiguration).filter_by(provider_id="lenco_airtel").delete()
                db.commit()

    def test_lenco_payment_is_verified_before_once_only_subscription_activation(self):
        registration_key = secrets.token_urlsafe(32)
        registration = self.client.post(
            "/api/subscription/registrations",
            headers=self.headers,
            json={
                "plan_id": "premium_monthly",
                "phone_number": "+260971234567",
                "idempotency_key": str(uuid.uuid4()),
                "access_key": registration_key,
            },
        )
        self.assertEqual(registration.status_code, 201, registration.text)
        registration_id = registration.json()["registration_id"]
        reference = registration.json()["payment"]["reference"]

        with SessionLocal() as db:
            db.add(PaymentProviderConfiguration(
                provider_id="lenco_airtel",
                enabled=True,
                environment="sandbox",
                options_json="{}",
            ))
            db.commit()

        def collection(status, amount="19.99", currency="ZMW"):
            return {
                "reference": reference,
                "amount": amount,
                "currency": currency,
                "type": "mobile-money",
                "status": status,
                "lencoReference": "lenco-" + reference[-8:],
                "mobileMoneyDetails": {"country": "zm", "operator": "airtel"},
            }

        env = {
            "KITE_LENCO_BASE_URL": "https://api.lenco.co/access/v2",
            "KITE_LENCO_API_TOKEN": "test-only-lenco-token",
            "KITE_LENCO_COLLECTION_ENABLED": "true",
            "KITE_LENCO_ZMW_COLLECTION_CONFIRMED": "true",
        }
        try:
            with patch.dict(os.environ, env, clear=False):
                with patch(
                    "backend.lenco._lenco_api_request",
                    side_effect=[
                        collection("pay-offline"),
                        collection("successful"),
                        collection("successful"),
                    ],
                ) as provider_request:
                    started = self.client.post(
                        f"/api/subscription/registrations/{registration_id}/payments",
                        headers=self.headers,
                        json={
                            "provider_id": "lenco_airtel",
                            "idempotency_key": str(uuid.uuid4()),
                        },
                    )
                    self.assertEqual(started.status_code, 200, started.text)
                    self.assertEqual(started.json()["payment"]["status"], "PENDING")
                    self.assertIn("Approve it on your selected mobile-money phone", started.json()["message"])
                    self.assertFalse(started.json()["premium_active"])

                    verify_url = f"/api/subscription/registrations/{registration_id}/verify"
                    verified = self.client.post(verify_url, headers=self.headers)
                    self.assertEqual(verified.status_code, 200, verified.text)
                    self.assertEqual(verified.json()["payment"]["status"], "SUCCESSFUL")
                    self.assertTrue(verified.json()["premium_active"])

                    repeated = self.client.post(verify_url, headers=self.headers)
                    self.assertEqual(repeated.status_code, 200, repeated.text)
                    self.assertTrue(repeated.json()["premium_active"])
                    self.assertEqual(provider_request.call_count, 3)

            with SessionLocal() as db:
                transaction = db.query(PaymentTransaction).filter_by(
                    registration_id=registration_id,
                ).one()
                self.assertEqual(transaction.currency, "ZMW")
                self.assertEqual(transaction.status, "SUCCESSFUL")
                self.assertEqual(
                    db.query(Subscription).filter_by(
                        token_hash=hashlib.sha256(
                            ("mobile-money:" + transaction.id).encode(),
                        ).hexdigest(),
                    ).count(),
                    1,
                )
        finally:
            with SessionLocal() as db:
                db.query(PaymentProviderConfiguration).filter_by(
                    provider_id="lenco_airtel",
                ).delete()
                db.commit()

    def test_lenco_payment_initiation_replay_does_not_create_another_attempt(self):
        registration = self.client.post(
            "/api/subscription/registrations",
            headers=self.headers,
            json={
                "plan_id": "premium_monthly",
                "phone_number": "+260971234567",
                "idempotency_key": str(uuid.uuid4()),
                "access_key": secrets.token_urlsafe(32),
            },
        )
        self.assertEqual(registration.status_code, 201, registration.text)
        registration_id = registration.json()["registration_id"]
        reference = registration.json()["payment"]["reference"]
        with SessionLocal() as db:
            db.add(PaymentProviderConfiguration(
                provider_id="lenco_airtel",
                enabled=True,
                environment="sandbox",
                options_json="{}",
            ))
            db.commit()

        collection = {
            "reference": reference,
            "amount": "19.99",
            "currency": "ZMW",
            "type": "mobile-money",
            "status": "pay-offline",
            "lencoReference": "lenco-" + reference[-8:],
            "mobileMoneyDetails": {"country": "zm", "operator": "airtel"},
        }
        env = {
            "KITE_LENCO_BASE_URL": "https://api.lenco.co/access/v2",
            "KITE_LENCO_API_TOKEN": "test-only-lenco-token",
            "KITE_LENCO_COLLECTION_ENABLED": "true",
            "KITE_LENCO_ZMW_COLLECTION_CONFIRMED": "true",
        }
        idempotency_key = str(uuid.uuid4())
        payload = {
            "provider_id": "lenco_airtel",
            "idempotency_key": idempotency_key,
        }
        try:
            with patch.dict(os.environ, env, clear=False), patch(
                "backend.lenco._lenco_api_request", return_value=collection,
            ) as provider_request:
                first = self.client.post(
                    f"/api/subscription/registrations/{registration_id}/payments",
                    headers=self.headers,
                    json=payload,
                )
                replay = self.client.post(
                    f"/api/subscription/registrations/{registration_id}/payments",
                    headers=self.headers,
                    json=payload,
                )
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(first.json()["payment"]["status"], "PENDING")
            self.assertEqual(replay.json()["payment"]["status"], "PENDING")
            provider_request.assert_called_once()
            with SessionLocal() as db:
                transaction = db.query(PaymentTransaction).filter_by(
                    registration_id=registration_id,
                ).one()
                self.assertEqual(
                    db.query(PaymentAttempt).filter_by(transaction_id=transaction.id).count(),
                    1,
                )
        finally:
            with SessionLocal() as db:
                db.query(PaymentProviderConfiguration).filter_by(
                    provider_id="lenco_airtel",
                ).delete()
                db.commit()

    def test_lenco_webhook_requires_signature_and_reverifies_status_idempotently(self):
        registration_key = secrets.token_urlsafe(32)
        registration = self.client.post(
            "/api/subscription/registrations",
            headers=self.headers,
            json={
                "plan_id": "premium_monthly",
                "phone_number": "+260971234567",
                "idempotency_key": str(uuid.uuid4()),
                "access_key": registration_key,
            },
        )
        self.assertEqual(registration.status_code, 201, registration.text)
        registration_id = registration.json()["registration_id"]
        reference = registration.json()["payment"]["reference"]
        lenco_reference = "lenco-" + reference[-8:]

        with SessionLocal() as db:
            db.add(PaymentProviderConfiguration(
                provider_id="lenco_mtn",
                enabled=True,
                environment="sandbox",
                options_json="{}",
            ))
            db.commit()

        def collection(status):
            return {
                "reference": reference,
                "amount": "19.99",
                "currency": "ZMW",
                "type": "mobile-money",
                "status": status,
                "lencoReference": lenco_reference,
                "mobileMoneyDetails": {"country": "zm", "operator": "mtn"},
            }

        token = "test-only-lenco-token"
        env = {
            "KITE_LENCO_BASE_URL": "https://api.lenco.co/access/v2",
            "KITE_LENCO_API_TOKEN": token,
            "KITE_LENCO_COLLECTION_ENABLED": "true",
            "KITE_LENCO_ZMW_COLLECTION_CONFIRMED": "true",
        }
        try:
            with patch.dict(os.environ, env, clear=False):
                with patch(
                    "backend.lenco._lenco_api_request",
                    return_value=collection("pay-offline"),
                ):
                    started = self.client.post(
                        f"/api/subscription/registrations/{registration_id}/payments",
                        headers=self.headers,
                        json={
                            "provider_id": "lenco_mtn",
                            "idempotency_key": str(uuid.uuid4()),
                        },
                    )
                self.assertEqual(started.status_code, 200, started.text)

                webhook_data = {
                    "event": "collection.failed",
                    "data": {
                        "reference": reference,
                        "amount": "19.99",
                        "currency": "ZMW",
                        "type": "mobile-money",
                        "status": "failed",
                        "lencoReference": lenco_reference,
                    },
                }
                body = __import__("json").dumps(webhook_data, separators=(",", ":")).encode()
                invalid = self.client.post(
                    "/api/payment-webhooks/lenco",
                    content=body,
                    headers={"X-Lenco-Signature": "invalid"},
                )
                self.assertEqual(invalid.status_code, 400)

                signing_key = hashlib.sha256(token.encode()).hexdigest().encode("ascii")
                signature = hmac.new(signing_key, body, hashlib.sha512).hexdigest()
                with patch(
                    "backend.lenco._lenco_api_request",
                    side_effect=[collection("successful"), collection("successful")],
                ) as provider_request:
                    first = self.client.post(
                        "/api/payment-webhooks/lenco",
                        content=body,
                        headers={"X-Lenco-Signature": signature},
                    )
                    duplicate = self.client.post(
                        "/api/payment-webhooks/lenco",
                        content=body,
                        headers={"X-Lenco-Signature": signature},
                    )
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(duplicate.status_code, 200, duplicate.text)
                self.assertEqual(first.json()["status"], "SUCCESSFUL")
                self.assertEqual(duplicate.json()["status"], "SUCCESSFUL")
                self.assertEqual(provider_request.call_count, 2)

            with SessionLocal() as db:
                transaction = db.query(PaymentTransaction).filter_by(
                    registration_id=registration_id,
                ).one()
                self.assertEqual(transaction.status, "SUCCESSFUL")
                self.assertEqual(
                    db.query(Subscription).filter_by(
                        token_hash=hashlib.sha256(
                            ("mobile-money:" + transaction.id).encode(),
                        ).hexdigest(),
                    ).count(),
                    1,
                )
        finally:
            with SessionLocal() as db:
                db.query(PaymentProviderConfiguration).filter_by(
                    provider_id="lenco_mtn",
                ).delete()
                db.commit()

    def test_only_configured_zambia_lenco_operators_are_advertised_to_checkout(self):
        with SessionLocal() as db:
            db.add_all([
                PaymentProviderConfiguration(
                    provider_id="lenco_airtel",
                    enabled=True,
                    environment="sandbox",
                    options_json="{}",
                ),
                PaymentProviderConfiguration(
                    provider_id="lenco_mtn",
                    enabled=True,
                    environment="sandbox",
                    options_json="{}",
                ),
                PaymentProviderConfiguration(
                    provider_id="lenco_zamtel",
                    enabled=False,
                    environment="sandbox",
                    options_json="{}",
                ),
            ])
            db.commit()
        env = {
            "KITE_LENCO_BASE_URL": "https://api.lenco.co/access/v2",
            "KITE_LENCO_API_TOKEN": "test-only-lenco-token",
            "KITE_LENCO_COLLECTION_ENABLED": "true",
            "KITE_LENCO_ZMW_COLLECTION_CONFIRMED": "true",
        }
        try:
            with patch.dict(os.environ, env, clear=False):
                response = self.client.get("/api/plans")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [item["id"] for item in response.json()["payment_providers"]],
                ["lenco_airtel", "lenco_mtn"],
            )
            self.assertTrue(all(plan["currency"] == "ZMW" for plan in response.json()["plans"]))
            self.assertNotIn(env["KITE_LENCO_API_TOKEN"], response.text)
        finally:
            with SessionLocal() as db:
                db.query(PaymentProviderConfiguration).filter(
                    PaymentProviderConfiguration.provider_id.in_(
                        ("lenco_airtel", "lenco_mtn", "lenco_zamtel"),
                    ),
                ).delete(synchronize_session=False)
                db.commit()

    def test_lenco_amount_mismatch_is_not_accepted_as_payment(self):
        registration_key = secrets.token_urlsafe(32)
        registration = self.client.post(
            "/api/subscription/registrations",
            headers=self.headers,
            json={
                "plan_id": "premium_monthly",
                "phone_number": "+260971234567",
                "idempotency_key": str(uuid.uuid4()),
                "access_key": registration_key,
            },
        )
        self.assertEqual(registration.status_code, 201, registration.text)
        registration_id = registration.json()["registration_id"]
        reference = registration.json()["payment"]["reference"]
        with SessionLocal() as db:
            db.add(PaymentProviderConfiguration(
                provider_id="lenco_airtel",
                enabled=True,
                environment="sandbox",
                options_json="{}",
            ))
            db.commit()

        def collection(status, amount):
            return {
                "reference": reference,
                "amount": amount,
                "currency": "ZMW",
                "type": "mobile-money",
                "status": status,
                "lencoReference": "lenco-" + reference[-8:],
                "mobileMoneyDetails": {"country": "zm", "operator": "airtel"},
            }

        env = {
            "KITE_LENCO_BASE_URL": "https://api.lenco.co/access/v2",
            "KITE_LENCO_API_TOKEN": "test-only-lenco-token",
            "KITE_LENCO_COLLECTION_ENABLED": "true",
            "KITE_LENCO_ZMW_COLLECTION_CONFIRMED": "true",
        }
        try:
            with patch.dict(os.environ, env, clear=False):
                with patch(
                    "backend.lenco._lenco_api_request",
                    side_effect=[
                        collection("pay-offline", "19.99"),
                        collection("successful", "20.00"),
                    ],
                ):
                    started = self.client.post(
                        f"/api/subscription/registrations/{registration_id}/payments",
                        headers=self.headers,
                        json={
                            "provider_id": "lenco_airtel",
                            "idempotency_key": str(uuid.uuid4()),
                        },
                    )
                    self.assertEqual(started.status_code, 200, started.text)
                    verified = self.client.post(
                        f"/api/subscription/registrations/{registration_id}/verify",
                        headers=self.headers,
                    )
            self.assertEqual(verified.status_code, 200, verified.text)
            self.assertEqual(verified.json()["payment"]["status"], "PENDING")
            self.assertEqual(
                verified.json()["payment"]["failure_code"], "VERIFICATION_MISMATCH",
            )
            self.assertFalse(verified.json()["premium_active"])
            with SessionLocal() as db:
                transaction = db.query(PaymentTransaction).filter_by(
                    registration_id=registration_id,
                ).one()
                self.assertEqual(
                    db.query(Subscription).filter_by(
                        token_hash=hashlib.sha256(
                            ("mobile-money:" + transaction.id).encode(),
                        ).hexdigest(),
                    ).count(),
                    0,
                )
        finally:
            with SessionLocal() as db:
                db.query(PaymentProviderConfiguration).filter_by(
                    provider_id="lenco_airtel",
                ).delete()
                db.commit()

    def test_lenco_payment_verification_is_scoped_to_registration_owner(self):
        registration = self.client.post(
            "/api/subscription/registrations",
            headers=self.headers,
            json={
                "plan_id": "premium_monthly",
                "phone_number": "+260971234567",
                "idempotency_key": str(uuid.uuid4()),
                "access_key": secrets.token_urlsafe(32),
            },
        )
        self.assertEqual(registration.status_code, 201, registration.text)
        registration_id = registration.json()["registration_id"]
        other_email = f"other-{uuid.uuid4().hex}@example.com"
        other = self.client.post("/api/auth/register", json={
            "name": "Other Learner",
            "email": other_email,
            "password": "secure-example-password",
        })
        self.assertEqual(other.status_code, 201, other.text)
        other_headers = {"Authorization": "Bearer " + self.issue_token(other_email)}
        with patch("backend.lenco._lenco_api_request") as provider_request:
            verified = self.client.post(
                f"/api/subscription/registrations/{registration_id}/verify",
                headers=other_headers,
            )
        self.assertEqual(verified.status_code, 404)
        provider_request.assert_not_called()

    def test_mobile_money_only_grants_entitlement_after_verified_completion(self):
        payment_idempotency_key = str(uuid.uuid4())
        provider_configuration = {
            "countries": [{
                "country": "ZMB",
                "providers": [{
                    "provider": "MTN_MOMO_ZMB",
                    "currencies": [{
                        "currency": "ZMW",
                        "operationTypes": {
                            "DEPOSIT": {
                                "status": "OPERATIONAL",
                                "minAmount": "1",
                                "maxAmount": "20000",
                                "decimalsInAmount": "TWO_PLACES",
                            },
                        },
                    }],
                }],
            }],
        }
        accepted = {"depositId": None, "status": "ACCEPTED"}
        duplicate = {"depositId": None, "status": "DUPLICATE_IGNORED"}

        def provider_request(method, path, payload=None):
            if method == "GET" and path.endswith("active-conf?country=ZMB&operationType=DEPOSIT"):
                return provider_configuration
            if method == "POST" and path == "/v2/deposits":
                response = dict(accepted if provider_request.calls == 0 else duplicate)
                response["depositId"] = payload["depositId"]
                provider_request.deposit_id = payload["depositId"]
                provider_request.calls += 1
                return response
            if method == "GET" and path == "/v2/deposits/" + provider_request.deposit_id:
                return {
                    "status": "FOUND",
                    "data": {
                        "depositId": provider_request.deposit_id,
                        "status": "COMPLETED",
                        "amount": "19.99",
                        "currency": "ZMW",
                        "country": "ZMB",
                        "payer": {
                            "type": "MMO",
                            "accountDetails": {"provider": "MTN_MOMO_ZMB"},
                        },
                    },
                }
            raise AssertionError("Unexpected provider request: " + method + " " + path)

        provider_request.calls = 0
        provider_request.deposit_id = ""
        pawa_env = {
            "KITE_PAWAPAY_ENABLED": "true",
            "KITE_PAWAPAY_ENV": "sandbox",
            "KITE_PAWAPAY_API_TOKEN": "sandbox-test-token",
        }
        registration_key = secrets.token_urlsafe(32)
        registration = self.client.post(
            "/api/subscription/registrations",
            headers=self.headers,
            json={
                "plan_id": "premium_monthly",
                "phone_number": "+260 76 345 6789",
                "idempotency_key": str(uuid.uuid4()),
                "access_key": registration_key,
            },
        )
        self.assertEqual(registration.status_code, 201, registration.text)
        payload = {
            "plan_id": "premium_monthly",
            "provider": "MTN_MOMO_ZMB",
            "phone_number": "+260763456789",
            "idempotency_key": payment_idempotency_key,
            "registration_id": registration.json()["registration_id"],
            "registration_key": registration_key,
        }
        with patch.dict(os.environ, pawa_env), patch(
            "backend.app.pawapay_request", side_effect=provider_request,
        ) as request:
            plan_response = self.client.get("/api/plans")
            self.assertTrue(plan_response.json()["mobile_money"]["enabled"])
            self.assertEqual(
                plan_response.json()["mobile_money"]["providers"],
                [{"id": "MTN_MOMO_ZMB", "name": "MTN MoMo"}],
            )
            started = self.client.post(
                "/api/subscription/mobile-money", headers=self.headers, json=payload,
            )
            self.assertEqual(started.status_code, 200, started.text)
            self.assertEqual(started.json()["status"], "PENDING")
            self.assertEqual(
                self.client.get("/api/lessons/python-loops", headers=self.headers).status_code, 402,
            )

            repeated = self.client.post(
                "/api/subscription/mobile-money", headers=self.headers, json=payload,
            )
            self.assertEqual(repeated.status_code, 200, repeated.text)
            self.assertEqual(repeated.json()["payment_id"], started.json()["payment_id"])
            self.assertEqual(provider_request.calls, 2)

            confirmed = self.client.get(
                "/api/subscription/mobile-money/" + started.json()["payment_id"],
                headers=self.headers,
            )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["status"], "COMPLETED")
        self.assertTrue(confirmed.json()["premium"])
        expires_at = datetime.fromisoformat(confirmed.json()["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        self.assertGreater(expires_at, datetime.now(timezone.utc) + timedelta(days=29))
        self.assertLess(expires_at, datetime.now(timezone.utc) + timedelta(days=31))
        self.assertEqual(
            self.client.get("/api/lessons/python-loops", headers=self.headers).status_code, 200,
        )
        linked_registration = self.client.get(
            "/api/subscription/registrations/" + registration.json()["registration_id"],
            headers={"X-Kite-Registration-Key": registration_key},
        )
        self.assertEqual(linked_registration.status_code, 200, linked_registration.text)
        self.assertEqual(linked_registration.json()["subscription_status"], "ACTIVE")
        self.assertEqual(linked_registration.json()["payment"]["status"], "SUCCESSFUL")
        subscription = self.client.get("/api/subscription", headers=self.headers).json()
        self.assertTrue(subscription["premium"])
        self.assertFalse(subscription["auto_renew"])
        self.assertIsNone(subscription["cancel_url"])
        self.assertEqual(subscription["billing_provider"], "mobile_money")
        with SessionLocal() as db:
            payment = db.get(MobileMoneyPayment, started.json()["payment_id"])
            self.assertEqual(payment.status, "COMPLETED")
            self.assertFalse(hasattr(payment, "phone_number"))
        self.assertEqual(request.call_count, 6)

    def test_mobile_money_does_not_grant_on_mismatched_completed_deposit(self):
        provider_configuration = {
            "countries": [{
                "country": "ZMB",
                "providers": [{
                    "provider": "ZAMTEL_ZMB",
                    "currencies": [{
                        "currency": "ZMW",
                        "operationTypes": {"DEPOSIT": {
                            "status": "OPERATIONAL",
                            "minAmount": "1",
                            "maxAmount": "20000",
                            "decimalsInAmount": "TWO_PLACES",
                        }},
                    }],
                }],
            }],
        }
        def provider_request(method, path, payload=None):
            if method == "GET" and path.endswith("active-conf?country=ZMB&operationType=DEPOSIT"):
                return provider_configuration
            if method == "POST":
                provider_request.deposit_id = payload["depositId"]
                return {"depositId": payload["depositId"], "status": "ACCEPTED"}
            return {
                "status": "FOUND",
                "data": {
                    "depositId": provider_request.deposit_id,
                    "status": "COMPLETED",
                    "amount": "9.99",
                    "currency": "ZMW",
                    "country": "ZMB",
                    "payer": {"accountDetails": {"provider": "ZAMTEL_ZMB"}},
                },
            }

        provider_request.deposit_id = ""
        pawa_env = {
            "KITE_PAWAPAY_ENABLED": "true",
            "KITE_PAWAPAY_ENV": "sandbox",
            "KITE_PAWAPAY_API_TOKEN": "sandbox-test-token",
        }
        with patch.dict(os.environ, pawa_env), patch(
            "backend.app.pawapay_request", side_effect=provider_request,
        ):
            started = self.client.post("/api/subscription/mobile-money", headers=self.headers, json={
                "plan_id": "premium_monthly",
                "provider": "ZAMTEL_ZMB",
                "phone_number": "260763456789",
                "idempotency_key": str(uuid.uuid4()),
            })
            confirmed = self.client.get(
                "/api/subscription/mobile-money/" + started.json()["payment_id"],
                headers=self.headers,
            )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["status"], "REVIEW_REQUIRED")
        self.assertFalse(confirmed.json()["premium"])
        self.assertEqual(self.client.get("/api/subscription", headers=self.headers).json()["plan"], "free")

    def test_google_sign_in_requires_supabase_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/api/auth/google/authorize", params={"code_challenge": "c" * 43})
        self.assertEqual(response.status_code, 503)

    def test_google_sign_in_uses_pkce_authorization_and_exchange(self):
        env = {
            "SUPABASE_URL": "https://kite.example",
            "SUPABASE_ANON_KEY": "test-anon-key",
            "PUBLIC_APP_URL": "https://kite.example.app",
        }
        with patch.dict(os.environ, env), patch("backend.app.supabase_request") as auth_request:
            authorize = self.client.get("/api/auth/google/authorize", params={"code_challenge": "c" * 43})
            auth_request.return_value = {
                "access_token": "google-access",
                "refresh_token": "google-refresh",
                "user": {
                    "id": "google-user-id",
                    "email": self.email,
                    "email_confirmed_at": "2025-01-01T00:00:00Z",
                    "user_metadata": {"name": "Kite Learner"},
                },
            }
            exchange = self.client.post("/api/auth/google/exchange", json={
                "code": "authorization-code",
                "code_verifier": "v" * 43,
            })
        self.assertEqual(authorize.status_code, 200, authorize.text)
        authorization_url = authorize.json()["authorization_url"]
        self.assertIn("provider=google", authorization_url)
        self.assertIn("code_challenge_method=s256", authorization_url)
        self.assertEqual(exchange.status_code, 200, exchange.text)
        self.assertNotIn("access_token", exchange.json())
        self.assertTrue(exchange.cookies.get("kite_access"))
        self.assertEqual(auth_request.call_args.args[0], "/token?grant_type=pkce")
        self.assertEqual(auth_request.call_args.kwargs["data"]["code_verifier"], "v" * 43)

    def test_quiz_xp_is_awarded_only_once(self):
        questions = self.client.get("/api/quizzes/python-basics", headers=self.headers).json()["questions"]
        self.assertEqual(len(questions), 10)
        incomplete = self.client.post("/api/quizzes/python-basics/submit", headers=self.headers,
                                      json={"answers": [0, 1, 1, 2, 1, 1, 2, 2, 1]})
        self.assertEqual(incomplete.status_code, 400)
        answers = [0, 1, 1, 2, 1, 1, 2, 2, 1, 1]
        first = self.client.post("/api/quizzes/python-basics/submit", headers=self.headers,
                                 json={"answers": answers})
        second = self.client.post("/api/quizzes/python-basics/submit", headers=self.headers,
                                  json={"answers": answers})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["score"], 10)
        self.assertEqual(first.json()["xp_awarded"], 50)
        self.assertEqual(second.json()["xp_awarded"], 0)
        quiz_notices = [
            item for item in self.client.get("/api/notifications", headers=self.headers).json()
            if item["category"] == "learning" and item["target_route"] == "quiz"
        ]
        self.assertEqual(len(quiz_notices), 2)

    def test_quiz_best_score_updates_on_retry_without_extra_xp(self):
        correct = [0, 1, 1, 2, 1, 1, 2, 2, 1, 1]
        partial = [-1, -1, -1] + correct[3:]
        first = self.client.post(
            "/api/quizzes/python-basics/submit", headers=self.headers, json={"answers": partial},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["score"], 7)
        self.assertIsNone(first.json()["feedback"][0]["your_answer"])
        self.assertFalse(first.json()["feedback"][0]["correct"])
        self.assertNotIn(
            "Quiz Master", self.client.get("/api/progress", headers=self.headers).json()["achievements"],
        )

        improved = self.client.post(
            "/api/quizzes/python-basics/submit", headers=self.headers, json={"answers": correct},
        )
        self.assertEqual(improved.status_code, 200, improved.text)
        self.assertEqual(improved.json()["score"], 10)
        self.assertEqual(improved.json()["xp_awarded"], 0)
        self.assertIn("Quiz Master", self.client.get("/api/progress", headers=self.headers).json()["achievements"])

    def test_challenge_is_server_checked_and_xp_is_idempotent(self):
        with patch("backend.app.call_runner", new_callable=AsyncMock) as run:
            run.return_value = {"stdout": "Hello, World!\n", "stderr": "", "error": None}
            first = self.client.post("/api/challenges/hello-world/submit", headers=self.headers,
                                     json={"code": 'print("Hello, World!")'})
            second = self.client.post("/api/challenges/hello-world/submit", headers=self.headers,
                                      json={"code": 'print("Hello, World!")'})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["passed"])
        self.assertEqual(first.json()["xp_awarded"], 100)
        self.assertEqual(second.json()["xp_awarded"], 0)

    def test_guest_can_submit_free_challenge_without_saved_xp(self):
        with patch("backend.app.call_runner", new_callable=AsyncMock) as run:
            run.return_value = {"stdout": "Hello, World!\n", "stderr": "", "error": None}
            response = self.client.post(
                "/api/challenges/hello-world/submit",
                json={"code": 'print("Hello, World!")'},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["passed"])
        self.assertTrue(response.json()["guest"])
        self.assertEqual(response.json()["xp_awarded"], 0)
        self.assertIsNone(response.json()["total_xp"])
        with SessionLocal() as db:
            user = db.query(User).filter_by(email=self.email).one()
            self.assertEqual(db.query(Activity).filter_by(
                user_id=user.id, kind="challenge", item_id="hello-world",
            ).count(), 0)


if __name__ == "__main__":
    unittest.main()
