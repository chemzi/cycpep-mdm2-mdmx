#!/usr/bin/env python3
"""Step 1a: Search RCSB PDB for MDM2/MDMX structures.
Criteria: human + peptide-containing complex + resolution <= 2.8 A.
Strategy: query entries by UniProt accession + resolution, then post-filter
for a short peptide partner chain when we parse structures.
"""
import json
import os
import sys
import requests

SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
OUTDIR = os.environ.get("RESEARCH_OUTDIR", "output")

def search_by_uniprot(accession, max_res=2.8):
    query = {
        "query": {
            "type": "group",
            "logical_operator": "and",
            "nodes": [
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_accession",
                        "operator": "exact_match",
                        "value": accession
                    }
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_name",
                        "operator": "exact_match",
                        "value": "UniProt"
                    }
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_entry_info.resolution_combined",
                        "operator": "less_or_equal",
                        "value": max_res
                    }
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_entry_info.selected_polymer_entity_types",
                        "operator": "exact_match",
                        "value": "Protein (only)"
                    }
                }
            ]
        },
        "return_type": "entry",
        "request_options": {
            "return_all_hits": True,
            "results_content_type": ["experimental"]
        }
    }
    r = requests.post(SEARCH_URL, json=query, timeout=60)
    r.raise_for_status()
    data = r.json()
    return [hit["identifier"] for hit in data.get("result_set", [])]

def main():
    result = {}
    for name, acc in [("MDM2", "Q00987"), ("MDMX", "O15151")]:
        ids = search_by_uniprot(acc)
        result[name] = sorted(ids)
        print(f"{name} ({acc}): {len(ids)} entries <=2.8A protein-only", file=sys.stderr)
        print(",".join(sorted(ids)), file=sys.stderr)
    os.makedirs(OUTDIR, exist_ok=True)
    with open(os.path.join(OUTDIR, "pdb_search_results.json"), "w") as f:
        json.dump(result, f, indent=2)

if __name__ == "__main__":
    main()
