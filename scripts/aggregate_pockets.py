#!/usr/bin/env python3
"""Aggregate per-structure interfaces into consensus interface + consensus pockets."""
import json
import os
from collections import defaultdict, Counter

OUTDIR = os.environ.get("RESEARCH_OUTDIR", "output")
data = json.load(open(os.path.join(OUTDIR, "interface_per_structure.json")))

def aggregate(target):
    structs = data[target]
    n = len(structs)
    # consensus interface across ALL peptide complexes
    resname = {}
    freq = Counter()
    for s in structs:
        seen = set()
        for rid, rn in s["interface_residues"]:
            seen.add(rid); resname[rid] = rn
        for rid in seen:
            freq[rid] += 1
    consensus = sorted(freq.items())

    # consensus pockets from CANONICAL structures only
    canon = [s for s in structs if s["canonical_numbering"]]
    pocket_freq = {p: Counter() for p in ["Phe19_pocket", "Trp23_pocket", "Leu26_pocket"]}
    for s in canon:
        for p, lst in s["pockets"].items():
            for rid, rn in lst:
                pocket_freq[p][rid] += 1
                resname[rid] = rn
    return n, len(canon), consensus, resname, pocket_freq

report = {}
for target in ["MDM2", "MDMX"]:
    n, ncanon, consensus, resname, pocket_freq = aggregate(target)
    print(f"\n########## {target}: {n} complexes, {ncanon} canonical ##########")
    print("--- Consensus interface (res: freq/N) [freq>=25% shown] ---")
    core = [(rid, resname[rid], f) for rid, f in consensus if f >= max(1, n*0.25)]
    print(" ".join(f"{rn}{rid}({f})" for rid, rn, f in core))
    pockets_out = {}
    for p in ["Phe19_pocket", "Trp23_pocket", "Leu26_pocket"]:
        pf = pocket_freq[p]
        # residue present in >=50% of canonical structures
        thr = max(1, ncanon * 0.5)
        sel = sorted([(rid, resname[rid], f) for rid, f in pf.items() if f >= thr])
        allr = sorted([(rid, resname[rid], f) for rid, f in pf.items()])
        print(f"--- {p} (>=50% of {ncanon} canon): " + " ".join(f"{rn}{rid}" for rid, rn, f in sel))
        print(f"      all: " + " ".join(f"{rn}{rid}({f})" for rid, rn, f in allr))
        pockets_out[p] = {"consensus": [[rid, rn] for rid, rn, f in sel],
                          "all_with_freq": [[rid, rn, f] for rid, rn, f in allr]}
    report[target] = {"n_complexes": n, "n_canonical": ncanon,
                      "consensus_interface": [[rid, resname[rid], f] for rid, f in consensus],
                      "pockets": pockets_out}

os.makedirs(OUTDIR, exist_ok=True)
json.dump(report, open(os.path.join(OUTDIR, "pocket_report.json"), "w"), indent=2)
print("\nSaved pocket_report.json")
