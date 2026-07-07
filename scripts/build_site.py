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
except ImportError:
    from trial_utils import build_canonical_trial_records
    from pearl_utils import attach_evidence_links, attach_feedback, build_canonical_pearls
    from pubmed_utils import attach_screening

DATA_DIR = Path(__file__).parent.parent / "data"
DOCS_DATA_DIR = Path(__file__).parent.parent / "docs" / "data"
TRIALS_FILE = DATA_DIR / "trials.json"
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

    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(canonical, f, ensure_ascii=False, separators=(",", ":"))

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

    print(f"\nSite data written to {OUTPUT_FILE}")
    print(f"  Unique trials:    {len(canonical)}")
    print(f"  Trial mentions:   {mention_count}")
    print(f"  Episodes covered: {len(episodes)}")
    print(f"  Top specialties: {specialty_counts.most_common(8)}")
    print(f"  Study types:     {dict(study_type_counts.most_common())}")
    print(f"  Trials with research screening: {screened_count}")

    build_pearls_site()


def build_pearls_site():
    """Canonicalize data/pearls.json into docs/data/pearls.json for the site."""
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PEARLS_FILE.exists():
        # Keep the site loadable even before the first pearls extraction.
        with open(PEARLS_OUTPUT_FILE, "w") as f:
            json.dump([], f)
        print(f"\nNo {PEARLS_FILE} yet; wrote empty {PEARLS_OUTPUT_FILE}.")
        return

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

    with open(PEARLS_OUTPUT_FILE, "w") as f:
        json.dump(canonical_pearls, f, ensure_ascii=False, separators=(",", ":"))

    with_citation = sum(1 for pearl in canonical_pearls if pearl.get("supporting_citations"))
    with_model_evidence = sum(1 for pearl in canonical_pearls if pearl.get("evidence_links"))
    print(f"\nPearl site data written to {PEARLS_OUTPUT_FILE}")
    print(f"  Pearl statements (raw):     {len(pearls)}")
    print(f"  Canonical pearls:           {len(canonical_pearls)}")
    print(f"  Pearls with a citation:     {with_citation}")
    print(f"  Pearls with model evidence: {with_model_evidence}")
    print(f"  Pearls with visitor flags:  {flagged_count}")


if __name__ == "__main__":
    main()
