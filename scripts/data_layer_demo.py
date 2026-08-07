"""Standalone data_layer smoke demo.

Moved out of data_layer.py's ``__main__`` block (PR8) so the core module
stays under the architecture-gate file-size limit.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_layer import CandidateIndex, EvidenceLogger, State


def main() -> None:
    print("=== data_layer.py 自检 ===")
    
    # 测试State
    s = State.load()
    print(f"State loaded: phase={s['phase']}, round={s['round']}")
    
    # 测试日志
    eid = EvidenceLogger.log("system", "test", {"msg": "冒烟测试"},
                              targets=["both"], phase="research")
    print(f"Log written: event_id={eid}")
    
    # 测试候选索引
    CandidateIndex.add({
        "candidate_id": "C0001",
        "sequence": "GFEWALAAK",
        "source_route": "route_A_mdm2_first",
        "source_batch": "batch_mdm2_len10"
    })
    CandidateIndex.add({
        "candidate_id": "C0002",
        "sequence": "PFNWALGGS",
        "source_route": "route_A_mdmx_first",
        "source_batch": "batch_mdmx_len12"
    })
    
    # 模拟评分
    CandidateIndex.update_score("C0001", {
        "monomer_plddt": 0.85,
        "self_rmsd": 1.2,
        "layer1_pass": "True",
        "iptm_mdm2": 0.84,
        "iptm_mdmx": 0.72,
        "dual_score": 0.72,
        "asymmetry": 0.12
    })
    CandidateIndex.update_score("C0002", {
        "monomer_plddt": 0.78,
        "self_rmsd": 1.8,
        "layer1_pass": "True",
        "iptm_mdm2": 0.61,
        "iptm_mdmx": 0.79,
        "dual_score": 0.64,
        "asymmetry": 0.18
    })
    
    print(f"\n索引表统计: {CandidateIndex.stats()}")
    print(f"\nTop 候选: {CandidateIndex.top_n(2)}")
    print("\n日志条目数:", len(EvidenceLogger.get_all()))
    print("自检通过")


if __name__ == "__main__":
    main()
