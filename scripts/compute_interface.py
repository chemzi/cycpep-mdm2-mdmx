"""
Step 3: biotite - 计算肽段-靶点界面残基（重原子距离 < 4A）。
使用 mmCIF 文件 + 长度二分链匹配（6-50 AA = 肽段）。
"""
import json, sys, urllib.request
from pathlib import Path

try:
    import numpy as np
    from biotite.structure.io.pdbx import CIFFile, get_structure
except ImportError:
    print("[compute_interface] biotite 未安装。", file=sys.stderr)
    sys.exit(1)

CUTOFF_A = 4.0
MAX_PDBS = 10


def download_cif(pdb_id: str, target_dir: Path) -> Path | None:
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{pdb_id}.cif"
    if path.exists():
        return path
    try:
        urllib.request.urlretrieve(f"https://files.rcsb.org/download/{pdb_id}.cif", path)
    except Exception:
        return None
    return path


def find_peptide_chains(atoms) -> list[str]:
    """长度二分：6-50 AA = 肽段。"""
    chains = {}
    for cid in set(atoms.chain_id):
        ca = atoms[(atoms.chain_id == cid) & (atoms.atom_name == "CA")]
        chains[str(cid)] = len(ca)
    peptide = sorted([(c, l) for c, l in chains.items() if 6 <= l <= 50], key=lambda x: x[1])
    return [c for c, _ in peptide]


def compute_interface(pdb_id: str, cif_path: Path) -> dict:
    try:
        atoms = get_structure(CIFFile.read(cif_path), model=1)
    except Exception as e:
        return {"pdb_id": pdb_id, "error": str(e)}

    peptide_chains = find_peptide_chains(atoms)
    if not peptide_chains:
        return {"pdb_id": pdb_id, "error": "no peptide chain"}

    peptide_mask = np.zeros(atoms.array_length(), dtype=bool)
    for cid in peptide_chains:
        peptide_mask |= atoms.chain_id == cid

    pep_h = atoms[peptide_mask & (atoms.element != "H")]
    tgt_h = atoms[~peptide_mask & (atoms.element != "H")]
    if len(pep_h) == 0 or len(tgt_h) == 0:
        return {"pdb_id": pdb_id, "error": "no heavy atoms"}

    iface_t, iface_p = set(), set()
    for pa in pep_h:
        pc = pa.coord
        for ta in tgt_h:
            if np.linalg.norm(pc - ta.coord) < CUTOFF_A:
                iface_t.add((ta.chain_id, ta.res_id, ta.res_name))
                iface_p.add((pa.chain_id, pa.res_id, pa.res_name))

    tl = sorted(iface_t, key=lambda x: (x[0], x[1]))
    pl = sorted(iface_p, key=lambda x: (x[0], x[1]))
    return {
        "pdb_id": pdb_id,
        "n_interface_target_residues": len(tl),
        "n_interface_peptide_residues": len(pl),
        "interface_target_residues": [f"{c}:{r}{n}" for c, r, n in tl],
        "interface_peptide_residues": [f"{c}:{r}{n}" for c, r, n in pl],
        "matched_chains": peptide_chains,
    }


def main() -> int:
    input_data = json.loads(sys.stdin.read())
    complexes = input_data.get("peptide_complexes", [])[:MAX_PDBS]
    pdb_dir = Path("targets")
    results = []
    for entry in complexes:
        pdb_id = entry["pdb_id"]
        cif_path = download_cif(pdb_id, pdb_dir)
        if cif_path is None:
            results.append({"pdb_id": pdb_id, "error": "download failed"})
            continue
        iface = compute_interface(pdb_id, cif_path)
        iface["resolution"] = entry.get("resolution")
        iface["target"] = entry.get("target")
        results.append(iface)

    with_interface = [r for r in results if r.get("n_interface_target_residues", 0) > 0]
    print(json.dumps({"all": results, "with_interface": with_interface,
                       "n_with_interface": len(with_interface)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
