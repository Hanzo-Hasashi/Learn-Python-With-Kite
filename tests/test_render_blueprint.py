import re
import unittest
from pathlib import Path


class RenderBlueprintTests(unittest.TestCase):
    def test_blueprint_defines_only_python_playground_service(self):
        root = Path(__file__).resolve().parent.parent
        blueprint = (root / "render.yaml").read_text(encoding="utf-8")

        names = re.findall(r"(?m)^    name:\s*(.+?)\s*$", blueprint)
        self.assertEqual(names, ["kite-playground"], names)
        self.assertNotIn("kite-backend", blueprint)
        self.assertIn("runtime: docker", blueprint)
        self.assertIn("dockerfilePath: ./runner/Dockerfile", blueprint)
        self.assertIn("dockerContext: .", blueprint)
        self.assertIn("healthCheckPath: /health", blueprint)
        self.assertIn("RUNNER_SHARED_SECRET", blueprint)


if __name__ == "__main__":
    unittest.main()
