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

import json, os, sys, argparse, re, threading, time, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
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
    },
    "L2_ipsae": {
        "desc": "ipSAE 界面置信度阈值（小环肽主指标，替代 ipTM）",
        "queries": [
            "ipSAE interface",
            "predicted aligned error protein interface",
            "AlphaFold interface confidence peptide",
        ],
    },
    "L3_dg": {
        "desc": "PRODIGY 预测结合自由能 dG 的筛选阈值",
        "queries": [
            "PRODIGY binding affinity",
            "PRODIGY protein peptide binding affinity",
        ],
    },
    "L3_sc": {
        "desc": "Rosetta shape complementarity (SC) 的筛选阈值",
        "queries": [
            "Rosetta shape complementarity protein interface",
            "shape complementarity protein peptide design",
        ],
    },
    "L3_dsasa": {
        "desc": "蛋白-肽界面埋藏表面积 dSASA 的筛选阈值",
        "queries": [
            "buried surface area protein peptide complex",
            "protein peptide interface delta SASA",
        ],
    },
    "L4_ring_closure": {
        "desc": "环肽环闭合几何 QC（肽键距离）",
        "queries": [
            "head-to-tail cyclic peptide",
            "macrocyclization peptide bond",
        ],
    },
    "L5_hotspot_coverage": {
        "desc": "热点残基覆盖率定义（设计意图）",
        "queries": [
            "hotspot residues protein interface design",
            "p53 MDM2 peptide inhibitor hotspot",
        ],
    },
    "L6_pose_convergence": {
        "desc": "多预测器/多 seed 结合 pose 收敛 RMSD",
        "queries": [
            "binding pose prediction RMSD",
            "ColabFold AlphaFold protein peptide docking",
        ],
    },
    "L7_scrmsd": {
        "desc": "序列 refold 自洽 scRMSD 阈值",
        "queries": [
            "ProteinMPNN",
            "de novo protein design self-consistency",
            "sequence design AlphaFold refold",
        ],
    },
}

EXTRACTION_PROMPT_TMPL = """你是计算结构生物学专家，专门做环肽/蛋白设计指标评估。

需要审核的指标是"{metric_desc}"。请只根据下方按 PMID 分隔的论文题目和摘要，判断论文是否明确写出了可作为筛选标准的数值。不要使用领域常识补数值，不要把其他指标的数字移植过来。

提取字段：
- value: 阈值数值
- operator: ">" / "<" / ">=" / "<="
- unit: 原文单位，无则 null
- metric_name: 指标名称
- evidence_grade: 有明确数字时只能写 "paper_explicit"
- source_pmid: 数字直接来自哪一篇论文
- evidence_quote: 包含指标名和数值的摘要原文短句，必须逐字来自该 PMID 的摘要
- context: 该数值在论文中的用途，例如训练过滤、最终筛选或结果描述
- confidence: high/medium/low

摘要没有明确数字，或数字仅描述某个实验结果而不是筛选标准时，返回 {{"found": false}}。

严格 JSON 输出，不要 markdown，不要解释。

以下是论文信息：
"""


# ===== PubMed 工具 =====
_LAST_REQ = [0.0]
_THROTTLE_LOCK = threading.Lock()

def _throttle(min_interval=0.5):
    """NCBI 限速：无 key 3 req/s，保守取 0.5s 间隔。"""
    with _THROTTLE_LOCK:
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
    """用 EFetch XML 建立严格的 PMID -> 摘要映射。"""
    texts = {}
    if not pmids:
        return texts
    _throttle()
    try:
        params = urllib.parse.urlencode({
            "db": "pubmed", "id": ",".join(pmids),
            "rettype": "abstract", "retmode": "xml",
        })
        with urllib.request.urlopen(f"{PUBMED_FETCH_URL}?{params}", timeout=30) as resp:
            root = ET.fromstring(resp.read().decode("utf-8", errors="replace"))
        for article in root.findall(".//PubmedArticle"):
            pmid_elem = article.find(".//MedlineCitation/PMID")
            if pmid_elem is None or not pmid_elem.text:
                continue
            sections = []
            for abstract in article.findall(".//Abstract/AbstractText"):
                section = " ".join("".join(abstract.itertext()).split())
                label = abstract.attrib.get("Label")
                if section:
                    sections.append(f"{label}: {section}" if label else section)
            texts[pmid_elem.text] = " ".join(sections)
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


def _normalize_evidence(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


# ===== 单层处理 =====
def research_one_layer(layer_key: str, cfg: dict, model: str) -> dict:
    """对一层指标：检索 -> 取摘要 -> LLM 抽取阈值。"""
    desc = cfg["desc"]

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

    # 每篇摘要与 PMID 成对传给模型，避免合并文本后来源归属不清。
    titles = []
    records = []
    for p in pmids:
        t = meta.get(p, {}).get("title", "")
        if t:
            titles.append(f"PMID {p}: {t}")
        abstract = abs_map.get(p, "")
        if abstract:
            records.append(f"PMID: {p}\nTITLE: {t}\nABSTRACT: {abstract[:5000]}")

    if not records:
        return {
            "layer": layer_key,
            "found": False,
            "desc": desc,
            "pmids_checked": pmids,
            "reason": "no_abstract_text",
        }

    if not os.environ.get("OPENAI_API_KEY"):
        return {
            "layer": layer_key,
            "found": False,
            "desc": desc,
            "pmids_checked": pmids,
            "papers_with_abstract": len(records),
            "reason": "llm_unavailable_no_api_key",
        }

    prompt = EXTRACTION_PROMPT_TMPL.format(metric_desc=desc)
    user_content = prompt + "\n\n--- PAPER ---\n".join(records)

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

    source_pmid = str(result.get("source_pmid", ""))
    evidence_quote = str(result.get("evidence_quote", ""))
    source_abstract = abs_map.get(source_pmid, "")
    quote_verified = (
        len(_normalize_evidence(evidence_quote)) >= 20
        and _normalize_evidence(evidence_quote) in _normalize_evidence(source_abstract)
    )
    evidence_grade = result.get("evidence_grade")
    auto_usable = (
        result.get("value") is not None
        and source_pmid in pmids
        and evidence_grade == "paper_explicit"
        and quote_verified
    )

    verified_result = {
        **result,
        "layer": layer_key,
        "desc": desc,
        "pmids_checked": pmids,
        "source_papers": titles,
        "quote_verified": quote_verified,
        "auto_usable": auto_usable,
    }
    if not auto_usable:
        verified_result["found"] = False
        verified_result["reason"] = "extraction_failed_source_verification"
    return verified_result


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

    llm_available = bool(os.environ.get("OPENAI_API_KEY"))
    print(
        f"[thresh] 检索 {len(layers)} 个指标阈值, 模型={model}, "
        f"LLM={'available' if llm_available else 'unavailable'}",
        file=sys.stderr,
    )

    results = {}
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(research_one_layer, k, c, model): k for k, c in layers.items()}
        for fut in as_completed(futs):
            k = futs[fut]
            try:
                r = fut.result(timeout=180)
                results[k] = r
                status = (
                    f"verified value={r.get('operator', '')}{r.get('value')}"
                    if r.get("auto_usable")
                    else f"not usable ({r.get('reason', 'no verified threshold')})"
                )
                print(f"[thresh] {k}: {status}", file=sys.stderr)
            except Exception as e:
                results[k] = {"layer": k, "found": False, "reason": f"exception: {e}"}
                print(f"[thresh] {k}: exception {e}", file=sys.stderr)

    n_found = sum(1 for r in results.values() if r.get("found"))
    n_auto_usable = sum(1 for r in results.values() if r.get("auto_usable"))
    output = {
        "metric_battery": results,
        "_meta": {
            "n_layers": len(results),
            "n_found": n_found,
            "n_auto_usable": n_auto_usable,
            "llm_model": model,
            "llm_available": llm_available,
            "run_status": "complete" if llm_available else "degraded_no_llm",
            "note": "只有 PMID 与摘要原句校验通过的 paper_explicit 数值可自动覆盖默认值；其余等待正对照标定。",
        },
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
