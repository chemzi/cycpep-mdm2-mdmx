"""
Step 3: biotite - 计算肽段-靶点界面残基（重原子距离 < 4A）。
使用 enrich_pdb 已由 UniProt 判定的 target chain 和 peptide chain。仅在单独
调用 compute_interface() 且没有链注释时保留长度规则作为显式 fallback。
"""
import json, os, sys, urllib.request
from pathlib import Path

try:
    import numpy as np
    from biotite.structure.io.pdbx import CIFFile, get_structure
except ImportError:
    print("[compute_interface] biotite 未安装。", file=sys.stderr)
    sys.exit(1)

CUTOFF_A = 4.0
MAX_PDBS_PER_TARGET = int(os.environ.get("MAX_PDBS_PER_TARGET", "10"))


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


def _chain_ids(entities: list[dict]) -> list[str]:
    return sorted({
        str(chain_id)
        for entity in entities
        for chain_id in entity.get("chain_ids", [])
        if chain_id
    })


def _limit_complexes_by_target(
    all_complexes: list[dict], max_per_target: int
) -> tuple[list[str], list[dict]]:
    """Select a bounded structure set for every configured target."""
    target_names = sorted({
        str(entry.get("target"))
        for entry in all_complexes
        if entry.get("target")
    })
    selected = []
    for target in target_names:
        target_entries = [
            entry for entry in all_complexes
            if entry.get("target") == target
        ]
        selected.extend(target_entries[:max_per_target])
    return target_names, selected


def compute_interface(
    pdb_id: str,
    cif_path: Path,
    peptide_chains: list[str] | None = None,
    target_chains: list[str] | None = None,
) -> dict:
    try:
        atoms = get_structure(CIFFile.read(cif_path), model=1, use_author_fields=True)
    except Exception as e:
        return {"pdb_id": pdb_id, "error": str(e)}

    chain_source = "enriched_chain_ids"
    if not peptide_chains:
        peptide_chains = find_peptide_chains(atoms)
        chain_source = "length_fallback"
    if not peptide_chains:
        return {"pdb_id": pdb_id, "error": "no peptide chain"}
    if not target_chains:
        return {
            "pdb_id": pdb_id,
            "error": "no UniProt-classified target chain",
            "peptide_chains": peptide_chains,
        }

    peptide_mask = np.isin(atoms.chain_id, peptide_chains)
    target_mask = np.isin(atoms.chain_id, target_chains)

    pep_h = atoms[peptide_mask & (atoms.element != "H")]
    tgt_h = atoms[target_mask & (atoms.element != "H")]
    if len(pep_h) == 0 or len(tgt_h) == 0:
        return {
            "pdb_id": pdb_id,
            "error": "no heavy atoms on selected target/peptide chains",
            "peptide_chains": peptide_chains,
            "target_chains": target_chains,
        }

    iface_t, iface_p = set(), set()
    distances = np.linalg.norm(
        pep_h.coord[:, np.newaxis, :] - tgt_h.coord[np.newaxis, :, :],
        axis=2,
    )
    pep_indices, target_indices = np.where(distances < CUTOFF_A)
    for pep_index, target_index in zip(pep_indices, target_indices):
        pa = pep_h[pep_index]
        ta = tgt_h[target_index]
        iface_t.add((str(ta.chain_id), int(ta.res_id), str(ta.res_name)))
        iface_p.add((str(pa.chain_id), int(pa.res_id), str(pa.res_name)))

    tl = sorted(iface_t, key=lambda x: (x[0], x[1]))
    pl = sorted(iface_p, key=lambda x: (x[0], x[1]))
    return {
        "pdb_id": pdb_id,
        "n_interface_target_residues": len(tl),
        "n_interface_peptide_residues": len(pl),
        "interface_target_residues": [f"{c}:{r}{n}" for c, r, n in tl],
        "interface_peptide_residues": [f"{c}:{r}{n}" for c, r, n in pl],
        "matched_peptide_chains": peptide_chains,
        "matched_target_chains": target_chains,
        "chain_selection_source": chain_source,
    }


def main() -> int:
    input_data = json.loads(sys.stdin.read())
    all_complexes = input_data.get("peptide_complexes", [])
    target_names, complexes = _limit_complexes_by_target(
        all_complexes, MAX_PDBS_PER_TARGET
    )
    pdb_dir = Path("targets")
    results = []
    for entry in complexes:
        pdb_id = entry["pdb_id"]
        cif_path = download_cif(pdb_id, pdb_dir)
        if cif_path is None:
            results.append({"pdb_id": pdb_id, "error": "download failed"})
            continue
        peptide_chain_ids = _chain_ids(entry.get("peptide_chains", []))
        target_chain_ids = _chain_ids(entry.get("target_chains", []))
        iface = compute_interface(
            pdb_id,
            cif_path,
            peptide_chains=peptide_chain_ids,
            target_chains=target_chain_ids,
        )
        iface["resolution"] = entry.get("resolution")
        iface["target"] = entry.get("target")
        iface["target_uniprot"] = entry.get("target_uniprot")
        results.append(iface)

    with_interface = [r for r in results if r.get("n_interface_target_residues", 0) > 0]
    print(json.dumps({
        "all": results,
        "with_interface": with_interface,
        "n_with_interface": len(with_interface),
        "n_by_target": {
            target: sum(1 for r in with_interface if r.get("target") == target)
            for target in target_names
        },
        "max_pdbs_per_target": MAX_PDBS_PER_TARGET,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
