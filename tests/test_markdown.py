"""Exercise the actual client renderer with Node when available on the developer machine."""
import shutil
import subprocess
import unittest
from pathlib import Path


class MarkdownTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise client JavaScript")
    def test_formatting_and_untrusted_content(self):
        result = subprocess.run([shutil.which("node"), str(Path(__file__).with_suffix(".js"))],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
