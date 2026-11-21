import json
import re
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "web"
TWA_CONFIG_PATH = ROOT / "android-twa" / "twa-manifest.json"
PACKAGE_ID = "com.learnpythonwithkite.app"


class ProgressiveWebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((WEB_ROOT / "manifest.webmanifest").read_text(encoding="utf-8"))

    def test_manifest_has_stable_install_identity_and_root_scope(self):
        manifest = self.manifest
        self.assertEqual(manifest["id"], "/")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertFalse(manifest["prefer_related_applications"])
        self.assertTrue(manifest["name"])
        self.assertTrue(manifest["short_name"])

    def test_manifest_has_existing_any_and_maskable_png_icons(self):
        icons = self.manifest["icons"]
        any_icons = {
            size
            for icon in icons
            if icon.get("purpose", "any") in ("any", "any maskable")
            for size in icon["sizes"].split()
        }
        self.assertIn("192x192", any_icons)
        self.assertIn("512x512", any_icons)

        maskable_icons = [
            icon for icon in icons if "maskable" in icon.get("purpose", "").split()
        ]
        self.assertTrue(maskable_icons)
        for icon in icons:
            with self.subTest(icon=icon["src"]):
                self.assertEqual(icon["type"], "image/png")
                path = WEB_ROOT / icon["src"].lstrip("/")
                self.assertTrue(path.is_file())
                header = path.read_bytes()[:24]
                self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
                width, height = struct.unpack(">II", header[16:24])
                self.assertIn(f"{width}x{height}", icon["sizes"].split())

    def test_manifest_and_service_worker_are_linked_from_the_web_shell(self):
        html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        worker = (WEB_ROOT / "sw.js").read_text(encoding="utf-8")
        pwa = (WEB_ROOT / "assets" / "pwa.js").read_text(encoding="utf-8")
        vercel = json.loads((WEB_ROOT / "vercel.json").read_text(encoding="utf-8"))

        self.assertIn('rel="manifest" href="/manifest.webmanifest"', html)
        self.assertIn('navigator.serviceWorker.register("/sw.js?', pwa)
        self.assertIn('url.pathname.indexOf("/api/") === 0', worker)
        self.assertTrue(any(
            item.get("source") == "/manifest.webmanifest"
            for item in vercel.get("headers", [])
        ))
        self.assertTrue(any(
            item.get("source") == "/sw.js"
            for item in vercel.get("headers", [])
        ))

    def test_service_worker_precache_contains_only_available_shell_files(self):
        worker = (WEB_ROOT / "sw.js").read_text(encoding="utf-8")
        shell_match = re.search(r"const APP_SHELL = \[(.*?)\];", worker, re.DOTALL)
        self.assertIsNotNone(shell_match)
        shell_paths = re.findall(r'"([^"]+)"', shell_match.group(1))
        self.assertIn("/", shell_paths)
        for shell_path in shell_paths:
            with self.subTest(path=shell_path):
                local_path = shell_path.split("?", 1)[0].lstrip("/")
                target = WEB_ROOT / (local_path or "index.html")
                self.assertTrue(target.is_file())

    def test_service_worker_never_caches_auth_api_or_logout_requests(self):
        worker = (WEB_ROOT / "sw.js").read_text(encoding="utf-8")
        app = (WEB_ROOT / "assets" / "app.js").read_text(encoding="utf-8")
        proxy = (WEB_ROOT / "api" / "proxy.js").read_text(encoding="utf-8")

        self.assertIn('if (request.method !== "GET" || request.headers.has("Authorization")) return;', worker)
        self.assertIn('url.pathname.indexOf("/api/") === 0', worker)
        self.assertIn('if (path === "/api/auth/logout") fetchOptions.cache = "no-store";', app)
        self.assertIn("res.setHeader('Cache-Control', 'private, no-store');", proxy)
        self.assertIn('navigator.onLine === false', app)
        self.assertIn("Please check your internet connection and try again.", app)

    def test_twa_and_capacitor_use_the_proposed_package_identity(self):
        config = json.loads(TWA_CONFIG_PATH.read_text(encoding="utf-8"))
        capacitor = json.loads((ROOT / "capacitor.config.json").read_text(encoding="utf-8"))

        self.assertEqual(config["packageId"], PACKAGE_ID)
        self.assertEqual(capacitor["appId"], PACKAGE_ID)
        self.assertEqual(config["host"], "learnpythonwithkite.vercel.app")
        self.assertEqual(config["startUrl"], "/")
        self.assertEqual(config["display"], "standalone")
        self.assertEqual(config["orientation"], "any")
        self.assertEqual(config["name"], "Learn Python with Kite")
        self.assertEqual(config["launcherName"], "Kite")
        self.assertFalse(config["enableNotifications"])
        self.assertEqual(config["fallbackType"], "customtabs")
        self.assertNotIn("fingerprints", config)
        self.assertFalse((TWA_CONFIG_PATH.parent / config["signingKey"]["path"]).exists())

        manifest_icon_urls = {
            "https://learnpythonwithkite.vercel.app" + icon["src"]
            for icon in self.manifest["icons"]
        }
        self.assertIn(config["iconUrl"], manifest_icon_urls)
        self.assertIn(config["maskableIconUrl"], manifest_icon_urls)

    def test_asset_links_header_is_prepared_and_real_fingerprint_is_not_fabricated(self):
        vercel = json.loads((WEB_ROOT / "vercel.json").read_text(encoding="utf-8"))
        assetlinks_headers = next(
            item["headers"] for item in vercel["headers"]
            if item.get("source") == "/.well-known/assetlinks.json"
        )
        self.assertIn(
            {"key": "Content-Type", "value": "application/json"},
            assetlinks_headers,
        )

        assetlinks_path = WEB_ROOT / ".well-known" / "assetlinks.json"
        if not assetlinks_path.exists():
            return

        statements = json.loads(assetlinks_path.read_text(encoding="utf-8"))
        self.assertIsInstance(statements, list)
        self.assertTrue(statements)
        for statement in statements:
            target = statement["target"]
            self.assertIn(
                "delegate_permission/common.handle_all_urls",
                statement["relation"],
            )
            self.assertEqual(target["namespace"], "android_app")
            self.assertEqual(target["package_name"], PACKAGE_ID)
            fingerprints = target["sha256_cert_fingerprints"]
            self.assertTrue(fingerprints)
            for fingerprint in fingerprints:
                self.assertRegex(
                    fingerprint,
                    r"^(?:[0-9A-F]{2}:){31}[0-9A-F]{2}$",
                )


if __name__ == "__main__":
    unittest.main()
