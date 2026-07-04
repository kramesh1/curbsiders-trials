"""
Reveal which episodes have NO extracted pearls yet.

The deterministic pearl layer (scripts/extract_pearls.py) only emits pearls for
episodes whose show notes contain a recognizable "<Topic> Pearls" heading, and it
silently skips the rest. So an episode absent from data/pearls.json has no pearls —
but that fact is invisible today. This read-only report surfaces the gap so those
episodes can be targeted for programmatic pearl generation (the owner-gated
scripts/generate_candidate_pearls.py path, which needs a transcript).

Each gap episode is annotated with whether we have a transcript for it (and its
source), because that is exactly what decides whether it's feedable to the
candidate-pearl generator.

Reads:  data/episodes.json, data/pearls.json, data/transcripts.json
Writes: data/pearls_coverage_gap.json  (newest episode first)

Usage:
  python scripts/pearl_coverage.py            # print summary + write the gap list
  python scripts/pearl_coverage.py --quiet    # write the file, print only the summary line
"""

import argparse
import sys
from pathlib import Path

try:
    from scripts.extract_trials import DATA_DIR, load_json, save_json
    from scripts.extract_pearls import EPISODES_FILE, PEARLS_FILE
    from scripts.fetch_transcripts import TRANSCRIPTS_FILE
except ImportError:
    from extract_trials import DATA_DIR, load_json, save_json
    from extract_pearls import EPISODES_FILE, PEARLS_FILE
    from fetch_transcripts import TRANSCRIPTS_FILE

GAP_FILE = DATA_DIR / "pearls_coverage_gap.json"


def transcript_source_by_episode(transcripts: list[dict]) -> dict[str, str]:
    """Map episode_url -> transcript source, preferring an official transcript."""
    best: dict[str, str] = {}
    for t in transcripts:
        url = t.get("episode_url")
        if not url or not t.get("text"):
            continue
        source = t.get("source") or "unknown"
        # Prefer the higher-fidelity official transcript if an episode has several.
        if url not in best or source == "official":
            best[url] = source
    return best


def compute_pearl_gap(episodes: list[dict], pearls: list[dict], transcripts: list[dict]) -> list[dict]:
    """Episodes with zero extracted pearls, annotated with transcript availability.

    Join key is episode_url (episodes.json `url` == pearls.json `episode_url`).
    Sorted newest episode first.
    """
    have_pearls = {p.get("episode_url") for p in pearls if p.get("episode_url")}
    sources = transcript_source_by_episode(transcripts)
    gap = []
    for e in episodes:
        url = e.get("url")
        if not url or url in have_pearls:
            continue
        source = sources.get(url)
        gap.append({
            "episode_number": e.get("episode_number"),
            "episode_title": e.get("title", ""),
            "episode_url": url,
            "has_transcript": source is not None,
            "transcript_source": source,
        })
    gap.sort(key=lambda r: -(r.get("episode_number") or 0))
    return gap


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="Print only the summary line")
    args = parser.parse_args()

    episodes = load_json(EPISODES_FILE, [])
    pearls = load_json(PEARLS_FILE, [])
    transcripts = load_json(TRANSCRIPTS_FILE, [])
    if not episodes:
        print(f"No episodes in {EPISODES_FILE}. Run scrape_episodes.py first.")
        return 1

    gap = compute_pearl_gap(episodes, pearls, transcripts)
    save_json(GAP_FILE, gap)

    total = len(episodes)
    with_pearls = total - len(gap)
    feedable = sum(1 for g in gap if g["has_transcript"])
    print(f"Pearl coverage: {with_pearls}/{total} episodes have pearls; "
          f"{len(gap)} have none ({feedable} of those have a transcript). -> {GAP_FILE.name}")

    if not args.quiet and gap:
        print("\nEpisodes with no pearls yet (newest first, transcript-backed ones are "
              "feedable to generate_candidate_pearls.py):")
        for g in gap[:25]:
            mark = g["transcript_source"] if g["has_transcript"] else "no transcript"
            print(f"  #{g['episode_number']:<4} [{mark:>12}] {g['episode_title'][:70]}")
        if len(gap) > 25:
            print(f"  ... and {len(gap) - 25} more (full list in {GAP_FILE.name}).")
        first = next((g for g in gap if g["has_transcript"]), None)
        if first:
            print(f"\nNext: python scripts/generate_candidate_pearls.py generate --episode {first['episode_number']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
