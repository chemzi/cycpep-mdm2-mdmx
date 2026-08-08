"""Architecture gate: CI-enforced anti-spaghetti checks (Roadmap PR8).

Checks (all pure-stdlib AST + file scans, no external dependencies):

1. file_size       -- repo-local .py files exceeding max lines (default 1000)
2. function_length -- functions/methods exceeding max lines (default 150)
3. action_handlers -- every executable planner action must have a real
                       Execution handler (reuses execution.action_registry)
4. private_imports -- absolute imports of underscore names from non-test
                      modules (test files are exempt by design); covers both
                      cross-package and same-package imports so a module can
                      never silently couple to a private name of a sibling
                      (relative imports stay exempt: they stay inside the
                      package boundary by construction)
5. bom              -- UTF-8 BOM files are rejected: ast.parse chokes on the
                      BOM byte, so a BOM would silently disable the
                      function-length and private-import checks for that file

Baseline semantics: existing violations live in architecture_baseline.json
and are allowed to remain (tracked debt that must only shrink); any NEW
violation fails the gate.  `--update-baseline` rewrites the baseline file
and is a maintenance command for maintainers only.

Usage:
    python scripts/architecture_gate.py                          # CI mode
    python scripts/architecture_gate.py --update-baseline       # regenerate
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_MAX_FILE_LINES = 1000
DEFAULT_MAX_FUNCTION_LINES = 150
DEFAULT_BASELINE = ROOT / "architecture_baseline.json"
BASELINE_SCHEMA_VERSION = 1

_EXCLUDED_PARTS = {"__pycache__", "node_modules"}


def iter_py_files(root: Path):
    """Yield repo-local .py files, skipping venvs / dot-dirs / caches."""
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if any(part.startswith(".") or part in _EXCLUDED_PARTS for part in parts):
            continue
        yield path


def check_file_sizes(root: Path, max_lines: int) -> list[dict]:
    violations = []
    for path in iter_py_files(root):
        with path.open(encoding="utf-8-sig") as handle:
            line_count = sum(1 for _ in handle)
        if line_count > max_lines:
            violations.append({
                "file": path.relative_to(root).as_posix(),
                "lines": line_count,
            })
    return violations


def check_function_lengths(root: Path, max_lines: int) -> list[dict]:
    violations = []
    for path in iter_py_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            # A file the gate cannot parse is invisible to AST checks (the
            # BOM blind spot all over again); report it instead of skipping.
            violations.append({
                "file": path.relative_to(root).as_posix(),
                "parse_error": f"{type(exc).__name__}: {exc}",
            })
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            if length > max_lines:
                violations.append({
                    "file": path.relative_to(root).as_posix(),
                    "function": node.name,
                    "line": node.lineno,
                    "lines": length,
                })
    return violations


def check_action_handlers() -> list[dict]:
    """Executable planner actions must map to a real Execution handler."""
    try:
        from execution.action_registry import validate_registry
        validate_registry()
        return []
    except Exception as exc:  # noqa: BLE001 -- the gate reports any breakage
        return [{"detail": f"executable action without handler: {exc}"}]


def check_private_imports(root: Path) -> list[dict]:
    """Absolute imports of underscore names in non-test code."""
    violations = []
    for path in iter_py_files(root):
        rel = path.relative_to(root)
        if rel.parts[0] == "test" or rel.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            violations.append({
                "file": rel.as_posix(),
                "parse_error": f"{type(exc).__name__}: {exc}",
            })
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if node.level:  # relative imports stay inside the package
                continue
            for alias in node.names:
                if alias.name.startswith("_"):
                    violations.append({
                        "file": rel.as_posix(),
                        "import": f"from {node.module} import {alias.name}",
                    })
    return violations


CHECK_ORDER = ["file_size", "function_length", "action_handlers", "private_imports", "bom"]


def check_bom(root: Path) -> list[dict]:
    """Reject UTF-8 BOM files that would bypass AST-based checks."""
    violations = []
    for path in iter_py_files(root):
        if path.read_bytes().startswith(b"\xef\xbb\xbf"):
            violations.append({"file": path.relative_to(root).as_posix()})
    return violations


def run_checks(root: Path, max_file_lines: int, max_function_lines: int) -> dict:
    return {
        "file_size": check_file_sizes(root, max_file_lines),
        "function_length": check_function_lengths(root, max_function_lines),
        "action_handlers": check_action_handlers(),
        "private_imports": check_private_imports(root),
        "bom": check_bom(root),
    }


def _item_key(check: str, item: dict) -> tuple:
    if check in ("file_size", "bom"):
        return ("file", item["file"])
    if check == "function_length":
        if "parse_error" in item:
            return ("file", item["file"], "parse_error")
        # Line is part of the identity: same-named functions in one file
        # (e.g. repeated __init__/run) must not share a baseline key, or a
        # new oversized function could be absorbed by a stale (file, name)
        # entry and slip through as "0 new" (P1-A).
        return ("file", item["file"], "function", item["function"], "line", item["line"])
    if check == "action_handlers":
        return ("detail", item["detail"])
    if "parse_error" in item:
        return ("file", item["file"], "parse_error")
    return ("file", item["file"], "import", item["import"])


def load_baseline(path: Path) -> dict:
    if not path.is_file():
        return {"schema_version": BASELINE_SCHEMA_VERSION, "items": {}}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("schema_version") != BASELINE_SCHEMA_VERSION:
        raise SystemExit(
            f"architecture baseline schema mismatch: {path} "
            f"(expected {BASELINE_SCHEMA_VERSION})"
        )
    return data


def write_baseline(path: Path, violations: dict) -> None:
    payload = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "items": {check: violations[check] for check in CHECK_ORDER},
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def format_report(violations: dict, baseline: dict, update: bool) -> tuple[str, bool]:
    lines = []
    new_total = 0
    baseline_items = {
        check: {
            _item_key(check, item)
            for item in baseline.get("items", {}).get(check, [])
        }
        for check in CHECK_ORDER
    }
    for check in CHECK_ORDER:
        items = violations[check]
        known = baseline_items[check]
        new_items = [item for item in items if _item_key(check, item) not in known]
        resolved = known - {_item_key(check, item) for item in items}
        new_total += len(new_items)
        lines.append(f"[{check}] {len(items)} violation(s), {len(new_items)} new")
        for item in new_items:
            lines.append(f"  NEW: {item}")
        for item in items:
            if _item_key(check, item) in known and not update:
                lines.append(f"  baseline: {item}")
        if resolved and not update:
            lines.append(f"  resolved: {len(resolved)} baseline item(s) no longer present")
    if update:
        lines.append("Baseline file rewritten with current violations.")
    ok = new_total == 0 and not update
    return "\n".join(lines), ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--max-file-lines", type=int, default=DEFAULT_MAX_FILE_LINES)
    parser.add_argument("--max-function-lines", type=int, default=DEFAULT_MAX_FUNCTION_LINES)
    parser.add_argument(
        "--update-baseline", action="store_true",
        help="rewrite the baseline file with current violations (maintenance)",
    )
    args = parser.parse_args(argv)

    violations = run_checks(ROOT, args.max_file_lines, args.max_function_lines)
    if args.update_baseline:
        write_baseline(args.baseline, violations)
        print(f"architecture baseline updated: {args.baseline}")
        return 0

    baseline = load_baseline(args.baseline)
    report, ok = format_report(violations, baseline, update=False)
    print(report)
    if not ok:
        print(
            "\nFAIL: new architecture violations found. "
            "Fix them, or (maintainers only) regenerate the baseline "
            "with --update-baseline."
        )
        return 1
    print("\nOK: no new architecture violations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
