"""
Step 1: RCSB Search API v2 — 检索 MDM2/MDMX 人源肽段复合物。

调用方式:
    python -m scripts.search_pdb > data/search_results.json

输出: JSON 数组, 每个元素 {pdb_id, title, resolution, organism, polymer_count, ...}
"""

import json, sys, time, urllib.request, urllib.error
from pathlib import Path

RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"

def search_mdm2_peptide_complexes() -> list[dict]:
    """搜 MDM2 (Q00987) 的人源肽段复合物, ≤2.8Å。"""
    query = {
        "query": {
            "type": "group",
            "logical_operator": "and",
            "nodes": [
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_polymer_entity.rcsb_entity_source_organism.taxonomy_lineage.name",
                        "operator": "exact_match",
                        "value": "Homo sapiens",
                    },
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_entry_info.resolution_combined",
                        "operator": "less_or_equal",
                        "value": 2.8,
                    },
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_polymer_entity.rcsb_entity_poly_type",
                        "operator": "exact_match",
                        "value": "Polypeptide(L)",
                    },
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_polymer_entity_container_identifiers.uniprot_ids",
                        "operator": "contains_phrase",
                        "value": "Q00987",
                    },
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_entry_info.polymer_entity_count_protein",
                        "operator": "greater_or_equal",
                        "value": 2,
                    },
                },
            ],
        },
        "return_type": "entry",
        "request_options": {
            "paginate": {"start": 0, "rows": 500},
            "results_content_type": ["experimental"],
            "sort": [{"sort_by": "score", "direction": "desc"}],
        },
    }
    return _execute(query, "MDM2")


def search_mdmx_peptide_complexes() -> list[dict]:
    """搜 MDMX (O15151) 的人源肽段复合物, ≤2.8Å。"""
    query = {
        "query": {
            "type": "group",
            "logical_operator": "and",
            "nodes": [
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_polymer_entity.rcsb_entity_source_organism.taxonomy_lineage.name",
                        "operator": "exact_match",
                        "value": "Homo sapiens",
                    },
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_entry_info.resolution_combined",
                        "operator": "less_or_equal",
                        "value": 2.8,
                    },
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_polymer_entity.rcsb_entity_poly_type",
                        "operator": "exact_match",
                        "value": "Polypeptide(L)",
                    },
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_polymer_entity_container_identifiers.uniprot_ids",
                        "operator": "contains_phrase",
                        "value": "O15151",
                    },
                },
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": "rcsb_entry_info.polymer_entity_count_protein",
                        "operator": "greater_or_equal",
                        "value": 2,
                    },
                },
            ],
        },
        "return_type": "entry",
        "request_options": {
            "paginate": {"start": 0, "rows": 500},
            "results_content_type": ["experimental"],
            "sort": [{"sort_by": "score", "direction": "desc"}],
        },
    }
    return _execute(query, "MDMX")


def _execute(query: dict, label: str) -> list[dict]:
    """发 RCSB Search API 请求，返回解析后的结果列表。"""
    data = json.dumps(query).encode("utf-8")
    req = urllib.request.Request(
        RCSB_SEARCH_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"[search_pdb] RCSB API 请求失败 ({label}): {e}", file=sys.stderr)
        return []

    results = []
    for entry in raw.get("result_set", []):
        pdb_id = entry.get("identifier", "")
        title = entry.get("title", "")
        resolution = None
        polymer_count = None
        for attr in entry.get("rcsb_entry_info", []):
            if attr.get("name") == "resolution_combined":
                resolution = attr.get("value")
            if attr.get("name") == "polymer_entity_count_protein":
                polymer_count = attr.get("value")
        results.append({
            "pdb_id": pdb_id,
            "title": title,
            "resolution": resolution,
            "polymer_count": polymer_count,
        })
    return results


def main() -> int:
    mdm2 = search_mdm2_peptide_complexes()
    mdmx = search_mdmx_peptide_complexes()

    output = {
        "MDM2": mdm2,
        "MDMX": mdmx,
        "n_mdm2": len(mdm2),
        "n_mdmx": len(mdmx),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
