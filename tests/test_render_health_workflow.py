import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "render-health-check.yml"


class RenderHealthWorkflowTests(unittest.TestCase):
    def test_manual_check_uses_secret_and_bounded_nonoverlapping_requests(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotRegex(workflow, re.compile(r"^\s+schedule:", re.MULTILINE))
        self.assertIn("RENDER_HEALTH_URL: ${{ secrets.RENDER_HEALTH_URL }}", workflow)
        self.assertIn("concurrency:", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("--connect-timeout 10 --max-time 90", workflow)
        self.assertIn("for attempt in 1 2 3", workflow)
        self.assertNotIn("kite-backend-958r.onrender.com", workflow)


if __name__ == "__main__":
    unittest.main()
