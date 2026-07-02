"""
Deterministic sub-episode segmentation for Curbsiders show notes.

Many episodes cover a dozen distinct topics, so a single episode-level label is
too coarse. Show notes give us the structure to be more specific for free:

  1. A "Show Segments" block lists the episode's specific sub-topics in order,
     e.g. "Ketogenic Diets", "Mediterranean & DASH Diets", "Intermittent
     Fasting and Meal Timing".
  2. The body restates each segment as a subheading (TOC "Ketogenic Diets" ->
     body "The Ketogenic Diet") with the relevant inline citations underneath.

So a trial's citation *position* deterministically maps to a segment, and a
pearl inherits its segment from the trial it is already linked to. This module
is pure and testable -- no model calls, no network.
"""

try:
    from scripts.trial_utils import (
        MATCH_STOPWORDS,
        clean_text,
        extract_markdown_links,
        normalize_key_text,
        normalize_pubmed_url,
    )
except ImportError:
    from trial_utils import (
        MATCH_STOPWORDS,
        clean_text,
        extract_markdown_links,
        normalize_key_text,
        normalize_pubmed_url,
    )

# Segment/TOC entries that are show scaffolding, not clinical content. Matched
# either exactly or as a leading word (so "Intro and pun" is dropped too).
SEGMENT_STOPWORDS = {
    "intro",
    "introduction",
    "case",
    "outro",
    "disclosures",
    "disclosure",
    "disclaimer",
    "references",
    "recommended reading",
    "credits",
    "guest bio",
    "picks of the week",
    "sponsor",
    "sponsors",
}
_SCAFFOLDING_FIRST_WORDS = {"intro", "outro", "case", "disclosures", "disclaimer", "references", "credits"}

# Substrings that mark a TOC line as boilerplate prose rather than a real topic
# (common in the "DIGEST"/"Hotcakes" news-roundup episodes).
SEGMENT_JUNK_SUBSTRINGS = (
    "based on",
    "featured in",
    "this show",
    "the digest",
    "subscribe",
    "claim cme",
    "patreon",
)


def _is_scaffolding_segment(title: str) -> bool:
    normalized = normalize_key_text(title)
    if not normalized:
        return True
    if normalized in {normalize_key_text(s) for s in SEGMENT_STOPWORDS}:
        return True
    if normalized.split()[0] in _SCAFFOLDING_FIRST_WORDS:
        return True
    if any(junk in normalized for junk in SEGMENT_JUNK_SUBSTRINGS):
        return True
    if title.rstrip().endswith("#"):
        return True
    return False

# Sentence-ish punctuation that marks a line as prose rather than a heading.
_SENTENCE_TAIL = (".", "!", "?", ";", ",", ":")

# A body subheading's next non-empty line must be prose this long (or a
# citation), which is what separates it from the consecutive short lines of the
# TOC (each followed by another short TOC entry).
_PROSE_MIN_CHARS = 60

MIN_SEGMENT_MATCH = 0.5             # body heading <-> segment title (Jaccard)
MIN_SEGMENT_COVERAGE = 0.6          # pearl covers this fraction of a segment's tokens


def _segment_tokens(text) -> set[str]:
    """Distinctive, lightly-singularized tokens (mirrors pearl_utils scoring)."""
    tokens = set()
    for token in normalize_key_text(text).split():
        if len(token) < 4 or token in MATCH_STOPWORDS:
            continue
        if token.endswith("s") and len(token) > 4:
            token = token[:-1]
        tokens.add(token)
    return tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _coverage(text_tokens: set[str], segment_tokens: set[str]) -> float:
    """Fraction of a segment's tokens present in a (longer) text like a pearl."""
    if not segment_tokens:
        return 0.0
    return len(text_tokens & segment_tokens) / len(segment_tokens)


def _looks_like_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 70:
        return False
    if stripped.startswith(("[", "-", "*", "•", "·", "http", "|")):
        return False
    if stripped.endswith(_SENTENCE_TAIL):
        return False
    return True


def parse_show_segments(show_notes: str) -> list[dict]:
    """Parse the "Show Segments" table of contents into ordered content topics.

    Returns [{"index": int, "title": str, "slug": str, "tokens": set}] for the
    clinical segments only (scaffolding like Intro/Outro/Case is dropped). The
    index is the position within the returned content list.
    """
    lines = [line.strip() for line in (show_notes or "").splitlines()]
    n = len(lines)
    start = None
    for i, line in enumerate(lines):
        if line.lower() == "show segments":
            start = i + 1
            break
    if start is None:
        return []

    titles: list[str] = []
    for i in range(start, n):
        line = lines[i]
        if not line:
            # A single blank line inside the TOC is tolerated; two ends it.
            if i + 1 < n and not lines[i + 1]:
                break
            continue
        if not _looks_like_heading(line):
            break
        # The TOC often runs straight into the "<X> Pearls" / "<X> Notes"
        # section with no blank line; those delimiters end the segment list.
        low = line.lower().rstrip(".: ")
        if low.endswith(("pearls", "notes", "references")):
            break
        titles.append(line)

    segments: list[dict] = []
    for title in titles:
        if _is_scaffolding_segment(title):
            continue
        tokens = _segment_tokens(title)
        if not tokens:
            continue
        segments.append({
            "index": len(segments),
            "title": clean_text(title),
            "slug": normalize_key_text(title),
            "tokens": tokens,
        })
    return segments


def _has_following_prose(lines: list[str], start: int) -> bool:
    """True when the FIRST non-empty line after `start` is prose or a citation.

    Body subheadings are immediately followed by their paragraph; TOC entries
    are followed by the next (short) TOC entry, so this cleanly tells them apart.
    """
    for j in range(start + 1, len(lines)):
        line = lines[j].strip()
        if not line:
            continue
        return len(line) >= _PROSE_MIN_CHARS or "](http" in line
    return False


def parse_body_sections(show_notes: str, segments: list[dict]) -> list[dict]:
    """Locate body subheadings that restate a segment, with their line ranges.

    A line is a body heading when it looks like a heading, is followed by prose
    (which distinguishes it from the consecutive short lines of the TOC), and
    token-matches one of the parsed segments. Returns ordered
    [{"segment_index", "segment_title", "start_line", "end_line"}].
    """
    if not segments:
        return []
    lines = (show_notes or "").splitlines()
    headings: list[dict] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not _looks_like_heading(line):
            continue
        if not _has_following_prose(lines, i):
            continue
        tokens = _segment_tokens(line)
        if not tokens:
            continue
        best = max(segments, key=lambda seg: _jaccard(tokens, seg["tokens"]))
        if _jaccard(tokens, best["tokens"]) < MIN_SEGMENT_MATCH:
            continue
        headings.append({
            "segment_index": best["index"],
            "segment_title": best["title"],
            "start_line": i,
        })

    # Collapse consecutive headings that map to the same segment (keep the first).
    deduped: list[dict] = []
    for heading in headings:
        if deduped and deduped[-1]["segment_index"] == heading["segment_index"]:
            continue
        deduped.append(heading)

    for idx, heading in enumerate(deduped):
        heading["end_line"] = deduped[idx + 1]["start_line"] if idx + 1 < len(deduped) else len(lines)
    return deduped


def locate_citation_in_show_notes(trial: dict, show_notes: str) -> int | None:
    """Line index of a trial's inline citation, by URL match then label match."""
    lines = (show_notes or "").splitlines()
    trial_url = normalize_pubmed_url(trial.get("pubmed_url"))
    label = normalize_key_text(trial.get("citation_label"))

    label_hit = None
    for i, line in enumerate(lines):
        links = extract_markdown_links(line)
        if not links:
            continue
        for link in links:
            if trial_url and normalize_pubmed_url(link.get("url")) == trial_url:
                return i
            if label and label_hit is None and normalize_key_text(link.get("label")) == label:
                label_hit = i
    return label_hit


def _section_for_line(body_sections: list[dict], line_index: int | None) -> dict | None:
    if line_index is None:
        return None
    for section in body_sections:
        if section["start_line"] <= line_index < section["end_line"]:
            return section
    return None


def assign_segment_to_trials(
    trials: list[dict],
    show_notes: str,
    segments: list[dict],
    body_sections: list[dict],
) -> list[dict]:
    """Attach `segment` + `segment_index` to each trial, in place.

    Trials whose citation cannot be located, or which fall outside any known
    body section, keep segment=None rather than being guessed.
    """
    for trial in trials:
        section = _section_for_line(body_sections, locate_citation_in_show_notes(trial, show_notes))
        trial["segment"] = section["segment_title"] if section else None
        trial["segment_index"] = section["segment_index"] if section else None
    return trials


def assign_segment_to_pearls(
    pearls: list[dict],
    episode_trials: list[dict],
    segments: list[dict],
) -> list[dict]:
    """Attach `segment` + `segment_index` to each pearl, in place.

    Priority: (1) the segment of the pearl's top-scored linked trial, (2) token
    overlap with a segment title, (3) positional order only when the pearl and
    segment counts match exactly, else None. We prefer None over a guess.
    """
    segment_by_key: dict[str, dict] = {}
    for trial in episode_trials:
        key = trial.get("canonical_key")
        if key and trial.get("segment") and key not in segment_by_key:
            segment_by_key[key] = trial
    index_by_title = {seg["title"]: seg["index"] for seg in segments}

    positional_ok = bool(segments) and len(pearls) == len(segments)

    for position, pearl in enumerate(pearls):
        segment_title = None
        segment_index = None

        # (1) inherit from the top-scored linked trial
        for citation in pearl.get("supporting_citations", []) or []:
            trial = segment_by_key.get(citation.get("canonical_key"))
            if trial:
                segment_title = trial.get("segment")
                segment_index = trial.get("segment_index")
                break

        # (2) token overlap with a segment title (coverage of the segment's
        # tokens, since a pearl sentence is much longer than a segment label)
        if segment_title is None and segments:
            pearl_tokens = _segment_tokens(pearl.get("pearl", ""))
            best = max(segments, key=lambda seg: _coverage(pearl_tokens, seg["tokens"]))
            if _coverage(pearl_tokens, best["tokens"]) >= MIN_SEGMENT_COVERAGE:
                segment_title = best["title"]
                segment_index = best["index"]

        # (3) positional fallback only when counts line up exactly
        if segment_title is None and positional_ok:
            segment_title = segments[position]["title"]
            segment_index = segments[position]["index"]

        pearl["segment"] = segment_title
        pearl["segment_index"] = segment_index if segment_title else None
        if segment_title and segment_index is None:
            pearl["segment_index"] = index_by_title.get(segment_title)
    return pearls
