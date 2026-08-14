"""
Step 6: PubMed E-utilities — 搜文献 + 摘要 + PMC 全文（优先用全文）。

调用方式:
    python -m scripts.pubmed_search > data/pubmed_results.json

输出: JSON {search_term, pmids, papers, n_total}
每篇论文含 title, authors, source, pubdate, doi, abstract, full_text (PMC 全文, 优先), source_type
"""

import argparse, json, sys, time, urllib.request, urllib.parse, xml.etree.ElementTree as ET

from project_config import load_project_config

PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
PUBMED_FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PMC_CONVERT_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"

def build_search_term(config: dict) -> str:
    target_terms = []
    for target in config["targets"]:
        names = [target["id"]]
        if target.get("gene_name"):
            names.append(target["gene_name"])
        if target.get("uniprot"):
            names.append(target["uniprot"])
        target_terms.append("(" + " OR ".join(dict.fromkeys(names)) + ")")
    relationship = " AND ".join(target_terms) if len(target_terms) > 1 else target_terms[0]
    return f"({relationship}) AND (peptide OR macrocycle OR cyclic peptide) AND (binder OR inhibitor OR ligand)"


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


def fetch_pubmed_abstracts(pmids: list[str]) -> dict[str, str]:
    """用 EFetch XML 获取 PMID 对应摘要；ESummary 本身不返回 abstract。"""
    texts = {}
    for i in range(0, len(pmids), 20):
        batch = pmids[i:i + 20]
        try:
            params = urllib.parse.urlencode({
                "db": "pubmed",
                "id": ",".join(batch),
                "rettype": "abstract",
                "retmode": "xml",
            })
            with urllib.request.urlopen(f"{PUBMED_FETCH_URL}?{params}", timeout=60) as resp:
                root = ET.fromstring(resp.read().decode("utf-8"))
            for article in root.findall(".//PubmedArticle"):
                pmid_elem = article.find(".//MedlineCitation/PMID")
                if pmid_elem is None or not pmid_elem.text:
                    continue
                sections = []
                for abstract in article.findall(".//Abstract/AbstractText"):
                    text = " ".join("".join(abstract.itertext()).split())
                    label = abstract.attrib.get("Label")
                    if text:
                        sections.append(f"{label}: {text}" if label else text)
                texts[pmid_elem.text] = " ".join(sections)
        except Exception as e:
            print(f"[pubmed] 摘要获取失败 (batch {i}): {e}", file=sys.stderr)
        time.sleep(0.35)
    return texts


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
                    texts[pmid] = full[:30000]  # 截断到 30000 字符
        except Exception as e:
            print(f"[pubmed] PMC 全文失败 (batch {i}): {e}", file=sys.stderr)
        time.sleep(0.5)
    return texts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None, help="project JSON config")
    parser.add_argument("--max-results", type=int, default=30)
    args = parser.parse_args()
    config = load_project_config(args.config)
    search_term = build_search_term(config)
    pmids = search_pubmed(search_term, max_results=args.max_results)
    print(f"[pubmed] 搜索到 {len(pmids)} 篇", file=sys.stderr)

    meta = fetch_metadata(pmids)
    abstracts = fetch_pubmed_abstracts(pmids)
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
        abstract_text = abstracts.get(pmid, "")

        papers.append({
            "pmid": pmid,
            "title": paper.get("title", ""),
            "pubdate": paper.get("pubdate", ""),
            "source": paper.get("source", ""),
            "authors": [a.get("name", "") for a in paper.get("authors", [])[:5]],
            "doi": paper.get("elocationid", ""),
            "content": full_text if full_text else abstract_text[:30000],
            "source_type": (
                "pmc_fulltext" if full_text
                else "pubmed_abstract" if abstract_text
                else "metadata_only"
            ),
        })

    n_pmc = sum(1 for p in papers if p["source_type"] == "pmc_fulltext")
    n_abstract = sum(1 for p in papers if p["source_type"] == "pubmed_abstract")
    n_metadata = sum(1 for p in papers if p["source_type"] == "metadata_only")
    print(
        f"[pubmed] {len(papers)} 篇: {n_pmc} PMC 全文 + "
        f"{n_abstract} 摘要 + {n_metadata} 仅元数据",
        file=sys.stderr,
    )

    output = {
        "project_id": config["project_id"],
        "targets": [{"id": target["id"], "uniprot": target.get("uniprot")} for target in config["targets"]],
        "search_term": search_term,
        "pmids": pmids,
        "papers": papers,
        "n_total": len(papers),
        "run_status": "complete" if papers else "failed_or_empty",
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
