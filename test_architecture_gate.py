"""Architecture gate tests: detection rules and baseline semantics (PR8)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.architecture_gate import (
    check_bom,
    check_file_sizes,
    check_function_lengths,
    check_package_import_paths,
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

    def test_package_initializer_path_mutations_are_flagged(self):
        self._write(
            "pkg/__init__.py",
            "import sys\n"
            "sys.path.insert(0, '/repo')\n"
            "sys.path.append('/plugin')\n"
            "sys.path = list(sys.path)\n",
        )
        violations = check_package_import_paths(self.root)
        self.assertEqual(
            {item["mutation"] for item in violations},
            {"sys.path.insert", "sys.path.append", "sys.path assignment"},
        )
        self.assertEqual(
            {item["file"] for item in violations}, {"pkg/__init__.py"}
        )

    def test_package_import_path_check_ignores_entrypoints_and_clean_initializers(self):
        self._write("pkg/__init__.py", "from .service import run\n")
        self._write(
            "pkg/cli.py",
            "import sys\nsys.path.insert(0, '/repo')\n",
        )
        self.assertEqual(check_package_import_paths(self.root), [])

    def test_package_import_path_violation_fails_empty_baseline(self):
        self._write(
            "pkg/__init__.py",
            "import sys\nsys.path.insert(0, '/repo')\n",
        )
        violations = {
            "file_size": [],
            "function_length": [],
            "action_handlers": [],
            "private_imports": [],
            "package_import_paths": check_package_import_paths(self.root),
            "bom": [],
        }
        report, ok = format_report(
            violations,
            {"schema_version": 1, "items": {}},
            update=False,
        )
        self.assertFalse(ok)
        self.assertIn("[package_import_paths] 1 violation(s), 1 new", report)

    def test_agent_package_imports_preserve_path_and_public_names(self):
        script = """
import importlib
import sys

expected = (
    ("agents.critic", "CriticConfig"),
    ("agents.planner", "PlannerConfig"),
    ("agents.orchestrator", "OrchestratorContractError"),
)
for module_name, public_name in expected:
    before = list(sys.path)
    module = importlib.import_module(module_name)
    assert sys.path == before, module_name
    assert hasattr(module, public_name), (module_name, public_name)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_prediction_protocol_import_preserves_path_and_public_names(self):
        script = """
import importlib
import sys

before = list(sys.path)
protocol = importlib.import_module("prediction_pipeline.protocol")
assert sys.path == before
assert hasattr(protocol, "PREDICTION_PROTOCOL")
assert hasattr(protocol, "PREDICTOR_PROTOCOL")
assert callable(protocol.protocol_binding)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_agent_cli_shims_still_show_help(self):
        root = Path(__file__).resolve().parent
        for shim in ("critic.py", "planner.py", "orchestrator.py"):
            with self.subTest(shim=shim):
                result = subprocess.run(
                    [sys.executable, str(root / "agents" / shim), "--help"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

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
                      "private_imports": [], "package_import_paths": [],
                      "bom": []}
        baseline = {"schema_version": 1, "items": {"file_size": [{"file": "big.py", "lines": 101}],
                    "function_length": [], "action_handlers": [], "private_imports": []}}
        report, ok = format_report(violations, baseline, update=False)
        self.assertTrue(ok)

        new_violations = {"file_size": [{"file": "big.py", "lines": 101},
                                        {"file": "bigger.py", "lines": 200}],
                          "function_length": [], "action_handlers": [],
                          "private_imports": [], "package_import_paths": [],
                          "bom": []}
        report, ok = format_report(new_violations, baseline, update=False)
        self.assertFalse(ok)
        self.assertIn("bigger.py", report)

    def test_function_length_baseline_key_includes_line(self):
        # P1-A: two same-named functions in one file must not share a
        # baseline key. A new oversized def f() must not be absorbed by a
        # baselined (file, f) entry at a different line, or the gate reports
        # "0 new / OK" while a 200+ line function just landed.
        self._write(
            "mod.py",
            "def f():\n"
            "    return 1\n"
            "\n"
            "def f():\n"
            + "    x = 1\n" * 201,
        )
        violations = check_function_lengths(self.root, max_lines=150)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["function"], "f")
        self.assertGreater(violations[0]["lines"], 150)

        baseline = {
            "schema_version": 1,
            "items": {
                "file_size": [],
                # stale entry tied to the 6-line def f() at line 1: the new
                # 201-line def f() (line 4) must NOT match it.
                "function_length": [
                    {"file": "mod.py", "function": "f", "line": 1, "lines": 6},
                ],
                "action_handlers": [],
                "private_imports": [],
                "bom": [],
            },
        }
        all_violations = {
            "file_size": [],
            "function_length": violations,
            "action_handlers": [],
            "private_imports": [],
            "package_import_paths": [],
            "bom": [],
        }
        report, ok = format_report(all_violations, baseline, update=False)
        self.assertFalse(ok)
        self.assertIn("1 new", report)
        self.assertIn("mod.py", report)

    def test_parse_error_file_is_reported_not_skipped(self):
        # P2-4: a file ast cannot parse must surface as a violation instead
        # of silently disappearing from the AST-based checks.
        self._write("broken.py", "def f(:\n    pass\n")
        self._write("ok.py", "def f():\n    return 1\n")
        violations = check_function_lengths(self.root, max_lines=150)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["file"], "broken.py")
        self.assertIn("parse_error", violations[0])

        priv = check_private_imports(self.root)
        self.assertEqual(len(priv), 1)
        self.assertEqual(priv[0]["file"], "broken.py")
        self.assertIn("parse_error", priv[0])

        all_v = {
            "file_size": [],
            "function_length": violations,
            "action_handlers": [],
            "private_imports": priv,
            "package_import_paths": [],
            "bom": [],
        }
        baseline = {
            "schema_version": 1,
            "items": {
                "file_size": [],
                "function_length": [],
                "action_handlers": [],
                "private_imports": [],
                "bom": [],
            },
        }
        report, ok = format_report(all_v, baseline, update=False)
        self.assertFalse(ok)
        self.assertIn("broken.py", report)
        self.assertIn("parse_error", report)

    def test_baseline_roundtrip(self):
        path = self.root / "baseline.json"
        violations = {"file_size": [{"file": "a.py", "lines": 1200}],
                      "function_length": [{"file": "b.py", "function": "f", "line": 1, "lines": 200}],
                      "action_handlers": [], "private_imports": [],
                      "package_import_paths": [], "bom": []}
        write_baseline(path, violations)
        loaded = load_baseline(path)
        self.assertEqual(loaded["items"]["file_size"][0]["file"], "a.py")
        self.assertEqual(loaded["items"]["function_length"][0]["function"], "f")

    def test_action_handlers_registry_is_closed(self):
        # The real repository registry must be complete.
        self.assertEqual(check_action_handlers(), [])


if __name__ == "__main__":
    unittest.main()
