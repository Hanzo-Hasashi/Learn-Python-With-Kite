"""Browser regression tests for the static authentication entry screens.

The frontend is served exactly as the static Vercel deployment serves it (``web/`` as the
document root, with no API behind it), and driven in a real Chromium browser. The tests are
skipped when Playwright or a Chrome/Chromium browser is not installed.
"""
import functools
import http.server
import json
import threading
import unittest
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the local toolchain
    sync_playwright = None


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _launch_browser(playwright):
    for options in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
        try:
            return playwright.chromium.launch(headless=True, **options)
        except PlaywrightError:
            continue
    return None


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class AuthenticationRouteBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = functools.partial(_QuietHandler, directory=str(WEB_ROOT))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        cls.playwright = sync_playwright().start()
        cls.browser = _launch_browser(cls.playwright)
        if cls.browser is None:
            cls.playwright.stop()
            cls.server.shutdown()
            raise unittest.SkipTest("No Chromium-based browser is available")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 420, "height": 860})
        self.page_errors = []
        self.crashed = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.on("crash", lambda: self.crashed.append(True))

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.crashed, [], "the page renderer crashed")
        self.assertEqual(self.page_errors, [], "uncaught JavaScript errors")

    def open(self, hash_value=""):
        self.page.goto(self.base + hash_value, wait_until="load")
        self.page.wait_for_function(
            "() => { const d = document.getElementById('f').contentDocument;"
            " return Boolean(d && d.documentElement && d.documentElement.dataset.kiteBridge === 'ready'"
            " && d.querySelector('#sbody h2')); }"
        )
        return self.page.frame_locator("#f")

    def frame_eval(self, expression):
        return self.page.evaluate(
            "(expr) => document.getElementById('f').contentWindow.eval(expr)", expression
        )

    def heading(self):
        return self.frame_eval("document.querySelector('#sbody h2').textContent")

    def wait_for_heading(self, text):
        self.page.wait_for_function(
            "(text) => { const h = document.getElementById('f').contentDocument.querySelector('#sbody h2');"
            " return h && h.textContent === text; }",
            arg=text,
            timeout=5000,
        )

    def assert_form_enhanced_once(self, kind):
        # Let any pending MutationObserver callbacks run before counting.
        self.page.wait_for_timeout(300)
        counts = self.frame_eval(
            f"""(() => {{
              const form = document.querySelector('form[data-f="{kind}"]');
              return {{
                remember: form.querySelectorAll('input[name="remember_me"]').length,
                confirm: form.querySelectorAll('input[name="cpw"]').length,
                strength: form.querySelectorAll('.kite-password-strength').length,
                nodes: document.getElementById('sbody').getElementsByTagName('*').length
              }};
            }})()"""
        )
        self.assertEqual(counts["remember"], 1, counts)
        self.assertEqual(counts["confirm"], 1 if kind == "up" else 0, counts)
        self.assertEqual(counts["strength"], 1 if kind == "up" else 0, counts)
        self.assertLess(counts["nodes"], 200, counts)

    def test_continue_with_email_opens_sign_up_without_freezing(self):
        frame = self.open()
        frame.get_by_text("Continue with email").click(timeout=10000)
        self.wait_for_heading("Create your account")
        self.assertEqual(self.page.evaluate("location.hash"), "#sign-up")
        self.assert_form_enhanced_once("up")
        frame.locator("#nm").fill("Ada Lovelace", timeout=3000)

    def test_log_in_and_password_reset_screens_render(self):
        frame = self.open()
        frame.get_by_role("link", name="Log in").click(timeout=10000)
        self.wait_for_heading("Welcome back")
        self.assertEqual(self.page.evaluate("location.hash"), "#log-in")
        self.assert_form_enhanced_once("in")
        frame.get_by_text("Forgot password?").click(timeout=3000)
        self.wait_for_heading("Reset your password")
        self.assertEqual(self.page.evaluate("location.hash"), "#reset")

    def test_kite_password_reset_token_opens_new_password_form(self):
        self.open("#reset?token=audit-placeholder-token")
        self.wait_for_heading("Choose a new password")
        self.page.wait_for_timeout(150)
        self.assertEqual(self.page.evaluate("location.hash"), "")
        self.assertTrue(
            self.frame_eval("Boolean(document.querySelector('form[data-kite-reset]'))")
        )

    def test_supabase_password_reset_fragment_opens_new_password_form(self):
        self.open("#reset#access_token=audit-placeholder-token&type=recovery")
        self.wait_for_heading("Choose a new password")
        self.page.wait_for_timeout(150)
        self.assertEqual(self.page.evaluate("location.hash"), "")
        self.assertTrue(
            self.frame_eval("Boolean(document.querySelector('form[data-kite-reset]'))")
        )

    def test_switching_between_sign_up_and_log_in_stays_stable(self):
        frame = self.open("#sign-up")
        self.wait_for_heading("Create your account")
        frame.get_by_role("link", name="Log in").click(timeout=3000)
        self.wait_for_heading("Welcome back")
        frame.get_by_role("link", name="Create an account").click(timeout=3000)
        self.wait_for_heading("Create your account")
        self.assert_form_enhanced_once("up")

    def test_done_route_is_not_normalized_to_launch(self):
        self.open("#done")
        self.page.wait_for_timeout(300)
        self.assertEqual(self.page.evaluate("location.hash"), "#done")
        self.assertEqual(self.frame_eval("location.hash"), "#done")

    def test_unknown_route_still_falls_back_to_launch(self):
        self.open("#not-a-kite-route")
        self.assertEqual(self.page.evaluate("location.hash"), "#launch")

    def test_lenco_checkout_waits_for_server_verified_payment_status(self):
        requests = []
        verification_requests = []
        user = {"id": 7, "name": "Kite Learner", "email": "learner@example.test"}
        plan = {
            "id": "premium_monthly",
            "name": "Premium Monthly",
            "price": "K19.99",
            "amount": "19.99",
            "currency": "ZMW",
            "period": "month",
            "premium": True,
            "summary": "Monthly Premium access",
            "features": [],
        }
        annual_plan = {
            "id": "premium_annual",
            "name": "Premium Annual",
            "price": "K99.99",
            "amount": "99.99",
            "currency": "ZMW",
            "period": "year",
            "premium": True,
            "summary": "Annual Premium access",
            "features": [],
            "best_value": True,
            "savings": "K139.89",
            "percentage_savings": "58.3%",
        }

        def response_for(request):
            path = request.url.split("?", 1)[0]
            if path.endswith("/api/auth/session"):
                return {"user": user, "csrf_token": "test-csrf"}
            if path.endswith("/api/me"):
                return {**user, "settings": {}}
            if path.endswith("/api/plans"):
                return {
                    "plans": [plan, annual_plan],
                    "payment_providers": [
                        {"id": "lenco_airtel", "name": "Lenco - Airtel Money"},
                    ],
                    "mobile_money": {"enabled": False, "providers": [], "currency": "ZMW"},
                }
            if path.endswith("/api/subscription"):
                return {"plan": "free", "status": "none", "premium": False}
            if path.endswith("/api/subscription/registrations") and request.method == "POST":
                return {
                    "registration_id": "test-registration",
                    "plan": plan,
                    "phone_number": "****4567",
                    "subscription_status": "PENDING_PAYMENT",
                    "subscription_expires_at": None,
                    "renewal": "manual",
                    "premium_active": False,
                    "payment": {
                        "id": "test-payment",
                        "reference": "KITE-SUB-TEST",
                        "provider_id": None,
                        "amount": "19.99",
                        "currency": "ZMW",
                        "status": "NOT_INITIATED",
                        "failure_code": None,
                    },
                    "message": "Registration saved. Payment has not started.",
                }
            if path.endswith("/api/subscription/registrations/test-registration/payments"):
                requests.append(request.post_data_json)
                return {
                    "registration_id": "test-registration",
                    "plan": plan,
                    "phone_number": "****4567",
                    "subscription_status": "PENDING_PAYMENT",
                    "subscription_expires_at": None,
                    "renewal": "manual",
                    "premium_active": False,
                    "payment": {
                        "id": "test-payment",
                        "reference": "KITE-SUB-TEST",
                        "provider_id": "lenco_airtel",
                        "amount": "19.99",
                        "currency": "ZMW",
                        "status": "PENDING",
                        "failure_code": None,
                    },
                    "message": (
                        "Lenco payment request sent. Approve it on your selected "
                        "mobile-money phone; Kite will activate Premium only after "
                        "server-side verification."
                    ),
                }
            if path.endswith("/api/subscription/registrations/test-registration/verify"):
                verification_requests.append(request.post_data_json)
                return {
                    "registration_id": "test-registration",
                    "plan": plan,
                    "phone_number": "****4567",
                    "subscription_status": "ACTIVE",
                    "subscription_expires_at": "2026-12-19T00:00:00+00:00",
                    "renewal": "manual",
                    "premium_active": True,
                    "payment": {
                        "id": "test-payment",
                        "reference": "KITE-SUB-TEST",
                        "provider_id": "lenco_airtel",
                        "amount": "19.99",
                        "currency": "ZMW",
                        "status": "SUCCESSFUL",
                        "failure_code": None,
                    },
                    "message": "Verified payment received. Your prepaid Kite Premium access is active.",
                }
            return {}

        def handle_api(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(response_for(route.request)),
            )

        self.page.route(
            "https://fonts.googleapis.com/**",
            lambda route: route.fulfill(status=200, content_type="text/css", body=""),
        )
        self.page.route("**/api/**", handle_api)
        self.page.goto(self.base + "#preferences", wait_until="load")
        self.page.wait_for_function(
            "() => { const frame = document.getElementById('f');"
            " return Boolean(frame && frame.contentDocument &&"
            " frame.contentDocument.documentElement &&"
            " frame.contentDocument.documentElement.dataset &&"
            " frame.contentDocument.documentElement.dataset.kiteBridge === 'ready'); }",
            timeout=30000,
        )
        frame = self.page.frame_locator("#f")
        frame.locator("#kite-plan-options button[data-plan-id='premium_monthly']").wait_for()
        self.assertIn(
            "ZMW 19.99 / month",
            frame.locator("#kite-plan-options article[data-plan-id='premium_monthly']").inner_text(),
        )
        annual_text = frame.locator("#kite-plan-options article[data-plan-id='premium_annual']").inner_text()
        self.assertIn("ZMW 99.99 / year", annual_text)
        self.assertIn("Save ZMW 139.89", annual_text)
        frame.locator("#kite-plan-options button[data-plan-id='premium_monthly']").click()
        frame.locator("#kite-continue-registration").click()
        frame.locator("#kite-mm-phone").fill("97 123 4567")
        frame.locator("#kite-mm-register").click()
        frame.locator("#kite-payment-provider").wait_for(state="visible")
        frame.locator("#kite-payment-provider").select_option("lenco_airtel")
        frame.locator("#kite-payment-initiate").click()

        self.page.wait_for_function(
            "() => document.getElementById('f').contentDocument"
            ".getElementById('kite-payment-status').textContent === 'Pending'"
        )
        self.assertIn(
            "Approve it on your selected mobile-money phone",
            frame.locator("#kite-mm-status").inner_text(),
        )
        self.assertEqual(frame.locator("#kite-registration-price").inner_text(), "ZMW 19.99 / month")
        self.assertEqual(
            frame.locator("#kite-registration-status").inner_text(),
            "Pending payment",
        )
        self.assertEqual(frame.locator("#kite-payment-status").inner_text(), "Pending")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["provider_id"], "lenco_airtel")
        self.assertFalse(verification_requests)

        frame.locator("#kite-registration-check").click()
        self.page.wait_for_function(
            "() => document.getElementById('f').contentDocument"
            ".getElementById('kite-payment-status').textContent === 'Successful'"
        )
        self.assertEqual(frame.locator("#kite-registration-status").inner_text(), "Active")
        self.assertTrue(verification_requests)

    def test_guest_can_start_a_free_lesson(self):
        frame = self.open()
        frame.get_by_text("Start with a free lesson").click(timeout=10000)
        self.page.wait_for_function("() => location.hash === '#lesson'", timeout=5000)


if __name__ == "__main__":
    unittest.main()
