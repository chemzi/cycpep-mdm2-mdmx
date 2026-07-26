"""
Step 2: RCSB GraphQL — 富集 PDB 条目，判定肽段复合物。

调用方式:
    python -m scripts.enrich_pdb < data/search_results.json > data/enriched_results.json

对每个 PDB，查询 polymer entities 的 UniProt、序列和实际链 ID。只有实体
UniProt 与目标一致时才标为 MDM2/MDMX；非靶标的 6~50 aa L 型多肽实体
才作为候选 binder 链。
"""

import json, sys, urllib.request, urllib.error

RCSB_GRAPHQL_URL = "https://data.rcsb.org/graphql"

QUERY = """
query ($id: String!) {
  entry(entry_id: $id) {
    rcsb_id
    exptl { method }
    rcsb_entry_info {
      resolution_combined
      polymer_entity_count_protein
    }
    polymer_entities {
      rcsb_id
      rcsb_polymer_entity_container_identifiers {
        uniprot_ids
        asym_ids
        auth_asym_ids
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
  }
}
"""

MIN_PEPTIDE_LEN = 6
MAX_PEPTIDE_LEN = 50
TARGET_UNIPROT = {"MDM2": "Q00987", "MDMX": "O15151"}


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
    other_polymers = []
    for poly in polymers:
        entity_type = (poly.get("entity_poly") or {}).get("type", "")
        seq = (poly.get("entity_poly") or {}).get("pdbx_seq_one_letter_code_can", "")
        seq_len = len(seq.replace("\n", "").replace(" ", "")) if seq else 0
        uniprots = []
        container = poly.get("rcsb_polymer_entity_container_identifiers") or {}
        for uid in container.get("uniprot_ids") or []:
            uniprots.append(uid)
        asym_ids = container.get("asym_ids") or []
        auth_asym_ids = container.get("auth_asym_ids") or []
        chain_ids = auth_asym_ids or asym_ids
        matched_targets = [
            target_name for target_name, uid in TARGET_UNIPROT.items()
            if uid in uniprots
        ]
        entity = {
            "entity_id": poly.get("rcsb_id", ""),
            "chain_ids": chain_ids,
            "asym_ids": asym_ids,
            "auth_asym_ids": auth_asym_ids,
            "uniprot_ids": uniprots,
            "sequence": seq.replace("\n", "").replace(" ", ""),
            "length": seq_len,
        }
        if matched_targets:
            entity["matched_targets"] = matched_targets
            targets.append(entity)
        elif entity_type == "polypeptide(L)" and MIN_PEPTIDE_LEN <= seq_len <= MAX_PEPTIDE_LEN:
            peptides.append({
                **entity,
            })
        else:
            other_polymers.append(entity)

    return {
        "pdb_id": pdb_id,
        "method": (entry.get("exptl") or [{}])[0].get("method", ""),
        "resolution": (
            ((entry.get("rcsb_entry_info") or {}).get("resolution_combined") or [None])[0]
        ),
        "polymer_count": (
            (entry.get("rcsb_entry_info") or {}).get("polymer_entity_count_protein")
        ),
        "is_peptide_complex": len(peptides) > 0,
        "n_peptide_chains": len(peptides),
        "peptide_chains": peptides,
        "target_chains": targets,
        "other_polymers": other_polymers,
        "targets_present": sorted({
            target_name
            for target in targets
            for target_name in target.get("matched_targets", [])
        }),
    }


def main() -> int:
    input_data = json.loads(sys.stdin.read())
    all_results = []
    MAX_PER_TARGET = 100  # 限制每个靶点最多处理 100 条，避免超时
    search_hits = {}
    for target_name in ["MDM2", "MDMX"]:
        entries = input_data.get(target_name, [])[:MAX_PER_TARGET]
        for e in entries:
            pdb_id = e["pdb_id"].upper()
            search_hits.setdefault(pdb_id, []).append({
                "target_search": target_name,
                "query_uniprot": e.get("query_uniprot"),
                "resolution": e.get("resolution"),
                "polymer_count": e.get("polymer_count"),
            })

    for i, (pdb_id, hits) in enumerate(search_hits.items()):
        enriched = enrich(pdb_id)
        if enriched:
            enriched["resolution"] = next(
                (h.get("resolution") for h in hits if h.get("resolution") is not None),
                enriched.get("resolution"),
            )
            enriched["polymer_count"] = next(
                (h.get("polymer_count") for h in hits if h.get("polymer_count") is not None),
                enriched.get("polymer_count"),
            )
            enriched["search_hits"] = hits
            all_results.append(enriched)
            if (i + 1) % 20 == 0:
                print(f"[enrich] progress: {i+1}/{len(search_hits)} unique PDB entries", file=sys.stderr)
            time.sleep(0.1)  # RCSB rate limit

    # 一个含两个靶标的条目拆成两条 target-specific 记录，后续界面计算按链执行。
    peptide_complexes = []
    for result in all_results:
        if not result.get("is_peptide_complex"):
            continue
        for target_name in result.get("targets_present", []):
            target_specific = dict(result)
            target_specific["target"] = target_name
            target_specific["target_uniprot"] = TARGET_UNIPROT[target_name]
            target_specific["target_chains"] = [
                chain for chain in result.get("target_chains", [])
                if target_name in chain.get("matched_targets", [])
            ]
            peptide_complexes.append(target_specific)

    print(json.dumps({
        "all": all_results,
        "peptide_complexes": peptide_complexes,
        "n_peptide_complexes": len(peptide_complexes),
        "n_unique_pdb_entries": len(all_results),
        "n_by_target": {
            target: sum(1 for r in peptide_complexes if r.get("target") == target)
            for target in TARGET_UNIPROT
        },
        "run_status": (
            "complete"
            if all(
                any(r.get("target") == target for r in peptide_complexes)
                for target in TARGET_UNIPROT
            )
            else "failed_or_incomplete"
        ),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import time
    sys.exit(main())
