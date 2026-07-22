"""
Step 2: RCSB GraphQL — 富集 PDB 条目，判定肽段复合物。

调用方式:
    python -m scripts.enrich_pdb < data/search_results.json > data/enriched_results.json

对每个 PDB，查询 polymer entities 的 type/sequence/chain，判断哪些是肽段复合物
（复合物中至少一条 chain type=poly(L) 且序列长度 6~50）。
"""

import json, sys, urllib.request, urllib.error

RCSB_GRAPHQL_URL = "https://data.rcsb.org/graphql"

QUERY = """
query ($id: String!) {
  entry(entry_id: $id) {
    rcsb_id
    exptl { method }
    polymer_entities {
      rcsb_id
      rcsb_polymer_entity_container_identifiers {
        uniprot_ids
      }
      entity_poly {
        pdbx_seq_one_letter_code_can
        type
      }
      rcsb_polymer_entity_feature {
        type
        feature_positions {
          values
        }
      }
    }
    nonpolymer_entities {
      pdbx_description
    }
  }
}
"""

MIN_PEPTIDE_LEN = 6
MAX_PEPTIDE_LEN = 50


def enrich(pdb_id: str) -> dict | None:
    """对单个 PDB 做 GraphQL 查询，返回结构化信息。"""
    payload = json.dumps({"query": QUERY, "variables": {"id": pdb_id}}).encode("utf-8")
    req = urllib.request.Request(
        RCSB_GRAPHQL_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"[enrich_pdb] GraphQL 请求失败 ({pdb_id}): {e}", file=sys.stderr)
        return None

    entry = raw.get("data", {}).get("entry")
    if not entry:
        return None

    polymers = entry.get("polymer_entities") or []
    peptides = []
    targets = []
    for poly in polymers:
        entity_type = (poly.get("entity_poly") or {}).get("type", "")
        seq = (poly.get("entity_poly") or {}).get("pdbx_seq_one_letter_code_can", "")
        seq_len = len(seq.replace("\n", "").replace(" ", "")) if seq else 0
        uniprots = []
        container = poly.get("rcsb_polymer_entity_container_identifiers") or {}
        for uid in container.get("uniprot_ids") or []:
            uniprots.append(uid)
        if entity_type == "polypeptide(L)" and MIN_PEPTIDE_LEN <= seq_len <= MAX_PEPTIDE_LEN:
            peptides.append({
                "chain_id": poly.get("rcsb_id", ""),
                "sequence": seq.replace("\n", "").replace(" ", ""),
                "length": seq_len,
            })
        else:
            targets.append({
                "chain_id": poly.get("rcsb_id", ""),
                "uniprot_ids": uniprots,
                "length": seq_len,
            })

    return {
        "pdb_id": pdb_id,
        "method": (entry.get("exptl") or [{}])[0].get("method", ""),
        "is_peptide_complex": len(peptides) > 0,
        "n_peptide_chains": len(peptides),
        "peptide_chains": peptides,
        "target_chains": targets,
    }


def main() -> int:
    input_data = json.loads(sys.stdin.read())
    all_results = []
    MAX_PER_TARGET = 100  # 限制每个靶点最多处理 100 条，避免超时
    for target_name in ["MDM2", "MDMX"]:
        entries = input_data.get(target_name, [])[:MAX_PER_TARGET]
        for i, e in enumerate(entries):
            pdb_id = e["pdb_id"]
            enriched = enrich(pdb_id)
            if enriched:
                # 合并搜索结果的 resolution/polymer_count
                enriched["resolution"] = e.get("resolution")
                enriched["polymer_count"] = e.get("polymer_count")
                enriched["target"] = target_name
                all_results.append(enriched)
            if (i + 1) % 20 == 0:
                print(f"[enrich] {target_name} progress: {i+1}/{min(len(entries), MAX_PER_TARGET)}", file=sys.stderr)
            time.sleep(0.1)  # RCSB rate limit

    # 只保留肽段复合物
    peptide_complexes = [r for r in all_results if r.get("is_peptide_complex")]
    print(json.dumps({
        "all": all_results,
        "peptide_complexes": peptide_complexes,
        "n_peptide_complexes": len(peptide_complexes),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import time
    sys.exit(main())
