from __future__ import annotations

from app.services.ncbi import PubMedArticle


def build_ris(citations: list[dict]) -> str:
    entries: list[str] = []
    seen_pmids: set[str] = set()

    for citation in citations:
        article = citation.get("article", {})
        pmid = str(article.get("pmid", "")).strip()
        if not pmid or pmid in seen_pmids:
            continue
        seen_pmids.add(pmid)
        entries.append(_article_to_ris(article))

    return "\n".join(entry for entry in entries if entry).strip() + ("\n" if entries else "")


def build_ris_from_articles(articles: list[PubMedArticle]) -> str:
    citations = [{"article": article.to_brief_dict()} for article in articles]
    return build_ris(citations)


def _article_to_ris(article: dict) -> str:
    lines = ["TY  - JOUR"]
    title = (article.get("title") or "").strip()
    journal = (article.get("journal") or "").strip()
    year = (article.get("year") or "").strip()
    volume = (article.get("volume") or "").strip()
    issue = (article.get("issue") or "").strip()
    pages = (article.get("pages") or "").strip()
    doi = (article.get("doi") or "").strip()
    pmid = (article.get("pmid") or "").strip()
    url = article.get("pubmed_url") or (f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "")

    if title:
        lines.append(f"TI  - {title}")

    authors = article.get("authors") or []
    for author in authors:
        author = str(author).strip()
        if author:
            lines.append(f"AU  - {author}")

    if year:
        lines.append(f"PY  - {year}")
    if journal:
        lines.append(f"JO  - {journal}")
    if volume:
        lines.append(f"VL  - {volume}")
    if issue:
        lines.append(f"IS  - {issue}")
    if pages:
        if "-" in pages:
            start_page, end_page = [part.strip() for part in pages.split("-", 1)]
            if start_page:
                lines.append(f"SP  - {start_page}")
            if end_page:
                lines.append(f"EP  - {end_page}")
        else:
            lines.append(f"SP  - {pages}")
    if doi:
        lines.append(f"DO  - {doi}")
    if pmid:
        lines.append(f"ID  - PMID:{pmid}")
    if url:
        lines.append(f"UR  - {url}")

    lines.append("ER  -")
    return "\n".join(lines)
