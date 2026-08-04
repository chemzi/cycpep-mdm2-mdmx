"""
Step 7: LLM 提取 — 逐篇从 PubMed 摘要中提取双靶分子信息。

调用方式:
    python -m scripts.llm_extract [--concurrency N] < data/pubmed_results.json > data/llm_extracted.json

逐篇处理，一次只给 LLM 一篇论文的摘要，减少幻觉。
支持并发（--concurrency 5）加速。

输出: JSON {known_binders, llm_provider, llm_model, n_papers_processed}
"""

import json, os, sys, argparse, re
from concurrent.futures import ThreadPoolExecutor, as_completed

EXTRACTION_PROMPT = """你是一个蛋白质结构生物学专家。从以下一篇 PubMed 论文中提取与项目靶点相关的肽类结合分子信息。

项目靶点：{target_ids}

如果论文确实描述了具体分子，提取以下字段（不确定的填 null，不要编造）：
- name: 分子名称（论文中使用的名称）
- type: 类型（linear peptide / stapled peptide / cyclic peptide / d-peptide / other）
- sequence: 氨基酸序列（单字母，必须从论文中精确复制）
- length: 序列长度（整数）
- affinity_by_target: 按项目靶点 ID 记录论文原始亲和力字符串；未报道填 null
- key_residues: 关键残基列表 [{"position": "Phe19", "residue": "Phe", "role": "anchor"}]
- pmid: 从输入中精确复制 PMID
- pdb_ids: 相关的 PDB ID 列表（如有）
- design_insight: 一句话设计启发

如果论文不涉及项目靶点的具体分子（如综述、方法学文章），返回 {"is_relevant": false}。

严格以 JSON 输出，不要 markdown 代码块，不要额外解释。

示例（参考格式，非真实数据）：
{
  "is_relevant": true,
  "name": "PMI",
  "type": "linear peptide",
  "sequence": "TSFAEYWNLLSP",
  "length": 12,
  "affinity_by_target": {"TARGET_A": "IC50 8.7 nM", "TARGET_B": null},
  "key_residues": [{"position": "Phe19", "residue": "Phe", "role": "anchor"}, {"position": "Trp23", "residue": "Trp", "role": "anchor"}, {"position": "Leu26", "residue": "Leu", "role": "anchor"}],
  "pmid": "34589387",
  "pdb_ids": ["3EQS", "3EQY"],
  "design_insight": "FxxWxxxL 三锚点motif，Phe/Trp/Leu 三残基固定"
}

以下是论文信息：
"""


def call_openai(system_prompt: str, user_content: str, model: str) -> str:
    """调用 OpenAI 兼容 API，返回 response content 字符串。"""
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY 未设置")

    import urllib.request

    is_stepfun = "stepfun" in base_url or "step_plan" in base_url
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 2048,
    }
    if not is_stepfun:
        payload["response_format"] = {"type": "json_object"}
    if "step-3.7" in model:
        payload["reasoning_effort"] = "low"

    payload_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result["choices"][0]["message"]["content"]


def extract_one_paper(paper: dict, model: str, target_ids: list[str]) -> dict | None:
    """对一篇论文调用 LLM 提取。返回结构化 dict 或 None（失败时）。"""
    pmid = paper.get("pmid", "?")
    title = paper.get("title", "")
    content = paper.get("content", paper.get("abstract", ""))[:5000]  # PMC 全文或摘要
    source = paper.get("source", "")
    source_type = paper.get("source_type", "abstract")

    # 构建用户内容
    type_label = "Full Text" if source_type == "pmc_fulltext" else "Abstract"
    user_content = f"PMID: {pmid}\nTitle: {title}\nSource: {source}\n{type_label}: {content}"

    try:
        raw = call_openai(
            system_prompt="你是一个蛋白质结构生物学专家。只输出合法 JSON，不要额外解释，不要 markdown 代码块。",
            user_content=EXTRACTION_PROMPT.replace("{target_ids}", ", ".join(target_ids)) + user_content,
            model=model,
        )
    except Exception as e:
        print(f"[llm_extract] PMID {pmid} API 调用失败: {e}", file=sys.stderr)
        return None

    # 解析 JSON
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            try:
                result = json.loads(match.group())
            except json.JSONDecodeError:
                print(f"[llm_extract] PMID {pmid} JSON 解析失败: {raw[:100]}", file=sys.stderr)
                return None
        else:
            return None

    if not isinstance(result, dict):
        print(f"[llm_extract] PMID {pmid} 返回的 JSON 不是对象", file=sys.stderr)
        return None
    if not result.get("is_relevant", True):
        return None

    # OpenAI-compatible providers occasionally return a one-item list for a
    # scalar display field. Keep one malformed paper from aborting the run.
    for field in ("name", "type", "sequence", "design_insight"):
        value = result.get(field)
        if isinstance(value, list):
            result[field] = "; ".join(
                str(item).strip() for item in value if str(item).strip()
            ) or None
        elif value is not None and not isinstance(value, str):
            result[field] = str(value)
    result["pmid"] = pmid  # 强制使用输入中的 PMID
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 逐篇提取双靶分子")
    parser.add_argument("--provider", default="openai", choices=["openai", "anthropic"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--concurrency", type=int, default=3,
                        help="并发数（默认 3）")
    args = parser.parse_args()

    if args.provider == "openai":
        model = args.model or os.environ.get("LLM_MODEL", "step-3.7-flash")
    else:
        model = args.model or os.environ.get("LLM_MODEL", "claude-sonnet-4-20250514")

    input_data = json.loads(sys.stdin.read())
    papers = input_data.get("papers", [])
    target_ids = [target.get("id") for target in input_data.get("targets", []) if target.get("id")]
    if not target_ids:
        target_ids = ["MDM2", "MDMX"]  # v5 cached PubMed payload compatibility

    if not papers:
        print(json.dumps({"error": "no papers to extract from"}, ensure_ascii=False))
        return 1
    if not os.environ.get("OPENAI_API_KEY"):
        print(json.dumps({
            "known_binders": [],
            "llm_provider": args.provider,
            "llm_model": model,
            "n_papers_processed": 0,
            "n_binders_found": 0,
            "run_status": "degraded_no_api_key",
            "error": "OPENAI_API_KEY is not configured",
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"[llm_extract] 逐篇提取 {len(papers)} 篇论文, 并发={args.concurrency}, 模型={model}", file=sys.stderr)

    all_binders = []
    n_processed = 0

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {executor.submit(extract_one_paper, p, model, target_ids): p for p in papers}
        for future in as_completed(futures):
            paper = futures[future]
            pmid = paper.get("pmid", "?")
            try:
                result = future.result(timeout=120)
                n_processed += 1
                if result and (result.get("name") or result.get("type") or result.get("sequence")):
                    all_binders.append(result)
                    print(f"[llm_extract] {n_processed}/{len(papers)}: found '{result.get('name') or 'unnamed'}' from PMID {pmid}", file=sys.stderr)
                else:
                    print(f"[llm_extract] {n_processed}/{len(papers)}: PMID {pmid} 无相关分子", file=sys.stderr)
            except Exception as e:
                print(f"[llm_extract] PMID {pmid} 失败: {e}", file=sys.stderr)

    # 去重（按 name 相似度简单去重）
    seen = set()
    unique = []
    for b in all_binders:
        name = b.get("name")
        key = name[:30].lower() if isinstance(name, str) else ""
        if key and key not in seen:
            seen.add(key)
            unique.append(b)

    output = {
        "known_binders": unique,
        "llm_provider": args.provider,
        "llm_model": model,
        "n_papers_processed": n_processed,
        "n_binders_found": len(unique),
        "run_status": "complete",
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
