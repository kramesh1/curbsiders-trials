"""
Validate the local Curbsiders trial repository artifacts.

Usage:
  python scripts/validate_repository.py
"""

import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
EPISODES_FILE = DATA_DIR / "episodes.json"
TRIALS_FILE = DATA_DIR / "trials.json"
STATE_FILE = DATA_DIR / "extraction_state.json"
SITE_TRIALS_FILE = DOCS_DIR / "data" / "trials.json"
INDEX_FILE = DOCS_DIR / "index.html"


def load_json(path: Path):
    with path.open() as f:
        return json.load(f)


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def main() -> int:
    errors: list[str] = []
    for path in [EPISODES_FILE, TRIALS_FILE, STATE_FILE, SITE_TRIALS_FILE, INDEX_FILE]:
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

    print("Repository validation")
    print(f"  Episodes scraped:        {len(episodes)}")
    print(f"  Episode states:          {dict(state_counts)}")
    print(f"  Trial mentions:          {len(trial_mentions)}")
    print(f"  Canonical records:       {len(canonical)}")
    print(f"  Records with links:      {linked_records}")
    print(f"  Zero-trial episodes:     {len(zero_trial_episodes)}")
    print(f"  Study types:             {dict(study_type_counts.most_common())}")

    if errors:
        print("\nValidation failed:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("\nValidation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
