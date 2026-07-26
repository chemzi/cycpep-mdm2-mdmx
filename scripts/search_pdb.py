"""
Step 1: RCSB Search API v2 — 检索 MDM2/MDMX 人源肽段复合物。

调用方式:
    python -m scripts.search_pdb > data/search_results.json

策略: 直接按 polymer entity 的 UniProt accession 检索，再叠加分辨率、
      人源和至少两条蛋白聚合物链的限制。靶标名称只作为输出标签。
"""

import json, sys, time, urllib.request, urllib.error
from pathlib import Path

RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"


def _search_target(target_name: str, uniprot: str) -> list[dict]:
    """搜索某一靶点的结构。

    UniProt 条件用于候选召回，enrich 阶段仍会再次核对每条实体和链，
    因为一个 PDB 条目可能同时含有多个靶标、融合蛋白或抗体。
    """
    query = {
        "query": {
            "type": "group",
            "logical_operator": "and",
            "nodes": [
                {
                    "type": "terminal",
                    "service": "text",
                    "parameters": {
                        "attribute": (
                            "rcsb_polymer_entity_container_identifiers."
                            "reference_sequence_identifiers.database_accession"
                        ),
                        "operator": "exact_match",
                        "value": uniprot,
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
                        "attribute": "rcsb_entity_source_organism.taxonomy_lineage.name",
                        "operator": "exact_match",
                        "value": "Homo sapiens",
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
            "paginate": {"start": 0, "rows": 50},
            "results_content_type": ["experimental"],
            "sort": [{"sort_by": "score", "direction": "desc"}],
        },
    }
    results = _execute(query, target_name)
    for result in results:
        result["query_uniprot"] = uniprot
    return results


def _execute(query: dict, label: str) -> list[dict]:
    """发 RCSB Search API 请求。"""
    data = json.dumps(query).encode("utf-8")
    req = urllib.request.Request(
        RCSB_SEARCH_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        print(f"[search_pdb] HTTP {e.code} ({label}): {body}", file=sys.stderr)
        return []
    except urllib.error.URLError as e:
        print(f"[search_pdb] 网络错误 ({label}): {e}", file=sys.stderr)
        return []

    results = []
    for entry in raw.get("result_set", []):
        results.append({
            "pdb_id": entry.get("identifier", ""),
            "title": entry.get("title", ""),
            "target_search": label,
        })

    # 补充 resolution 信息
    for entry in raw.get("result_set", []):
        pdb_id = entry.get("identifier", "")
        for r in results:
            if r["pdb_id"] == pdb_id:
                for attr in entry.get("rcsb_entry_info", []):
                    if attr.get("name") == "resolution_combined":
                        r["resolution"] = attr.get("value")
                    if attr.get("name") == "polymer_entity_count_protein":
                        r["polymer_count"] = attr.get("value")
                break

    return results


def main() -> int:
    mdm2 = _search_target("MDM2", "Q00987")
    mdmx = _search_target("MDMX", "O15151")

    output = {
        "MDM2": mdm2,
        "MDMX": mdmx,
        "n_mdm2": len(mdm2),
        "n_mdmx": len(mdmx),
        "run_status": "complete" if mdm2 and mdmx else "failed_or_incomplete",
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
