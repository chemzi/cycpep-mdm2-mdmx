"""Consistency gates between runtime authorities and operator documentation."""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

from agents.design.config import COLABDESIGN_COMMIT
from prediction_pipeline.boltz_worker import BOLTZ2_CHECKPOINT_SHA256, BOLTZ_VERSION
from prediction_pipeline.execution_identity import PRODIGY_VERSION
from prediction_pipeline.rosetta_worker import PYROSETTA_VERSION


ROOT = Path(__file__).resolve().parent
DOCUMENT_PATHS = (
    ROOT / "README.md",
    ROOT / "docs" / "INSTALLATION.md",
    ROOT / "THIRD_PARTY.md",
    ROOT / "docs" / "workflow_launcher.md",
)


def _document(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _environment_selectors(path: Path, *, design: bool = False) -> set[str]:
    """Extract deployment selectors from production configuration source."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    selectors: set[str] = set()
    path_helpers = {"_path", "_optional_path", "_python_path", "_optional_python_path"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        function_name = node.func.id if isinstance(node.func, ast.Name) else None
        attribute_name = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if function_name in path_helpers or attribute_name == "get":
            selectors.add(first.value)
    selectors = {name for name in selectors if re.fullmatch(r"[A-Z][A-Z0-9_]+", name)}
    if design:
        selectors.discard("RUNNER_TEMP")  # CI fallback, not a deployment selector.
    else:
        selectors = {name for name in selectors if not name.endswith("_TIMEOUT")}
    return selectors


class RuntimeDocumentationConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = {path: _document(path) for path in DOCUMENT_PATHS}
        cls.installation = cls.documents[ROOT / "docs" / "INSTALLATION.md"]
        cls.inventory = cls.documents[ROOT / "THIRD_PARTY.md"]

    def test_relative_markdown_links_resolve(self):
        for source, content in self.documents.items():
            for raw_target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
                target = raw_target.split("#", 1)[0].strip()
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                with self.subTest(source=source.name, target=target):
                    self.assertTrue((source.parent / target).resolve().exists())

    def test_operator_paths_document_doctor_before_launch(self):
        for relative in ("README.md", "docs/INSTALLATION.md", "docs/workflow_launcher.md"):
            content = self.documents[ROOT / relative]
            doctor = content.find("python -m workflow doctor --project")
            launch = content.find("python -m workflow launch --project")
            with self.subTest(document=relative):
                self.assertGreaterEqual(doctor, 0)
                self.assertGreater(launch, doctor)

    def test_machine_enforced_identities_match_production_authorities(self):
        protocol = json.loads((ROOT / "protocols" / "design_v1.json").read_text(encoding="utf-8"))
        identities = (
            COLABDESIGN_COMMIT,
            BOLTZ_VERSION,
            BOLTZ2_CHECKPOINT_SHA256,
            PYROSETTA_VERSION,
            PRODIGY_VERSION,
            protocol["parameters"]["ligandmpnn"]["checkpoint"],
        )
        for identity in identities:
            with self.subTest(identity=identity, document="THIRD_PARTY.md"):
                self.assertIn(identity, self.inventory)
            with self.subTest(identity=identity, document="docs/INSTALLATION.md"):
                self.assertIn(identity, self.installation)

    def test_public_runtime_environment_selectors_are_documented(self):
        selectors = _environment_selectors(ROOT / "execution" / "config.py")
        selectors |= _environment_selectors(
            ROOT / "agents" / "design" / "config.py", design=True
        )
        documented = self.installation + "\n" + self.inventory
        for selector in sorted(selectors):
            with self.subTest(selector=selector):
                self.assertIn(selector, documented)


if __name__ == "__main__":
    unittest.main()
