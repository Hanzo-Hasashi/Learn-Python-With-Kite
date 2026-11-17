"""Browser regression tests for the static authentication entry screens.

The frontend is served exactly as the static Vercel deployment serves it (``web/`` as the
document root, with no API behind it), and driven in a real Chromium browser. The tests are
skipped when Playwright or a Chrome/Chromium browser is not installed.
"""
import functools
import http.server
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

    def test_guest_can_start_a_free_lesson(self):
        frame = self.open()
        frame.get_by_text("Start with a free lesson").click(timeout=10000)
        self.page.wait_for_function("() => location.hash === '#lesson'", timeout=5000)


if __name__ == "__main__":
    unittest.main()
