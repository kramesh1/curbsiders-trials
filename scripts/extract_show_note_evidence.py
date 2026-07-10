"""
Build data/show_note_evidence.json from actual Curbsiders show-note hyperlinks.

Usage:
  python scripts/extract_show_note_evidence.py
"""

import argparse
import json
from pathlib import Path

try:
    from scripts.show_note_evidence import (
        annotate_show_note_matches,
        build_show_note_evidence_records,
    )
    from scripts.trial_utils import build_canonical_trial_records
except ImportError:
    from show_note_evidence import (
        annotate_show_note_matches,
        build_show_note_evidence_records,
    )
    from trial_utils import build_canonical_trial_records


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
EPISODES_FILE = DATA_DIR / "episodes.json"
TRIALS_FILE = DATA_DIR / "trials.json"
OUTPUT_FILE = DATA_DIR / "show_note_evidence.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE)
    args = parser.parse_args()

    episodes = load_json(EPISODES_FILE, [])
    if not episodes:
        print(f"No episodes found at {EPISODES_FILE}. Run scrape_episodes.py first.")
        return 1

    show_note_records = build_show_note_evidence_records(episodes)
    trials = load_json(TRIALS_FILE, [])
    if trials:
        canonical_trials = build_canonical_trial_records(trials)
        show_note_records = annotate_show_note_matches(show_note_records, canonical_trials)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as f:
        json.dump(show_note_records, f, ensure_ascii=False, indent=2)
        f.write("\n")

    matched = sum(1 for record in show_note_records if record.get("canonical_key"))
    link_mentions = sum(record.get("link_count", 0) for record in show_note_records)
    episode_count = len({
        episode.get("episode_url")
        for record in show_note_records
        for episode in record.get("episodes", [])
        if episode.get("episode_url")
    })

    print(f"Show-note evidence written to {args.output}")
    print(f"  Canonical cited evidence URLs: {len(show_note_records)}")
    print(f"  Evidence hyperlink mentions:  {link_mentions}")
    print(f"  Episodes with evidence links: {episode_count}")
    print(f"  Matched existing records:     {matched}")
    print(f"  Show-note-only records:       {len(show_note_records) - matched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
