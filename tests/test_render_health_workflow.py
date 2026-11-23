import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "render-health-check.yml"


class RenderHealthWorkflowTests(unittest.TestCase):
    def test_manual_checks_cover_production_endpoints_without_secrets(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotRegex(workflow, re.compile(r"^\s+schedule:", re.MULTILINE))
        self.assertIn("https://kite-backend-958r.onrender.com/health", workflow)
        self.assertIn("https://learnpythonwithkite.vercel.app/", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertIn("concurrency:", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("--connect-timeout 15 --max-time 90", workflow)
        self.assertIn("for attempt in 1 2 3 4", workflow)


if __name__ == "__main__":
    unittest.main()
