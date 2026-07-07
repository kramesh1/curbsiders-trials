"""
Merge human-approved candidate pearls into the pearl population used by the site.

scripts/generate_candidate_pearls.py drafts candidate pearls from full-episode
transcripts and, once a human sets review_status="approved" and runs its
`promote` command, writes them to data/approved_pearls.json. Nothing downstream
reads that file by default -- this script is the missing link that maps an
approved candidate into the same record shape scripts/extract_pearls.py
produces (running it through the same deterministic linking/segment/category
pipeline) and merges it into data/pearls.json, deduped against the existing
show-notes pearls for that episode so a candidate never shadows a real one.

This is deliberately NOT part of ingest.py: a plain ingest.py run regenerates
data/pearls.json from show notes only (via extract_pearls.py) and would
silently drop any merged-in candidates. Run this AFTER extract_pearls.py /
ingest.py, then rebuild the site:

  python scripts/ingest.py
  python scripts/merge_approved_pearls.py
  python scripts/build_site.py

Reads:
  data/approved_pearls.json   human-approved candidate pearls
  data/episodes.json
  data/trials.json
  data/pearls.json            deterministic show-notes pearls (extract_pearls.py)

Writes:
  data/pearls.json            deterministic pearls + mapped approved candidates

Usage:
  python scripts/merge_approved_pearls.py
"""

import sys
from pathlib import Path

try:
    from scripts.extract_trials import load_json, save_json
    from scripts.extract_pearls import (
        DATA_DIR,
        EPISODES_FILE,
        TRIALS_FILE,
        PEARLS_FILE,
        group_trials_by_episode,
        _clinical_topic,
    )
    from scripts.generate_candidate_pearls import APPROVED_FILE
    from scripts.pearl_utils import (
        link_pearls_to_trials,
        trial_canonical_key,
        _dedupe_pearls_within_episode,
        _pearl_dedupe_key,
    )
    from scripts.segment_utils import assign_segment_to_pearls, parse_show_segments
    from scripts.category_utils import derive_episode_category
    from scripts.trial_utils import clean_text
except ImportError:
    from extract_trials import load_json, save_json
    from extract_pearls import (
        DATA_DIR,
        EPISODES_FILE,
        TRIALS_FILE,
        PEARLS_FILE,
        group_trials_by_episode,
        _clinical_topic,
    )
    from generate_candidate_pearls import APPROVED_FILE
    from pearl_utils import (
        link_pearls_to_trials,
        trial_canonical_key,
        _dedupe_pearls_within_episode,
        _pearl_dedupe_key,
    )
    from segment_utils import assign_segment_to_pearls, parse_show_segments
    from category_utils import derive_episode_category
    from trial_utils import clean_text


def group_approved_by_episode(approved: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for candidate in approved:
        url = candidate.get("episode_url")
        if url and (candidate.get("statement") or "").strip():
            grouped.setdefault(url, []).append(candidate)
    return grouped


def build_pearls_from_approved(approved: list[dict], episodes: list[dict], trials: list[dict]) -> list[dict]:
    """Map approved candidate pearls into the extract_pearls.py record shape.

    Runs each episode's candidates through the same deterministic
    linking/segment/category pipeline build_episode_pearls() uses, so a
    candidate-sourced pearl looks and behaves identically to a show-notes one.
    """
    trials_by_episode = group_trials_by_episode(trials)
    episodes_by_url = {e.get("url"): e for e in episodes if e.get("url")}
    approved_by_episode = group_approved_by_episode(approved)

    all_pearls: list[dict] = []
    for url, candidates in approved_by_episode.items():
        episode = episodes_by_url.get(url)
        if not episode:
            continue

        pearls = [
            {"topic": clean_text(c.get("topic")) or None, "pearl": c["statement"].strip()}
            for c in candidates
        ]
        pearls = _dedupe_pearls_within_episode(pearls)
        if not pearls:
            continue

        show_notes = episode.get("show_notes", "")
        episode_trials = trials_by_episode.get(url, [])
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
            pearl["pearl_source"] = "candidate_generation"
        all_pearls.extend(pearls)

    return all_pearls


def merge_pearls(deterministic_pearls: list[dict], candidate_pearls: list[dict]) -> tuple[list[dict], int]:
    """Combine deterministic + candidate pearls, deduped by (episode_url, text).

    A candidate never shadows an existing show-notes pearl for the same
    episode; ties go to the deterministic record.
    """
    for pearl in deterministic_pearls:
        pearl.setdefault("pearl_source", "show_notes")

    seen = {
        (p.get("episode_url"), _pearl_dedupe_key(p.get("pearl", "")))
        for p in deterministic_pearls
    }
    merged = list(deterministic_pearls)
    skipped = 0
    for pearl in candidate_pearls:
        key = (pearl.get("episode_url"), _pearl_dedupe_key(pearl.get("pearl", "")))
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        merged.append(pearl)
    return merged, skipped


def main() -> int:
    approved = load_json(APPROVED_FILE, [])
    if not approved:
        print(f"No approved candidates in {APPROVED_FILE}. Nothing to merge.")
        print("Run: python scripts/generate_candidate_pearls.py promote")
        return 0

    episodes = load_json(EPISODES_FILE, [])
    trials = load_json(TRIALS_FILE, [])
    deterministic_pearls = load_json(PEARLS_FILE, [])

    candidate_pearls = build_pearls_from_approved(approved, episodes, trials)
    merged, skipped = merge_pearls(deterministic_pearls, candidate_pearls)
    save_json(PEARLS_FILE, merged)

    added = len(candidate_pearls) - skipped
    print(f"Merged {added} approved candidate pearl(s) into {PEARLS_FILE}.")
    if skipped:
        print(f"Skipped {skipped} candidate(s) that duplicated an existing show-notes pearl in the same episode.")
    print(f"Total pearls now: {len(merged)} across "
          f"{len({p['episode_url'] for p in merged if p.get('episode_url')})} episodes.")
    print("\nNote: a plain `ingest.py`/`extract_pearls.py` re-run regenerates pearls.json from "
          "show notes only and will drop this merge. Re-run this script after each such rebuild, "
          "then `python scripts/build_site.py`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
