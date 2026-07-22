"""
Step 3: biotite - 计算肽段-靶点界面残基（重原子距离 < 4A）。

调用方式:
    python -m scripts.compute_interface < data/enriched_results.json > data/interface_results.json

chain ID 映射: enrich 返回 rcsb_id(如 "5VK1_2"), PDB 文件用单字母链标识(如 "B")。
通过肽段序列匹配找到正确的 PDB 链。
"""

import json, sys, urllib.request
from pathlib import Path

try:
    import numpy as np
    from biotite.structure.io.pdb import PDBFile
except ImportError:
    print("[compute_interface] biotite 未安装。pip install biotite numpy", file=sys.stderr)
    sys.exit(1)

CUTOFF_A = 4.0
MAX_PDBS = 10

# 三字母→单字母氨基酸转换表
AA3TO1 = {
    "ALA":"A","CYS":"C","ASP":"D","GLU":"E","PHE":"F",
    "GLY":"G","HIS":"H","ILE":"I","LYS":"K","LEU":"L",
    "MET":"M","ASN":"N","PRO":"P","GLN":"Q","ARG":"R",
    "SER":"S","THR":"T","VAL":"V","TRP":"W","TYR":"Y",
    "MSE":"M","SEC":"U","PYL":"O","HYP":"P","MLY":"K",
}


def download_pdb(pdb_id: str, target_dir: Path) -> Path | None:
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{pdb_id}.pdb"
    if path.exists():
        return path
    try:
        urllib.request.urlretrieve(f"https://files.rcsb.org/download/{pdb_id}.pdb", path)
    except Exception:
        return None
    return path


def get_chain_info(atoms) -> dict[str, dict]:
    """从 PDB atom array 提取每条链的序列长度和单字母序列。"""
    chains = {}
    for chain_id in set(atoms.chain_id):
        chain_atoms = atoms[atoms.chain_id == chain_id]
        # 取 CA 原子获取残基序列
        ca_atoms = chain_atoms[chain_atoms.atom_name == "CA"]
        res_names = [str(r) for r in ca_atoms.res_name]
        seq = "".join(AA3TO1.get(rn, "X") for rn in res_names)
        chains[str(chain_id)] = {
            "length": len(seq),
            "sequence": seq,
        }
    return chains


def match_peptide_chains(atoms, peptide_chains: list[dict]) -> list[str]:
    """将 enrich 的肽段信息匹配到 PDB 的真实链标识。

    策略: 1) 序列相似匹配（去掉 X/UNK）2) 长度匹配（肽段 6-50, 找最短链）
    """
    chain_info = get_chain_info(atoms)
    matched = []
    # 目标肽段数量
    n_expected = len(peptide_chains) if peptide_chains else 1

    def _clean(seq):
        return seq.replace("X", "").replace("U", "")

    for pc in peptide_chains:
        pc_seq = _clean(pc.get("sequence", ""))
        pc_len = len(pc_seq)

        best_cid = None
        # 策略 1: 序列相似匹配
        for cid, info in chain_info.items():
            if cid in matched:
                continue
            cleaned = _clean(info["sequence"])
            if pc_seq and cleaned == pc_seq:
                best_cid = cid
                break

        # 策略 2: 找最短的未匹配短链（长度相近）
        if best_cid is None:
            short_chains = [(cid, info) for cid, info in chain_info.items()
                           if cid not in matched and 6 <= info["length"] <= 50 and info["length"] <= 50]
            if short_chains:
                short_chains.sort(key=lambda x: x[1]["length"])
                best_cid = short_chains[0][0]

        if best_cid:
            matched.append(best_cid)

    # 如果 enrich 没有提供肽段信息，直接找最短链
    if not matched and not peptide_chains:
        short_chains = [(cid, info) for cid, info in chain_info.items()
                       if 6 <= info["length"] <= 50]
        if short_chains:
            short_chains.sort(key=lambda x: x[1]["length"])
            matched = [short_chains[0][0]]

    return matched[:n_expected]


def compute_interface(pdb_id: str, pdb_path: Path, peptide_chain_ids: list[str]) -> dict:
    """计算肽段与靶点之间的界面残基。"""
    try:
        pdb_file = PDBFile.read(pdb_path)
        atoms = pdb_file.get_structure(model=1)
    except Exception as e:
        return {"pdb_id": pdb_id, "error": str(e), "n_interface_residues": 0}

    # 分离肽段原子和靶��原子
    peptide_mask = np.zeros(atoms.array_length(), dtype=bool)
    for chain_id in peptide_chain_ids:
        peptide_mask |= atoms.chain_id == chain_id
    target_mask = ~peptide_mask

    if not peptide_mask.any() or not target_mask.any():
        return {"pdb_id": pdb_id, "error": "no peptide or target atoms", "n_interface_residues": 0}

    peptide_heavy = atoms[peptide_mask & (atoms.element != "H")]
    target_heavy = atoms[target_mask & (atoms.element != "H")]

    if len(peptide_heavy) == 0 or len(target_heavy) == 0:
        return {"pdb_id": pdb_id, "error": "no heavy atoms", "n_interface_residues": 0}

    # 计算界面残基
    interface_target_residues = set()
    interface_peptide_residues = set()

    for p_atom in peptide_heavy:
        p_coord = p_atom.coord
        for t_atom in target_heavy:
            if np.linalg.norm(p_coord - t_atom.coord) < CUTOFF_A:
                interface_target_residues.add((t_atom.chain_id, t_atom.res_id, t_atom.res_name))
                interface_peptide_residues.add((p_atom.chain_id, p_atom.res_id, p_atom.res_name))

    target_residue_list = sorted(interface_target_residues, key=lambda x: (x[0], x[1]))
    peptide_residue_list = sorted(interface_peptide_residues, key=lambda x: (x[0], x[1]))

    return {
        "pdb_id": pdb_id,
        "n_interface_target_residues": len(target_residue_list),
        "n_interface_peptide_residues": len(peptide_residue_list),
        "interface_target_residues": [f"{c}:{r}{n}" for c, r, n in target_residue_list],
        "interface_peptide_residues": [f"{c}:{r}{n}" for c, r, n in peptide_residue_list],
    }


def main() -> int:
    input_data = json.loads(sys.stdin.read())
    peptide_complexes = input_data.get("peptide_complexes", [])[:MAX_PDBS]

    pdb_dir = Path("targets")
    results = []
    for entry in peptide_complexes:
        pdb_id = entry["pdb_id"]
        peptide_chains_data = entry.get("peptide_chains", [])

        pdb_path = download_pdb(pdb_id, pdb_dir)
        if pdb_path is None:
            results.append({"pdb_id": pdb_id, "error": "download failed", "n_interface_residues": 0})
            continue

        # 读取 PDB 并匹配真实链标识
        try:
            atoms = PDBFile.read(pdb_path).get_structure(model=1)
            real_chain_ids = match_peptide_chains(atoms, peptide_chains_data)
        except Exception:
            real_chain_ids = []

        if not real_chain_ids:
            results.append({"pdb_id": pdb_id, "error": "chain matching failed", "n_interface_residues": 0,
                          "enrich_chains": [c.get("chain_id","?") for c in peptide_chains_data]})
            continue

        iface = compute_interface(pdb_id, pdb_path, real_chain_ids)
        iface["resolution"] = entry.get("resolution")
        iface["target"] = entry.get("target")
        iface["matched_chains"] = real_chain_ids
        results.append(iface)

    with_interface = [r for r in results if r.get("n_interface_target_residues", 0) > 0]
    print(json.dumps({
        "all": results,
        "with_interface": with_interface,
        "n_with_interface": len(with_interface),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
