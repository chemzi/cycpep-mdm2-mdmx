"""
Step 4: 跨结构聚合口袋残基 + 三口袋分类。

调用方式:
    python -m scripts.aggregate_pockets < data/interface_results.json > data/pocket_residues.json

���每个靶点（MDM2/MDMX），聚合所有肽段复合物中出现频率 ≥ 50% 的界面残基，
按已知三口袋归类（Phe19/Trp23/Leu26 pocket lining residues）。
"""

import json, sys
from collections import Counter

# 三口袋的参考残基映射（基于 1YCR/3DAB 坐标）
# 这些是口袋衬里残基的锚点定义
POCKET_DEFINITIONS = {
    "MDM2": {
        "Phe19_pocket": ["Gly58", "Ile61", "Met62", "Tyr67", "Gln72", "Val75", "Val93"],
        "Trp23_pocket": ["Leu54", "Leu57", "Gly58", "Ile61", "Val93"],
        "Leu26_pocket": ["Leu54", "Val93", "His96", "Ile99", "Tyr100"],
    },
    "MDMX": {
        "Phe19_pocket": ["Gly57", "Ile60", "Met61", "Tyr66", "Gln71", "Val74", "Val92"],
        "Trp23_pocket": ["Met53", "Leu56", "Gly57", "Ile60", "Val92", "Leu98"],
        "Leu26_pocket": ["Met53", "Val92", "Pro95", "Leu98", "Tyr99"],
    },
}

FREQ_THRESHOLD = 0.5  # 出现在超过 50% 结构中才算共识残基


def aggregate(target_name: str, interface_entries: list[dict]) -> dict:
    """聚合某一靶点的界面残基。"""
    # 统计每个残基（chain:res_id:res_name）出现次数
    residue_counter = Counter()
    n_structures = 0
    for entry in interface_entries:
        if entry.get("target") != target_name:
            continue
        residues = entry.get("interface_target_residues", [])
        if residues:
            n_structures += 1
            for r in residues:
                residue_counter[r] += 1

    if n_structures == 0:
        return {"n_structures": 0, "consensus_residues": {}, "pocket_residues": POCKET_DEFINITIONS[target_name]}

    # 频率 ≥ 50% 的共识残基
    consensus = {}
    for residue, count in residue_counter.most_common():
        freq = count / n_structures
        if freq >= FREQ_THRESHOLD:
            consensus[residue] = {"count": count, "frequency": round(freq, 3)}

    # 按口袋归类
    pocket_consensus = {}
    for pocket_name, reference_residues in POCKET_DEFINITIONS[target_name].items():
        pocket_consensus[pocket_name] = []
        for ref in reference_residues:
            for res_key in consensus:
                if ref in res_key:
                    pocket_consensus[pocket_name].append(res_key)
                    break

    return {
        "n_structures": n_structures,
        "consensus_residues": consensus,
        "pocket_consensus": pocket_consensus,
        "pocket_residues": POCKET_DEFINITIONS[target_name],
    }


def main() -> int:
    input_data = json.loads(sys.stdin.read())
    with_interface = input_data.get("with_interface", [])

    mdm2_entries = [e for e in with_interface if e.get("target") == "MDM2"]
    mdmx_entries = [e for e in with_interface if e.get("target") == "MDMX"]

    output = {
        "MDM2": aggregate("MDM2", with_interface),
        "MDMX": aggregate("MDMX", with_interface),
        "n_mdm2_structures": len(mdm2_entries),
        "n_mdmx_structures": len(mdmx_entries),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
