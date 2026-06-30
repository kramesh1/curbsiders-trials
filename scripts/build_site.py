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
except ImportError:
    from trial_utils import build_canonical_trial_records

DATA_DIR = Path(__file__).parent.parent / "data"
DOCS_DATA_DIR = Path(__file__).parent.parent / "docs" / "data"
TRIALS_FILE = DATA_DIR / "trials.json"
OUTPUT_FILE = DOCS_DATA_DIR / "trials.json"


def main():
    if not TRIALS_FILE.exists():
        print(f"Error: {TRIALS_FILE} not found. Run extract_trials.py first.")
        return

    with open(TRIALS_FILE) as f:
        trials = json.load(f)
    print(f"Loaded {len(trials)} trial mentions")

    canonical = build_canonical_trial_records(trials)
    print(f"After canonicalization: {len(canonical)} unique trial records")

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


if __name__ == "__main__":
    main()
