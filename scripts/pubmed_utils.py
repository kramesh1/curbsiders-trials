"""
Resolve a PubMed ID from a citation URL and fetch its real abstract.

Everything else in this pipeline derives trial detail from the podcast's own
show-notes prose (see scripts/trial_detail_utils.py, which deliberately defers
PICO extraction to "a future model-backed pass" to avoid inventing detail the
show notes never stated). This module is that pass's grounding source: for the
subset of citations that resolve to a real PubMed ID, it fetches the actual
published abstract via NCBI's free E-utilities API, so a later model summary
can be checked against real study text instead of a secondhand gloss.

No API key is required, but NCBI asks that unauthenticated callers stay at or
below 3 requests/second -- _throttle() enforces that. Pass an api_key to allow
the documented higher rate (10/sec) once one is available.

Fetch failures (bad PMID, no abstract on record, network error) return None
rather than raising, so a batch caller can fall back to a show-notes-only
summary instead of crashing.
"""

import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

try:
    from scripts.trial_utils import normalize_pubmed_url
except ImportError:
    from trial_utils import normalize_pubmed_url

EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# PMIDs are parseable out of these two citation URL shapes; anything else
# (doi.org, publisher domains like nejm.org/jamanetwork.com) is left alone --
# resolve_pmid() returns None and the caller falls back to show-notes-only.
_PUBMED_PATH_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")
_LEGACY_PUBMED_PATH_RE = re.compile(r"ncbi\.nlm\.nih\.gov/pubmed/(\d+)")

_last_request_time = 0.0
_MIN_INTERVAL_NO_KEY = 1.0 / 3  # NCBI's documented no-API-key limit
_MIN_INTERVAL_WITH_KEY = 1.0 / 10


def _throttle(*, has_api_key: bool) -> None:
    global _last_request_time
    min_interval = _MIN_INTERVAL_WITH_KEY if has_api_key else _MIN_INTERVAL_NO_KEY
    elapsed = time.monotonic() - _last_request_time
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_request_time = time.monotonic()


def resolve_pmid(pubmed_url) -> str | None:
    """Parse a PMID out of a pubmed.ncbi.nlm.nih.gov or legacy ncbi.nlm.nih.gov/pubmed URL."""
    normalized = normalize_pubmed_url(pubmed_url)
    if not normalized:
        return None
    match = _PUBMED_PATH_RE.search(normalized) or _LEGACY_PUBMED_PATH_RE.search(normalized)
    return match.group(1) if match else None


def _abstract_text(article: ET.Element) -> str | None:
    parts = []
    for node in article.iter("AbstractText"):
        label = node.get("Label")
        text = "".join(node.itertext()).strip()
        if not text:
            continue
        parts.append(f"{label}: {text}" if label else text)
    return "\n".join(parts) if parts else None


def _first_text(article: ET.Element, path: str) -> str | None:
    node = article.find(path)
    if node is None:
        return None
    text = "".join(node.itertext()).strip()
    return text or None


def _publication_types(article: ET.Element) -> list[str]:
    return [t.text.strip() for t in article.iter("PublicationType") if t.text and t.text.strip()]


def _pub_year(article: ET.Element) -> int | None:
    for path in (".//PubDate/Year", ".//PubDate/MedlineDate"):
        text = _first_text(article, path)
        if not text:
            continue
        match = re.search(r"\b(19|20)\d{2}\b", text)
        if match:
            return int(match.group(0))
    return None


def fetch_pubmed_abstract(pmid: str, *, api_key: str | None = None, email: str | None = None, tool: str = "curbsiders_to_trials") -> dict | None:
    """Fetch {title, abstract, journal, year, publication_types, pmid} for a PMID, or None on any failure."""
    params = {"db": "pubmed", "id": pmid, "rettype": "abstract", "retmode": "xml", "tool": tool}
    if email:
        params["email"] = email
    if api_key:
        params["api_key"] = api_key

    _throttle(has_api_key=bool(api_key))
    url = f"{EFETCH_URL}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None

    return parse_efetch_response(raw, pmid=pmid)


def parse_efetch_response(raw: bytes, *, pmid: str) -> dict | None:
    """Parse an efetch XML payload into {pmid, title, abstract, journal, year,
    publication_types}, or None if it has no usable abstract."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None

    article = root.find(".//Article")
    if article is None:
        return None

    abstract = _abstract_text(article)
    if not abstract:
        return None

    return {
        "pmid": pmid,
        "title": _first_text(article, ".//ArticleTitle"),
        "abstract": abstract,
        "journal": _first_text(article, ".//Journal/Title") or _first_text(article, ".//Journal/ISOAbbreviation"),
        "year": _pub_year(article),
        "publication_types": _publication_types(article),
    }


_SCREENING_FIELDS = (
    "population", "intervention", "comparator", "outcome", "study_quality_limitations",
)


def attach_screening(canonical_trials: list[dict], screening_records: list[dict]) -> list[dict]:
    """Copy approved PICO/quality screening from a trial_screening_approved.json
    sidecar onto matching canonical trial records, by canonical_key.

    Mirrors scripts.pearl_utils.attach_evidence_links: matches by key rather
    than replacing the canonical set wholesale, so a trial with no screening
    record yet renders unchanged. Returns new dicts; does not mutate the input.
    """
    by_key = {r["canonical_key"]: r for r in screening_records if r.get("canonical_key")}
    out = []
    for trial in canonical_trials:
        record = by_key.get(trial.get("canonical_key"))
        if record:
            trial = dict(trial)
            for field in _SCREENING_FIELDS:
                if record.get(field):
                    trial[field] = record[field]
            trial["grounded_in"] = record.get("grounded_in")
            trial["screening_confidence"] = record.get("confidence")
        out.append(trial)
    return out
