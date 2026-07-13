"""
Deterministic show-note evidence hyperlink inventory.

This module harvests the actual hyperlinks in Curbsiders show notes and turns
likely clinical-evidence links into a canonical layer. It is intentionally
deterministic: no model decides whether a cited URL exists.
"""

from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

try:
    from scripts.trial_utils import (
        clean_text,
        extract_markdown_links,
        normalize_key_text,
        normalize_pubmed_url,
        normalize_study_type,
        normalize_year,
    )
except ImportError:
    from trial_utils import (
        clean_text,
        extract_markdown_links,
        normalize_key_text,
        normalize_pubmed_url,
        normalize_study_type,
        normalize_year,
    )


PMID_RE = re.compile(r"(?:pubmed\.ncbi\.nlm\.nih\.gov/|/pubmed/)(\d{4,9})", re.IGNORECASE)
PMCID_RE = re.compile(r"\bPMC\d+\b", re.IGNORECASE)
NCT_RE = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)

HIGH_CONFIDENCE_DOMAINS = {
    "pubmed.ncbi.nlm.nih.gov",
    "pmc.ncbi.nlm.nih.gov",
    "clinicaltrials.gov",
    "doi.org",
    "dx.doi.org",
    "cochranelibrary.com",
    "uspreventiveservicestaskforce.org",
}

EVIDENCE_DOMAINS = {
    "acc.org",
    "acpjournals.org",
    "ahajournals.org",
    "annals.org",
    "atsjournals.org",
    "bmj.com",
    "cdc.gov",
    "diabetesjournals.org",
    "gastrojournal.org",
    "idsociety.org",
    "jamanetwork.com",
    "journals.lww.com",
    "mayoclinicproceedings.org",
    "nejm.org",
    "ncbi.nlm.nih.gov",
    "nature.com",
    "onlinelibrary.wiley.com",
    "academic.oup.com",
    "publications.aap.org",
    "rheumatology.org",
    "sciencedirect.com",
    "springer.com",
    "thelancet.com",
    "thoracic.org",
}

EXCLUDED_DOMAINS = {
    "amazon.com",
    "apple.com",
    "facebook.com",
    "instagram.com",
    "patreon.com",
    "spotify.com",
    "thecurbsiders.com",
    "twitter.com",
    "vcuhealth.org",
    "x.com",
    "youtube.com",
}

EVIDENCE_LABEL_TERMS = {
    "guideline",
    "guidelines",
    "recommendation",
    "recommendations",
    "consensus",
    "statement",
    "trial",
    "study",
    "cohort",
    "randomized",
    "randomised",
    "meta",
    "analysis",
    "review",
    "systematic",
    "jama",
    "nejm",
    "lancet",
    "annals",
    "cochrane",
    "pubmed",
    "pmid",
}


def canonical_domain(url: str | None) -> str:
    parsed = urlparse(url or "")
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def domain_matches(domain: str, candidates: set[str]) -> bool:
    return any(domain == candidate or domain.endswith(f".{candidate}") for candidate in candidates)


def normalize_evidence_url(url: str | None) -> str | None:
    cleaned = normalize_pubmed_url(url)
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    if not parsed.scheme or not parsed.netloc:
        return cleaned

    domain = canonical_domain(cleaned)
    path = unquote(parsed.path).rstrip("/")

    pmid = extract_pmid(cleaned)
    if pmid:
        return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}"

    pmcid = extract_pmcid(cleaned)
    if pmcid:
        return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}"

    nct_id = extract_nct_id(cleaned)
    if domain.endswith("clinicaltrials.gov") and nct_id:
        return f"https://clinicaltrials.gov/study/{nct_id}"

    doi = extract_doi(cleaned)
    if domain in {"doi.org", "dx.doi.org"} and doi:
        return f"https://doi.org/{doi.lower()}"

    query = ""
    if domain.endswith("thelancet.com") and parsed.query:
        # Lancet article IDs sometimes contain parentheses in the path; keep the
        # path but drop tracking query parameters.
        query = ""
    return urlunparse(("https", domain, path or "", "", query, ""))


def extract_pmid(value: str | None) -> str | None:
    match = PMID_RE.search(value or "")
    return match.group(1) if match else None


def extract_pmcid(value: str | None) -> str | None:
    match = PMCID_RE.search(value or "")
    return match.group(0).upper() if match else None


def extract_nct_id(value: str | None) -> str | None:
    match = NCT_RE.search(value or "")
    return match.group(0).upper() if match else None


def extract_doi(value: str | None) -> str | None:
    text = unquote(value or "")
    parsed = urlparse(text)
    if canonical_domain(text) in {"doi.org", "dx.doi.org"}:
        doi = parsed.path.lstrip("/")
        return doi.lower() if doi else None
    query = parse_qs(parsed.query)
    for key in ("doi", "DOI"):
        if query.get(key):
            return query[key][0].lower()
    match = DOI_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(").,;").lower()


def evidence_identity_key(label: str | None, url: str | None) -> str | None:
    combined = " ".join(filter(None, [label or "", url or ""]))
    pmid = extract_pmid(combined)
    if pmid:
        return f"pmid|{pmid}"
    doi = extract_doi(combined)
    if doi:
        return f"doi|{doi}"
    pmcid = extract_pmcid(combined)
    if pmcid:
        return f"pmcid|{pmcid}"
    nct_id = extract_nct_id(combined)
    if nct_id:
        return f"nct|{nct_id}"
    normalized_url = normalize_evidence_url(url)
    if normalized_url:
        return f"url|{normalized_url}"
    return None


def likely_evidence_link(label: str | None, url: str | None) -> bool:
    normalized_url = normalize_evidence_url(url)
    if not normalized_url:
        return False
    domain = canonical_domain(normalized_url)
    if domain_matches(domain, EXCLUDED_DOMAINS):
        return False
    if any([extract_pmid(normalized_url), extract_pmcid(normalized_url), extract_doi(normalized_url), extract_nct_id(normalized_url)]):
        return True
    if domain_matches(domain, HIGH_CONFIDENCE_DOMAINS | EVIDENCE_DOMAINS):
        return True
    label_tokens = set(normalize_key_text(label).split())
    return bool(label_tokens & EVIDENCE_LABEL_TERMS) and domain_matches(domain, EVIDENCE_DOMAINS)


def infer_study_type_from_link(label: str | None, url: str | None) -> str:
    text = normalize_key_text(" ".join(filter(None, [label or "", url or ""])))
    if any(term in text for term in ["meta analysis", "metaanalysis"]):
        return "meta-analysis"
    if "systematic review" in text or "cochrane" in text:
        return "systematic review"
    if any(term in text for term in ["guideline", "guidelines", "recommendation", "consensus", "statement", "uspstf"]):
        return "guideline"
    if any(term in text for term in ["randomized", "randomised", "trial"]):
        return "RCT"
    if any(term in text for term in ["cohort", "case control", "observational"]):
        return "observational"
    return "other"


def build_show_note_evidence_records(episodes: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for episode in episodes:
        show_notes = episode.get("show_notes") or ""
        for link in extract_markdown_links(show_notes):
            label = clean_text(link.get("label"))
            url = normalize_evidence_url(link.get("url"))
            if not likely_evidence_link(label, url):
                continue
            evidence_key = evidence_identity_key(label, url)
            if not evidence_key:
                continue
            grouped.setdefault(evidence_key, []).append({
                "label": label,
                "url": url,
                "domain": canonical_domain(url),
                "episode_number": episode.get("episode_number"),
                "episode_title": clean_text(episode.get("title")),
                "episode_url": clean_text(episode.get("url")),
                "episode_date": clean_text(episode.get("date")),
            })

    records = []
    for evidence_key, mentions in grouped.items():
        labels = [mention.get("label") for mention in mentions if mention.get("label")]
        urls = [mention.get("url") for mention in mentions if mention.get("url")]
        primary_label = _choose_label(labels) or _choose_label(urls) or "Cited evidence"
        primary_url = Counter(urls).most_common(1)[0][0] if urls else None
        episodes_by_url = {}
        for mention in mentions:
            episode_url = mention.get("episode_url")
            if not episode_url:
                continue
            episode = episodes_by_url.setdefault(episode_url, {
                "episode_number": mention.get("episode_number"),
                "episode_title": mention.get("episode_title"),
                "episode_url": episode_url,
                "episode_date": mention.get("episode_date"),
            })
            if not episode.get("episode_title") and mention.get("episode_title"):
                episode["episode_title"] = mention["episode_title"]

        episode_list = sorted(
            episodes_by_url.values(),
            key=lambda item: (-(item.get("episode_number") or 0), item.get("episode_title") or ""),
        )
        records.append({
            "evidence_key": evidence_key,
            "canonical_key": None,
            "citation_label": primary_label,
            "url": primary_url,
            "urls": sorted(set(urls)),
            "domains": sorted({mention.get("domain") for mention in mentions if mention.get("domain")}),
            "year": normalize_year(" ".join(labels)),
            "study_type": infer_study_type_from_link(primary_label, primary_url),
            "episodes": episode_list,
            "episode_count": len(episode_list),
            "link_count": len(mentions),
            "mentions": _dedupe_mentions(mentions),
        })

    records.sort(key=lambda item: (-(item.get("episodes", [{}])[0].get("episode_number") or 0), item.get("citation_label") or ""))
    return records


def annotate_show_note_matches(show_note_records: list[dict], canonical_trials: list[dict]) -> list[dict]:
    alias_map = build_trial_alias_map(canonical_trials)
    annotated = []
    for record in show_note_records:
        updated = deepcopy(record)
        matched_key = match_show_note_record(record, alias_map)
        updated["canonical_key"] = matched_key
        annotated.append(updated)
    return annotated


def merge_show_note_evidence(canonical_trials: list[dict], show_note_records: list[dict]) -> tuple[list[dict], dict]:
    trials = [deepcopy(trial) for trial in canonical_trials]
    by_key = {trial.get("canonical_key"): trial for trial in trials if trial.get("canonical_key")}
    alias_map = build_trial_alias_map(trials)
    matched = 0
    unmatched_records = []

    for record in show_note_records:
        canonical_key = record.get("canonical_key") or match_show_note_record(record, alias_map)
        if canonical_key and canonical_key in by_key:
            matched += 1
            attach_show_note_record(by_key[canonical_key], record)
        else:
            unmatched_records.append(build_unmatched_trial_record(record))

    trials.extend(unmatched_records)
    for trial in trials:
        trial.setdefault("source_layers", ["model_extraction"])
        if trial.get("show_note_citations") and "show_notes_links" not in trial["source_layers"]:
            trial["source_layers"].append("show_notes_links")

    trials.sort(key=lambda trial: (-(trial.get("latest_episode_number") or 0), trial.get("citation_label") or ""))
    for index, trial in enumerate(trials):
        trial["id"] = index

    return trials, {
        "show_note_records": len(show_note_records),
        "matched_records": matched,
        "unmatched_records": len(unmatched_records),
        "show_note_link_mentions": sum(record.get("link_count", 0) for record in show_note_records),
    }


def build_trial_alias_map(canonical_trials: list[dict]) -> dict[str, str]:
    aliases = {}
    for trial in canonical_trials:
        canonical_key = trial.get("canonical_key")
        if not canonical_key:
            continue
        for alias in sorted(trial_aliases(trial)):
            aliases.setdefault(alias, canonical_key)
    return aliases


def trial_aliases(trial: dict) -> set[str]:
    aliases = {trial.get("canonical_key")}
    url = normalize_evidence_url(trial.get("pubmed_url"))
    if url:
        aliases.add(f"url|{url}")
        identity = evidence_identity_key(trial.get("citation_label"), url)
        if identity:
            aliases.add(identity)
    nct_id = extract_nct_id(trial.get("nct_id"))
    if nct_id:
        aliases.add(f"nct|{nct_id}")
    return {alias for alias in aliases if alias}


def match_show_note_record(record: dict, alias_map: dict[str, str]) -> str | None:
    aliases = [record.get("evidence_key")]
    for url in sorted(record.get("urls", []) or []):
        aliases.append(evidence_identity_key(record.get("citation_label"), url))
        aliases.append(f"url|{normalize_evidence_url(url)}")
    for alias in dict.fromkeys(aliases):
        if alias and alias in alias_map:
            return alias_map[alias]
    return None


def attach_show_note_record(trial: dict, record: dict) -> None:
    existing_keys = {
        (citation.get("evidence_key"), citation.get("url"))
        for citation in trial.get("show_note_citations", []) or []
    }
    citations = list(trial.get("show_note_citations", []) or [])
    for mention in record.get("mentions", []) or []:
        key = (record.get("evidence_key"), mention.get("url"))
        if key in existing_keys:
            continue
        citations.append({
            "evidence_key": record.get("evidence_key"),
            "label": mention.get("label") or record.get("citation_label"),
            "url": mention.get("url"),
            "episode_number": mention.get("episode_number"),
            "episode_title": mention.get("episode_title"),
            "episode_url": mention.get("episode_url"),
        })
        existing_keys.add(key)
    trial["show_note_citations"] = sorted(
        citations,
        key=lambda item: (-(item.get("episode_number") or 0), item.get("label") or ""),
    )
    trial["show_note_link_count"] = len(citations)
    trial["show_note_episode_count"] = len({
        citation.get("episode_url") for citation in citations if citation.get("episode_url")
    })
    if not trial.get("pubmed_url") and record.get("url"):
        trial["pubmed_url"] = record["url"]


def build_unmatched_trial_record(record: dict) -> dict:
    episodes = record.get("episodes", []) or []
    latest_episode_number = max((episode.get("episode_number") or 0) for episode in episodes) if episodes else 0
    citation_label = record.get("citation_label") or "Cited evidence"
    pubmed_url = record.get("url")
    study_type = normalize_study_type(record.get("study_type"))
    return {
        "canonical_key": f"show_note|{record.get('evidence_key')}",
        "citation_label": citation_label,
        "paper_title": None,
        "pubmed_url": pubmed_url,
        "year": record.get("year"),
        "brief_summary": f"Cited in Curbsiders show notes as: {citation_label}.",
        "study_type": study_type,
        "specialty_tags": [],
        "nct_id": extract_nct_id(pubmed_url),
        "sample_size": None,
        "journal": None,
        "segments": [],
        "episode_categories": [],
        "context_topic": None,
        "context_topics": [],
        "episode_titles": [episode.get("episode_title") for episode in episodes if episode.get("episode_title")],
        "episodes": episodes,
        "mention_count": record.get("link_count", 0),
        "episode_count": len(episodes),
        "latest_episode_number": latest_episode_number,
        "show_note_citations": [
            {
                "evidence_key": record.get("evidence_key"),
                "label": mention.get("label") or citation_label,
                "url": mention.get("url"),
                "episode_number": mention.get("episode_number"),
                "episode_title": mention.get("episode_title"),
                "episode_url": mention.get("episode_url"),
            }
            for mention in record.get("mentions", []) or []
        ],
        "show_note_link_count": record.get("link_count", 0),
        "show_note_episode_count": len(episodes),
        "source_layers": ["show_notes_links"],
    }


def attach_pearl_backlinks(canonical_trials: list[dict], canonical_pearls: list[dict]) -> list[dict]:
    trials = [deepcopy(trial) for trial in canonical_trials]
    by_key = {trial.get("canonical_key"): trial for trial in trials if trial.get("canonical_key")}

    for pearl in canonical_pearls:
        for link in pearl.get("evidence_links", []) or []:
            canonical_key = link.get("canonical_key")
            trial = by_key.get(canonical_key)
            if not trial:
                continue
            backlinks = trial.setdefault("linked_pearls", [])
            if any(existing.get("pearl_key") == pearl.get("pearl_key") for existing in backlinks):
                continue
            backlinks.append({
                "pearl_key": pearl.get("pearl_key"),
                "pearl": pearl.get("pearl"),
                "support": link.get("support"),
                "confidence": link.get("confidence"),
                "rationale": link.get("rationale"),
                "episodes": (pearl.get("episodes") or [])[:3],
            })

    for trial in trials:
        backlinks = trial.get("linked_pearls") or []
        backlinks.sort(key=lambda item: (item.get("pearl") or ""))
        trial["linked_pearl_count"] = len(backlinks)
    return trials


def repair_pearl_evidence_links(canonical_pearls: list[dict], canonical_trials: list[dict]) -> tuple[list[dict], int]:
    """Rewrite stale pearl evidence canonical_keys to current evidence records.

    The reviewed pearl sidecar stores canonical keys from the trial layer that
    existed when linking was run. As extraction improves, a label-only key can
    become a PubMed or DOI key. This keeps reviewed links live without changing
    the owner-gated sidecar.
    """
    trial_keys = {trial.get("canonical_key") for trial in canonical_trials if trial.get("canonical_key")}
    alias_map = build_trial_alias_map(canonical_trials)
    label_map = build_trial_label_map(canonical_trials)
    by_key = {trial.get("canonical_key"): trial for trial in canonical_trials if trial.get("canonical_key")}
    repaired = 0
    out = []

    for pearl in canonical_pearls:
        updated_pearl = deepcopy(pearl)
        links = []
        for link in updated_pearl.get("evidence_links", []) or []:
            updated_link = dict(link)
            key = updated_link.get("canonical_key")
            if key not in trial_keys:
                replacement = match_pearl_link(updated_link, alias_map, label_map)
                if replacement:
                    updated_link["canonical_key"] = replacement
                    trial = by_key.get(replacement, {})
                    for field in ("citation_label", "paper_title", "pubmed_url", "year", "study_type", "journal", "sample_size", "nct_id"):
                        if not updated_link.get(field) and trial.get(field):
                            updated_link[field] = trial[field]
                    repaired += 1
            links.append(updated_link)
        updated_pearl["evidence_links"] = links
        updated_pearl["evidence_link_count"] = len(links)
        out.append(updated_pearl)
    return out, repaired


def _choose_label(values: list[str]) -> str | None:
    cleaned = [clean_text(value) for value in values if clean_text(value)]
    if not cleaned:
        return None
    counts = Counter(cleaned)
    return max(cleaned, key=lambda value: (counts[value], len(value)))


def build_trial_label_map(canonical_trials: list[dict]) -> dict[str, str]:
    candidates: dict[str, list[dict]] = {}
    for trial in canonical_trials:
        for field in ("citation_label", "paper_title"):
            label = normalize_key_text(trial.get(field))
            if label:
                candidates.setdefault(label, []).append(trial)

    label_map = {}
    for label, trials in candidates.items():
        ordered = sorted(
            trials,
            key=lambda trial: (
                0 if trial.get("pubmed_url") else 1,
                0 if "model_extraction" in (trial.get("source_layers") or ["model_extraction"]) else 1,
                -(trial.get("episode_count") or 0),
            ),
        )
        if ordered and ordered[0].get("canonical_key"):
            label_map[label] = ordered[0]["canonical_key"]
    return label_map


def match_pearl_link(link: dict, alias_map: dict[str, str], label_map: dict[str, str]) -> str | None:
    aliases = [link.get("canonical_key")]
    url = normalize_evidence_url(link.get("pubmed_url"))
    if url:
        aliases.append(f"url|{url}")
    identity = evidence_identity_key(link.get("citation_label"), link.get("pubmed_url"))
    if identity:
        aliases.append(identity)
    for alias in dict.fromkeys(aliases):
        if alias and alias in alias_map:
            return alias_map[alias]

    for label in (link.get("citation_label"), link.get("paper_title")):
        normalized = normalize_key_text(label)
        if normalized and normalized in label_map:
            return label_map[normalized]
    return None


def _dedupe_mentions(mentions: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for mention in mentions:
        key = (mention.get("label"), mention.get("url"), mention.get("episode_url"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(mention)
    return deduped
