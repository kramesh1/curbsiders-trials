"""
Deterministic enrichment of trial mentions with structured detail.

Today a trial mention is citation-oriented; the study's particulars (registry
id, sample size, journal) live only as prose. This module recovers the
high-precision, low-hallucination subset of that detail by parsing the show-note
text around each inline citation -- no model calls, so it is free, reproducible,
and safe to re-run on every ingest.

Free-text PICO (population / intervention / comparator / outcome) is deliberately
left to a future model-backed pass; extracting it deterministically would invite
exactly the hallucination the pipeline avoids.
"""

import re

try:
    from scripts.trial_utils import clean_text, normalize_pubmed_url
    from scripts.segment_utils import locate_citation_in_show_notes
except ImportError:
    from trial_utils import clean_text, normalize_pubmed_url
    from segment_utils import locate_citation_in_show_notes

NCT_RE = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)
# Sample size: "n = 1,234", "N=480", "n = 20". Capture the digits+commas.
SAMPLE_SIZE_RE = re.compile(r"\b[nN]\s*=\s*([\d,]{1,9})\b")

# Curated journal names / abbreviations -> canonical display name. Kept
# deliberately high-precision: only names that don't collide with ordinary
# clinical prose (so "blood pressure" or "chest pain" never yields a journal).
JOURNAL_PATTERNS = [
    (re.compile(r"\bNEJM\b|new england journal of medicine", re.I), "New England Journal of Medicine"),
    (re.compile(r"\bJAMA\b", re.I), "JAMA"),
    (re.compile(r"\bthe lancet\b|\blancet\.", re.I), "The Lancet"),
    (re.compile(r"\bBMJ\b|british medical journal", re.I), "BMJ"),
    (re.compile(r"annals of internal medicine", re.I), "Annals of Internal Medicine"),
    (re.compile(r"kidney international", re.I), "Kidney International"),
    (re.compile(r"\bJASN\b|journal of the american society of nephrology", re.I), "JASN"),
    (re.compile(r"diabetes care", re.I), "Diabetes Care"),
    (re.compile(r"\bJACC\b", re.I), "JACC"),
    (re.compile(r"clinical infectious diseases|\bCID\b", re.I), "Clinical Infectious Diseases"),
]

# Publisher domains embedded in a link -> journal, for the rare non-PubMed link.
PUBLISHER_JOURNALS = {
    "nejm.org": "New England Journal of Medicine",
    "jamanetwork.com": "JAMA",
    "thelancet.com": "The Lancet",
    "bmj.com": "BMJ",
    "annals.org": "Annals of Internal Medicine",
    "ahajournals.org": "Circulation",
}

_CONTEXT_WINDOW = 2


def extract_citation_context(show_notes: str, line_index: int | None, window: int = _CONTEXT_WINDOW) -> str:
    if line_index is None:
        return ""
    lines = (show_notes or "").splitlines()
    lo = max(0, line_index - window)
    hi = min(len(lines), line_index + window + 1)
    return " ".join(line.strip() for line in lines[lo:hi] if line.strip())


def _parse_sample_size(text: str) -> int | None:
    best = None
    for match in SAMPLE_SIZE_RE.finditer(text or ""):
        try:
            value = int(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if value <= 0:
            continue
        if best is None or value > best:
            best = value
    return best


def _parse_journal(text: str, pubmed_url: str | None) -> str | None:
    if pubmed_url:
        for domain, journal in PUBLISHER_JOURNALS.items():
            if domain in pubmed_url:
                return journal
    for pattern, journal in JOURNAL_PATTERNS:
        if pattern.search(text or ""):
            return journal
    return None


def parse_detail_from_context(context: str, *, citation_label: str = "", pubmed_url: str | None = None) -> dict:
    """Return {nct_id, sample_size, journal}, each None when not found."""
    haystack = " ".join(part for part in (citation_label, context) if part)
    nct = NCT_RE.search(haystack)
    return {
        "nct_id": nct.group(0).upper() if nct else None,
        "sample_size": _parse_sample_size(context),
        "journal": _parse_journal(haystack, pubmed_url),
    }


def enrich_trials_with_details(trials: list[dict], show_notes: str) -> list[dict]:
    """Attach nct_id / sample_size / journal to each of an episode's trials."""
    for trial in trials:
        line_index = locate_citation_in_show_notes(trial, show_notes)
        context = extract_citation_context(show_notes, line_index)
        detail = parse_detail_from_context(
            context,
            citation_label=clean_text(trial.get("citation_label")) or "",
            pubmed_url=normalize_pubmed_url(trial.get("pubmed_url")),
        )
        trial["nct_id"] = detail["nct_id"]
        trial["sample_size"] = detail["sample_size"]
        trial["journal"] = detail["journal"]
    return trials
