"""
Step 7: LLM 提取 — 从 PubMed 文献摘要中提取双靶分子信息。

调用方式:
    python -m scripts.llm_extract < data/pubmed_results.json > data/llm_extracted.json

使用方法:
    本脚本需要配置 LLM API。默认使用环境变量 OPENAI_API_KEY 或 ANTHROPIC_API_KEY。
    也支持传入 --provider openai|anthropic|local 和 --model 参数。

    # OpenAI
    export OPENAI_API_KEY=sk-xxx
    python -m scripts.llm_extract --provider openai < data/pubmed_results.json

    # Anthropic
    export ANTHROPIC_API_KEY=sk-ant-xxx
    python -m scripts.llm_extract --provider anthropic < data/pubmed_results.json

    # DeepSeek (兼容 OpenAI API)
    export OPENAI_API_KEY=sk-xxx
    export OPENAI_BASE_URL=https://api.deepseek.com
    python -m scripts.llm_extract --provider openai --model deepseek-chat < data/pubmed_results.json

输出: JSON 对象 {known_binders, pocket_insights, llm_raw_response}
"""

import json, os, sys, argparse
from pathlib import Path

EXTRACTION_PROMPT = """你是一个蛋白质结构生物学专家。从以下 PubMed 文献列表中提取所有 MDM2/MDMX 双靶肽类抑制剂的信息。

对于每个分子，提取（如果有）：
1. name: 分子名称
2. type: 类型（linear peptide / stapled peptide / cyclic peptide / other）
3. sequence: 氨基酸序列（单字母）
4. kd_mdm2: MDM2 亲和力
5. kd_mdmx: MDMX 亲和力
6. key_residues: 关键残基列表（对应 p53 的 Phe19/Trp23/Leu26）
7. pmid: 来源 PMID
8. pdb_ids: 相关的 PDB ID（如果有）
9. design_insight: 设计上的启发（如为什么能双靶、什么结构特征关键）

另外，分析文献中关于三口袋（Phe19/Trp23/Leu26 pocket）的结构洞察：
- 每个口袋的保守/差异特征
- 双靶设计的关键约束

请以 JSON 格式输出，结构为：
{
  "known_dual_binders": [...],
  "pocket_analysis": {
    "Phe19_pocket": {"insight": "...", "constraints": "..."},
    "Trp23_pocket": {"insight": "...", "constraints": "..."},
    "Leu26_pocket": {"insight": "...", "constraints": "..."}
  },
  "design_recommendation": "..."
}

如果某个分子或口袋的信息在文献中不存在，用 null 替代，不要编造。

以下是文献列表：
"""


def call_openai(prompt: str, model: str = "gpt-4o") -> dict:
    """调用 OpenAI API 做结构化提取。"""
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY 未设置")

    import urllib.request
    # Step 3.7 Flash 是推理模型：max_tokens 要给够，否则 content 为空
    # 推理模型不支持 response_format json_object，改用 system prompt 约束
    is_stepfun = "stepfun" in base_url or "step_plan" in base_url
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个蛋白质结构生物学专家。只输出合法 JSON，不要额外解释，不要 markdown 代码块。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    if not is_stepfun:
        payload["response_format"] = {"type": "json_object"}
    if "step-3.7" in model:
        payload["reasoning_effort"] = "low"  # 降低推理 token 消耗

    payload_bytes = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result


def call_anthropic(prompt: str, model: str = "claude-sonnet-4-20250514") -> dict:
    """调用 Anthropic API 做结构化提取。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 未设置")

    import urllib.request
    payload = json.dumps({
        "model": model,
        "max_tokens": 4096,
        "temperature": 0.1,
        "messages": [
            {"role": "user", "content": prompt + "\n\n只输出 JSON，不要额外解释。"},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 提取双靶分子信息")
    parser.add_argument("--provider", default="openai", choices=["openai", "anthropic"])
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    input_data = json.loads(sys.stdin.read())
    papers = input_data.get("papers", [])

    if not papers:
        print(json.dumps({"error": "no papers to extract from"}, ensure_ascii=False))
        return 1

    # 构建 prompt
    papers_text = json.dumps(papers, ensure_ascii=False, indent=2)
    full_prompt = EXTRACTION_PROMPT + papers_text

    # 调 LLM
    try:
        if args.provider == "openai":
            model = args.model or os.environ.get("LLM_MODEL", "step-3.7-flash")
            llm_result = call_openai(full_prompt, model=model)
            raw_content = llm_result["choices"][0]["message"]["content"]
        elif args.provider == "anthropic":
            model = args.model or os.environ.get("LLM_MODEL", "claude-sonnet-4-20250514")
            llm_result = call_anthropic(full_prompt, model=model)
            raw_content = llm_result["content"][0]["text"]
        else:
            raise ValueError(f"unknown provider: {args.provider}")
    except Exception as e:
        print(f"[llm_extract] LLM 调用失败: {e}", file=sys.stderr)
        output = {"error": str(e), "known_binders": [], "pocket_analysis": {}}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 1

    # 解析 JSON
    try:
        extracted = json.loads(raw_content)
    except json.JSONDecodeError:
        # 尝试从文本中提取 JSON 块
        import re
        match = re.search(r'\{[\s\S]*\}', raw_content)
        if match:
            try:
                extracted = json.loads(match.group())
            except json.JSONDecodeError:
                extracted = {"raw": raw_content, "parse_error": True}
        else:
            extracted = {"raw": raw_content, "parse_error": True}

    output = {
        "known_binders": extracted.get("known_dual_binders", []),
        "pocket_analysis": extracted.get("pocket_analysis", {}),
        "design_recommendation": extracted.get("design_recommendation", ""),
        "llm_provider": args.provider,
        "llm_model": model,
        "llm_raw_response": raw_content[:2000],  # 截断，避免日志爆炸
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
