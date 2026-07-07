"""
Step 3a: Extract teaching "Pearls" from scraped show notes and link them to the
clinical-evidence mentions already extracted for the same episode.

This is deterministic (no model calls): pearls are pulled verbatim from the
show-note "<Topic> Pearls" sections, and linked to trials by term overlap. It
is therefore cheap and safe to re-run on every ingest.

Reads:
  data/episodes.json   scraped show notes
  data/trials.json     episode-level trial mentions

Writes:
  data/pearls.json     episode-level pearls with supporting citations

Usage:
  python scripts/extract_pearls.py
"""

import json
from collections import Counter
from pathlib import Path

try:
    from scripts.pearl_utils import (
        link_pearls_to_trials,
        parse_pearls_from_show_notes,
        trial_canonical_key,
    )
    from scripts.segment_utils import assign_segment_to_pearls, parse_show_segments
    from scripts.category_utils import derive_episode_category
    from scripts.trial_utils import clean_text
except ImportError:
    from pearl_utils import (
        link_pearls_to_trials,
        parse_pearls_from_show_notes,
        trial_canonical_key,
    )
    from segment_utils import assign_segment_to_pearls, parse_show_segments
    from category_utils import derive_episode_category
    from trial_utils import clean_text

DATA_DIR = Path(__file__).parent.parent / "data"
EPISODES_FILE = DATA_DIR / "episodes.json"
TRIALS_FILE = DATA_DIR / "trials.json"
PEARLS_FILE = DATA_DIR / "pearls.json"


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


def _clinical_topic(pearl: dict, context_by_key: dict, category: str | None) -> str | None:
    """Best available specific label for a pearl."""
    if pearl.get("segment"):
        return pearl["segment"]
    if clean_text(pearl.get("topic")):
        return clean_text(pearl["topic"])
    for citation in pearl.get("supporting_citations", []) or []:
        context = context_by_key.get(citation.get("canonical_key"))
        if context:
            return context
    return category


def build_episode_pearls(episodes: list[dict], trials: list[dict]) -> list[dict]:
    trials_by_episode = group_trials_by_episode(trials)
    all_pearls: list[dict] = []

    for episode in episodes:
        url = episode.get("url", "")
        show_notes = episode.get("show_notes", "")
        pearls = parse_pearls_from_show_notes(show_notes)
        if not pearls:
            continue

        episode_trials = trials_by_episode.get(url, [])
        # Stamp the canonical key so pearls can inherit an already-enriched
        # trial's segment (and be looked up for their clinical topic).
        for trial in episode_trials:
            trial["canonical_key"] = trial_canonical_key(trial)
        context_by_key = {
            trial["canonical_key"]: clean_text(trial.get("context_topic"))
            for trial in episode_trials
            if trial.get("canonical_key")
        }

        link_pearls_to_trials(pearls, episode_trials)
        segments = parse_show_segments(show_notes)
        assign_segment_to_pearls(pearls, episode_trials, segments)
        category = derive_episode_category(episode, episode_trials)

        for pearl in pearls:
            pearl["episode_category"] = category["category"]
            pearl["secondary_categories"] = category["secondary_categories"]
            pearl["clinical_topic"] = _clinical_topic(pearl, context_by_key, category["category"])
            pearl["episode_number"] = episode.get("episode_number")
            pearl["episode_title"] = episode.get("title", "")
            pearl["episode_url"] = url
            pearl["episode_date"] = episode.get("date", "")
            pearl["pearl_source"] = "show_notes"
        all_pearls.extend(pearls)

    return all_pearls


def main():
    if not EPISODES_FILE.exists():
        print(f"Error: {EPISODES_FILE} not found. Run scrape_episodes.py first.")
        return

    episodes = load_json(EPISODES_FILE, [])
    trials = load_json(TRIALS_FILE, [])
    print(f"Loaded {len(episodes)} episodes and {len(trials)} trial mentions")

    pearls = build_episode_pearls(episodes, trials)

    with open(PEARLS_FILE, "w") as f:
        json.dump(pearls, f, indent=2, ensure_ascii=False)

    episodes_with_pearls = len({p["episode_url"] for p in pearls if p.get("episode_url")})
    linked = sum(1 for p in pearls if p.get("supporting_citations"))
    with_segment = sum(1 for p in pearls if p.get("segment"))
    with_clinical_topic = sum(1 for p in pearls if p.get("clinical_topic"))
    with_category = sum(1 for p in pearls if p.get("episode_category"))
    topic_counts = Counter(p.get("topic") for p in pearls if p.get("topic"))

    print(f"\nPearls written to {PEARLS_FILE}")
    print(f"  Pearl statements:        {len(pearls)}")
    print(f"  Episodes with pearls:    {episodes_with_pearls}")
    print(f"  Pearls with a citation:  {linked}")
    print(f"  Pearls with a segment:   {with_segment}")
    print(f"  Pearls with clinical_topic:{with_clinical_topic}")
    print(f"  Pearls with a category:  {with_category}")
    print(f"  Top pearl topics:        {topic_counts.most_common(8)}")


if __name__ == "__main__":
    main()
