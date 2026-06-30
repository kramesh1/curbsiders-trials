import re
from collections import Counter
from urllib.parse import urlparse

VALID_SPECIALTY_TAGS = {
    "cardiology",
    "infectious disease",
    "pulmonology",
    "nephrology",
    "endocrinology",
    "gastroenterology",
    "neurology",
    "hematology",
    "oncology",
    "preventive medicine",
    "rheumatology",
    "dermatology",
    "psychiatry",
    "geriatrics",
    "emergency medicine",
    "general internal medicine",
}

STUDY_TYPE_ALIASES = {
    "rct": "RCT",
    "randomized controlled trial": "RCT",
    "randomised controlled trial": "RCT",
    "observational": "observational",
    "cohort": "observational",
    "case-control": "observational",
    "case control": "observational",
    "meta-analysis": "meta-analysis",
    "meta analysis": "meta-analysis",
    "meta": "meta-analysis",
    "systematic review": "systematic review",
    "systematic": "systematic review",
    "guideline": "guideline",
    "guidelines": "guideline",
    "case series": "case series",
    "other": "other",
}

# Sentinel strings models sometimes emit in place of a real null. They must be
# coalesced to None so they don't survive normalization as junk titles/URLs.
NULL_SENTINELS = {"null", "none", "n/a", "na", "nil", "undefined"}

MATCH_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "among",
    "about",
    "according",
    "analysis",
    "article",
    "clinical",
    "data",
    "effect",
    "effects",
    "events",
    "event",
    "journal",
    "patient",
    "patients",
    "prevention",
    "prevention",
    "randomised",
    "randomized",
    "review",
    "risk",
    "risks",
    "study",
    "trials",
    "trial",
    "use",
    "used",
}


def clean_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)
    if not text or text.lower() in NULL_SENTINELS:
        return None
    return text


def normalize_key_text(value) -> str:
    text = clean_text(value) or ""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_specialty_tags(tags) -> list[str]:
    if not tags:
        return []
    if isinstance(tags, str):
        tags = re.split(r"[,;/]", tags)
    normalized = []
    for tag in tags:
        cleaned = clean_text(tag)
        if not cleaned:
            continue
        cleaned = cleaned.lower()
        if cleaned in VALID_SPECIALTY_TAGS:
            normalized.append(cleaned)
    return sorted(set(normalized))


def normalize_study_type(value) -> str:
    cleaned = normalize_key_text(value)
    if not cleaned:
        return "other"
    return STUDY_TYPE_ALIASES.get(cleaned, "other")


def normalize_year(value) -> int | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, int):
        return value if 1900 <= value <= 2100 else None
    match = re.search(r"\b(19|20)\d{2}\b", str(value))
    if not match:
        return None
    year = int(match.group(0))
    return year if 1900 <= year <= 2100 else None


def normalize_pubmed_url(url) -> str | None:
    cleaned = clean_text(url)
    if not cleaned:
        return None
    cleaned = cleaned.replace("http://", "https://")
    parsed = urlparse(cleaned)
    if not parsed.scheme or not parsed.netloc:
        return cleaned
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}" if path else f"{parsed.scheme}://{parsed.netloc}"


def extract_markdown_links(text: str) -> list[dict]:
    links = []
    i = 0
    while i < len(text):
        label_start = text.find("[", i)
        if label_start == -1:
            break
        label_end = text.find("](", label_start)
        if label_end == -1:
            break
        url_start = label_end + 2
        depth = 1
        cursor = url_start
        while cursor < len(text) and depth > 0:
            char = text[cursor]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            cursor += 1
        if depth != 0:
            i = label_start + 1
            continue

        label = clean_text(text[label_start + 1:label_end])
        url = clean_text(text[url_start:cursor - 1])
        if label and url and url.startswith(("http://", "https://")):
            links.append({"label": label, "url": url})
        i = cursor
    return links


def recover_missing_urls_from_show_notes(trials: list[dict], show_notes: str) -> list[dict]:
    links = extract_markdown_links(show_notes)
    if not links:
        return trials

    recovered = []
    for trial in trials:
        if clean_text(trial.get("pubmed_url")):
            recovered.append(trial)
            continue

        best = None
        for link in links:
            score = _link_match_score(trial, link)
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, link)

        updated = dict(trial)
        if best and _is_confident_link_match(trial, best[0], best[1]):
            updated["pubmed_url"] = best[1]["url"]
        recovered.append(updated)
    return recovered


def _is_confident_link_match(trial: dict, score: float, link: dict) -> bool:
    label = normalize_key_text(link.get("label"))
    paper_title = normalize_key_text(trial.get("paper_title"))
    citation_label = normalize_key_text(trial.get("citation_label"))
    if paper_title and label == paper_title:
        return True
    if citation_label and label == citation_label:
        return True
    return score >= 5.0


def _link_match_score(trial: dict, link: dict) -> float:
    link_label = clean_text(link.get("label")) or ""
    normalized_link = normalize_key_text(link_label)
    if not normalized_link:
        return 0.0

    paper_title = clean_text(trial.get("paper_title")) or ""
    citation_label = clean_text(trial.get("citation_label")) or ""
    context_topic = clean_text(trial.get("context_topic")) or ""

    normalized_title = normalize_key_text(paper_title)
    normalized_label = normalize_key_text(citation_label)

    score = 0.0
    if normalized_title and normalized_title == normalized_link:
        score += 12.0
    if normalized_label and normalized_label == normalized_link:
        score += 10.0
    if normalized_title and _substantial_substring_match(normalized_title, normalized_link, min_chars=24):
        score += 6.0
    if normalized_label and _substantial_substring_match(normalized_label, normalized_link, min_chars=18):
        score += 4.0

    score += 6.0 * _token_overlap_ratio(paper_title, link_label)
    score += 4.0 * _token_overlap_ratio(context_topic, link_label)
    score += 3.0 * _token_overlap_ratio(citation_label, link_label)

    if _looks_like_raw_url_label(link):
        score -= 3.0
    return score


def _substantial_substring_match(left: str, right: str, *, min_chars: int) -> bool:
    if min(len(left), len(right)) < min_chars:
        return False
    return left in right or right in left


def _token_overlap_ratio(left: str, right: str) -> float:
    left_tokens = _match_tokens(left)
    right_tokens = _match_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens)


def _match_tokens(value: str) -> set[str]:
    return {
        token
        for token in normalize_key_text(value).split()
        if len(token) >= 4 and token not in MATCH_STOPWORDS
    }


def _looks_like_raw_url_label(link: dict) -> bool:
    label = clean_text(link.get("label")) or ""
    url = clean_text(link.get("url")) or ""
    return label.startswith(("http://", "https://")) or normalize_pubmed_url(label) == normalize_pubmed_url(url)


def normalize_trial_record(trial: dict) -> dict | None:
    citation_label = clean_text(trial.get("citation_label"))
    paper_title = clean_text(trial.get("paper_title"))
    pubmed_url = normalize_pubmed_url(trial.get("pubmed_url"))
    if not any([citation_label, paper_title, pubmed_url]):
        return None

    record = {
        "citation_label": citation_label,
        "paper_title": paper_title,
        "pubmed_url": pubmed_url,
        "year": normalize_year(trial.get("year")),
        "brief_summary": clean_text(trial.get("brief_summary")),
        "context_topic": clean_text(trial.get("context_topic")),
        "study_type": normalize_study_type(trial.get("study_type")),
        "specialty_tags": normalize_specialty_tags(trial.get("specialty_tags")),
        "episode_number": trial.get("episode_number"),
        "episode_title": clean_text(trial.get("episode_title")),
        "episode_url": clean_text(trial.get("episode_url")),
        "episode_date": clean_text(trial.get("episode_date")),
    }
    return record


def choose_preferred_text(values) -> str | None:
    candidates = [clean_text(v) for v in values if clean_text(v)]
    if not candidates:
        return None
    return max(candidates, key=len)


def most_common_value(values, default=None):
    filtered = [v for v in values if v not in (None, "", [])]
    if not filtered:
        return default
    return Counter(filtered).most_common(1)[0][0]


def trial_identity_key(trial: dict) -> tuple:
    pubmed_url = normalize_pubmed_url(trial.get("pubmed_url"))
    if pubmed_url:
        return ("pubmed", pubmed_url)

    paper_title = normalize_key_text(trial.get("paper_title"))
    if paper_title:
        return ("title", paper_title)

    citation_label = normalize_key_text(trial.get("citation_label"))
    year = normalize_year(trial.get("year"))
    if citation_label:
        return ("label", citation_label, year)

    return ("fallback", id(trial))


def merge_trial_records(records: list[dict]) -> dict:
    merged = dict(records[0])
    merged["citation_label"] = choose_preferred_text(r.get("citation_label") for r in records)
    merged["paper_title"] = choose_preferred_text(r.get("paper_title") for r in records)
    merged["pubmed_url"] = choose_preferred_text(r.get("pubmed_url") for r in records)
    merged["year"] = most_common_value([normalize_year(r.get("year")) for r in records])
    merged["brief_summary"] = choose_preferred_text(r.get("brief_summary") for r in records)
    merged["context_topic"] = choose_preferred_text(r.get("context_topic") for r in records)
    merged["study_type"] = most_common_value(
        [normalize_study_type(r.get("study_type")) for r in records],
        default="other",
    )
    merged["specialty_tags"] = sorted({
        tag
        for record in records
        for tag in normalize_specialty_tags(record.get("specialty_tags"))
    })
    merged["episode_number"] = most_common_value([r.get("episode_number") for r in records])
    merged["episode_title"] = choose_preferred_text(r.get("episode_title") for r in records)
    merged["episode_url"] = choose_preferred_text(r.get("episode_url") for r in records)
    merged["episode_date"] = choose_preferred_text(r.get("episode_date") for r in records)
    return merged


def dedupe_trial_mentions(trials: list[dict]) -> list[dict]:
    grouped: dict[tuple, list[dict]] = {}
    for trial in trials:
        normalized = normalize_trial_record(trial)
        if not normalized:
            continue
        grouped.setdefault(trial_identity_key(normalized), []).append(normalized)
    return [merge_trial_records(records) for records in grouped.values()]


def build_canonical_trial_records(trials: list[dict]) -> list[dict]:
    grouped: dict[tuple, list[dict]] = {}
    for trial in trials:
        normalized = normalize_trial_record(trial)
        if not normalized:
            continue
        grouped.setdefault(trial_identity_key(normalized), []).append(normalized)

    canonical_records = []
    for key, records in grouped.items():
        merged = merge_trial_records(records)
        episodes = {}
        for record in records:
            episode_url = record.get("episode_url")
            if not episode_url:
                continue
            episode = episodes.setdefault(episode_url, {
                "episode_number": record.get("episode_number"),
                "episode_title": record.get("episode_title"),
                "episode_url": episode_url,
                "episode_date": record.get("episode_date"),
            })
            if not episode.get("episode_title") and record.get("episode_title"):
                episode["episode_title"] = record["episode_title"]
            if not episode.get("episode_number") and record.get("episode_number"):
                episode["episode_number"] = record["episode_number"]
            if not episode.get("episode_date") and record.get("episode_date"):
                episode["episode_date"] = record["episode_date"]

        episode_list = sorted(
            episodes.values(),
            key=lambda item: (-(item.get("episode_number") or 0), item.get("episode_title") or ""),
        )
        context_topics = sorted({
            topic for topic in (clean_text(r.get("context_topic")) for r in records) if topic
        })
        specialty_tags = sorted({
            tag
            for record in records
            for tag in normalize_specialty_tags(record.get("specialty_tags"))
        })

        canonical_records.append({
            "canonical_key": "|".join(str(part) for part in key),
            "citation_label": merged.get("citation_label"),
            "paper_title": merged.get("paper_title"),
            "pubmed_url": merged.get("pubmed_url"),
            "year": merged.get("year"),
            "brief_summary": merged.get("brief_summary"),
            "study_type": merged.get("study_type"),
            "specialty_tags": specialty_tags,
            "context_topic": merged.get("context_topic"),
            "context_topics": context_topics,
            "episode_titles": [ep.get("episode_title") for ep in episode_list if ep.get("episode_title")],
            "episodes": episode_list,
            "mention_count": len(records),
            "episode_count": len(episode_list),
            "latest_episode_number": max((ep.get("episode_number") or 0) for ep in episode_list) if episode_list else 0,
        })

    canonical_records.sort(
        key=lambda trial: (-(trial.get("latest_episode_number") or 0), trial.get("citation_label") or "")
    )
    for index, trial in enumerate(canonical_records):
        trial["id"] = index
    return canonical_records


def split_show_notes_into_chunks(show_notes: str, max_chars: int = 6000, overlap_lines: int = 3) -> list[str]:
    lines = [clean_text(line) for line in show_notes.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []

    chunks = []
    current = []
    current_len = 0

    def flush():
        nonlocal current, current_len
        if not current:
            return
        chunks.append("\n".join(current))
        current = current[-overlap_lines:] if overlap_lines else []
        current_len = sum(len(line) + 1 for line in current)

    for line in lines:
        if len(line) > max_chars:
            if current:
                flush()
            start = 0
            while start < len(line):
                segment = line[start:start + max_chars]
                chunks.append(segment)
                start += max_chars
            current = []
            current_len = 0
            continue

        projected = current_len + len(line) + (1 if current else 0)
        if current and projected > max_chars:
            flush()
        current.append(line)
        current_len += len(line) + (1 if current_len else 0)

    if current:
        chunks.append("\n".join(current))

    return chunks
