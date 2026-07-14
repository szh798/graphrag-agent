from __future__ import annotations

import os
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class OfflineDemoContractTests(unittest.TestCase):
    def test_interview_demo_scripts_are_present_and_executable(self):
        expected_scripts = [
            PROJECT_ROOT / "scripts" / "start-demo.sh",
            PROJECT_ROOT / "scripts" / "stop-demo.sh",
            PROJECT_ROOT / "scripts" / "verify-demo.sh",
            PROJECT_ROOT / "scripts" / "package-offline-demo.sh",
        ]

        for script in expected_scripts:
            with self.subTest(script=script.name):
                self.assertTrue(script.exists(), f"{script} is missing")
                self.assertTrue(os.access(script, os.X_OK), f"{script} is not executable")

    def test_interview_demo_docs_are_present(self):
        expected_docs = [
            PROJECT_ROOT / "README-interview-demo.md",
            PROJECT_ROOT / "docs" / "offline-deployment-guide.md",
            PROJECT_ROOT / "docs" / "troubleshooting-offline-demo.md",
        ]

        for doc in expected_docs:
            with self.subTest(doc=doc.name):
                self.assertTrue(doc.exists(), f"{doc} is missing")
                self.assertGreater(doc.stat().st_size, 800, f"{doc} is too small to be useful")


if __name__ == "__main__":
    unittest.main()
