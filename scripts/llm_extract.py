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

EXTRACTION_PROMPT = """你是一个蛋白质结构生物学专家。从以下一篇 PubMed 论文中提取 MDM2/MDMX 双靶肽类抑制剂的信息。

如果论文确实描述了一个具体的双靶分子，提取以下字段（不确定的填 null，不要编造）：
- name: 分子名称（论文中使用的名称）
- type: 类型（linear peptide / stapled peptide / cyclic peptide / d-peptide / other）
- sequence: 氨基酸序列（单字母，必须从论文中精确复制）
- length: 序列长度（整数）
- kd_mdm2: MDM2 亲和力（论文中的原始数值和单位，如 "Ki 0.9 nM"）
- kd_mdmx: MDMX 亲和力（同上）
- key_residues: 关键残基列表 [{"position": "Phe19", "residue": "Phe", "role": "anchor"}]
- pmid: 从输入中精确复制 PMID
- pdb_ids: 相关的 PDB ID 列表（如有）
- design_insight: 一句话设计启发

如果论文不涉及具体的双靶分子（如综述、方法学文章），返回 {"is_relevant": false}。

严格以 JSON 输出，不要 markdown 代码块，不要额外解释。

示例（参考格式，非真实数据）：
{
  "is_relevant": true,
  "name": "PMI",
  "type": "linear peptide",
  "sequence": "TSFAEYWNLLSP",
  "length": 12,
  "kd_mdm2": "IC50 8.7 nM",
  "kd_mdmx": "IC50 15.2 nM",
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


def extract_one_paper(paper: dict, model: str) -> dict | None:
    """对一篇论文调用 LLM 提取。返回结构化 dict 或 None（失败时）。"""
    pmid = paper.get("pmid", "?")
    title = paper.get("title", "")
    abstract = paper.get("abstract", "")
    source = paper.get("source", "")

    # 构建用户内容：标题 + 摘要 + PMID
    user_content = f"PMID: {pmid}\nTitle: {title}\nSource: {source}\nAbstract: {abstract}"

    try:
        raw = call_openai(
            system_prompt="你是一个蛋白质结构生物学专家。只输出合法 JSON，不要额外解释，不要 markdown 代码块。",
            user_content=EXTRACTION_PROMPT + user_content,
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

    if not result.get("is_relevant", True):
        return None

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

    if not papers:
        print(json.dumps({"error": "no papers to extract from"}, ensure_ascii=False))
        return 1

    print(f"[llm_extract] 逐篇提取 {len(papers)} 篇论文, 并发={args.concurrency}, 模型={model}", file=sys.stderr)

    all_binders = []
    n_processed = 0

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {executor.submit(extract_one_paper, p, model): p for p in papers}
        for future in as_completed(futures):
            paper = futures[future]
            pmid = paper.get("pmid", "?")
            try:
                result = future.result(timeout=120)
                n_processed += 1
                if result and (result.get("name") or result.get("type") or result.get("sequence")):
                    all_binders.append(result)
                    print(f"[llm_extract] {n_processed}/{len(papers)}: found '{result['name']}' from PMID {pmid}", file=sys.stderr)
                else:
                    print(f"[llm_extract] {n_processed}/{len(papers)}: PMID {pmid} 无相关分子", file=sys.stderr)
            except Exception as e:
                print(f"[llm_extract] PMID {pmid} 失败: {e}", file=sys.stderr)

    # 去重（按 name 相似度简单去重）
    seen = set()
    unique = []
    for b in all_binders:
        key = (b.get("name") or "")[:30].lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(b)

    output = {
        "known_binders": unique,
        "llm_provider": args.provider,
        "llm_model": model,
        "n_papers_processed": n_processed,
        "n_binders_found": len(unique),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
