#!/usr/bin/env python3
"""Step 1d: Superimpose MDM2 (1YCR) and MDMX (3DAB) p53-binding domains via
sequence-alignment-based Calpha fit; quantify pocket differences:
 - residue equivalence table for pocket-lining residues
 - per-pocket solvent-accessible surface area (apo domain) -> pocket size/openness
 - gatekeeper side-chain protrusion (MDM2 Leu54 vs MDMX Met53) at Phe19 entrance
 - Leu26 pocket floor occlusion (MDMX Tyr99) vs MDM2 His96 -> depth
 - burial depth of bound p53 anchors
"""
import json
import os
import numpy as np
import biotite.database.rcsb as rcsb
import biotite.structure.io.pdbx as pdbx
import biotite.structure as struc
import biotite.sequence as seq
import biotite.sequence.align as align

OUTDIR = os.environ.get("RESEARCH_OUTDIR", "output")
STRUCTDIR = os.environ.get("RESEARCH_STRUCTDIR", "structures")

POCKETS_MDM2 = {"Phe19_pocket": [58, 61, 62, 67, 72, 75, 93],
                "Trp23_pocket": [54, 57, 58, 61, 93],
                "Leu26_pocket": [54, 93, 96, 99, 100]}
POCKETS_MDMX = {"Phe19_pocket": [57, 60, 61, 66, 71, 74, 92],
                "Trp23_pocket": [53, 56, 57, 60, 92, 98],
                "Leu26_pocket": [53, 92, 95, 98, 99]}
ALL_POCKET_MDM2 = sorted(set(sum(POCKETS_MDM2.values(), [])))

def load(pdb_id):
    path = rcsb.fetch(pdb_id, "cif", STRUCTDIR)
    arr = pdbx.get_structure(pdbx.CIFFile.read(path), model=1)
    return arr[arr.element != "H"]

def dchain(arr, c):
    return arr[(arr.chain_id == c) & struc.filter_amino_acids(arr)]

def ca_seq(domain):
    ca = domain[domain.atom_name == "CA"]
    letters = "".join(seq.ProteinSequence.convert_letter_3to1(n) for n in ca.res_name)
    return ca, seq.ProteinSequence(letters), ca.res_id, ca.res_name

def sidechain_tip(arr, chn, resid, atom):
    m = (arr.chain_id == chn) & (arr.res_id == resid) & (arr.atom_name == atom)
    return arr.coord[m][0] if m.sum() else None

def per_res_sasa(domain):
    sasa = struc.sasa(domain, vdw_radii="Single")
    out = {}
    for rid in np.unique(domain.res_id):
        m = (domain.res_id == rid) & ~np.isin(domain.atom_name, ["N", "CA", "C", "O"])
        vals = sasa[m]; out[int(rid)] = float(np.nansum(vals[~np.isnan(vals)]))
    return out

# --- Load ---
mdm2 = load("1YCR"); mdmx = load("3DAB")
mdm2_dom = dchain(mdm2, "A"); mdmx_dom = dchain(mdmx, "A")
mdm2_pep = mdm2[mdm2.chain_id == "B"]; mdmx_pep = mdmx[mdmx.chain_id == "B"]

# --- Sequence align domains, build matched CA ---
ca2, s2, rid2, rn2 = ca_seq(mdm2_dom)
cax, sx, ridx, rnx = ca_seq(mdmx_dom)
matrix = align.SubstitutionMatrix.std_protein_matrix()
aln = align.align_optimal(s2, sx, matrix, gap_penalty=(-10, -1))[0]
trace = aln.trace
mask = (trace[:, 0] != -1) & (trace[:, 1] != -1)
idx2 = trace[mask, 0]; idxx = trace[mask, 1]
matched2 = ca2[idx2]; matchedx = cax[idxx]
n_matched = int(mask.sum())

# residue equivalence map MDM2 res_id -> MDMX res_id
eqmap = {int(rid2[i]): (int(ridx[j]), rnx[j]) for i, j in zip(idx2, idxx)}

# --- Superpose MDMX onto MDM2 using matched CA ---
fitted_matchedx, transform = struc.superimpose(matched2, matchedx)
rmsd = struc.rmsd(matched2.coord, fitted_matchedx.coord)
fitted_mdmx = transform.apply(mdmx)
print(f"Sequence identity-based CA superposition: {n_matched} matched CA, RMSD = {rmsd:.2f} A")

# pocket residue equivalence
equiv_pocket = []
for r in ALL_POCKET_MDM2:
    nm2 = rn2[list(rid2).index(r)] if r in rid2 else "?"
    if r in eqmap:
        rx, nx = eqmap[r]; equiv_pocket.append((r, nm2, rx, nx))
    else:
        equiv_pocket.append((r, nm2, -1, "?"))

result = {"superposition_rmsd_A": round(float(rmsd), 2), "n_matched_CA": n_matched,
          "residue_equivalence_pockets": [[f"{an}{a}", (f"{bn}{b}" if b > 0 else "gap")] for a, an, b, bn in equiv_pocket],
          "pocket_sasa": {}, "gatekeeper": {}, "leu26_floor": {}, "anchor_depth": {}}
print("\n--- Pocket residue equivalence (MDM2 -> MDMX) ---")
for a, an, b, bn in equiv_pocket:
    print(f"  {an}{a} -> {bn}{b}")

# --- Per-pocket apo SASA ---
sasa2 = per_res_sasa(mdm2_dom); sasax = per_res_sasa(mdmx_dom)
print("\n--- Per-pocket apo side-chain SASA (A^2) ---")
for p in POCKETS_MDM2:
    v2 = sum(sasa2.get(r, 0) for r in POCKETS_MDM2[p])
    vx = sum(sasax.get(r, 0) for r in POCKETS_MDMX[p])
    result["pocket_sasa"][p] = {"MDM2": round(v2, 1), "MDMX": round(vx, 1),
                                "diff_MDMX_minus_MDM2": round(vx - v2, 1)}
    print(f"  {p}: MDM2={v2:6.1f}  MDMX={vx:6.1f}  d(MDMX-MDM2)={vx-v2:+6.1f}")

# --- Gatekeeper: Leu54 vs Met53 relative to p53 Phe19 ring ---
f19 = mdm2_pep[mdm2_pep.res_id == 19]
f19_ring = f19.coord[np.isin(f19.atom_name, ["CG", "CD1", "CD2", "CE1", "CE2", "CZ"])].mean(0)
leu54 = np.stack([sidechain_tip(mdm2, "A", 54, a) for a in ["CD1", "CD2"]]).mean(0)
met53 = sidechain_tip(fitted_mdmx, "A", 53, "CE")
d54 = float(np.linalg.norm(leu54 - f19_ring)); d53 = float(np.linalg.norm(met53 - f19_ring))
result["gatekeeper"] = {"ref": "MDM2 p53 Phe19 ring centroid",
    "MDM2_Leu54_tip_to_Phe19_A": round(d54, 2), "MDMX_Met53_CE_to_Phe19_A": round(d53, 2),
    "delta_A": round(d54 - d53, 2),
    "interpretation": "positive delta => MDMX Met53 tip closer to Phe19 anchor (encroaches/narrows entrance)"}
print(f"\n--- Phe19 entrance gatekeeper ---\n  Leu54->Phe19={d54:.2f}  Met53->Phe19={d53:.2f}  delta={d54-d53:+.2f}")

# --- Leu26 floor: His96 vs Tyr99 relative to p53 Leu26 tip ---
l26 = mdm2_pep[mdm2_pep.res_id == 26]
l26_tip = l26.coord[np.isin(l26.atom_name, ["CD1", "CD2", "CG"])].mean(0)
his96 = np.stack([c for c in [sidechain_tip(mdm2, "A", 96, a) for a in ["CE1", "NE2", "ND1", "CD2"]] if c is not None]).mean(0)
tyr99 = np.stack([sidechain_tip(fitted_mdmx, "A", 99, a) for a in ["CG", "CD1", "CD2", "CE1", "CE2", "CZ"]]).mean(0)
dh = float(np.linalg.norm(his96 - l26_tip)); dy = float(np.linalg.norm(tyr99 - l26_tip))
result["leu26_floor"] = {"ref": "MDM2 p53 Leu26 side-chain tip",
    "MDM2_His96_to_Leu26_A": round(dh, 2), "MDMX_Tyr99_ring_to_Leu26_A": round(dy, 2),
    "delta_A": round(dh - dy, 2),
    "interpretation": "positive delta => MDMX Tyr99 ring closer to Leu26 (fills floor, shallower pocket)"}
print(f"\n--- Leu26 pocket floor ---\n  His96->Leu26={dh:.2f}  Tyr99->Leu26={dy:.2f}  delta={dh-dy:+.2f}")

# --- Anchor burial depth ---
def depth(pep, resid, atoms, dom):
    m = pep.res_id == resid
    tip = pep.coord[m & np.isin(pep.atom_name, atoms)].mean(0)
    core = dom.coord[dom.atom_name == "CA"].mean(0)
    return float(np.linalg.norm(tip - core))
print("\n--- Anchor burial (tip -> domain CA centroid; larger=deeper) ---")
for label, resid, atoms in [("Phe19", 19, ["CZ", "CE1", "CE2"]), ("Trp23", 23, ["NE1", "CZ2", "CH2"]), ("Leu26", 26, ["CD1", "CD2"])]:
    d2 = depth(mdm2_pep, resid, atoms, mdm2_dom); dx = depth(mdmx_pep, resid, atoms, mdmx_dom)
    result["anchor_depth"][label] = {"MDM2_A": round(d2, 2), "MDMX_A": round(dx, 2),
                                     "delta_MDM2_minus_MDMX": round(d2 - dx, 2)}
    print(f"  {label}: MDM2={d2:.2f}  MDMX={dx:.2f}  (MDM2-MDMX={d2-dx:+.2f})")

os.makedirs(OUTDIR, exist_ok=True)
json.dump(result, open(os.path.join(OUTDIR, "pocket_differences.json"), "w"), indent=2)
print("\nSaved pocket_differences.json")
