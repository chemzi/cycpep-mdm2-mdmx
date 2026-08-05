"""PR5 ProjectContext 测试：core/context + design/research/data_layer 去全局化。

覆盖：
- ProjectContext 构造 / 校验 / 多项目隔离 / 默认路径推导
- data_layer 惰性（import 无项目全局、env 生效、State/Evidence 走惰性路径）
- research 惰性 + run(project_config=...) 注入与恢复
- design DesignContext 对齐 ProjectContext
"""
import os, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST_ROOT = Path(tempfile.mkdtemp(prefix="cycpep-pr5-test-"))
# Set before any data_layer access so lazy path resolution stays in /tmp.
os.environ["CYCPEP_DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["CYCPEP_EVIDENCE_DIR"] = str(TEST_ROOT / "evidence")

sys.path.insert(0, str(ROOT))

from core.context import ProjectContext, ProjectPaths  # noqa: E402
from project_config import load_project_config  # noqa: E402


class ProjectContextTests(unittest.TestCase):
    def test_default_loads_environment_project(self):
        pc = ProjectContext.default()
        self.assertEqual(pc.project_id, load_project_config()["project_id"])
        self.assertEqual(pc.config["project_id"], pc.project_id)

    def test_from_config_normalizes(self):
        pc = ProjectContext.from_config(
            {"project_id": "keap1", "targets": [{"id": "KEAP1"}]}
        )
        self.assertEqual(pc.project_id, "keap1")
        self.assertEqual(pc.targets, ("KEAP1",))
        self.assertEqual(pc.config["modality"], "cyclic_peptide")

    def test_frozen_and_validation(self):
        pc = ProjectContext.from_config(
            {"project_id": "keap1", "targets": [{"id": "KEAP1"}]}
        )
        with self.assertRaises(Exception):
            pc.project_id = "x"  # frozen dataclass
        with self.assertRaises(ValueError):
            ProjectContext.from_config({"project_id": "x", "targets": []})
        with self.assertRaises(TypeError):
            ProjectContext(project_id="x", config="not-a-mapping")
        with self.assertRaises(TypeError):
            ProjectContext(
                project_id="x",
                config={"project_id": "y", "targets": [{"id": "A"}]},
                paths="not-a-ProjectPaths",
            )

    def test_paths_field_validation_and_coercion(self):
        with self.assertRaises(TypeError):
            ProjectPaths(data_dir=42)
        coerced = ProjectPaths(data_dir=str(TEST_ROOT / "custom"))
        self.assertIsInstance(coerced.data_dir, Path)
        self.assertEqual(coerced.data_dir, TEST_ROOT / "custom")

    def test_multi_project_paths_are_isolated(self):
        a = ProjectContext.from_config(
            {"project_id": "mdm2_mdmx", "targets": [{"id": "MDM2"}, {"id": "MDMX"}]}
        )
        b = ProjectContext.from_config(
            {"project_id": "keap1", "targets": [{"id": "KEAP1"}]}
        )
        self.assertNotEqual(a.resolve_paths().data_dir, b.resolve_paths().data_dir)
        self.assertIn("mdm2_mdmx", str(a.resolve_paths().data_dir))
        self.assertIn("keap1", str(b.resolve_paths().data_dir))

    def test_reference_project_keeps_flat_layout(self):
        pc = ProjectContext.from_config(
            {"project_id": "mdm2_mdmx_reference", "targets": [{"id": "MDM2"}]}
        )
        self.assertEqual(pc.resolve_paths().data_dir, ROOT / "data")
        self.assertEqual(pc.resolve_paths().evidence_dir, ROOT / "evidence")

    def test_explicit_paths_override_defaults(self):
        pc = ProjectContext.from_config(
            {"project_id": "keap1", "targets": [{"id": "KEAP1"}]},
            paths=ProjectPaths(data_dir=TEST_ROOT / "custom"),
        )
        self.assertEqual(pc.resolve_paths().data_dir, TEST_ROOT / "custom")
        self.assertIn("keap1", str(pc.resolve_paths().evidence_dir))


class DataLayerLazyTests(unittest.TestCase):
    def test_import_has_no_project_globals_in_namespace(self):
        import data_layer
        self.assertNotIn("ACTIVE_PROJECT_CONFIG", data_layer.__dict__)
        self.assertNotIn("DATA_DIR", data_layer.__dict__)
        self.assertNotIn("INDEX_PATH", data_layer.__dict__)

    def test_lazy_access_resolves_and_uses_env(self):
        import data_layer
        self.assertEqual(data_layer.DATA_DIR, TEST_ROOT / "data")
        self.assertEqual(data_layer.EVIDENCE_DIR, TEST_ROOT / "evidence")
        self.assertEqual(data_layer.STATE_PATH, TEST_ROOT / "data" / "state.json")
        self.assertEqual(data_layer.INDEX_PATH, TEST_ROOT / "data" / "candidate_index.csv")
        self.assertEqual(data_layer.ACTIVE_PROJECT_CONFIG["project_id"], "mdm2_mdmx_reference")

    def test_state_defaults_are_project_aware(self):
        import data_layer
        s = data_layer.State.load()
        self.assertEqual(s["project_id"], data_layer.ACTIVE_PROJECT_CONFIG["project_id"])
        self.assertEqual(
            sorted(s["design_budget"]),
            ["route_A_mdm2", "route_A_mdmx", "route_B", "route_C"],
        )

    def test_evidence_logger_writes_through_lazy_path(self):
        import data_layer
        event_id = data_layer.EvidenceLogger.log(
            "design", "test", {"probe": 1}, phase="design"
        )
        self.assertTrue(event_id)
        entries = data_layer.EvidenceLogger.get_all()
        self.assertTrue(any(e["event_id"] == event_id for e in entries))
        self.assertTrue((TEST_ROOT / "evidence" / "evidence_log.jsonl").exists())


class ResearchLazyTests(unittest.TestCase):
    def test_lazy_module_names(self):
        import agents.research as research
        self.assertEqual(
            research.PROJECT_CONFIG["project_id"], research._cfg()["project_id"]
        )
        self.assertIsInstance(research.CACHE_PATH, Path)
        self.assertIsInstance(research.THRESHOLDS_CACHE, Path)

    def test_run_injects_project_config_with_restore(self):
        import agents.research as research
        custom = {"project_id": "keap1", "targets": [{"id": "KEAP1"}]}
        seen = {}

        def fake_impl(state=None, force_recompute=False, skip_pipeline=False):
            seen["cfg"] = research._cfg()
            seen["cache"] = research.CACHE_PATH
            return {"ok": True}

        original = research._run_impl
        research._run_impl = fake_impl
        try:
            result = research.run(project_config=custom)
        finally:
            research._run_impl = original
        self.assertEqual(seen["cfg"]["project_id"], "keap1")
        self.assertIn("keap1", seen["cache"].name)
        self.assertEqual(result, {"ok": True})
        self.assertNotEqual(research._cfg()["project_id"], "keap1")


class DesignContextTests(unittest.TestCase):
    def test_design_accepts_project_context(self):
        from agents.design import Design, DesignContext
        pc = ProjectContext.from_config(
            {"project_id": "keap1", "targets": [{"id": "KEAP1"}]}
        )
        d = Design(pc)
        self.assertEqual(d.project_config["project_id"], "keap1")
        dc = DesignContext.from_project_context(pc)
        self.assertEqual(dc.project_config["project_id"], "keap1")

    def test_design_default_context_identity(self):
        from agents.design import config as design_config
        from agents.design import Design
        d = Design()
        self.assertIs(d.project_config, design_config.ACTIVE_PROJECT_CONFIG)


if __name__ == "__main__":
    unittest.main()
