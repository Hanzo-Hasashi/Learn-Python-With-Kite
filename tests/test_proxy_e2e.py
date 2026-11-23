import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
import httpx
import uvicorn
import threading

TEST_PROXY_SECRET = "e2e-proxy-test-secret-that-is-at-least-32-chars-long"
REPO_ROOT = Path(__file__).resolve().parent.parent

_ORIGINAL_ENVIRONMENT = os.environ.copy()
os.environ.update({
    "KITE_ENV": "development",
    "DATABASE_URL": "sqlite://",
    "KITE_VERCEL_PROXY": "true",
    "KITE_PROXY_SECRET": TEST_PROXY_SECRET,
    "KITE_SECRET_KEY": "e2e-proxy-bootstrap-secret-key-at-least-32",
    "PUBLIC_APP_URL": "https://learnpythonwithkite.vercel.app",
    "PUBLIC_API_URL": "https://learnpythonwithkite.vercel.app",
    "KITE_CORS_ORIGINS": "https://learnpythonwithkite.vercel.app",
    "KITE_ALLOW_LOCAL_AUTH": "true",
    "SUPABASE_URL": "",
    "SUPABASE_ANON_KEY": "",
})
for _key in ("MIGRATION_DATABASE_URL", "RENDER", "VERCEL", "VERCEL_ENV"):
    os.environ.pop(_key, None)

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import backend.app as backend_app
from backend.app import app, startup, auth_attempts, Base

os.environ.clear()
os.environ.update(_ORIGINAL_ENVIRONMENT)


TEST_ENVIRONMENT = {
    "KITE_ENV": "development",
    "DATABASE_URL": "sqlite://",
    "KITE_VERCEL_PROXY": "true",
    "KITE_PROXY_SECRET": TEST_PROXY_SECRET,
    "KITE_SECRET_KEY": "e2e-test-secret-key-at-least-32-characters",
    "PUBLIC_APP_URL": "https://learnpythonwithkite.vercel.app",
    "PUBLIC_API_URL": "https://learnpythonwithkite.vercel.app",
    "KITE_CORS_ORIGINS": "https://learnpythonwithkite.vercel.app",
    "KITE_ALLOW_LOCAL_AUTH": "true",
    "SUPABASE_URL": "",
    "SUPABASE_ANON_KEY": "",
}


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class VercelProxyE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node_path = shutil.which("node")
        if not cls.node_path:
            raise unittest.SkipTest("Node.js is not installed on this system.")

        cls.addClassCleanup(cls.cleanup_resources)
        cls._environment_patcher = patch.dict(os.environ, TEST_ENVIRONMENT, clear=False)
        cls._environment_patcher.start()
        cls.db_directory = tempfile.TemporaryDirectory()
        cls.database_url = "sqlite:///" + str(
            Path(cls.db_directory.name, "test_proxy.db")
        ).replace("\\", "/")
        os.environ["DATABASE_URL"] = cls.database_url
        for key in ("MIGRATION_DATABASE_URL", "RENDER", "VERCEL", "VERCEL_ENV"):
            os.environ.pop(key, None)
        cls.previous_engine = backend_app.engine
        cls.previous_session_factory = backend_app.SessionLocal
        cls.test_engine = create_engine(
            cls.database_url,
            **backend_app.database_engine_options(cls.database_url, serverless=False),
        )
        backend_app.engine = cls.test_engine
        backend_app.SessionLocal = sessionmaker(
            bind=cls.test_engine,
            autoflush=False,
            expire_on_commit=False,
        )

        cls.backend_port = find_free_port()
        cls.proxy_port = find_free_port()

        # Apply database migrations to test database
        alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
        command.upgrade(alembic_cfg, "head")
        startup()

        # Start FastAPI backend in a background thread
        config = uvicorn.Config(app, host="127.0.0.1", port=cls.backend_port, log_level="warning")
        cls.server = uvicorn.Server(config)
        cls.backend_thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.backend_thread.start()

        # Wait for FastAPI to become ready
        backend_ready = False
        for _ in range(50):
            try:
                with httpx.Client() as client:
                    resp = client.get(f"http://127.0.0.1:{cls.backend_port}/health", timeout=1.0)
                    if resp.status_code == 200:
                        backend_ready = True
                        break
            except Exception:
                time.sleep(0.1)

        if not backend_ready:
            raise RuntimeError(f"FastAPI backend failed to start on port {cls.backend_port}")

        # Start Node.js proxy server process
        proxy_env = os.environ.copy()
        proxy_env["KITE_BACKEND_URL"] = f"http://127.0.0.1:{cls.backend_port}"
        proxy_env["KITE_PROXY_SECRET"] = TEST_SECRET = TEST_PROXY_SECRET

        repo_root = Path(__file__).resolve().parent.parent
        cls.proxy_process = subprocess.Popen(
            [cls.node_path, str(repo_root / "tests" / "run_proxy_server.js"), str(cls.proxy_port)],
            env=proxy_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait for proxy to listen
        proxy_ready = False
        for _ in range(50):
            try:
                with httpx.Client() as client:
                    resp = client.get(f"http://127.0.0.1:{cls.proxy_port}/api/lessons", timeout=1.0)
                    if resp.status_code == 200:
                        proxy_ready = True
                        break
            except Exception:
                time.sleep(0.1)

        if not proxy_ready:
            cls.proxy_process.kill()
            raise RuntimeError(f"Node.js proxy failed to start on port {cls.proxy_port}")

    @classmethod
    def cleanup_resources(cls):
        try:
            if hasattr(cls, "server"):
                cls.server.should_exit = True
                if hasattr(cls, "backend_thread"):
                    cls.backend_thread.join(timeout=10)
            if hasattr(cls, "proxy_process") and cls.proxy_process.poll() is None:
                cls.proxy_process.kill()
                cls.proxy_process.wait()
            if hasattr(cls, "test_engine"):
                try:
                    if not hasattr(cls, "backend_thread") or not cls.backend_thread.is_alive():
                        Base.metadata.drop_all(cls.test_engine)
                finally:
                    cls.test_engine.dispose()
            if hasattr(cls, "previous_engine"):
                backend_app.stop_account_deletion_reconciliation_worker()
        finally:
            if hasattr(cls, "previous_engine"):
                backend_app.engine = cls.previous_engine
                backend_app.SessionLocal = cls.previous_session_factory
            if hasattr(cls, "db_directory"):
                cls.db_directory.cleanup()
            if hasattr(cls, "_environment_patcher"):
                cls._environment_patcher.stop()

    def test_01_direct_access_to_fastapi_without_secret_is_blocked_with_404(self):
        """Verify that direct requests to FastAPI without x-kite-proxy-secret return 404."""
        with httpx.Client() as client:
            resp = client.get(f"http://127.0.0.1:{self.backend_port}/api/lessons")
            self.assertEqual(resp.status_code, 404)
            self.assertEqual(resp.headers.get("cache-control"), "private, no-store")

    def test_02_proxy_forwards_get_lessons_with_secret_injection(self):
        """Verify that requests through Node proxy receive 200 and return lessons."""
        with httpx.Client() as client:
            resp = client.get(f"http://127.0.0.1:{self.proxy_port}/api/lessons")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIsInstance(data, list)
            self.assertGreater(len(data), 0)
            self.assertEqual(data[0]["id"], "python-variables")

    def test_03_query_parameters_preserved_through_proxy(self):
        """Verify that query parameters are preserved through the proxy."""
        with httpx.Client() as client:
            resp = client.get(f"http://127.0.0.1:{self.proxy_port}/api/lessons?module_id=python-foundations")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIsInstance(data, list)
            self.assertGreater(len(data), 0)

    def test_04_full_auth_lifecycle_through_proxy(self):
        """Complete end-to-end lifecycle: Register -> Login (Set-Cookie) -> Session -> CSRF PATCH -> Logout."""
        email = f"learner-{uuid.uuid4().hex}@example.com"
        password = "SecurePassword123!"

        # Use an httpx Client to retain and transmit cookies across requests
        with httpx.Client(base_url=f"http://127.0.0.1:{self.proxy_port}") as client:
            # 1. Register through proxy
            reg_resp = client.post("/api/auth/register", json={
                "name": "Proxy Learner",
                "email": email,
                "password": password,
            })
            self.assertEqual(reg_resp.status_code, 201, reg_resp.text)

            # 2. Login through proxy
            login_resp = client.post("/api/auth/login", json={
                "email": email,
                "password": password,
            })
            self.assertEqual(login_resp.status_code, 200, login_resp.text)

            # Verify that cookies are present in client cookie jar
            self.assertIn("kite_access", client.cookies)
            self.assertIn("kite_session", client.cookies)

            login_data = login_resp.json()
            csrf_token = login_data.get("csrf_token")
            self.assertTrue(csrf_token, "Login must return a csrf_token")

            # 3. Retrieve session through proxy using received cookies
            session_resp = client.get("/api/auth/session")
            self.assertEqual(session_resp.status_code, 200, session_resp.text)
            user_data = session_resp.json()
            self.assertEqual(user_data["user"]["email"], email)

            # 4. Perform state-changing PATCH /api/me with CSRF token through proxy
            patch_resp = client.patch(
                "/api/me",
                json={"name": "Updated Proxy Learner"},
                headers={"X-Kite-CSRF": csrf_token},
            )
            self.assertEqual(patch_resp.status_code, 200, patch_resp.text)
            self.assertEqual(patch_resp.json()["name"], "Updated Proxy Learner")

            # 5. Logout through proxy
            logout_resp = client.post("/api/auth/logout", headers={"X-Kite-CSRF": csrf_token})
            self.assertEqual(logout_resp.status_code, 200, logout_resp.text)

            # 6. Verify the cookies are cleared and protected routes reject the old session.
            self.assertNotIn("kite_access", client.cookies)
            self.assertNotIn("kite_refresh", client.cookies)
            self.assertNotIn("kite_session", client.cookies)
            post_logout_resp = client.get("/api/auth/session")
            self.assertEqual(post_logout_resp.status_code, 401, post_logout_resp.text)
            protected_resp = client.get("/api/me")
            self.assertEqual(protected_resp.status_code, 401, protected_resp.text)

    def test_05_client_spoofed_secret_is_stripped_by_proxy(self):
        """Verify that a client supplying a fake secret header does not break proxy injection."""
        with httpx.Client() as client:
            resp = client.get(
                f"http://127.0.0.1:{self.proxy_port}/api/lessons",
                headers={"X-Kite-Proxy-Secret": "malicious-fake-secret-that-does-not-match"},
            )
            # The proxy must strip the fake secret and inject the true server secret
            self.assertEqual(resp.status_code, 200)

    def test_06_non_api_paths_rejected_with_404(self):
        """Verify that requests outside /api/ are rejected immediately with 404."""
        with httpx.Client() as client:
            resp = client.get(f"http://127.0.0.1:{self.proxy_port}/admin/secrets")
            self.assertEqual(resp.status_code, 404)

            resp2 = client.get(f"http://127.0.0.1:{self.proxy_port}/etc/passwd")
            self.assertEqual(resp2.status_code, 404)


if __name__ == "__main__":
    unittest.main()
