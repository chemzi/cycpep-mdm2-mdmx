"""
Step 6: PubMed E-utilities — 搜文献 + 摘要 + PMC 全文（优先用全文）。

调用方式:
    python -m scripts.pubmed_search > data/pubmed_results.json

输出: JSON {search_term, pmids, papers, n_total}
每篇论文含 title, authors, source, pubdate, doi, abstract, full_text (PMC 全文, 优先), source_type
"""

import json, sys, time, urllib.request, urllib.parse, xml.etree.ElementTree as ET

PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
PUBMED_FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PMC_CONVERT_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"

SEARCH_TERM = "MDM2 MDMX dual peptide inhibitor"


def search_pubmed(term: str, max_results: int = 30) -> list[str]:
    params = urllib.parse.urlencode({
        "db": "pubmed", "term": term, "retmax": max_results,
        "retmode": "json", "sort": "relevance",
    })
    try:
        with urllib.request.urlopen(f"{PUBMED_SEARCH_URL}?{params}", timeout=30) as resp:
            return json.loads(resp.read().decode()).get("esearchresult", {}).get("idlist", [])
    except Exception as e:
        print(f"[pubmed] 搜索失败: {e}", file=sys.stderr)
        return []


def fetch_metadata(pmids: list[str]) -> dict:
    if not pmids: return {}
    try:
        params = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(pmids), "retmode": "json"})
        with urllib.request.urlopen(f"{PUBMED_SUMMARY_URL}?{params}", timeout=30) as resp:
            return json.loads(resp.read().decode()).get("result", {})
    except Exception as e:
        print(f"[pubmed] 元数据失败: {e}", file=sys.stderr)
        return {}


def fetch_pmc_ids(pmids: list[str]) -> dict[str, str]:
    """PMID -> PMCID 映射。"""
    pmc_map = {}
    for i in range(0, len(pmids), 20):
        batch = pmids[i:i+20]
        try:
            url = f"{PMC_CONVERT_URL}?ids={','.join(batch)}&format=json"
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            for rec in data.get("records", []):
                pid = rec.get("pmid")
                pcid = rec.get("pmcid")
                if pid and pcid:
                    pmc_map[pid] = pcid
        except Exception as e:
            print(f"[pubmed] PMC 转换失败 (batch {i}): {e}", file=sys.stderr)
        time.sleep(0.3)
    return pmc_map


def fetch_pmc_fulltext(pmc_ids: list[str]) -> dict[str, str]:
    """获取 PMC 全文（提取正文所有段落）。"""
    texts = {}
    for i in range(0, len(pmc_ids), 5):
        batch = pmc_ids[i:i+5]
        try:
            params = urllib.parse.urlencode({"db": "pmc", "id": ",".join(batch), "rettype": "xml"})
            with urllib.request.urlopen(f"{PUBMED_FETCH_URL}?{params}", timeout=60) as resp:
                xml_text = resp.read().decode("utf-8")
            root = ET.fromstring(xml_text)
            for article in root.findall(".//{*}article"):
                # Find PMID in article-meta
                pmid_elem = article.find(".//{*}article-id[@pub-id-type='pmid']")
                if pmid_elem is None:
                    continue
                pmid = pmid_elem.text
                # Extract all paragraph text from body
                paragraphs = []
                for p in article.findall(".//{*}body//{*}p"):
                    text = p.text or ""
                    for child in p.iter():
                        if child.text and child is not p:
                            text += " " + child.text
                        if child.tail:
                            text += " " + child.tail
                    if text.strip():
                        paragraphs.append(text.strip())
                full = " ".join(paragraphs)
                if pmid:
                    texts[pmid] = full[:8000]  # 截断到 8000 字符
        except Exception as e:
            print(f"[pubmed] PMC 全文失败 (batch {i}): {e}", file=sys.stderr)
        time.sleep(0.5)
    return texts


def main() -> int:
    pmids = search_pubmed(SEARCH_TERM, max_results=30)
    print(f"[pubmed] 搜索到 {len(pmids)} 篇", file=sys.stderr)

    meta = fetch_metadata(pmids)
    print(f"[pubmed] 获取 PMC 映射...", file=sys.stderr)
    pmc_map = fetch_pmc_ids(pmids)
    print(f"[pubmed] PMC 可用: {len(pmc_map)}/{len(pmids)}", file=sys.stderr)

    # 获取 PMC 全文
    pmc_ids = list(pmc_map.values())
    pmc_texts = {}
    if pmc_ids:
        print(f"[pubmed] 获取 PMC 全文 ({len(pmc_ids)} 篇)...", file=sys.stderr)
        pmc_texts = fetch_pmc_fulltext(pmc_ids)

    papers = []
    for pmid in pmids:
        paper = meta.get(pmid, {})
        if not paper or "error" in paper:
            continue

        # 尝试用 PMC 全文，没有则用摘要
        full_text = pmc_texts.get(pmid, "")
        abstract_text = ""
        if not full_text:
            abstract_text = paper.get("abstract", "")

        papers.append({
            "pmid": pmid,
            "title": paper.get("title", ""),
            "pubdate": paper.get("pubdate", ""),
            "source": paper.get("source", ""),
            "authors": [a.get("name", "") for a in paper.get("authors", [])[:5]],
            "doi": paper.get("elocationid", ""),
            "content": full_text if full_text else abstract_text[:3000],
            "source_type": "pmc_fulltext" if full_text else "pubmed_abstract",
        })

    n_pmc = sum(1 for p in papers if p["source_type"] == "pmc_fulltext")
    print(f"[pubmed] {len(papers)} 篇: {n_pmc} PMC 全文 + {len(papers)-n_pmc} 摘要", file=sys.stderr)

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
