"""
Validate the local Curbsiders trial repository artifacts.

Usage:
  python scripts/validate_repository.py
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

try:
    from scripts.trial_utils import VALID_SPECIALTY_TAGS
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from trial_utils import VALID_SPECIALTY_TAGS

NCT_RE = re.compile(r"^NCT\d{8}$")


def _bad_categories(records: list[dict]) -> list[str]:
    """Category-vocabulary entries that fall outside the specialty vocabulary."""
    bad = []
    for record in records:
        for category in record.get("episode_categories", []) or []:
            if category not in VALID_SPECIALTY_TAGS:
                bad.append(category)
    return bad


def _bad_segments(records: list[dict]) -> list[str]:
    """Segment entries that are empty or not strings."""
    return [
        repr(segment)
        for record in records
        for segment in record.get("segments", []) or []
        if not isinstance(segment, str) or not segment.strip()
    ]


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
EPISODES_FILE = DATA_DIR / "episodes.json"
TRIALS_FILE = DATA_DIR / "trials.json"
STATE_FILE = DATA_DIR / "extraction_state.json"
PEARLS_FILE = DATA_DIR / "pearls.json"
SHOW_NOTE_EVIDENCE_FILE = DATA_DIR / "show_note_evidence.json"
SITE_TRIALS_FILE = DOCS_DIR / "data" / "trials.json"
SITE_PEARLS_FILE = DOCS_DIR / "data" / "pearls.json"
INDEX_FILE = DOCS_DIR / "index.html"


def load_json(path: Path):
    try:
        with path.open() as f:
            return json.load(f)
    except json.JSONDecodeError as error:
        print(f"ERROR: {path.relative_to(ROOT)} is not valid JSON ({error}).")
        sys.exit(1)
    except OSError as error:
        print(f"ERROR: could not read {path.relative_to(ROOT)} ({error}).")
        sys.exit(1)


VAGUE_LABEL_RE = re.compile(
    r"^(a |the )?\d{0,4}\s*(recent |retrospective |prospective |observational |cohort |randomized )*"
    r"(study|trial|analysis|review|guidance|guideline|report)s?\.?$",
    re.IGNORECASE,
)


def _is_vague_citation_label(label: str | None) -> bool:
    """A citation_label that fails CURATION_GUIDE's "recognizable to a clinician" bar.

    Catches bare footnote numbers left unresolved by extraction (e.g. "12", "4,5,6")
    and generic phrases with no name/trial acronym to actually identify the source.
    """
    label = (label or "").strip()
    if not label:
        return False  # missing label is a separate, softer condition (see bad_identity)
    if re.fullmatch(r"[\d,\s]+", label):
        return True
    return bool(VAGUE_LABEL_RE.match(label))


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def main() -> int:
    errors: list[str] = []
    for path in [EPISODES_FILE, TRIALS_FILE, STATE_FILE, SHOW_NOTE_EVIDENCE_FILE, SITE_TRIALS_FILE, INDEX_FILE]:
        require(path.exists(), f"Missing required file: {path.relative_to(ROOT)}", errors)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    episodes = load_json(EPISODES_FILE)
    trial_mentions = load_json(TRIALS_FILE)
    state = load_json(STATE_FILE)
    canonical = load_json(SITE_TRIALS_FILE)

    episode_urls = {episode.get("url") for episode in episodes if episode.get("url")}
    canonical_ids = [trial.get("id") for trial in canonical]
    records_without_identity = [
        trial
        for trial in canonical
        if not any([trial.get("citation_label"), trial.get("paper_title"), trial.get("pubmed_url")])
    ]
    records_without_episodes = [trial for trial in canonical if not trial.get("episodes")]
    records_with_bad_backlinks = [
        trial
        for trial in canonical
        for episode in trial.get("episodes", [])
        if episode.get("episode_url") and episode.get("episode_url") not in episode_urls
    ]
    state_counts = Counter(info.get("status") for info in state.values())
    zero_trial_episodes = [
        url
        for url, info in state.items()
        if info.get("status") == "completed" and info.get("deduped_mentions") == 0
    ]
    study_type_counts = Counter(trial.get("study_type", "other") for trial in canonical)
    linked_records = sum(1 for trial in canonical if trial.get("pubmed_url"))

    require(len(canonical_ids) == len(set(canonical_ids)), "Canonical record IDs are not unique.", errors)
    require(not records_without_identity, "Some canonical records have no label, title, or URL.", errors)
    require(not records_without_episodes, "Some canonical records have no episode backlinks.", errors)
    require(not records_with_bad_backlinks, "Some canonical records link to unknown episode URLs.", errors)
    require(state_counts.get("failed", 0) == 0, "Extraction state contains failed episodes.", errors)
    require(
        state_counts.get("completed", 0) == len(episodes),
        "Completed extraction count does not match scraped episode count.",
        errors,
    )

    # Pearls are an optional layer; validate them only once they exist.
    canonical_pearls = load_json(SITE_PEARLS_FILE) if SITE_PEARLS_FILE.exists() else []
    show_note_evidence = load_json(SHOW_NOTE_EVIDENCE_FILE)
    trial_keys = {trial.get("canonical_key") for trial in canonical}
    pearls_without_text = [pearl for pearl in canonical_pearls if not str(pearl.get("pearl", "")).strip()]
    pearls_without_episodes = [pearl for pearl in canonical_pearls if not pearl.get("episodes")]
    pearls_with_bad_backlinks = [
        pearl
        for pearl in canonical_pearls
        for episode in pearl.get("episodes", [])
        if episode.get("episode_url") and episode.get("episode_url") not in episode_urls
    ]
    dangling_citation_links = [
        citation.get("canonical_key")
        for pearl in canonical_pearls
        for citation in pearl.get("supporting_citations", [])
        if citation.get("canonical_key") and citation.get("canonical_key") not in trial_keys
    ]
    dangling_reviewed_links = [
        link.get("canonical_key")
        for pearl in canonical_pearls
        for link in pearl.get("evidence_links", [])
        if link.get("canonical_key") and link.get("canonical_key") not in trial_keys
    ]
    show_note_keys = [record.get("evidence_key") for record in show_note_evidence]
    show_note_missing_url = [record.get("evidence_key") for record in show_note_evidence if not record.get("url")]
    show_note_bad_backlinks = [
        record.get("evidence_key")
        for record in show_note_evidence
        for episode in record.get("episodes", [])
        if episode.get("episode_url") and episode.get("episode_url") not in episode_urls
    ]
    show_note_bad_matches = [
        record.get("canonical_key")
        for record in show_note_evidence
        if record.get("canonical_key") and record.get("canonical_key") not in trial_keys
    ]
    trial_bad_pearl_backlinks = [
        trial.get("canonical_key")
        for trial in canonical
        for pearl in trial.get("linked_pearls", [])
        if not str(pearl.get("pearl", "")).strip()
    ]
    pearls_with_heuristic_citation = sum(1 for pearl in canonical_pearls if pearl.get("supporting_citations"))
    pearls_with_reviewed_evidence = sum(1 for pearl in canonical_pearls if pearl.get("evidence_links"))
    evidence_with_linked_pearls = sum(1 for trial in canonical if trial.get("linked_pearls"))
    evidence_from_show_notes = sum(1 for trial in canonical if "show_notes_links" in (trial.get("source_layers") or []))

    require(not pearls_without_text, "Some canonical pearls have empty text.", errors)
    require(not pearls_without_episodes, "Some canonical pearls have no episode backlinks.", errors)
    require(not pearls_with_bad_backlinks, "Some canonical pearls link to unknown episode URLs.", errors)
    require(not dangling_citation_links, "Some pearl citations reference unknown canonical trial keys.", errors)
    require(not dangling_reviewed_links, "Some reviewed pearl evidence links reference unknown canonical trial keys.", errors)
    require(len(show_note_keys) == len(set(show_note_keys)), "Show-note evidence keys are not unique.", errors)
    require(not show_note_missing_url, "Some show-note evidence records have no URL.", errors)
    require(not show_note_bad_backlinks, "Some show-note evidence records link to unknown episode URLs.", errors)
    require(not show_note_bad_matches, "Some show-note evidence records match unknown canonical trial keys.", errors)
    require(not trial_bad_pearl_backlinks, "Some evidence records have malformed linked pearl backlinks.", errors)

    # Classification / detail fields are soft: absence is legitimate (~28% of
    # episodes lack the structure). Only malformed *present* values fail the gate.
    bad_nct = [t.get("nct_id") for t in canonical if t.get("nct_id") and not NCT_RE.match(str(t.get("nct_id")))]
    bad_sample = [
        t.get("sample_size")
        for t in canonical
        if t.get("sample_size") is not None and not (isinstance(t.get("sample_size"), int) and t.get("sample_size") > 0)
    ]
    bad_categories = _bad_categories(canonical) + _bad_categories(canonical_pearls)
    bad_segments = _bad_segments(canonical) + _bad_segments(canonical_pearls)
    trials_with_segment = sum(1 for t in canonical if t.get("segments"))
    pearls_with_segment = sum(1 for p in canonical_pearls if p.get("segments"))
    vague_labels = [
        t.get("citation_label")
        for t in canonical
        if _is_vague_citation_label(t.get("citation_label"))
    ]

    require(not bad_nct, "Some canonical trials have a malformed nct_id (expected NCT########).", errors)
    require(not bad_sample, "Some canonical trials have a non-positive-integer sample_size.", errors)
    require(not bad_categories, "Some records have a category outside the specialty vocabulary.", errors)
    require(not bad_segments, "Some records have an empty or non-string segment.", errors)
    if vague_labels:
        print(f"\nWARNING: {len(vague_labels)} canonical trial(s) have a bare-number or generic "
              f"citation_label not recognizable to a clinician: {vague_labels}")

    print("Repository validation")
    print(f"  Episodes scraped:        {len(episodes)}")
    print(f"  Episode states:          {dict(state_counts)}")
    print(f"  Trial mentions:          {len(trial_mentions)}")
    print(f"  Canonical records:       {len(canonical)}")
    print(f"  Records with links:      {linked_records}")
    print(f"  Records from show-note links: {evidence_from_show_notes}")
    print(f"  Zero-trial episodes:     {len(zero_trial_episodes)}")
    print(f"  Study types:             {dict(study_type_counts.most_common())}")
    print(f"  Show-note evidence records: {len(show_note_evidence)}")
    print(f"  Canonical pearls:        {len(canonical_pearls)}")
    print(f"  Pearls with heuristic citations: {pearls_with_heuristic_citation}")
    print(f"  Pearls with reviewed evidence:   {pearls_with_reviewed_evidence}")
    print(f"  Evidence records with linked pearls: {evidence_with_linked_pearls}")
    print(f"  Trials with a segment:   {trials_with_segment}")
    print(f"  Pearls with a segment:   {pearls_with_segment}")

    if errors:
        print("\nValidation failed:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("\nValidation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
