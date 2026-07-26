"""
Step 8: 阈值文献检索 — 为七层指标电池的每一层找文献依据的阈值。

导师要求（DeeCamp Kickoff）：
- "at thresholds you can justify" — 阈值必须可论证，不能拍脑袋
- 部分阈值论文已给（RFpeptides: pLDDT>0.8, scRMSD<2.0A），其余要查文献
- 正对照标定是最终手段，但文献阈值是起点和辩护依据

七层指标 → 检索目标：
  L1 pLDDT           -> RFpeptides / AlphaFold 置信度阈值
  L2 ipSAE           -> ipSAE 原始论文（Dunbrack）+ 肽-蛋白界面应用
  L3 dG/SC/dSASA     -> 界面物理可接受范围（PRODIGY/Rosetta/Rosetta SC）
  L4 环化QC          -> 肽键几何/环闭合距离
  L5 热点覆盖        -> hotspot coverage 定义（无硬阈值，取定义）
  L6 多预测器收敛    -> pose RMSD 收敛阈值
  L7 scRMSD          -> RFpeptides / ProteinMPNN 自洽 RMSD

调用方式:
    echo '{}' | python -m scripts.threshold_research [--concurrency N] > data/thresholds.json

输出: JSON {metric_battery: {L1: {...}, ...}, _meta: {...}}
每层: {value, operator, unit, source, pmid, confidence, note}
"""

import json, os, sys, argparse, re, time, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
PUBMED_FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# ===== 每层指标的检索配置 =====
LAYER_QUERIES = {
    "L1_plddt": {
        "desc": "环肽/蛋白单体 pLDDT 置信度阈值",
        "queries": [
            "RFpeptides",
            "macrocycle de novo design",
            "AlphaFold pLDDT protein design",
        ],
        "known_hint": "RFpeptides (Rettie Nat Chem Biol 2025) uses pLDDT > 0.8",
    },
    "L2_ipsae": {
        "desc": "ipSAE 界面置信度阈值（小环肽主指标，替代 ipTM）",
        "queries": [
            "ipSAE interface",
            "predicted aligned error protein interface",
            "AlphaFold interface confidence peptide",
        ],
        "known_hint": "ipSAE from Dunbrack lab; typical accept ~0.5-0.6 for peptide",
    },
    "L3_interface_physics": {
        "desc": "界面物理 dG / SC / dSASA 可接受范围",
        "queries": [
            "PRODIGY binding affinity",
            "shape complementarity protein interface",
            "buried surface area protein peptide complex",
        ],
        "known_hint": "dG<-10 kcal/mol strong; SC>0.6 good; dSASA>400 A^2",
    },
    "L4_ring_closure": {
        "desc": "环肽环闭合几何 QC（肽键距离）",
        "queries": [
            "head-to-tail cyclic peptide",
            "macrocyclization peptide bond",
        ],
        "known_hint": "peptide bond ~1.33A; N-C terminal distance < 2.0A indicates closed",
    },
    "L5_hotspot_coverage": {
        "desc": "热点残基覆盖率定义（设计意图）",
        "queries": [
            "hotspot residues protein interface design",
            "p53 MDM2 peptide inhibitor hotspot",
        ],
        "known_hint": "cover >= 2/3 of designed hotspot pockets",
    },
    "L6_pose_convergence": {
        "desc": "多预测器/多 seed 结合 pose 收敛 RMSD",
        "queries": [
            "binding pose prediction RMSD",
            "ColabFold AlphaFold protein peptide docking",
        ],
        "known_hint": "pose RMSD < 2.0 A across predictors/seeds = converged",
    },
    "L7_scrmsd": {
        "desc": "序列 refold 自洽 scRMSD 阈值",
        "queries": [
            "ProteinMPNN",
            "de novo protein design self-consistency",
            "sequence design AlphaFold refold",
        ],
        "known_hint": "RFpeptides/ProteinMPNN: bb-RMSD or scRMSD < 2.0 A",
    },
}

EXTRACTION_PROMPT_TMPL = """你是计算结构生物学专家，专门做环肽/蛋白设计指标评估。

我在为一篇 de novo 环肽 binder 设计工作确定评估指标"{metric_desc}"的阈值。请根据以下论文摘要，给出该指标可辩护的阈值或判断标准。

注意：摘要里往往没有精确数字。如果论文用了这个指标但没给具体值，请基于你的领域知识给出公认的经验值，并诚实标注证据等级。

提取（不要编造论文里没有的具体数字；给经验值时用 evidence_grade 区分）：
- value: 阈值数值（如 0.8, 2.0, -10）
- operator: ">" / "<" / ">=" / "<="
- unit: 单位（"A" / "kcal/mol" / 无则 null）
- metric_name: 指标名称
- evidence_grade: "paper_explicit"(论文明确给出) / "field_consensus"(领域公认经验) / "estimate"(粗略估计)
- context: 依据来源一句话（论文标题或领域常识）
- confidence: high/medium/low

如果实在无法给出任何合理数值，返回 {{"found": false}}。

已知线索（参考，以论文/常识为准）：{known_hint}

严格 JSON 输出，不要 markdown，不要解释。

示例：
{{"found": true, "value": 0.8, "operator": ">", "unit": null, "metric_name": "pLDDT", "evidence_grade": "paper_explicit", "context": "RFpeptides graduation criterion", "confidence": "high"}}

以下是论文信息：
"""


# ===== PubMed 工具 =====
_LAST_REQ = [0.0]

def _throttle(min_interval=0.5):
    """NCBI 限速：无 key 3 req/s，保守取 0.5s 间隔。"""
    now = time.time()
    wait = min_interval - (now - _LAST_REQ[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_REQ[0] = time.time()


def search_pubmed(term: str, max_results: int = 5, retries: int = 3) -> list[str]:
    params = urllib.parse.urlencode({
        "db": "pubmed", "term": term, "retmax": max_results,
        "retmode": "json", "sort": "relevance",
    })
    for attempt in range(retries):
        _throttle()
        try:
            with urllib.request.urlopen(f"{PUBMED_SEARCH_URL}?{params}", timeout=30) as resp:
                return json.loads(resp.read().decode()).get("esearchresult", {}).get("idlist", [])
        except Exception as e:
            if "429" in str(e) and attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            print(f"[thresh] 搜索失败 '{term}': {e}", file=sys.stderr)
            return []
    return []


def fetch_abstracts(pmids: list[str]) -> dict[str, dict]:
    if not pmids:
        return {}
    _throttle()
    try:
        params = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(pmids), "retmode": "json"})
        with urllib.request.urlopen(f"{PUBMED_SUMMARY_URL}?{params}", timeout=30) as resp:
            return json.loads(resp.read().decode()).get("result", {})
    except Exception as e:
        print(f"[thresh] 元数据失败: {e}", file=sys.stderr)
        return {}


def fetch_full_abstract(pmids: list[str]) -> dict[str, str]:
    """efetch 拿完整摘要文本。"""
    texts = {}
    if not pmids:
        return texts
    _throttle()
    try:
        params = urllib.parse.urlencode({
            "db": "pubmed", "id": ",".join(pmids),
            "rettype": "abstract", "retmode": "text",
        })
        with urllib.request.urlopen(f"{PUBMED_FETCH_URL}?{params}", timeout=30) as resp:
            full = resp.read().decode("utf-8", errors="replace")
        # efetch text 把所有摘要连在一起，按 PMID 粗略分
        # 简化：整体返回，LLM 自己找
        texts["_combined"] = full[:12000]
    except Exception as e:
        print(f"[thresh] 摘要获取失败: {e}", file=sys.stderr)
    return texts


# ===== LLM =====
def call_openai(system_prompt: str, user_content: str, model: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY 未设置")
    is_stepfun = "stepfun" in base_url or "step_plan" in base_url
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 1024,
    }
    if not is_stepfun:
        payload["response_format"] = {"type": "json_object"}
    if "step-3.7" in model:
        payload["reasoning_effort"] = "low"
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())["choices"][0]["message"]["content"]


def parse_json_loose(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'\{[\s\S]*\}', raw)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                return None
    return None


# ===== 单层处理 =====
def research_one_layer(layer_key: str, cfg: dict, model: str) -> dict:
    """对一层指标：检索 -> 取摘要 -> LLM 抽取阈值。"""
    desc = cfg["desc"]
    hint = cfg["known_hint"]

    # 多 query 合并 pmids（串行 + 全局限速，避免 429）
    pmids = []
    for q in cfg["queries"]:
        pmids.extend(search_pubmed(q, max_results=4))
    # 去重保持顺序
    seen = set()
    pmids = [p for p in pmids if not (p in seen or seen.add(p))][:6]

    if not pmids:
        return {"layer": layer_key, "found": False, "reason": "no_papers", "desc": desc}

    meta = fetch_abstracts(pmids)
    abs_map = fetch_full_abstract(pmids)
    combined = abs_map.get("_combined", "")

    # 拼论文标题列表
    titles = []
    for p in pmids:
        t = meta.get(p, {}).get("title", "")
        if t:
            titles.append(f"PMID {p}: {t}")
    header = "检索到的论文：\n" + "\n".join(titles) + "\n\n摘要内容：\n"

    prompt = EXTRACTION_PROMPT_TMPL.format(metric_desc=desc, known_hint=hint)
    user_content = prompt + header + combined[:10000]

    try:
        raw = call_openai(
            system_prompt="你是计算结构生物学专家。只输出合法 JSON，不要 markdown，不要解释。",
            user_content=user_content,
            model=model,
        )
        result = parse_json_loose(raw)
    except Exception as e:
        print(f"[thresh] {layer_key} LLM 失败: {e}", file=sys.stderr)
        result = None

    if not result or not result.get("found"):
        return {
            "layer": layer_key, "found": False, "desc": desc,
            "pmids_checked": pmids, "reason": "llm_no_threshold",
        }

    result.update({
        "layer": layer_key,
        "desc": desc,
        "pmids": pmids,
        "source_papers": [t for t in titles],
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="七层指标阈值文献检索")
    parser.add_argument("--model", default=None)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--layers", default=None,
                        help="只跑指定层，逗号分隔，如 L1_plddt,L7_scrmsd")
    args = parser.parse_args()

    model = args.model or os.environ.get("LLM_MODEL", "step-3.7-flash")
    _ = sys.stdin.read()  # 兼容管道输入

    layers = LAYER_QUERIES
    if args.layers:
        wanted = set(args.layers.split(","))
        layers = {k: v for k, v in LAYER_QUERIES.items() if k in wanted}

    print(f"[thresh] 检索 {len(layers)} 层指标阈值, 模型={model}", file=sys.stderr)

    results = {}
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(research_one_layer, k, c, model): k for k, c in layers.items()}
        for fut in as_completed(futs):
            k = futs[fut]
            try:
                r = fut.result(timeout=180)
                results[k] = r
                status = f"found value={r.get('value')}{r.get('operator','')}" if r.get("found") else "not found"
                print(f"[thresh] {k}: {status}", file=sys.stderr)
            except Exception as e:
                results[k] = {"layer": k, "found": False, "reason": f"exception: {e}"}
                print(f"[thresh] {k}: exception {e}", file=sys.stderr)

    n_found = sum(1 for r in results.values() if r.get("found"))
    output = {
        "metric_battery": results,
        "_meta": {
            "n_layers": len(results),
            "n_found": n_found,
            "llm_model": model,
            "note": "文献阈值 + 已知论文值；最终以正对照标定为准",
        },
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
