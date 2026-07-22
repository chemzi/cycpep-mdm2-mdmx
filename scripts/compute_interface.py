#!/usr/bin/env python3
"""Step 1b/1c: Download verified peptide complexes and compute protein-peptide
interface residues with biotite (heavy-atom distance < 4.0 A).
Also assign per-anchor pockets (Phe19/Trp23/Leu26) for canonically p53-numbered peptides.
"""
import json
import os
import sys
import numpy as np
import biotite.database.rcsb as rcsb
import biotite.structure.io.pdbx as pdbx

CUTOFF = 4.0
MAX_RES = 2.8  # enforce the same resolution ceiling used at search time
OUTDIR = os.environ.get("RESEARCH_OUTDIR", "output")
STRUCTDIR = os.environ.get("RESEARCH_STRUCTDIR", "structures")
ANCHORS = [(19, "PHE", "Phe19_pocket"), (23, "TRP", "Trp23_pocket"), (26, "LEU", "Leu26_pocket")]

def load(pdb_id):
    path = rcsb.fetch(pdb_id, "cif", STRUCTDIR)
    cif = pdbx.CIFFile.read(path)
    arr = pdbx.get_structure(cif, model=1)
    return arr[arr.element != "H"]  # heavy atoms only

def centroid(arr, chain):
    return arr.coord[arr.chain_id == chain].mean(axis=0)

def min_dist_residues(dom, pep_coords):
    """Return domain residues (res_id, res_name) with any heavy atom < CUTOFF of pep_coords."""
    if len(pep_coords) == 0:
        return []
    hits = {}
    dc = dom.coord
    # chunked broadcasting
    for i in range(0, len(dc), 2000):
        sub = dc[i:i+2000]
        d = np.sqrt(((sub[:, None, :] - pep_coords[None, :, :]) ** 2).sum(-1)).min(axis=1)
        mask = d < CUTOFF
        for idx in np.where(mask)[0]:
            j = i + idx
            hits[int(dom.res_id[j])] = dom.res_name[j]
    return sorted(hits.items())

def analyze_structure(pdb_id, dom_chains, pep_chains):
    arr = load(pdb_id)
    present = set(np.unique(arr.chain_id))
    dom_chains = [c for c in dom_chains if c in present]
    pep_chains = [c for c in pep_chains if c in present]
    if not dom_chains or not pep_chains:
        return None
    pep_chain = pep_chains[0]
    # nearest domain chain to this peptide
    pc = centroid(arr, pep_chain)
    dom_chain = min(dom_chains, key=lambda c: np.linalg.norm(centroid(arr, c) - pc))
    dom = arr[arr.chain_id == dom_chain]
    pep = arr[arr.chain_id == pep_chain]

    interface = min_dist_residues(dom, pep.coord)

    # detect canonical p53 numbering on peptide (19=PHE,23=TRP,26=LEU)
    pep_resmap = {int(r): n for r, n in zip(pep.res_id, pep.res_name)}
    canonical = all(pep_resmap.get(rid) == name for rid, name, _ in ANCHORS)

    pockets = {}
    if canonical:
        for rid, name, label in ANCHORS:
            anchor_coords = pep.coord[pep.res_id == rid]
            pockets[label] = min_dist_residues(dom, anchor_coords)
    return {
        "pdb": pdb_id, "dom_chain": dom_chain, "pep_chain": pep_chain,
        "canonical_numbering": canonical,
        "interface_residues": [[r, n] for r, n in interface],
        "pockets": {k: [[r, n] for r, n in v] for k, v in pockets.items()},
    }

def main():
    enriched = json.load(open(os.path.join(OUTDIR, "pdb_enriched.json")))
    out = {}
    for target in ["MDM2", "MDMX"]:
        pep_complexes = [r for r in enriched[target]
                         if r["is_peptide_complex"]
                         and r["resolution"] is not None and r["resolution"] <= MAX_RES]
        per_struct = []
        for r in pep_complexes:
            dom_chains = []
            for e in r["domain_entities"]:
                dom_chains += e["auth_chains"]
            pep_chains = []
            for e in r["peptide_entities"]:
                pep_chains += e["auth_chains"]
            try:
                res = analyze_structure(r["pdb"], dom_chains, pep_chains)
            except Exception as ex:
                print(f"  !! {r['pdb']} failed: {ex}", file=sys.stderr)
                continue
            if res:
                res["resolution"] = r["resolution"]
                per_struct.append(res)
                print(f"  {target} {res['pdb']} dom={res['dom_chain']} pep={res['pep_chain']} "
                      f"canon={res['canonical_numbering']} n_iface={len(res['interface_residues'])}",
                      file=sys.stderr)
        out[target] = per_struct
    os.makedirs(OUTDIR, exist_ok=True)
    json.dump(out, open(os.path.join(OUTDIR, "interface_per_structure.json"), "w"), indent=2)
    print("\nSaved interface_per_structure.json", file=sys.stderr)

if __name__ == "__main__":
    main()
