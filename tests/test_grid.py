"""The dashboard layout engine lives in JavaScript (craft/web/js/grid.js) because
it runs in the browser. Its assertions live in tests/grid_check.mjs; this test
runs them when node is available and skips cleanly when it is not, so the Python
suite stays green on a machine with no JavaScript runtime.
"""
import glob
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
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

    def test_every_browser_module_parses(self):
        """A stray paren in a view only shows up when that view loads; catch it here."""
        files = sorted(glob.glob(os.path.join(ROOT, "craft", "web", "js", "**", "*.js"), recursive=True))
        self.assertGreater(len(files), 15)
        for path in files:
            # `node --check` parses CommonJS by default, so check a .mjs copy
            with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
                fh.write(open(path, encoding="utf-8").read())
                temp = fh.name
            try:
                proc = subprocess.run([NODE, "--check", temp], capture_output=True, text=True, timeout=30)
            finally:
                os.unlink(temp)
            if proc.returncode != 0:
                self.fail(f"{os.path.relpath(path, ROOT)} does not parse:\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
