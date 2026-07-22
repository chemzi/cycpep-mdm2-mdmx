"""
Step 6: PubMed E-utilities — 搜文献 + 获取摘要正文。

调用方式:
    python -m scripts.pubmed_search > data/pubmed_results.json

输出: JSON {search_term, pmids, papers, n_total}
每篇论文含 title, authors, source, pubdate, doi, abstract_text
"""

import json, sys, time, urllib.request, urllib.parse, xml.etree.ElementTree as ET

PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
PUBMED_FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

SEARCH_TERM = "MDM2 MDMX dual peptide inhibitor"


def search_pubmed(term: str, max_results: int = 30) -> list[str]:
    """搜索 PubMed，返回 PMID 列表。"""
    params = urllib.parse.urlencode({
        "db": "pubmed", "term": term, "retmax": max_results,
        "retmode": "json", "sort": "relevance",
    })
    url = f"{PUBMED_SEARCH_URL}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[pubmed_search] 搜索失败: {e}", file=sys.stderr)
        return []
    return data.get("esearchresult", {}).get("idlist", [])


def fetch_metadata(pmids: list[str]) -> dict[str, dict]:
    """获取 PMID 列表的标题/作者/期刊元数据。"""
    if not pmids:
        return {}
    params = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(pmids), "retmode": "json"})
    url = f"{PUBMED_SUMMARY_URL}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[pubmed_search] 元数据获取失败: {e}", file=sys.stderr)
        return {}
    return data.get("result", {})


def fetch_abstracts(pmids: list[str]) -> dict[str, str]:
    """获取 PMID 列表的摘要正文（XML efetch）。"""
    if not pmids:
        return {}
    # 分批，每批最多 50 个 PMID
    abstracts = {}
    for i in range(0, len(pmids), 50):
        batch = pmids[i:i+50]
        params = urllib.parse.urlencode({
            "db": "pubmed", "id": ",".join(batch),
            "rettype": "abstract", "retmode": "xml",
        })
        url = f"{PUBMED_FETCH_URL}?{params}"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                xml_text = resp.read().decode("utf-8")
            root = ET.fromstring(xml_text)
            for article in root.findall(".//PubmedArticle"):
                pmid_elem = article.find(".//PMID")
                if pmid_elem is None:
                    continue
                pmid = pmid_elem.text
                # 提取所有 AbstractText 元素
                abstract_parts = []
                for abs_elem in article.findall(".//AbstractText"):
                    label = abs_elem.get("Label", "")
                    text = abs_elem.text or ""
                    abstract_parts.append(f"{label}: {text}" if label else text)
                abstracts[pmid] = "\n".join(abstract_parts)
        except Exception as e:
            print(f"[pubmed_search] 摘要获取失败 (batch {i}): {e}", file=sys.stderr)
        time.sleep(0.5)  # NCBI rate limit
    return abstracts


def main() -> int:
    pmids = search_pubmed(SEARCH_TERM, max_results=30)
    print(f"[pubmed_search] 搜索到 {len(pmids)} 篇", file=sys.stderr)

    # 取元数据
    meta = fetch_metadata(pmids)
    # 取摘要
    print(f"[pubmed_search] 获取摘要...", file=sys.stderr)
    abstract_map = fetch_abstracts(pmids)

    papers = []
    for pmid in pmids:
        paper = meta.get(pmid, {})
        if not paper or "error" in paper:
            continue
        abstract_text = abstract_map.get(pmid, "")
        papers.append({
            "pmid": pmid,
            "title": paper.get("title", ""),
            "pubdate": paper.get("pubdate", ""),
            "source": paper.get("source", ""),
            "authors": [a.get("name", "") for a in paper.get("authors", [])[:5]],
            "doi": paper.get("elocationid", ""),
            "abstract": abstract_text[:3000],  # 截断过长摘要
        })

    print(f"[pubmed_search] {len(papers)} 篇带摘要", file=sys.stderr)
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
