#!/usr/bin/env python3
"""Step 1a (cont.): Enrich search hits via RCSB GraphQL to find peptide complexes.
A qualifying peptide complex = has the target domain entity (maps to target UniProt)
AND a separate polymer entity that is a short peptide (<= 35 aa).
"""
import json
import os
import sys
import requests

GRAPHQL = "https://data.rcsb.org/graphql"
OUTDIR = os.environ.get("RESEARCH_OUTDIR", "output")

QUERY = """
query($ids:[String!]!){
  entries(entry_ids:$ids){
    rcsb_id
    rcsb_entry_info{ resolution_combined }
    struct{ title }
    polymer_entities{
      rcsb_id
      entity_poly{ rcsb_sample_sequence_length pdbx_seq_one_letter_code_can rcsb_entity_polymer_type }
      rcsb_polymer_entity{ pdbx_description }
      rcsb_polymer_entity_container_identifiers{
        asym_ids
        auth_asym_ids
        reference_sequence_identifiers{ database_accession database_name }
      }
    }
  }
}
"""

TARGET_ACC = {"MDM2": "Q00987", "MDMX": "O15151"}

def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

def fetch(ids):
    out = []
    for batch in chunks(ids, 40):
        r = requests.post(GRAPHQL, json={"query": QUERY, "variables": {"ids": batch}}, timeout=90)
        r.raise_for_status()
        data = r.json()
        out.extend(data["data"]["entries"])
    return out

def analyze(entries, target_acc):
    results = []
    for e in entries:
        pid = e["rcsb_id"]
        res = e["rcsb_entry_info"]["resolution_combined"]
        res = res[0] if res else None
        title = (e.get("struct") or {}).get("title", "")
        domain_ents, peptide_ents = [], []
        for ent in e["polymer_entities"]:
            ep = ent["entity_poly"] or {}
            length = ep.get("rcsb_sample_sequence_length")
            ptype = ep.get("rcsb_entity_polymer_type")
            seq = ep.get("pdbx_seq_one_letter_code_can", "")
            desc = (ent["rcsb_polymer_entity"] or {}).get("pdbx_description", "")
            cid = ent["rcsb_polymer_entity_container_identifiers"] or {}
            auth = cid.get("auth_asym_ids") or []
            refs = cid.get("reference_sequence_identifiers") or []
            accs = [r["database_accession"] for r in refs] if refs else []
            if ptype != "Protein":
                continue
            info = {"entity": ent["rcsb_id"], "length": length, "auth_chains": auth,
                    "desc": desc, "accs": accs, "seq": seq}
            if target_acc in accs:
                domain_ents.append(info)
            elif length is not None and length <= 35:
                peptide_ents.append(info)
        is_peptide_complex = bool(domain_ents and peptide_ents)
        results.append({
            "pdb": pid, "resolution": res, "title": title,
            "is_peptide_complex": is_peptide_complex,
            "domain_entities": domain_ents,
            "peptide_entities": peptide_ents,
        })
    return results

def main():
    with open(os.path.join(OUTDIR, "pdb_search_results.json")) as f:
        search = json.load(f)
    enriched = {}
    for name, acc in TARGET_ACC.items():
        entries = fetch(search[name])
        res = analyze(entries, acc)
        pep = [r for r in res if r["is_peptide_complex"]]
        enriched[name] = res
        print(f"\n=== {name}: {len(pep)}/{len(res)} peptide complexes ===", file=sys.stderr)
        for r in sorted(pep, key=lambda x: (x["resolution"] or 99)):
            plens = [p["length"] for p in r["peptide_entities"]]
            print(f"  {r['pdb']}  {r['resolution']}A  pep_len={plens}  {r['title'][:60]}", file=sys.stderr)
    os.makedirs(OUTDIR, exist_ok=True)
    with open(os.path.join(OUTDIR, "pdb_enriched.json"), "w") as f:
        json.dump(enriched, f, indent=2)

if __name__ == "__main__":
    main()
