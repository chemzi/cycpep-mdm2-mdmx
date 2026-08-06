"""Design Agent command-line entry point."""

from __future__ import annotations

import argparse
import sys
import uuid

from data_layer import CandidateIndex  # noqa: E402

from .agent import Design  # noqa: E402
from .config import DESIGN_PIPELINE_VERSION  # noqa: E402
from .candidates import configure_candidate_updates, flush_candidate_updates  # noqa: E402


def main(argv=None) -> int:
    """Run the Design Agent CLI; returns the process exit code."""
    parser = argparse.ArgumentParser(
        description=f"Design Agent v{DESIGN_PIPELINE_VERSION}"
    )
    parser.add_argument("--route", choices=["A", "B", "C", "all"], default="all")
    parser.add_argument(
        "--target",
        default=None,
        help="configured target ID or PDB ID; defaults to the first approved target",
    )
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--lengths", default="10,12,14")
    parser.add_argument("--hotspots", default=None)
    parser.add_argument(
        "--chain",
        default=None,
        help="must match the approved target chain when provided",
    )
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--candidate-updates-path", default=None)
    parser.add_argument("--candidate-update-job-id", default=None)
    args = parser.parse_args(argv)
    configure_candidate_updates(args.candidate_updates_path)

    lengths = [int(value) for value in args.lengths.split(",")]
    target_spec = {}
    if args.chain:
        target_spec["chain"] = args.chain
    if args.target:
        target_spec["target_name"] = args.target
    if args.hotspots:
        target_spec["hotspots"] = args.hotspots
    design_config = {"n": args.n, "lengths": lengths, "seed": args.seed}

    # Context-aware entry point (Engineering Standard P1-1).  The default
    # context derives from the approved project config at call time.
    design = Design()

    all_candidates = []
    if args.route in ("A", "all"):
        print(f"[Route A v5] target={args.target}, n={args.n}, len={lengths}")
        result = design.design_rfpeptides(
            target_spec=target_spec, design_config=design_config
        )
        all_candidates.extend(result)
        print(f"[Route A] 完成: {len(result)} candidates")
    if args.route in ("B", "all"):
        print(f"[Route B v5] n={args.n}")
        result = design.design_motif_guided(
            target_spec=target_spec, design_config=design_config
        )
        all_candidates.extend(result)
        print(f"[Route B] 完成: {len(result)} candidates")
    if args.route in ("C", "all"):
        print(f"[Route C v5] n={args.n}")
        result = design.design_atsp_derived(
            target_spec=target_spec, design_config=design_config
        )
        all_candidates.extend(result)
        print(f"[Route C] 完成: {len(result)} candidates")

    flush_candidate_updates(args.candidate_update_job_id or f"design-{uuid.uuid4().hex}")
    print(f"\nDone: {len(all_candidates)} candidates")
    if not args.candidate_updates_path:
        print(CandidateIndex.stats())
    return 0


if __name__ == "__main__":
    sys.exit(main())
