"""The dashboard layout engine lives in JavaScript (craft/web/js/grid.js) because
it runs in the browser. Its assertions live in tests/grid_check.mjs; this test
runs them when node is available and skips cleanly when it is not, so the Python
suite stays green on a machine with no JavaScript runtime.
"""
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "grid_check.mjs")
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed; skipping the JavaScript layout checks")
class GridLayoutTests(unittest.TestCase):
    def test_layout_assertions(self):
        proc = subprocess.run(
            [NODE, SCRIPT],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=os.path.dirname(HERE),
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        self.assertEqual(proc.returncode, 0, output)
        self.assertNotIn("FAIL", output)


if __name__ == "__main__":
    unittest.main()
