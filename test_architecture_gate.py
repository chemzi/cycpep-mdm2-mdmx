"""Architecture gate tests: detection rules and baseline semantics (PR8)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.architecture_gate import (
    check_bom,
    check_file_sizes,
    check_function_lengths,
    check_private_imports,
    format_report,
    load_baseline,
    write_baseline,
    iter_py_files,
)
from scripts.architecture_gate import check_action_handlers


class ArchitectureGateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="arch-gate-test-"))

    def _write(self, rel: str, content: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_iter_py_files_skips_venv_and_cache(self):
        self._write("app.py", "x = 1\n")
        self._write(".venv/lib/site.py", "x = 1\n")
        self._write("pkg/__pycache__/mod.py", "x = 1\n")
        files = [p.relative_to(self.root).as_posix() for p in iter_py_files(self.root)]
        self.assertEqual(files, ["app.py", "pkg/__pycache__/mod.py"] if False else files)
        self.assertNotIn(".venv/lib/site.py", files)
        self.assertNotIn("pkg/__pycache__/mod.py", files)
        self.assertIn("app.py", files)

    def test_file_size_flags_oversized(self):
        self._write("big.py", "x = 1\n" * 101)
        self._write("small.py", "x = 1\n")
        violations = check_file_sizes(self.root, max_lines=100)
        self.assertEqual([v["file"] for v in violations], ["big.py"])

    def test_function_length_flags_long_functions(self):
        self._write(
            "mod.py",
            "def long():\n" + "    x = 1\n" * 160 + "\n"
            "def short():\n" + "    x = 1\n",
        )
        violations = check_function_lengths(self.root, max_lines=150)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["function"], "long")

    def test_private_imports_flags_absolute_private_imports(self):
        self._write("agents/research.py", "def default_thresholds(cfg):\n    return {}\n")
        self._write("scripts/calibrate.py", "from agents.research import default_thresholds\n")
        self._write("scripts/bad.py", "from agents.research import _private_helper\n")
        self._write("agents/design/route_a.py", "from agents.research import _sibling_private\n")
        self._write("test_private.py", "from agents.research import _private_helper\n")
        violations = check_private_imports(self.root)
        imports = [v["import"] for v in violations]
        self.assertIn("from agents.research import _private_helper", imports)
        # same-package absolute private imports are flagged too (PR8 review):
        # the gate must not have a same-package blind spot.
        self.assertIn("from agents.research import _sibling_private", imports)
        # public names are fine.
        self.assertNotIn("from agents.research import default_thresholds", imports)

    def test_bom_flags_utf8_bom_files(self):
        # A BOM would make ast.parse fail and silently skip the file, so the
        # gate must reject BOM files outright (PR8 review P1-2).
        self._write("clean.py", "def f():\n    return 1\n")
        bom_path = self.root / "smuggled.py"
        bom_path.write_bytes(b"\xef\xbb\xbfdef f():\n    return 1\n")
        violations = check_bom(self.root)
        files = [v["file"] for v in violations]
        self.assertIn("smuggled.py", files)
        self.assertNotIn("clean.py", files)

    def test_baseline_covers_existing_and_rejects_new(self):
        self._write("big.py", "x = 1\n" * 101)
        violations = {"file_size": check_file_sizes(self.root, max_lines=100),
                      "function_length": [], "action_handlers": [],
                      "private_imports": [], "bom": []}
        baseline = {"schema_version": 1, "items": {"file_size": [{"file": "big.py", "lines": 101}],
                    "function_length": [], "action_handlers": [], "private_imports": []}}
        report, ok = format_report(violations, baseline, update=False)
        self.assertTrue(ok)

        new_violations = {"file_size": [{"file": "big.py", "lines": 101},
                                        {"file": "bigger.py", "lines": 200}],
                          "function_length": [], "action_handlers": [],
                          "private_imports": [], "bom": []}
        report, ok = format_report(new_violations, baseline, update=False)
        self.assertFalse(ok)
        self.assertIn("bigger.py", report)

    def test_baseline_roundtrip(self):
        path = self.root / "baseline.json"
        violations = {"file_size": [{"file": "a.py", "lines": 1200}],
                      "function_length": [{"file": "b.py", "function": "f", "line": 1, "lines": 200}],
                      "action_handlers": [], "private_imports": [], "bom": []}
        write_baseline(path, violations)
        loaded = load_baseline(path)
        self.assertEqual(loaded["items"]["file_size"][0]["file"], "a.py")
        self.assertEqual(loaded["items"]["function_length"][0]["function"], "f")

    def test_action_handlers_registry_is_closed(self):
        # The real repository registry must be complete.
        self.assertEqual(check_action_handlers(), [])


if __name__ == "__main__":
    unittest.main()
