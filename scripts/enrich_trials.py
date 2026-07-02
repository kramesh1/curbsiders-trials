"""
Step 2b: Deterministically enrich trial mentions in place.

Runs after model extraction and before pearls. For each episode it:
  - parses the "Show Segments" structure and assigns each citation a segment,
  - recovers nct_id / sample_size / journal from the text around each citation,
  - derives the episode's category from its trials' specialty tags + title,
and writes the enriched mentions back to data/trials.json.

Deterministic and idempotent -- no model calls -- so it is safe to re-run on
every ingest and improves coverage whenever show notes or trials change.

Usage:
  python scripts/enrich_trials.py
"""

import json
from collections import Counter
from pathlib import Path

try:
    from scripts.category_utils import derive_episode_category
    from scripts.segment_utils import (
        assign_segment_to_trials,
        parse_body_sections,
        parse_show_segments,
    )
    from scripts.trial_detail_utils import enrich_trials_with_details
except ImportError:
    from category_utils import derive_episode_category
    from segment_utils import (
        assign_segment_to_trials,
        parse_body_sections,
        parse_show_segments,
    )
    from trial_detail_utils import enrich_trials_with_details

DATA_DIR = Path(__file__).parent.parent / "data"
EPISODES_FILE = DATA_DIR / "episodes.json"
TRIALS_FILE = DATA_DIR / "trials.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def group_trials_by_episode(trials: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for trial in trials:
        url = trial.get("episode_url")
        if url:
            grouped.setdefault(url, []).append(trial)
    return grouped


def enrich_all(episodes: list[dict], trials: list[dict]) -> list[dict]:
    """Enrich every trial mention with segment + detail + episode category."""
    show_notes_by_url = {e.get("url"): e.get("show_notes", "") for e in episodes}
    episode_by_url = {e.get("url"): e for e in episodes}
    trials_by_episode = group_trials_by_episode(trials)

    for url, episode_trials in trials_by_episode.items():
        show_notes = show_notes_by_url.get(url, "")
        segments = parse_show_segments(show_notes)
        body_sections = parse_body_sections(show_notes, segments)

        assign_segment_to_trials(episode_trials, show_notes, segments, body_sections)
        enrich_trials_with_details(episode_trials, show_notes)

        category = derive_episode_category(episode_by_url.get(url, {}), episode_trials)
        for trial in episode_trials:
            trial["episode_category"] = category["category"]
            trial["secondary_categories"] = category["secondary_categories"]
    return trials


def main():
    if not TRIALS_FILE.exists():
        print(f"Error: {TRIALS_FILE} not found. Run extract_trials.py first.")
        return

    episodes = load_json(EPISODES_FILE, [])
    trials = load_json(TRIALS_FILE, [])
    print(f"Loaded {len(episodes)} episodes and {len(trials)} trial mentions")

    enrich_all(episodes, trials)

    with open(TRIALS_FILE, "w") as f:
        json.dump(trials, f, indent=2, ensure_ascii=False)

    with_segment = sum(1 for t in trials if t.get("segment"))
    with_nct = sum(1 for t in trials if t.get("nct_id"))
    with_n = sum(1 for t in trials if t.get("sample_size"))
    with_journal = sum(1 for t in trials if t.get("journal"))
    with_category = sum(1 for t in trials if t.get("episode_category"))
    category_counts = Counter(t.get("episode_category") for t in trials if t.get("episode_category"))

    print(f"\nEnriched mentions written to {TRIALS_FILE}")
    print(f"  With a segment:          {with_segment}")
    print(f"  With an NCT id:          {with_nct}")
    print(f"  With a sample size:      {with_n}")
    print(f"  With a journal:          {with_journal}")
    print(f"  With an episode category:{with_category}")
    print(f"  Top categories:          {category_counts.most_common(8)}")


if __name__ == "__main__":
    main()
