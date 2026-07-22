"""
Step 6: PubMed E-utilities — 搜 MDM2/MDMX 双靶肽类抑制剂文献。

调用方式:
    python -m scripts.pubmed_search > data/pubmed_results.json

输出: JSON 对象 {pmids, papers, n_total}
"""

import json, sys, time, urllib.request, urllib.parse, xml.etree.ElementTree as ET

PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PUBMED_SUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

SEARCH_TERM = "MDM2 MDMX dual peptide inhibitor"


def search_pubmed(term: str, max_results: int = 50) -> list[str]:
    """搜索 PubMed，返回 PMID 列表。"""
    params = urllib.parse.urlencode({
        "db": "pubmed",
        "term": term,
        "retmax": max_results,
        "retmode": "json",
        "sort": "relevance",
    })
    url = f"{PUBMED_SEARCH_URL}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[pubmed_search] 搜索失败: {e}", file=sys.stderr)
        return []
    return data.get("esearchresult", {}).get("idlist", [])


def fetch_summaries(pmids: list[str]) -> list[dict]:
    """获取 PMID 列表的摘要信息。"""
    if not pmids:
        return []
    params = urllib.parse.urlencode({
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "json",
    })
    url = f"{PUBMED_SUMMARY_URL}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[pubmed_search] 摘要获取失败: {e}", file=sys.stderr)
        return []

    results = []
    for pmid in pmids:
        paper = data.get("result", {}).get(pmid, {})
        if paper and "error" not in paper:
            results.append({
                "pmid": pmid,
                "title": paper.get("title", ""),
                "pubdate": paper.get("pubdate", ""),
                "source": paper.get("source", ""),
                "authors": [a.get("name", "") for a in paper.get("authors", [])[:5]],
                "doi": paper.get("elocationid", ""),
            })
        time.sleep(0.1)  # NCBI rate limit
    return results


def main() -> int:
    pmids = search_pubmed(SEARCH_TERM)
    papers = fetch_summaries(pmids)

    output = {
        "search_term": SEARCH_TERM,
        "pmids": pmids,
        "papers": papers,
        "n_total": len(papers),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
