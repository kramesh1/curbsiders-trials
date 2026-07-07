"""
Deterministic extraction and linking of Curbsiders "Pearls".

Curbsiders show notes contain human-written teaching statements under a
"<Topic> Pearls" heading (e.g. "Nutrition Pearls", "Hypertension Pearls").
Those statements are the distilled teaching unit we want to surface. Because
they are already clean, quotable clinician language, we extract them verbatim
rather than paraphrasing with a model -- this keeps them faithful and free of
hallucination.

Each pearl is then linked to the clinical-evidence mentions already extracted
for the same episode (data/trials.json), so a teaching point carries its
supporting trials/guidelines. Linking is a deterministic term-overlap heuristic,
so the output is reproducible and unit-testable with no network calls.
"""

import re

try:
    from scripts.trial_utils import (
        MATCH_STOPWORDS,
        choose_preferred_text,
        clean_text,
        extract_markdown_links,
        normalize_key_text,
        normalize_pubmed_url,
        normalize_specialty_tags,
        trial_identity_key,
    )
except ImportError:
    from trial_utils import (
        MATCH_STOPWORDS,
        choose_preferred_text,
        clean_text,
        extract_markdown_links,
        normalize_key_text,
        normalize_pubmed_url,
        normalize_specialty_tags,
        trial_identity_key,
    )

# A heading line that introduces a pearls block, e.g. "Nutrition Pearls".
# We keep the optional label (the words before "Pearls") to use as a topic.
# Allows the curly apostrophe show notes use in possessives ("Women's Pearls")
# and the singular "Pearl" some episodes use for a single named pearl segment
# ("Kashlak Pearl", "Matt's Pearl").
PEARL_HEADING_RE = re.compile(
    r"^(?P<label>[A-Za-z0-9][A-Za-z0-9 ,&/()'’\-]{0,48}?\s+)?pearls?$",
    re.IGNORECASE,
)

# Trailing decoration on an otherwise-bare heading line: "Clinical Pearls:",
# "Kashlak Pearl:", "Matt's Pearl –" (heading-only, content follows on later
# lines). Stripped before matching against PEARL_HEADING_RE.
HEADING_DECORATION_RE = re.compile(r"[:\-–—]+\s*$")

# A line that is only a bare list-enumeration marker with no text ("1.", "2)"),
# an artifact of markdown lists getting split across lines when scraped. Safe
# to skip without ending the pearls block, since it can never itself be a
# heading or a real pearl statement.
BARE_ENUMERATION_RE = re.compile(r"^\d+[.)]$")

# Leading bullet/enumeration markers to strip from a pearl line.
BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*•·▪—]+|\d+[.)])\s+")

# A trailing "(Author et al. 2024)" style citation tail on a pearl line.
CITATION_TAIL_RE = re.compile(r"\s*\([^()]*\b(?:19|20)\d{2}[a-z]?\.?\)\s*$")

BARE_URL_RE = re.compile(r"https?://\S+")

# Labels that carry no useful topic; treat these pearl blocks as untopiced.
GENERIC_PEARL_LABELS = {
    "clinical",
    "key",
    "top",
    "quick",
    "some",
    "the",
    "a few",
    "bonus",
    "tales from the curbside top",
    "kashlak",
}

# A label that is only a host/guest name in possessive form ("Matt's",
# "Rahul Ganatra's") carries no clinical topic -- it's a recurring named
# segment, not a subject. A possessive phrase that continues past the name
# ("Women's Hematology") is a real topic and should not match.
POSSESSIVE_NAME_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z .]*['’]s$")

# Lines that clearly end a pearls block even if they superficially look long.
BOILERPLATE_PREFIXES = (
    "http://",
    "https://",
    "www.",
    "claim cme",
    "patreon",
    "subscribe",
    "disclosures",
    "the curbsiders report",
)

MAX_PEARLS_PER_BLOCK = 30
MIN_PEARL_WORDS = 5
MIN_PEARL_LONG_CHARS = 45
DEFAULT_MIN_LINK_SCORE = 0.20
DEFAULT_MAX_LINKS = 4


def _ends_with_sentence_punct(line: str) -> bool:
    return bool(re.search(r"[.!?]$", line.strip()))


def _clean_pearl_topic(label) -> str | None:
    topic = clean_text(label)
    if not topic:
        return None
    topic = topic.strip(" ,-&/")
    if not topic or topic.lower() in GENERIC_PEARL_LABELS:
        return None
    if POSSESSIVE_NAME_LABEL_RE.match(topic):
        return None
    return topic


def _is_pearl_statement(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.lower().startswith(BOILERPLATE_PREFIXES):
        return False
    words = stripped.split()
    if len(words) < MIN_PEARL_WORDS:
        return False
    return _ends_with_sentence_punct(stripped) or len(stripped) >= MIN_PEARL_LONG_CHARS


def _strip_pearl_line(line: str) -> str:
    stripped = BULLET_PREFIX_RE.sub("", line.strip()).strip()
    return stripped


def _strip_heading_decoration(line: str) -> str:
    """Strip trailing punctuation-only decoration so a heading like
    "Clinical Pearls:" or "Matt's Pearl –" matches PEARL_HEADING_RE."""
    return HEADING_DECORATION_RE.sub("", line.strip()).strip()


def parse_pearls_from_show_notes(show_notes: str) -> list[dict]:
    """Extract verbatim pearl statements grouped under their topic heading.

    Returns a list of {"topic": str|None, "pearl": str}. Within-episode
    duplicates (the pearls list often restates a line that also appears in a
    body segment) are collapsed.
    """
    lines = [line.strip() for line in (show_notes or "").splitlines()]
    n = len(lines)
    pearls: list[dict] = []

    i = 0
    while i < n:
        line = lines[i]
        heading = PEARL_HEADING_RE.match(_strip_heading_decoration(line)) if line else None
        if not heading or _ends_with_sentence_punct(line):
            i += 1
            continue

        topic = _clean_pearl_topic(heading.group("label"))
        i += 1
        collected = 0
        while i < n and collected < MAX_PEARLS_PER_BLOCK:
            candidate = lines[i]
            if not candidate:
                i += 1
                continue
            # A bare list-number artifact ("1.") from a split markdown list;
            # skip it without ending the block.
            if BARE_ENUMERATION_RE.match(candidate):
                i += 1
                continue
            # A new pearls heading ends this block; let the outer loop handle it.
            if PEARL_HEADING_RE.match(_strip_heading_decoration(candidate)) and not _ends_with_sentence_punct(candidate):
                break
            stripped = _strip_pearl_line(candidate)
            if not _is_pearl_statement(stripped):
                break
            pearls.append({"topic": topic, "pearl": stripped})
            collected += 1
            i += 1

    return _dedupe_pearls_within_episode(pearls)


def _pearl_dedupe_key(pearl_text: str) -> str:
    return normalize_key_text(CITATION_TAIL_RE.sub("", pearl_text))


def _dedupe_pearls_within_episode(pearls: list[dict]) -> list[dict]:
    by_key: dict[str, dict] = {}
    for pearl in pearls:
        key = _pearl_dedupe_key(pearl["pearl"])
        if not key:
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = dict(pearl)
            continue
        # Prefer the longer statement and keep a topic if either has one.
        if len(pearl["pearl"]) > len(existing["pearl"]):
            existing["pearl"] = pearl["pearl"]
        if not existing.get("topic") and pearl.get("topic"):
            existing["topic"] = pearl["topic"]
    return list(by_key.values())


def _content_tokens(text) -> set[str]:
    """Distinctive, lightly-singularized tokens for overlap scoring."""
    tokens = set()
    for token in normalize_key_text(text).split():
        if len(token) < 4 or token in MATCH_STOPWORDS:
            continue
        if token.endswith("s") and len(token) > 4:
            token = token[:-1]
        tokens.add(token)
    return tokens


def _pearl_urls(pearl_text: str) -> set[str]:
    urls = {link["url"] for link in extract_markdown_links(pearl_text)}
    urls.update(BARE_URL_RE.findall(pearl_text))
    normalized = {normalize_pubmed_url(url) for url in urls}
    return {url for url in normalized if url}


def trial_canonical_key(trial: dict) -> str | None:
    """Canonical key string matching build_canonical_trial_records().

    Returns None for mentions with no stable identity (they have no canonical
    site record to link to).
    """
    key = trial_identity_key(trial)
    if key and key[0] == "fallback":
        return None
    return "|".join(str(part) for part in key)


def _pearl_trial_score(pearl_text: str, pearl_urls: set[str], trial: dict) -> float:
    distinctive = " ".join(
        part
        for part in (
            trial.get("citation_label"),
            trial.get("paper_title"),
            trial.get("context_topic"),
        )
        if part
    )
    trial_tokens = _content_tokens(distinctive)
    pearl_tokens = _content_tokens(pearl_text)

    score = 0.0
    if trial_tokens and pearl_tokens:
        overlap = trial_tokens & pearl_tokens
        score = len(overlap) / len(trial_tokens)

    trial_url = normalize_pubmed_url(trial.get("pubmed_url"))
    if trial_url and trial_url in pearl_urls:
        score += 1.0
    return score


def link_pearls_to_trials(
    pearls: list[dict],
    episode_trials: list[dict],
    *,
    min_score: float = DEFAULT_MIN_LINK_SCORE,
    max_links: int = DEFAULT_MAX_LINKS,
) -> list[dict]:
    """Attach supporting_citations + specialty_tags to each pearl in place."""
    for pearl in pearls:
        pearl_text = pearl["pearl"]
        pearl_urls = _pearl_urls(pearl_text)

        scored: list[tuple[float, dict]] = []
        for trial in episode_trials:
            canonical_key = trial_canonical_key(trial)
            if not canonical_key:
                continue
            score = _pearl_trial_score(pearl_text, pearl_urls, trial)
            if score >= min_score:
                scored.append((score, trial))

        scored.sort(key=lambda pair: pair[0], reverse=True)

        supporting: list[dict] = []
        seen_keys: set[str] = set()
        specialties: set[str] = set()
        for score, trial in scored:
            canonical_key = trial_canonical_key(trial)
            if canonical_key in seen_keys:
                continue
            seen_keys.add(canonical_key)
            supporting.append({
                "citation_label": clean_text(trial.get("citation_label")),
                "paper_title": clean_text(trial.get("paper_title")),
                "pubmed_url": normalize_pubmed_url(trial.get("pubmed_url")),
                "year": trial.get("year"),
                "study_type": trial.get("study_type") or "other",
                # Deterministic trial detail (present once enrich_trials has run).
                "journal": clean_text(trial.get("journal")),
                "sample_size": trial.get("sample_size"),
                "nct_id": clean_text(trial.get("nct_id")),
                "canonical_key": canonical_key,
                "score": round(score, 3),
            })
            specialties.update(normalize_specialty_tags(trial.get("specialty_tags")))
            if len(supporting) >= max_links:
                break

        pearl["supporting_citations"] = supporting
        pearl["specialty_tags"] = sorted(specialties)
    return pearls


def attach_evidence_links(pearls: list[dict], linked_records: list[dict]) -> list[dict]:
    """Copy model `evidence_links` from a pearls_linked.json sidecar onto `pearls`.

    Matches by (episode_url, pearl_key) rather than swapping pearls.json for the
    sidecar wholesale, so pearls added since the last `link_pearls_evidence.py
    apply` run (linking is owner-gated, not part of ingest.py) still show up --
    they simply carry no evidence_links yet. Returns new dicts; does not mutate
    the input pearls.
    """
    links_by_key = {
        (record.get("episode_url"), _pearl_dedupe_key(record.get("pearl", ""))): record.get("evidence_links")
        for record in linked_records
        if record.get("evidence_links")
    }
    out = []
    for pearl in pearls:
        key = (pearl.get("episode_url"), _pearl_dedupe_key(pearl.get("pearl", "")))
        evidence_links = links_by_key.get(key)
        if evidence_links:
            pearl = dict(pearl)
            pearl["evidence_links"] = evidence_links
        out.append(pearl)
    return out


# Rank model-linked evidence so the canonical merge can keep the single best
# link per trial when the same pearl/trial pair recurs across episodes.
_SUPPORT_RANK = {"direct": 2, "background": 1}
_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _evidence_link_rank(link: dict) -> tuple[int, int]:
    return (
        _SUPPORT_RANK.get(link.get("support"), 0),
        _CONFIDENCE_RANK.get(link.get("confidence"), 0),
    )


def build_canonical_pearls(pearls: list[dict]) -> list[dict]:
    """Collapse the same pearl said across episodes into one record.

    Merges episode backlinks, topics, specialty tags, and the union of
    supporting citations (keyed by canonical_key, keeping the highest score).
    """
    grouped: dict[str, list[dict]] = {}
    for pearl in pearls:
        key = _pearl_dedupe_key(pearl.get("pearl", ""))
        if not key:
            continue
        grouped.setdefault(key, []).append(pearl)

    canonical: list[dict] = []
    for records in grouped.values():
        pearl_text = choose_preferred_text(r.get("pearl") for r in records)
        topics = sorted({t for t in (clean_text(r.get("topic")) for r in records) if t})
        specialty_tags = sorted({
            tag
            for record in records
            for tag in normalize_specialty_tags(record.get("specialty_tags"))
        })
        segments = sorted({s for s in (clean_text(r.get("segment")) for r in records) if s})
        clinical_topics = sorted({t for t in (clean_text(r.get("clinical_topic")) for r in records) if t})
        episode_categories = sorted({
            c
            for record in records
            for c in normalize_specialty_tags(
                [record.get("episode_category"), *(record.get("secondary_categories") or [])]
            )
        })

        episodes: dict[str, dict] = {}
        for record in records:
            url = clean_text(record.get("episode_url"))
            if not url or url in episodes:
                continue
            episodes[url] = {
                "episode_number": record.get("episode_number"),
                "episode_title": clean_text(record.get("episode_title")),
                "episode_url": url,
                "episode_date": clean_text(record.get("episode_date")),
            }
        episode_list = sorted(
            episodes.values(),
            key=lambda item: (-(item.get("episode_number") or 0), item.get("episode_title") or ""),
        )

        citations: dict[str, dict] = {}
        for record in records:
            for citation in record.get("supporting_citations", []) or []:
                canonical_key = citation.get("canonical_key")
                if not canonical_key:
                    continue
                existing = citations.get(canonical_key)
                if existing is None or (citation.get("score") or 0) > (existing.get("score") or 0):
                    citations[canonical_key] = dict(citation)
        supporting = sorted(
            citations.values(),
            key=lambda item: (-(item.get("score") or 0), item.get("citation_label") or ""),
        )

        evidence_links: dict[str, dict] = {}
        for record in records:
            for link in record.get("evidence_links", []) or []:
                canonical_key = link.get("canonical_key")
                if not canonical_key:
                    continue
                existing = evidence_links.get(canonical_key)
                if existing is None or _evidence_link_rank(link) > _evidence_link_rank(existing):
                    evidence_links[canonical_key] = dict(link)
        model_evidence = sorted(
            evidence_links.values(),
            key=lambda item: (-_evidence_link_rank(item)[0], -_evidence_link_rank(item)[1], item.get("citation_label") or ""),
        )

        canonical.append({
            "pearl": pearl_text,
            "topics": topics,
            "specialty_tags": specialty_tags,
            "segments": segments,
            "clinical_topics": clinical_topics,
            "episode_categories": episode_categories,
            "episodes": episode_list,
            "episode_count": len(episode_list),
            "latest_episode_number": max((ep.get("episode_number") or 0) for ep in episode_list) if episode_list else 0,
            "supporting_citations": supporting,
            "citation_count": len(supporting),
            "evidence_links": model_evidence,
            "evidence_link_count": len(model_evidence),
        })

    canonical.sort(
        key=lambda pearl: (-(pearl.get("latest_episode_number") or 0), pearl.get("pearl") or "")
    )
    for index, pearl in enumerate(canonical):
        pearl["id"] = index
        pearl["pearl_key"] = _pearl_dedupe_key(pearl["pearl"])
    return canonical


def attach_feedback(canonical_pearls: list[dict], approved_feedback: list[dict]) -> list[dict]:
    """Copy aggregated, human-approved visitor feedback onto matching pearls.

    approved_feedback rows come from scripts/import_feedback.py apply, one row per
    (pearl_key) or (pearl_key, canonical_key), each carrying a flag_summary (counts
    per reason_code). Matches by key rather than replacing the canonical set
    wholesale, so a pearl with no feedback yet renders unchanged. Returns new
    dicts; does not mutate the input.
    """
    pearl_level = {
        row["pearl_key"]: row["flag_summary"]
        for row in approved_feedback
        if row.get("pearl_key") and not row.get("canonical_key") and row.get("flag_summary")
    }
    link_level = {
        (row["pearl_key"], row["canonical_key"]): row["flag_summary"]
        for row in approved_feedback
        if row.get("pearl_key") and row.get("canonical_key") and row.get("flag_summary")
    }
    if not pearl_level and not link_level:
        return canonical_pearls

    out = []
    for pearl in canonical_pearls:
        pearl_key = pearl.get("pearl_key")
        flag_summary = pearl_level.get(pearl_key)
        evidence_links = pearl.get("evidence_links") or []
        linked_flags = {
            link.get("canonical_key"): link_level.get((pearl_key, link.get("canonical_key")))
            for link in evidence_links
        }
        linked_flags = {key: value for key, value in linked_flags.items() if value}
        if not flag_summary and not linked_flags:
            out.append(pearl)
            continue

        pearl = dict(pearl)
        if flag_summary:
            pearl["flag_summary"] = flag_summary
        if linked_flags:
            pearl["evidence_links"] = [
                {**link, "flag_summary": linked_flags[link.get("canonical_key")]}
                if link.get("canonical_key") in linked_flags
                else link
                for link in evidence_links
            ]
        out.append(pearl)
    return out
