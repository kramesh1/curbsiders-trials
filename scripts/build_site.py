"""
Step 3: Prepare data/trials.json for the GitHub Pages site.
Canonicalizes across episodes and copies to docs/data/trials.json.

Usage: python scripts/build_site.py
"""

import json
from collections import Counter
from pathlib import Path

try:
    from scripts.trial_utils import build_canonical_trial_records
    from scripts.pearl_utils import attach_evidence_links, attach_feedback, build_canonical_pearls
    from scripts.pubmed_utils import attach_screening
    from scripts.show_note_evidence import (
        annotate_show_note_matches,
        attach_pearl_backlinks,
        build_show_note_evidence_records,
        merge_show_note_evidence,
        repair_pearl_evidence_links,
    )
except ImportError:
    from trial_utils import build_canonical_trial_records
    from pearl_utils import attach_evidence_links, attach_feedback, build_canonical_pearls
    from pubmed_utils import attach_screening
    from show_note_evidence import (
        annotate_show_note_matches,
        attach_pearl_backlinks,
        build_show_note_evidence_records,
        merge_show_note_evidence,
        repair_pearl_evidence_links,
    )

DATA_DIR = Path(__file__).parent.parent / "data"
DOCS_DATA_DIR = Path(__file__).parent.parent / "docs" / "data"
TRIALS_FILE = DATA_DIR / "trials.json"
EPISODES_FILE = DATA_DIR / "episodes.json"
SHOW_NOTE_EVIDENCE_FILE = DATA_DIR / "show_note_evidence.json"
PEARLS_FILE = DATA_DIR / "pearls.json"
LINKED_PEARLS_FILE = DATA_DIR / "pearls_linked.json"
SCREENING_APPROVED_FILE = DATA_DIR / "trial_screening_approved.json"
FEEDBACK_APPROVED_FILE = DATA_DIR / "pearl_feedback_approved.json"
OUTPUT_FILE = DOCS_DATA_DIR / "trials.json"
PEARLS_OUTPUT_FILE = DOCS_DATA_DIR / "pearls.json"


def main():
    if not TRIALS_FILE.exists():
        print(f"Error: {TRIALS_FILE} not found. Run extract_trials.py first.")
        return

    with open(TRIALS_FILE) as f:
        trials = json.load(f)
    print(f"Loaded {len(trials)} trial mentions")

    canonical = build_canonical_trial_records(trials)
    print(f"After canonicalization: {len(canonical)} unique trial records")

    screened_count = 0
    if SCREENING_APPROVED_FILE.exists():
        with open(SCREENING_APPROVED_FILE) as f:
            screening_records = json.load(f)
        canonical = attach_screening(canonical, screening_records)
        screened_count = sum(1 for trial in canonical if trial.get("grounded_in"))

    show_note_stats = None
    if EPISODES_FILE.exists():
        with open(EPISODES_FILE) as f:
            episodes = json.load(f)
        show_note_records = build_show_note_evidence_records(episodes)
        annotated_show_note_records = annotate_show_note_matches(show_note_records, canonical)
        with open(SHOW_NOTE_EVIDENCE_FILE, "w") as f:
            json.dump(annotated_show_note_records, f, ensure_ascii=False, separators=(",", ":"))
        canonical, show_note_stats = merge_show_note_evidence(canonical, annotated_show_note_records)

    canonical_pearls, flagged_count = build_pearls_site(write_output=False)
    repaired_pearl_links = 0
    if canonical_pearls:
        canonical_pearls, repaired_pearl_links = repair_pearl_evidence_links(canonical_pearls, canonical)
        canonical = attach_pearl_backlinks(canonical, canonical_pearls)

    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(canonical, f, ensure_ascii=False, separators=(",", ":"))

    write_pearls_site(canonical_pearls)

    # Print stats
    episodes = {
        episode["episode_url"]
        for trial in canonical
        for episode in trial.get("episodes", [])
        if episode.get("episode_url")
    }
    mention_count = sum(trial.get("mention_count", 0) for trial in canonical)
    specialty_counts = Counter(
        tag for trial in canonical for tag in trial.get("specialty_tags", [])
    )
    study_type_counts = Counter(trial.get("study_type", "other") for trial in canonical)

    linked_pearl_records = sum(1 for trial in canonical if trial.get("linked_pearls"))
    print(f"\nSite data written to {OUTPUT_FILE}")
    print(f"  Unique evidence records: {len(canonical)}")
    print(f"  Evidence mentions:       {mention_count}")
    print(f"  Episodes covered:        {len(episodes)}")
    print(f"  Top specialties: {specialty_counts.most_common(8)}")
    print(f"  Study types:     {dict(study_type_counts.most_common())}")
    print(f"  Trials with research screening: {screened_count}")
    print(f"  Evidence records with linked pearls: {linked_pearl_records}")
    print(f"  Repaired stale pearl evidence links: {repaired_pearl_links}")
    if show_note_stats:
        print(f"  Show-note evidence layer: {show_note_stats}")


def build_pearls_site(write_output=True):
    """Canonicalize data/pearls.json into docs/data/pearls.json for the site."""
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PEARLS_FILE.exists():
        if write_output:
            # Keep the site loadable even before the first pearls extraction.
            with open(PEARLS_OUTPUT_FILE, "w") as f:
                json.dump([], f)
            print(f"\nNo {PEARLS_FILE} yet; wrote empty {PEARLS_OUTPUT_FILE}.")
        return [], 0

    with open(PEARLS_FILE) as f:
        pearls = json.load(f)

    if LINKED_PEARLS_FILE.exists():
        with open(LINKED_PEARLS_FILE) as f:
            linked_records = json.load(f)
        pearls = attach_evidence_links(pearls, linked_records)

    canonical_pearls = build_canonical_pearls(pearls)

    flagged_count = 0
    if FEEDBACK_APPROVED_FILE.exists():
        with open(FEEDBACK_APPROVED_FILE) as f:
            approved_feedback = json.load(f)
        canonical_pearls = attach_feedback(canonical_pearls, approved_feedback)
        flagged_count = sum(1 for pearl in canonical_pearls if pearl.get("flag_summary"))

    if write_output:
        write_pearls_site(canonical_pearls)

    with_heuristic_citation = sum(1 for pearl in canonical_pearls if pearl.get("supporting_citations"))
    with_reviewed_evidence = sum(1 for pearl in canonical_pearls if pearl.get("evidence_links"))
    action = "written to" if write_output else "prepared for"
    print(f"\nPearl site data {action} {PEARLS_OUTPUT_FILE}")
    print(f"  Pearl statements (raw):     {len(pearls)}")
    print(f"  Canonical pearls:           {len(canonical_pearls)}")
    print(f"  Pearls with heuristic citations: {with_heuristic_citation}")
    print(f"  Pearls with reviewed evidence:   {with_reviewed_evidence}")
    print(f"  Pearls with visitor flags:  {flagged_count}")
    return canonical_pearls, flagged_count


def write_pearls_site(canonical_pearls):
    with open(PEARLS_OUTPUT_FILE, "w") as f:
        json.dump(canonical_pearls, f, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
