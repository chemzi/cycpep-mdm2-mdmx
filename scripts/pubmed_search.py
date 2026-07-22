#!/usr/bin/env python3
"""Step 2: PubMed E-utilities search for MDM2/MDMX dual peptide inhibitors.
Retrieves PMIDs, titles, years, abstracts for the given keyword sets (last ~10 yrs
plus seminal papers), for downstream manual extraction of dual binders.
"""
import json
import os
import time
import sys
import requests

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OUTDIR = os.environ.get("RESEARCH_OUTDIR", "output")
QUERIES = [
    "MDM2 MDMX dual peptide inhibitor",
    "cyclic peptide MDM2 p53",
    "stapled peptide MDMX p53",
    "ATSP-7041",
    "ALRN-6924",
    "dual MDM2 MDMX stapled peptide p53",
]

def esearch(term, retmax=40, mindate=2010):
    params = {"db": "pubmed", "term": term, "retmax": retmax, "retmode": "json",
              "sort": "relevance", "mindate": mindate, "maxdate": 2026, "datetype": "pdat"}
    r = requests.get(f"{BASE}/esearch.fcgi", params=params, timeout=40)
    r.raise_for_status()
    return r.json()["esearchresult"].get("idlist", [])

def esummary(pmids):
    if not pmids:
        return {}
    params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "json"}
    r = requests.get(f"{BASE}/esummary.fcgi", params=params, timeout=40)
    r.raise_for_status()
    return r.json().get("result", {})

def efetch_abstract(pmids):
    if not pmids:
        return ""
    params = {"db": "pubmed", "id": ",".join(pmids), "rettype": "abstract", "retmode": "text"}
    r = requests.get(f"{BASE}/efetch.fcgi", params=params, timeout=60)
    r.raise_for_status()
    return r.text

def main():
    all_pmids = {}
    for q in QUERIES:
        ids = esearch(q)
        all_pmids[q] = ids
        print(f"[{q}] -> {len(ids)} hits", file=sys.stderr)
        time.sleep(0.4)
    # unique union
    union = []
    for ids in all_pmids.values():
        for i in ids:
            if i not in union:
                union.append(i)
    print(f"\nUnion: {len(union)} unique PMIDs", file=sys.stderr)
    summ = esummary(union)
    catalog = []
    for pid in union:
        s = summ.get(pid, {})
        catalog.append({"pmid": pid, "title": s.get("title", ""),
                        "year": (s.get("pubdate", "") or "")[:4],
                        "journal": s.get("fulljournalname", s.get("source", ""))})
    os.makedirs(OUTDIR, exist_ok=True)
    json.dump({"queries": all_pmids, "catalog": catalog},
              open(os.path.join(OUTDIR, "pubmed_catalog.json"), "w"), indent=2)
    for c in sorted(catalog, key=lambda x: x["year"], reverse=True):
        print(f"  {c['pmid']} ({c['year']}) {c['title'][:80]}", file=sys.stderr)

if __name__ == "__main__":
    main()
