"""
Incremental ingest of new Curbsiders episodes.

Runs the full pipeline but only does model work on episodes that are new since
the last run, then rebuilds the deterministic layers (pearls + site) and
validates. Designed to be safe to run on a schedule (e.g. weekly).

Phases:
  1. scrape    refresh data/episodes.json (skips already-scraped episodes)
  2. extract   run the trial extractor on pending episodes only
  3. enrich    deterministically add segments + trial detail + category
  4. pearls    re-derive data/pearls.json (deterministic, cheap)
  5. site      rebuild docs/data/*.json
  6. validate  run repository validation

Usage:
  python scripts/ingest.py                      # full incremental run (openai)
  python scripts/ingest.py --dry-run            # report new episodes, do nothing
  python scripts/ingest.py --skip-scrape        # use the current episodes.json
  python scripts/ingest.py --enrich-only        # rebuild deterministic layers only (no model)
  python scripts/ingest.py --report             # print a coverage report after validation
  python scripts/ingest.py --backend batch      # extract via the Batch API
  python scripts/ingest.py --backend anthropic --workers 4

Exit codes: 0 on success, 1 if a phase fails (e.g. validation).
"""

import argparse
import subprocess
import sys
from pathlib import Path

try:
    from scripts.extract_trials import (
        EPISODES_FILE,
        STATE_FILE,
        TRIALS_FILE,
        completed_episode_urls,
        extract_episode_number,
        load_json,
        pending_episodes,
    )
    from scripts.extract_pearls import PEARLS_FILE
except ImportError:
    from extract_trials import (
        EPISODES_FILE,
        STATE_FILE,
        TRIALS_FILE,
        completed_episode_urls,
        extract_episode_number,
        load_json,
        pending_episodes,
    )
    from extract_pearls import PEARLS_FILE

SCRIPTS_DIR = Path(__file__).parent


def plan_ingest(episodes: list[dict], state: dict) -> list[dict]:
    """Episodes that still need extraction (not completed and not failed)."""
    return pending_episodes(episodes, state, retry_failures=False, limit=None)


def describe_episode(episode: dict) -> str:
    number = extract_episode_number(episode)
    title = episode.get("title") or episode.get("url") or "?"
    return f"#{number if number is not None else '?'} {title}"


def run_step(name: str, command: list[str]) -> None:
    print(f"\n=== {name} ===")
    print("$ " + " ".join(command))
    result = subprocess.run(command)
    if result.returncode != 0:
        raise RuntimeError(f"Step '{name}' failed with exit code {result.returncode}")


def python_step(name: str, script: str, *script_args: str) -> None:
    run_step(name, [sys.executable, str(SCRIPTS_DIR / script), *script_args])


def count_pearls() -> int:
    return len(load_json(PEARLS_FILE, []))


def _pct(part: int, whole: int) -> str:
    return f"{(100 * part / whole):.0f}%" if whole else "0%"


def print_report() -> None:
    """Coverage of the deterministic classification + detail layers."""
    from collections import Counter

    episodes = load_json(EPISODES_FILE, [])
    trials = load_json(TRIALS_FILE, [])
    pearls = load_json(PEARLS_FILE, [])

    trial_seg = sum(1 for t in trials if t.get("segment"))
    trial_nct = sum(1 for t in trials if t.get("nct_id"))
    trial_n = sum(1 for t in trials if t.get("sample_size"))
    trial_journal = sum(1 for t in trials if t.get("journal"))
    trial_cat = sum(1 for t in trials if t.get("episode_category"))

    pearl_seg = sum(1 for p in pearls if p.get("segment"))
    pearl_topic = sum(1 for p in pearls if p.get("clinical_topic"))
    categories = Counter(t.get("episode_category") for t in trials if t.get("episode_category"))

    print("\n=== Classification & detail coverage ===")
    print(f"  Episodes:                {len(episodes)}")
    print(f"  Trials with a segment:   {trial_seg}/{len(trials)} ({_pct(trial_seg, len(trials))})")
    print(f"  Trials with a category:  {trial_cat}/{len(trials)} ({_pct(trial_cat, len(trials))})")
    print(f"  Trials with an NCT id:   {trial_nct}/{len(trials)} ({_pct(trial_nct, len(trials))})")
    print(f"  Trials with sample size: {trial_n}/{len(trials)} ({_pct(trial_n, len(trials))})")
    print(f"  Trials with a journal:   {trial_journal}/{len(trials)} ({_pct(trial_journal, len(trials))})")
    print(f"  Pearls with a segment:   {pearl_seg}/{len(pearls)} ({_pct(pearl_seg, len(pearls))})")
    print(f"  Pearls with clinical_topic:{pearl_topic}/{len(pearls)} ({_pct(pearl_topic, len(pearls))})")
    print(f"  Category distribution:   {categories.most_common()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        choices=["openai", "anthropic", "ollama", "batch"],
        default="openai",
        help="Extraction backend. 'batch' uses the OpenAI Batch API (extract_trials_batch run).",
    )
    parser.add_argument("--model", default=None, help="Model name (defaults per backend)")
    parser.add_argument("--workers", type=int, default=8, help="Parallel workers for sync backends")
    parser.add_argument("--skip-scrape", action="store_true", help="Do not re-scrape; use the current episodes.json")
    parser.add_argument(
        "--enrich-only",
        action="store_true",
        help="Skip scrape + model extraction; only re-run the deterministic enrich/pearls/site/validate layers",
    )
    parser.add_argument("--report", action="store_true", help="Print a classification/detail coverage report after validation")
    parser.add_argument("--dry-run", action="store_true", help="Report pending episodes and exit without changing data")
    parser.add_argument("--max-wait-minutes", type=int, default=60, help="Batch backend: max minutes to wait")
    args = parser.parse_args()

    if args.enrich_only:
        args.skip_scrape = True

    if not EPISODES_FILE.exists() and args.skip_scrape:
        print(f"Error: {EPISODES_FILE} not found and --skip-scrape was set.")
        return 1

    # Phase 1: scrape (unless skipped). The scraper is itself incremental.
    if not args.skip_scrape and not args.dry_run:
        python_step("scrape", "scrape_episodes.py")

    episodes = load_json(EPISODES_FILE, [])
    state = load_json(STATE_FILE, {})
    pending = plan_ingest(episodes, state)

    completed = len(completed_episode_urls(state, retry_failures=False))
    print(f"\nEpisodes total: {len(episodes)} | already processed: {completed} | pending: {len(pending)}")
    for episode in pending[:20]:
        print(f"  - {describe_episode(episode)}")
    if len(pending) > 20:
        print(f"  ... and {len(pending) - 20} more")

    if args.dry_run:
        print("\nDry run: no extraction, pearls, or site rebuild performed.")
        return 0

    pearls_before = count_pearls()

    # Phase 2: extract new episodes (skip entirely if nothing is pending).
    if args.enrich_only:
        print("\nEnrich-only run; skipping model extraction.")
    elif pending:
        if args.backend == "batch":
            batch_args = ["run", "--max-wait-minutes", str(args.max_wait_minutes)]
            if args.model:
                batch_args += ["--model", args.model]
            python_step("extract (batch)", "extract_trials_batch.py", *batch_args)
        else:
            extract_args = ["--backend", args.backend, "--workers", str(args.workers)]
            if args.model:
                extract_args += ["--model", args.model]
            python_step("extract", "extract_trials.py", *extract_args)
    else:
        print("\nNo pending episodes; skipping extraction.")

    # Phase 3 + 4 + 5: always rebuild the deterministic layers so segments,
    # trial detail, category, and linking pick up any new trials or show-note
    # edits. All three are pure functions of episodes.json + trials.json.
    python_step("enrich", "enrich_trials.py")
    python_step("pearls", "extract_pearls.py")
    python_step("site", "build_site.py")

    # Phase 5: validate.
    try:
        python_step("validate", "validate_repository.py")
    except RuntimeError as error:
        print(f"\nIngest completed extraction but validation failed: {error}")
        return 1

    pearls_after = count_pearls()
    trials_after = len(load_json(TRIALS_FILE, []))
    print("\n=== Ingest summary ===")
    print(f"  New episodes processed:  {0 if args.enrich_only else len(pending)}")
    print(f"  Trial mentions (total):  {trials_after}")
    print(f"  Pearls (total):          {pearls_after} ({pearls_after - pearls_before:+d})")

    if args.report:
        print_report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
