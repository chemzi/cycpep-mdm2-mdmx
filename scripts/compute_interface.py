"""
Step 3: biotite — 计算肽段-靶点界面残基（重原子距离 < 4Å）。

调用方式:
    python -m scripts.compute_interface < data/enriched_results.json > data/interface_results.json

依赖: pip install biotite
"""

import json, sys, tempfile, os, urllib.request
from pathlib import Path
from hashlib import md5

try:
    import numpy as np
    from biotite.structure.io.pdb import PDBFile
    from biotite.structure import get_residues, filter_backbone
except ImportError:
    print("[compute_interface] biotite 未安装。pip install biotite numpy", file=sys.stderr)
    sys.exit(1)

CUTOFF_A = 4.0  # 重原子距离阈值


def download_pdb(pdb_id: str, target_dir: Path) -> Path:
    """下载 PDB 文件到本地。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{pdb_id}.pdb"
    if path.exists():
        return path
    url = f"https://files.rcsb.org/download/{pdb_id}.pdb"
    try:
        urllib.request.urlretrieve(url, path)
    except Exception:
        return None
    return path


def compute_interface(pdb_id: str, pdb_path: Path, peptide_chain_ids: list[str]) -> dict:
    """计算肽段与靶点之间的界面残基。"""
    try:
        pdb_file = PDBFile.read(pdb_path)
        atoms = pdb_file.get_structure(model=1)
    except Exception as e:
        return {"pdb_id": pdb_id, "error": str(e), "n_interface_residues": 0}

    # 分离肽段原子和靶点原子
    peptide_mask = np.zeros(atoms.array_length(), dtype=bool)
    target_mask = np.zeros(atoms.array_length(), dtype=bool)
    for chain_id in peptide_chain_ids:
        peptide_mask |= atoms.chain_id == chain_id
    target_mask = ~peptide_mask

    if not peptide_mask.any() or not target_mask.any():
        return {"pdb_id": pdb_id, "error": "no peptide or target atoms", "n_interface_residues": 0}

    peptide_atoms = atoms[peptide_mask]
    target_atoms = atoms[target_mask]

    # 计算重原子距离矩阵，找 < 4Å 的接触对
    # 取非氢原子
    peptide_heavy = peptide_atoms[peptide_atoms.element != "H"]
    target_heavy = target_atoms[target_atoms.element != "H"]

    if len(peptide_heavy) == 0 or len(target_heavy) == 0:
        return {"pdb_id": pdb_id, "error": "no heavy atoms", "n_interface_residues": 0}

    # 逐距离计算（大PDB用KD-Tree，这里结构不大直接算）
    interface_target_residues = set()
    interface_peptide_residues = set()

    for i, p_atom in enumerate(peptide_heavy):
        p_coord = p_atom.coord
        for j, t_atom in enumerate(target_heavy):
            dist = np.linalg.norm(p_coord - t_atom.coord)
            if dist < CUTOFF_A:
                interface_target_residues.add((
                    t_atom.chain_id,
                    t_atom.res_id,
                    t_atom.res_name,
                ))
                interface_peptide_residues.add((
                    p_atom.chain_id,
                    p_atom.res_id,
                    p_atom.res_name,
                ))

    # 按残基汇总
    target_residue_list = sorted(interface_target_residues, key=lambda x: (x[0], x[1]))
    peptide_residue_list = sorted(interface_peptide_residues, key=lambda x: (x[0], x[1]))

    return {
        "pdb_id": pdb_id,
        "n_interface_target_residues": len(target_residue_list),
        "n_interface_peptide_residues": len(peptide_residue_list),
        "interface_target_residues": [f"{c}:{r}{n}" for c, r, n in target_residue_list],
        "interface_peptide_residues": [f"{c}:{r}{n}" for c, r, n in peptide_residue_list],
    }


MAX_PDBS = 10  # 测试用：最多处理 10 个 PDB

def main() -> int:
    input_data = json.loads(sys.stdin.read())
    peptide_complexes = input_data.get("peptide_complexes", [])[:MAX_PDBS]

    pdb_dir = Path("targets")
    results = []
    for entry in peptide_complexes:
        pdb_id = entry["pdb_id"]
        peptide_ids = [c["chain_id"] for c in entry.get("peptide_chains", [])]

        pdb_path = download_pdb(pdb_id, pdb_dir)
        if pdb_path is None:
            results.append({"pdb_id": pdb_id, "error": "download failed", "n_interface_residues": 0})
            continue

        iface = compute_interface(pdb_id, pdb_path, peptide_ids)
        iface["resolution"] = entry.get("resolution")
        iface["target"] = entry.get("target")
        results.append(iface)

    # 只保留成功计算界面的
    with_interface = [r for r in results if r.get("n_interface_target_residues", 0) > 0]
    print(json.dumps({
        "all": results,
        "with_interface": with_interface,
        "n_with_interface": len(with_interface),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
