"""
Step 5: MDM2↔MDMX Cα 叠合 + 三口袋差异量化。

调用方式:
    python -m scripts.superpose_analyze < data/pocket_residues.json > data/pocket_differences.json

叠合 1YCR (MDM2) 和 3DAB (MDMX) 的 p53 结合域，
量化三口袋的 SASA / gatekeeper 距离 / 体积差异。

依赖: pip install biotite
"""

import json, re, sys, urllib.request
from pathlib import Path

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

# 叠合用残基范围（p53 结合域核心，覆盖 1YCR/3DAB 的轻微编号偏移）
SUPERPOSE_RESI_RANGE = (20, 115)
THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
}


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

    # 两个 PDB 的残基编号存在偏移，先按序列做全局比对再配对同源 Cα。
    mdm2_seq = "".join(THREE_TO_ONE.get(str(name), "X") for name in mdm2_ca.res_name)
    mdmx_seq = "".join(THREE_TO_ONE.get(str(name), "X") for name in mdmx_ca.res_name)
    aligned_pairs = _global_align_pairs(mdm2_seq, mdmx_seq)
    mdm2_indices = [left for left, _ in aligned_pairs]
    mdmx_indices = [right for _, right in aligned_pairs]
    mdm2_common = mdm2_ca[mdm2_indices]
    mdmx_common = mdmx_ca[mdmx_indices]

    if len(mdm2_common) < 10 or len(mdmx_common) < 10:
        return {"error": "too few common CA atoms for superposition", "rmsd": None}

    # 叠合
    mdmx_fitted, transform = superimpose(mdm2_common, mdmx_common)
    mdmx_transformed = mdmx_atoms.copy()
    mdmx_transformed.coord = transform.apply(mdmx_transformed.coord)

    # 计算 CA RMSD
    diffs = np.linalg.norm(mdm2_common.coord - mdmx_fitted.coord, axis=1)
    rmsd = float(np.sqrt(np.mean(diffs ** 2)))

    # 三口袋 SASA 计算（只用靶点自身原子，不算肽段）
    mdm2_sasa = _compute_sasa_for_pockets(mdm2_atoms, "MDM2")
    mdmx_sasa = _compute_sasa_for_pockets(mdmx_transformed, "MDMX")
    sasa_delta = {
        pocket: round(mdmx_sasa.get(pocket, 0.0) - mdm2_sasa.get(pocket, 0.0), 1)
        for pocket in set(mdm2_sasa) & set(mdmx_sasa)
    }

    return {
        "mdm2_ref": MDM2_REF_PDB,
        "mdmx_ref": MDMX_REF_PDB,
        "n_ca_atoms_superposed": len(aligned_pairs),
        "alignment_identity": round(
            sum(mdm2_seq[i] == mdmx_seq[j] for i, j in aligned_pairs)
            / len(aligned_pairs),
            3,
        ),
        "alignment_method": "Needleman-Wunsch sequence alignment followed by C-alpha fit",
        "ca_rmsd_A": round(rmsd, 3),
        "sasa": {
            "MDM2": mdm2_sasa,
            "MDMX": mdmx_sasa,
            "MDMX_minus_MDM2": sasa_delta,
        },
    }


def _global_align_pairs(sequence_a: str, sequence_b: str) -> list[tuple[int, int]]:
    """Needleman-Wunsch 全局比对，返回所有非 gap 的同源位置索引对。"""
    n, m = len(sequence_a), len(sequence_b)
    score = np.zeros((n + 1, m + 1), dtype=int)
    trace = np.zeros((n + 1, m + 1), dtype=np.int8)
    gap = -2
    score[:, 0] = np.arange(n + 1) * gap
    score[0, :] = np.arange(m + 1) * gap
    trace[1:, 0] = 1
    trace[0, 1:] = 2

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            candidates = (
                score[i - 1, j - 1] + (2 if sequence_a[i - 1] == sequence_b[j - 1] else -1),
                score[i - 1, j] + gap,
                score[i, j - 1] + gap,
            )
            direction = int(np.argmax(candidates))
            score[i, j] = candidates[direction]
            trace[i, j] = direction

    pairs = []
    i, j = n, m
    while i > 0 or j > 0:
        direction = trace[i, j]
        if i > 0 and j > 0 and direction == 0:
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif i > 0 and (j == 0 or direction == 1):
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def _compute_sasa_for_pockets(atoms, target_name: str) -> dict:
    """按当前结构所属靶标的残基编号计算三口袋 SASA。"""
    from biotite.structure import filter_amino_acids
    aa = atoms[filter_amino_acids(atoms)]
    try:
        sasa_values = sasa(aa, point_number=2000)
    except Exception:
        return {}

    from scripts.aggregate_pockets import POCKET_DEFINITIONS
    result = {}
    for pocket_name, ref_residues in POCKET_DEFINITIONS.get(target_name, {}).items():
        pocket_sasa = 0.0
        for ref in ref_residues:
            match = re.fullmatch(r"([A-Za-z]{3})(-?\d+)", ref)
            if not match:
                continue
            res_name, res_id = match.group(1).upper(), int(match.group(2))
            residue_mask = (aa.res_name == res_name) & (aa.res_id == res_id)
            if np.any(residue_mask):
                pocket_sasa += float(sasa_values[residue_mask].sum())
        result[pocket_name] = round(pocket_sasa, 1)
    return result


def main() -> int:
    input_data = json.loads(sys.stdin.read())
    pdb_dir = Path("targets")
    analysis = run_analysis(pdb_dir)

    # 从 input 中获取口袋残基定义
    pocket_residues = input_data

    output = dict(analysis)
    output["pocket_residues"] = pocket_residues
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
