#!/usr/bin/env python3
"""Research pipeline orchestrator.

Runs the 6 调研 stages in order and records, for each stage, a tool_trace
(tool_name / tool_version / cmd / exit_code / duration_sec / output_sha256).
This is the *reproducible* backbone of the Research Agent: given network access
and biotite, `run_pipeline()` regenerates every provenance artifact from scratch.

Stages
------
1 search_pdb        RCSB Search API v2      -> pdb_search_results.json
2 enrich_pdb        RCSB Data GraphQL       -> pdb_enriched.json
3 compute_interface biotite (<4A interface) -> interface_per_structure.json
4 aggregate_pockets consensus pockets       -> pocket_report.json
5 superpose_analyze biotite CA superpose    -> pocket_differences.json
6 pubmed_search     NCBI E-utilities        -> pubmed_catalog.json

CLI
---
    python scripts/run_pipeline.py                 # full recompute into scripts/provenance/
    python scripts/run_pipeline.py --outdir /tmp/out --structdir /tmp/cif
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTDIR = os.path.join(HERE, "provenance")
DEFAULT_STRUCTDIR = os.path.join(HERE, "provenance", "_structures")

# Static, machine-readable description of each stage (also used by agents/research.py
# to build tool_trace entries when publishing already-computed artifacts).
STAGES = [
    {"stage": 1, "name": "pdb_search", "script": "search_pdb.py",
     "tool_name": "RCSB Search API", "tool_version": "v2",
     "endpoint": "https://search.rcsb.org/rcsbsearch/v2/query",
     "input_file": None, "output_file": "pdb_search_results.json"},
    {"stage": 2, "name": "enrich_peptide_complex", "script": "enrich_pdb.py",
     "tool_name": "RCSB Data GraphQL", "tool_version": "v1",
     "endpoint": "https://data.rcsb.org/graphql",
     "input_file": "pdb_search_results.json", "output_file": "pdb_enriched.json"},
    {"stage": 3, "name": "interface_residues", "script": "compute_interface.py",
     "tool_name": "biotite", "tool_version": None,
     "endpoint": "biotite.database.rcsb.fetch + heavy-atom<4.0A",
     "input_file": "pdb_enriched.json", "output_file": "interface_per_structure.json"},
    {"stage": 4, "name": "aggregate_pockets", "script": "aggregate_pockets.py",
     "tool_name": "python(collections.Counter)", "tool_version": "stdlib",
     "endpoint": "consensus: residue in >=50% of canonical structures",
     "input_file": "interface_per_structure.json", "output_file": "pocket_report.json"},
    {"stage": 5, "name": "superpose_differences", "script": "superpose_analyze.py",
     "tool_name": "biotite", "tool_version": None,
     "endpoint": "seq-align CA superpose (1YCR vs 3DAB); SASA/gatekeeper/floor/depth",
     "input_file": None, "output_file": "pocket_differences.json"},
    {"stage": 6, "name": "pubmed_search", "script": "pubmed_search.py",
     "tool_name": "NCBI E-utilities", "tool_version": "esearch/esummary/efetch",
     "endpoint": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
     "input_file": None, "output_file": "pubmed_catalog.json"},
]


def sha256(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def tool_versions():
    """Best-effort runtime versions of the scientific deps (may be absent)."""
    v = {"python": sys.version.split()[0]}
    for mod in ("biotite", "numpy", "requests"):
        try:
            v[mod] = __import__(mod).__version__
        except Exception:
            v[mod] = None
    return v


def resolve_versions(stage, versions):
    """Fill in the dynamic tool_version (e.g. biotite) at run/publish time."""
    tv = stage["tool_version"]
    if tv is None and stage["tool_name"] == "biotite":
        return versions.get("biotite") or "unknown"
    return tv


def run_pipeline(outdir=DEFAULT_OUTDIR, structdir=DEFAULT_STRUCTDIR, stages=None):
    """Execute each stage as a subprocess; return a list of tool_trace dicts."""
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(structdir, exist_ok=True)
    env = dict(os.environ, RESEARCH_OUTDIR=outdir, RESEARCH_STRUCTDIR=structdir)
    versions = tool_versions()
    traces = []
    for s in (stages or STAGES):
        script_path = os.path.join(HERE, s["script"])
        cmd = [sys.executable, script_path]
        t0 = time.time()
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        dur = round(time.time() - t0, 2)
        out_path = os.path.join(outdir, s["output_file"])
        trace = {
            "stage": s["stage"], "name": s["name"],
            "tool_name": s["tool_name"], "tool_version": resolve_versions(s, versions),
            "endpoint": s["endpoint"],
            "cmd": " ".join(["python", os.path.join("scripts", s["script"])]),
            "exit_code": proc.returncode, "duration_sec": dur,
            "input_file": s["input_file"], "output_file": s["output_file"],
            "output_sha256": sha256(out_path),
            "stderr_tail": (proc.stderr or "").strip().splitlines()[-3:],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        traces.append(trace)
        status = "OK" if proc.returncode == 0 else f"FAIL({proc.returncode})"
        print(f"[stage {s['stage']}] {s['name']:<24} {status:<10} "
              f"{dur:>7.2f}s  sha={str(trace['output_sha256'])[:12]}", file=sys.stderr)
        if proc.returncode != 0:
            print(proc.stderr[-800:], file=sys.stderr)
            break
    return traces


def main():
    ap = argparse.ArgumentParser(description="Run the MDM2/MDMX research pipeline.")
    ap.add_argument("--outdir", default=DEFAULT_OUTDIR)
    ap.add_argument("--structdir", default=DEFAULT_STRUCTDIR)
    args = ap.parse_args()
    traces = run_pipeline(args.outdir, args.structdir)
    report = {"generated": datetime.now(timezone.utc).isoformat(),
              "tool_versions": tool_versions(), "stages": traces}
    report_path = os.path.join(args.outdir, "run_report.json")
    json.dump(report, open(report_path, "w"), ensure_ascii=False, indent=2)
    ok = all(t["exit_code"] == 0 for t in traces) and len(traces) == len(STAGES)
    print(f"\nPipeline {'COMPLETE' if ok else 'INCOMPLETE'} -> {report_path}", file=sys.stderr)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
