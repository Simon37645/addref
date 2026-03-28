from __future__ import annotations

import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from typing import Any

import requests


class NCBIError(RuntimeError):
    pass


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
DEFAULT_RETRY_COUNT = 4
REQUEST_INTERVAL_WITHOUT_API_KEY = 0.34
REQUEST_INTERVAL_WITH_API_KEY = 0.12
_REQUEST_PACING_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0


@dataclass(slots=True)
class PubMedArticle:
    pmid: str
    title: str
    abstract: str
    journal: str
    year: str
    volume: str
    issue: str
    pages: str
    doi: str
    authors: list[str]

    def to_brief_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["authors_short"] = ", ".join(self.authors[:4]) if self.authors else ""
        data["pubmed_url"] = f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"
        return data

    def to_reference_line(self, index: int) -> str:
        authors = ", ".join(self.authors[:6])
        if len(self.authors) > 6:
            authors = f"{authors}, et al."
        bits = [f"[{index}]"]
        if authors:
            bits.append(f"{authors}.")
        if self.title:
            bits.append(f"{self.title}.")
        journal_bits = []
        if self.journal:
            journal_bits.append(self.journal)
        year_issue = ""
        if self.year:
            year_issue = self.year
        if self.volume:
            year_issue = f"{year_issue};{self.volume}" if year_issue else self.volume
        if self.issue:
            year_issue = f"{year_issue}({self.issue})" if year_issue else f"({self.issue})"
        if self.pages:
            year_issue = f"{year_issue}:{self.pages}" if year_issue else self.pages
        if year_issue:
            journal_bits.append(year_issue)
        if journal_bits:
            bits.append(" ".join(journal_bits) + ".")
        if self.doi:
            bits.append(f"doi:{self.doi}.")
        return " ".join(part for part in bits if part).strip()


class NCBIClient:
    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, api_key: str, email: str = "", tool_name: str = "addref-local") -> None:
        self.api_key = api_key.strip()
        self.email = email.strip()
        self.tool_name = tool_name
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    f"{self.tool_name}/1.0"
                    if not self.email
                    else f"{self.tool_name}/1.0 ({self.email})"
                )
            }
        )

    def search_pubmed(self, query: str, retmax: int = 8) -> list[PubMedArticle]:
        params = {
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "retmax": str(retmax),
            "sort": "relevance",
            "tool": self.tool_name,
        }
        if self.api_key:
            params["api_key"] = self.api_key
        if self.email:
            params["email"] = self.email

        response = self._get("esearch.fcgi", params=params, timeout=30, operation="esearch")
        payload = response.json()
        ids = payload.get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        return self.fetch_pubmed_details(ids)

    def fetch_pubmed_details(self, pmids: list[str]) -> list[PubMedArticle]:
        if not pmids:
            return []

        params = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml",
            "tool": self.tool_name,
        }
        if self.api_key:
            params["api_key"] = self.api_key
        if self.email:
            params["email"] = self.email

        try:
            response = self._get("efetch.fcgi", params=params, timeout=45, operation="efetch")
            return self._parse_pubmed_articles(response.text, pmids)
        except NCBIError as exc:
            if len(pmids) <= 1:
                raise

            recovered: list[PubMedArticle] = []
            for pmid in pmids:
                try:
                    recovered.extend(self._fetch_pubmed_details_single_batch([pmid]))
                except NCBIError:
                    continue
            if recovered:
                return recovered
            raise exc

    def _fetch_pubmed_details_single_batch(self, pmids: list[str]) -> list[PubMedArticle]:
        params = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml",
            "tool": self.tool_name,
        }
        if self.api_key:
            params["api_key"] = self.api_key
        if self.email:
            params["email"] = self.email

        response = self._get("efetch.fcgi", params=params, timeout=45, operation="efetch")
        return self._parse_pubmed_articles(response.text, pmids)

    def _parse_pubmed_articles(self, xml_text: str, pmids: list[str]) -> list[PubMedArticle]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise NCBIError("Failed to parse PubMed XML response.") from exc

        article_map: dict[str, PubMedArticle] = {}
        for article_node in root.findall(".//PubmedArticle"):
            article = _parse_pubmed_article(article_node)
            if article:
                article_map[article.pmid] = article

        return [article_map[pmid] for pmid in pmids if pmid in article_map]

    def _get(
        self,
        endpoint: str,
        *,
        params: dict[str, str],
        timeout: int,
        operation: str,
    ) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(DEFAULT_RETRY_COUNT + 1):
            self._pace_requests()
            try:
                response = self.session.get(f"{self.base_url}/{endpoint}", params=params, timeout=timeout)
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= DEFAULT_RETRY_COUNT:
                    raise NCBIError(f"NCBI {operation} request failed: {exc}") from exc
                self._sleep_before_retry(attempt)
                continue

            if response.status_code < 400:
                return response

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < DEFAULT_RETRY_COUNT:
                last_error = NCBIError(
                    f"NCBI {operation} temporary failure {response.status_code}: {response.text[:220]}"
                )
                self._sleep_before_retry(attempt)
                continue

            raise NCBIError(f"NCBI {operation} failed with {response.status_code}: {response.text[:300]}")

        raise NCBIError(f"NCBI {operation} request failed: {last_error}")

    def _pace_requests(self) -> None:
        global _LAST_REQUEST_AT
        min_interval = REQUEST_INTERVAL_WITH_API_KEY if self.api_key else REQUEST_INTERVAL_WITHOUT_API_KEY
        with _REQUEST_PACING_LOCK:
            now = time.monotonic()
            wait_seconds = max(0.0, min_interval - (now - _LAST_REQUEST_AT))
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            _LAST_REQUEST_AT = time.monotonic()

    def _sleep_before_retry(self, attempt: int) -> None:
        time.sleep(min(6.0, 0.6 * (2**attempt)))


def _parse_pubmed_article(node: ET.Element) -> PubMedArticle | None:
    pmid = _text(node.find("./MedlineCitation/PMID"))
    if not pmid:
        return None

    title = _collapse_space(_itertext(node.find("./MedlineCitation/Article/ArticleTitle")))
    abstract_parts = []
    for abstract_node in node.findall("./MedlineCitation/Article/Abstract/AbstractText"):
        label = abstract_node.attrib.get("Label", "").strip()
        text = _collapse_space(_itertext(abstract_node))
        if not text:
            continue
        abstract_parts.append(f"{label}: {text}" if label else text)

    journal = _collapse_space(_text(node.find("./MedlineCitation/Article/Journal/Title")))
    volume = _collapse_space(_text(node.find("./MedlineCitation/Article/Journal/JournalIssue/Volume")))
    issue = _collapse_space(_text(node.find("./MedlineCitation/Article/Journal/JournalIssue/Issue")))
    pages = _collapse_space(_text(node.find("./MedlineCitation/Article/Pagination/MedlinePgn")))
    year = _extract_year(node)
    doi = _extract_doi(node)
    authors = _extract_authors(node)

    return PubMedArticle(
        pmid=pmid,
        title=title,
        abstract=" ".join(abstract_parts).strip(),
        journal=journal,
        year=year,
        volume=volume,
        issue=issue,
        pages=pages,
        doi=doi,
        authors=authors,
    )


def _extract_authors(node: ET.Element) -> list[str]:
    authors: list[str] = []
    for author_node in node.findall("./MedlineCitation/Article/AuthorList/Author"):
        collective = _collapse_space(_text(author_node.find("./CollectiveName")))
        if collective:
            authors.append(collective)
            continue
        last_name = _collapse_space(_text(author_node.find("./LastName")))
        fore_name = _collapse_space(_text(author_node.find("./ForeName")))
        initials = _collapse_space(_text(author_node.find("./Initials")))
        if last_name and fore_name:
            authors.append(f"{last_name} {fore_name}")
        elif last_name and initials:
            authors.append(f"{last_name} {initials}")
        elif last_name:
            authors.append(last_name)
    return authors


def _extract_year(node: ET.Element) -> str:
    candidates = [
        "./MedlineCitation/Article/Journal/JournalIssue/PubDate/Year",
        "./MedlineCitation/Article/ArticleDate/Year",
        ".//PubMedPubDate[@PubStatus='pubmed']/Year",
        ".//PubMedPubDate[@PubStatus='entrez']/Year",
    ]
    for path in candidates:
        value = _collapse_space(_text(node.find(path)))
        if value:
            return value

    medline_date = _collapse_space(
        _text(node.find("./MedlineCitation/Article/Journal/JournalIssue/PubDate/MedlineDate"))
    )
    if medline_date:
        match = re.search(r"(19|20)\d{2}", medline_date)
        if match:
            return match.group(0)
    return ""


def _extract_doi(node: ET.Element) -> str:
    for article_id in node.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if article_id.attrib.get("IdType") == "doi":
            return _collapse_space(_itertext(article_id))
    return ""


def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return html.unescape(node.text)


def _itertext(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return html.unescape("".join(node.itertext()))


def _collapse_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
