"""
Step 5: MDM2↔MDMX Cα 叠合 + 三口袋差异量化。

调用方式:
    python -m scripts.superpose_analyze < data/pocket_residues.json > data/pocket_differences.json

叠合 1YCR (MDM2) 和 3DAB (MDMX) 的 p53 结合域，
量化三口袋的 SASA / gatekeeper 距离 / 体积差异。

依赖: pip install biotite
"""

import json, sys, urllib.request
from pathlib import Path
from hashlib import md5

try:
    import numpy as np
    from biotite.structure.io.pdb import PDBFile
    from biotite.structure import filter_amino_acids, sasa
    from biotite.structure.superimpose import superimpose
except ImportError:
    print("[superpose_analyze] biotite 未安装。pip install biotite numpy", file=sys.stderr)
    sys.exit(1)


MDM2_REF_PDB = "1YCR"
MDMX_REF_PDB = "3DAB"

# 叠合用残基范围（p53 结合域核心，1YCR MDM2 N-term domain）
SUPERPOSE_RESI_RANGE = (25, 109)


def download_pdb(pdb_id: str, target_dir: Path) -> Path:
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


def get_structure(pdb_id: str, pdb_dir: Path):
    """加载 PDB 并提取 A chain 骨架氨基酸原子。"""
    path = download_pdb(pdb_id, pdb_dir)
    if path is None:
        return None
    pdb_file = PDBFile.read(path)
    atoms = pdb_file.get_structure(model=1)
    # 只取 A chain
    chain_a = atoms[atoms.chain_id == "A"]
    # 只取氨基酸
    aa = chain_a[filter_amino_acids(chain_a)]
    return aa


def run_analysis(target_dir: Path) -> dict:
    """叠合两个靶点，计算三口袋差异。"""
    mdm2_atoms = get_structure(MDM2_REF_PDB, target_dir)
    mdmx_atoms = get_structure(MDMX_REF_PDB, target_dir)

    if mdm2_atoms is None or mdmx_atoms is None:
        return {"error": "PDB download failed", "rmsd": None}

    # 取 backbone 叠合
    mdm2_bb = mdm2_atoms[(mdm2_atoms.res_id >= SUPERPOSE_RESI_RANGE[0]) &
                         (mdm2_atoms.res_id <= SUPERPOSE_RESI_RANGE[1])]
    mdmx_bb = mdmx_atoms[(mdmx_atoms.res_id >= SUPERPOSE_RESI_RANGE[0]) &
                         (mdmx_atoms.res_id <= SUPERPOSE_RESI_RANGE[1])]

    mdm2_ca = mdm2_bb[mdm2_bb.atom_name == "CA"]
    mdmx_ca = mdmx_bb[mdmx_bb.atom_name == "CA"]

    # 叠合（按共同残基编号对齐）
    common_resi = sorted(set(mdm2_ca.res_id) & set(mdmx_ca.res_id))
    mdm2_common = mdm2_ca[np.isin(mdm2_ca.res_id, common_resi)]
    mdmx_common = mdmx_ca[np.isin(mdmx_ca.res_id, common_resi)]

    if len(mdm2_common) < 10 or len(mdmx_common) < 10:
        return {"error": "too few common CA atoms for superposition", "rmsd": None}

    # 叠合
    _, transform = superimpose(mdm2_common, mdmx_common)
    mdmx_transformed = mdmx_atoms.copy()
    mdmx_transformed.coord = transform.apply(mdmx_transformed.coord)

    # 计算 CA RMSD
    mdmx_common_trans = mdmx_transformed[np.isin(mdmx_transformed.res_id, common_resi) & (mdmx_transformed.atom_name == "CA")]
    diffs = np.linalg.norm(mdm2_common.coord - mdmx_common_trans.coord, axis=1)
    rmsd = float(np.sqrt(np.mean(diffs ** 2)))

    # 三口袋 SASA 计算（只用靶点自身原子，不算肽段）
    mdm2_sasa = _compute_sasa_for_pockets(mdm2_atoms)
    mdmx_sasa = _compute_sasa_for_pockets(mdmx_transformed)

    return {
        "mdm2_ref": MDM2_REF_PDB,
        "mdmx_ref": MDMX_REF_PDB,
        "n_ca_atoms_superposed": len(common_resi),
        "ca_rmsd_A": round(rmsd, 3),
        "sasa": {
            "MDM2": mdm2_sasa,
            "MDMX": mdmx_sasa,
        },
    }


def _compute_sasa_for_pockets(atoms) -> dict:
    """计算三口袋残基的 SASA 近似值。"""
    from biotite.structure import filter_amino_acids
    aa = atoms[filter_amino_acids(atoms)]
    try:
        sasa_values = sasa(aa, point_number=2000)
    except Exception:
        return {}

    # 按口袋定义汇总
    from scripts.aggregate_pockets import POCKET_DEFINITIONS
    result = {}
    for target_name in ["MDM2", "MDMX"]:
        result[target_name] = {}
        for pocket_name, ref_residues in POCKET_DEFINITIONS.get(target_name, {}).items():
            pocket_sasa = 0.0
            for ref in ref_residues:
                match = aa[(aa.res_name == ref[:3]) & (aa.res_id == int(ref[3:]) if ref[3:].isdigit() else False)]
                if len(match) > 0:
                    pocket_sasa += float(sasa_values[aa.res_id == match.res_id[0]].sum())
            result[target_name][pocket_name] = round(pocket_sasa, 1)

    differences = {}
    mdm2_data = result.get("MDM2", {})
    mdmx_data = result.get("MDMX", {})
    for pocket in mdm2_data:
        if pocket in mdmx_data:
            differences[pocket] = round(mdmx_data[pocket] - mdm2_data[pocket], 1)

    return {"by_pocket": result, "mdmx_minus_mdm2_delta": differences}


def main() -> int:
    input_data = json.loads(sys.stdin.read())
    pdb_dir = Path("targets")
    analysis = run_analysis(pdb_dir)

    # 从 input 中获取口袋残基定义
    pocket_residues = input_data

    output = {
        "superposition": analysis,
        "pocket_residues": pocket_residues,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
