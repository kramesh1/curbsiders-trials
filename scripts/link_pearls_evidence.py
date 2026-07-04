"""
Model-assisted upgrade of pearl -> evidence linking.

The deterministic linker (scripts/pearl_utils.link_pearls_to_trials) is fast,
reproducible, and safe, but it links by term overlap. That is both lossy (it
leaves ~31% of pearls with no citation at all) and imprecise (sharing words is
not the same as a trial actually *supporting* the teaching point). Since the
human-curated show-note pearls are the crown jewels of this repository, they
deserve the best evidence tracking we can give them.

This pass asks a model, one episode at a time, which of that episode's OWN
already-extracted trials support each pearl. It is fenced the same way the rest
of the model work in this repo is:

  1. GROUNDED. The model is handed a numbered list of the episode's trials and may
     only refer to them by index. It cannot cite a paper we did not extract; the
     universe of citable evidence is closed and supplied.
  2. VERIFIABLE. Every index the model returns is range-checked and mapped back to
     the canonical_key we offered. Anything out of range (a hallucinated index) is
     dropped, not trusted.
  3. OWNER-GATED. Output goes to its own sidecar file, data/pearl_evidence_links.json.
     It NEVER writes data/pearls.json, and it is not part of ingest.py -- it spends
     tokens and must be run deliberately. An `apply` step merges reviewed links into
     a separate published artifact (data/pearls_linked.json), leaving the
     deterministic file untouched.

Model defaults to claude-opus-4-8; override with --model. Requires ANTHROPIC_API_KEY.

Usage:
  python scripts/link_pearls_evidence.py generate --episode 500   # one episode
  python scripts/link_pearls_evidence.py generate --limit 5       # first 5 pending episodes
  python scripts/link_pearls_evidence.py generate                 # all (spends tokens!)
  python scripts/link_pearls_evidence.py report                   # coverage lift vs term-overlap
  python scripts/link_pearls_evidence.py apply                    # merge reviewed links -> pearls_linked.json
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.extract_trials import DATA_DIR, load_json, save_json, parse_json_response
    from scripts.extract_pearls import PEARLS_FILE
    from scripts.pearl_utils import _pearl_dedupe_key, trial_canonical_key
    from scripts.trial_utils import clean_text, normalize_pubmed_url
except ImportError:
    from extract_trials import DATA_DIR, load_json, save_json, parse_json_response
    from extract_pearls import PEARLS_FILE
    from pearl_utils import _pearl_dedupe_key, trial_canonical_key
    from trial_utils import clean_text, normalize_pubmed_url

TRIALS_FILE = DATA_DIR / "trials.json"
LINKS_FILE = DATA_DIR / "pearl_evidence_links.json"
LINKED_PEARLS_FILE = DATA_DIR / "pearls_linked.json"
BATCH_JOB_FILE = DATA_DIR / "pearl_evidence_batch.json"
DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 8000

SYSTEM_PROMPT = """\
You are a medical librarian deciding which studies support a specific teaching point.

You are given ONE podcast episode's teaching pearls and the list of clinical studies \
already extracted from that same episode. For each pearl, choose the studies (if any) \
from the provided list that give direct evidence for the SPECIFIC claim the pearl makes.

Rules that matter more than anything else:
- Refer to pearls and studies ONLY by the integer indices given. Never invent a study; \
you may only choose from the numbered list provided.
- Link a study to a pearl only when the study is evidence FOR that pearl's claim. Sharing \
a topic is NOT enough -- a hypertension trial does not support every hypertension pearl. \
When unsure, leave the pearl unlinked; a missing link is better than a wrong one.
- Mark "support": "direct" when the study's result is the basis for the claim (a threshold, \
a drug choice, an outcome). Mark "support": "background" when the study is related and \
informative but not the direct basis. Omit anything weaker.
- A pearl may link to zero, one, or several studies. Most pearls link to zero or one.
- Give a one-line rationale grounded in what the study is, and a confidence.

Return ONLY a JSON object of the form:
  {"links": [{"pearl": <int>, "trial": <int>, "support": "direct|background", \
"confidence": "high|medium|low", "rationale": "..."}, ...]}
No prose before or after the JSON.
"""


def group_by_episode(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        url = row.get("episode_url")
        if url:
            grouped.setdefault(url, []).append(row)
    return grouped


def episode_trial_pool(episode_trials: list[dict]) -> list[dict]:
    """The episode's trials that have a stable identity, deduped by canonical_key.

    Only trials with a canonical_key can be linked (they are the ones with a
    canonical site record); fallback-identity mentions are skipped, mirroring the
    deterministic linker.
    """
    pool: list[dict] = []
    seen: set[str] = set()
    for trial in episode_trials:
        key = trial_canonical_key(trial)
        if not key or key in seen:
            continue
        seen.add(key)
        pool.append(trial)
    return pool


def _trial_display(trial: dict) -> str:
    """A compact one-line description of a trial for the prompt."""
    bits = []
    label = clean_text(trial.get("citation_label"))
    if label:
        bits.append(label)
    title = clean_text(trial.get("paper_title"))
    if title:
        bits.append(f'"{title}"')
    meta = []
    for field in ("study_type", "year", "journal"):
        value = trial.get(field)
        if value:
            meta.append(str(value))
    if trial.get("sample_size"):
        meta.append(f"n={trial['sample_size']}")
    if meta:
        bits.append(f"({', '.join(meta)})")
    summary = clean_text(trial.get("brief_summary")) or clean_text(trial.get("context_topic"))
    if summary:
        bits.append(f"- {summary}")
    return " ".join(bits)


def build_link_prompt(episode_number, episode_title: str, pearls: list[dict], pool: list[dict]) -> str:
    pearl_lines = "\n".join(f"[{i}] {p['pearl']}" for i, p in enumerate(pearls))
    trial_lines = "\n".join(f"[{i}] {_trial_display(t)}" for i, t in enumerate(pool))
    return (
        f"Episode #{episode_number}: {episode_title}\n\n"
        f"PEARLS (teaching points to support):\n{pearl_lines}\n\n"
        f"STUDIES extracted from this episode (the only citable evidence):\n{trial_lines}\n\n"
        "For each pearl that a study directly supports, emit a link. Return the JSON object now."
    )


def _citation_view(trial: dict, canonical_key: str) -> dict:
    """The stored, human-readable citation for a linked trial."""
    return {
        "canonical_key": canonical_key,
        "citation_label": clean_text(trial.get("citation_label")),
        "paper_title": clean_text(trial.get("paper_title")),
        "pubmed_url": normalize_pubmed_url(trial.get("pubmed_url")),
        "year": trial.get("year"),
        "study_type": trial.get("study_type") or "other",
        "journal": clean_text(trial.get("journal")),
        "sample_size": trial.get("sample_size"),
        "nct_id": clean_text(trial.get("nct_id")),
    }


def verify_links(raw_links, pearls: list[dict], pool: list[dict]) -> tuple[dict[int, list[dict]], int]:
    """Map the model's (pearl, trial) index pairs back to real citations.

    Returns (links_by_pearl_index, dropped_count). A pair is kept only when both
    indices are in range -- an out-of-range index means the model referred to a
    study (or pearl) that was not on offer, so it is dropped rather than trusted.
    """
    by_pearl: dict[int, list[dict]] = {}
    dropped = 0
    for link in raw_links or []:
        try:
            pi = int(link.get("pearl"))
            ti = int(link.get("trial"))
        except (TypeError, ValueError):
            dropped += 1
            continue
        if not (0 <= pi < len(pearls) and 0 <= ti < len(pool)):
            dropped += 1
            continue
        trial = pool[ti]
        canonical_key = trial_canonical_key(trial)
        citation = _citation_view(trial, canonical_key)
        support = link.get("support")
        citation["support"] = support if support in ("direct", "background") else "direct"
        confidence = link.get("confidence")
        citation["confidence"] = confidence if confidence in ("high", "medium", "low") else None
        citation["rationale"] = clean_text(link.get("rationale"))
        bucket = by_pearl.setdefault(pi, [])
        # A model may emit the same trial twice for a pearl; keep the first.
        if any(c["canonical_key"] == canonical_key for c in bucket):
            continue
        bucket.append(citation)
    return by_pearl, dropped


def _call_model(client, model: str, prompt: str) -> str:
    """One linking call: system rules + the episode's pearls/trials as the user turn."""
    message = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,  # an episode can have many pearls, each with a rationale
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return next((b.text for b in message.content if b.type == "text"), "")


def build_link_records(parsed_links, pearls: list[dict], pool: list[dict], model: str, generated_at: str) -> tuple[list[dict], int]:
    """Turn one model response's (verified) links into per-pearl sidecar records.

    Shared by the synchronous and batch paths, so both produce identical output.
    """
    by_pearl, dropped = verify_links(parsed_links, pearls, pool)
    records = []
    for pi, pearl in enumerate(pearls):
        links = by_pearl.get(pi, [])
        if not links:
            continue
        records.append({
            "episode_url": pearl.get("episode_url"),
            "episode_number": pearl.get("episode_number"),
            "pearl_key": _pearl_dedupe_key(pearl["pearl"]),
            "pearl": pearl["pearl"],
            "links": links,
            "review_status": "pending",
            "generated_by": model,
            "generated_at": generated_at,
        })
    return records, dropped


def generate_for_episode(client, model: str, episode_number, episode_title, pearls, pool) -> tuple[list[dict], int]:
    """Return (link-records for this episode's pearls, dropped_index_count)."""
    prompt = build_link_prompt(episode_number, episode_title, pearls, pool)
    raw = _call_model(client, model, prompt)
    parsed = parse_json_response(raw)  # tolerates the {"links": [...]} wrapper
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return build_link_records(parsed, pearls, pool, model, now)


def eligible_episode_urls(pearls_by_ep: dict, trials_by_ep: dict, *, episode: int | None = None) -> list[str]:
    """Episodes worth a model call: >=1 pearl AND >=1 linkable trial, newest first."""
    urls = [url for url in pearls_by_ep if episode_trial_pool(trials_by_ep.get(url, []))]
    if episode is not None:
        urls = [url for url in urls if any(p.get("episode_number") == episode for p in pearls_by_ep[url])]
    urls.sort(key=lambda url: -(pearls_by_ep[url][0].get("episode_number") or 0))
    return urls


def cmd_generate(args) -> int:
    pearls = load_json(PEARLS_FILE, [])
    trials = load_json(TRIALS_FILE, [])
    if not pearls:
        print(f"No pearls in {PEARLS_FILE}. Run extract_pearls.py first.")
        return 1

    pearls_by_ep = group_by_episode(pearls)
    trials_by_ep = group_by_episode(trials)

    episode_urls = eligible_episode_urls(pearls_by_ep, trials_by_ep, episode=args.episode)
    if args.episode is not None and not episode_urls:
        print(f"No episode #{args.episode} with both pearls and linkable trials.")
        return 1

    existing = load_json(LINKS_FILE, [])
    done_urls = {r["episode_url"] for r in existing}
    if not args.refresh:
        episode_urls = [url for url in episode_urls if url not in done_urls]
    if args.limit is not None:
        episode_urls = episode_urls[: args.limit]

    if not episode_urls:
        print("Nothing to generate (all eligible episodes already have links).")
        return 0

    try:
        import anthropic
    except ImportError:
        print("Error: the anthropic package is required (pip install anthropic).")
        return 1
    client = anthropic.Anthropic()

    print(f"Linking evidence for {len(episode_urls)} episode(s) with {args.model}.")
    print("Owner-gated: writes a sidecar, never data/pearls.json.\n")

    # Keep links for episodes not in this run; replace those we regenerate.
    regenerated = set(episode_urls)
    kept = [r for r in existing if r["episode_url"] not in regenerated]
    linked_pearls = 0
    total_dropped = 0
    for i, url in enumerate(episode_urls):
        ep_pearls = pearls_by_ep[url]
        pool = episode_trial_pool(trials_by_ep.get(url, []))
        num = ep_pearls[0].get("episode_number")
        title = ep_pearls[0].get("episode_title", "")
        try:
            records, dropped = generate_for_episode(client, args.model, num, title, ep_pearls, pool)
        except Exception as error:  # noqa: BLE001 - keep processing the rest
            print(f"  [{i+1}/{len(episode_urls)}] #{num}: error {type(error).__name__}: {error}")
            continue
        kept.extend(records)
        linked_pearls += len(records)
        total_dropped += dropped
        print(f"  [{i+1}/{len(episode_urls)}] #{num}: {len(records)}/{len(ep_pearls)} pearls linked"
              f" from {len(pool)} studies"
              f"{f' ({dropped} bad index dropped)' if dropped else ''}")
        save_json(LINKS_FILE, kept)

    print(f"\nDone. {linked_pearls} pearls linked, written to {LINKS_FILE}.")
    if total_dropped:
        print(f"Dropped {total_dropped} out-of-range index reference(s) the model returned.")
    print("Review links (set review_status to \"approved\"), then run: "
          "python scripts/link_pearls_evidence.py apply")
    return 0


def build_batch_requests(episode_urls, pearls_by_ep, trials_by_ep, model) -> tuple[list[dict], dict]:
    """One Messages-API request per episode, plus a custom_id -> episode_url map.

    custom_id is a short index (batch custom_ids are length-limited); the map lets
    `collect` re-attach each result to its episode. The prompt is built exactly as
    in the synchronous path, so batch and sync produce identical links.
    """
    requests = []
    custom_map: dict[str, str] = {}
    for i, url in enumerate(episode_urls):
        ep_pearls = pearls_by_ep[url]
        pool = episode_trial_pool(trials_by_ep.get(url, []))
        num = ep_pearls[0].get("episode_number")
        title = ep_pearls[0].get("episode_title", "")
        custom_id = f"ep-{i:04d}"
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": model,
                "max_tokens": MAX_TOKENS,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": build_link_prompt(num, title, ep_pearls, pool)}],
            },
        })
        custom_map[custom_id] = url
    return requests, custom_map


def cmd_submit_batch(args) -> int:
    pearls = load_json(PEARLS_FILE, [])
    trials = load_json(TRIALS_FILE, [])
    if not pearls:
        print(f"No pearls in {PEARLS_FILE}. Run extract_pearls.py first.")
        return 1

    pearls_by_ep = group_by_episode(pearls)
    trials_by_ep = group_by_episode(trials)
    episode_urls = eligible_episode_urls(pearls_by_ep, trials_by_ep, episode=args.episode)

    existing = load_json(LINKS_FILE, [])
    done_urls = {r["episode_url"] for r in existing}
    if not args.refresh:
        episode_urls = [url for url in episode_urls if url not in done_urls]
    if args.limit is not None:
        episode_urls = episode_urls[: args.limit]

    if not episode_urls:
        print("Nothing to submit (all eligible episodes already have links).")
        return 0

    requests, custom_map = build_batch_requests(episode_urls, pearls_by_ep, trials_by_ep, args.model)

    try:
        import anthropic
    except ImportError:
        print("Error: the anthropic package is required (pip install anthropic).")
        return 1
    client = anthropic.Anthropic()

    print(f"Submitting a batch of {len(requests)} episode(s) at 50% Batch-API pricing with {args.model}.")
    batch = client.messages.batches.create(requests=requests)

    job = {
        "batch_id": batch.id,
        "model": args.model,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "custom_map": custom_map,
        # Sanity check for collect: the index->citation mapping is only valid if
        # pearls.json/trials.json haven't changed between submit and collect.
        "fingerprint": {"pearls": len(pearls), "trials": len(trials)},
    }
    save_json(BATCH_JOB_FILE, job)

    print(f"  batch id:          {batch.id}")
    print(f"  processing status: {batch.processing_status}")
    print(f"  job saved to:      {BATCH_JOB_FILE}")
    print("\nCollect results when the batch ends (usually <1h) with:")
    print("  python scripts/link_pearls_evidence.py collect --wait")
    return 0


def _print_batch_status(batch) -> None:
    counts = getattr(batch, "request_counts", None)
    detail = ""
    if counts is not None:
        detail = (f"  (processing {getattr(counts, 'processing', 0)}, "
                  f"succeeded {getattr(counts, 'succeeded', 0)}, "
                  f"errored {getattr(counts, 'errored', 0)})")
    print(f"batch {batch.id}: {batch.processing_status}{detail}")


def cmd_collect(args) -> int:
    job = load_json(BATCH_JOB_FILE, None)
    if not job:
        print(f"No batch job found ({BATCH_JOB_FILE}). Run `submit-batch` first.")
        return 1

    try:
        import anthropic
    except ImportError:
        print("Error: the anthropic package is required (pip install anthropic).")
        return 1
    client = anthropic.Anthropic()
    batch_id = job["batch_id"]

    deadline = time.time() + args.max_wait_minutes * 60
    while True:
        try:
            batch = client.messages.batches.retrieve(batch_id)
        except anthropic.APIConnectionError as error:
            # A transient network/DNS blip shouldn't kill a long poll. The batch
            # keeps running server-side; just wait and retry until the deadline.
            if not args.wait or time.time() >= deadline:
                print(f"Connection error retrieving batch and not retrying: {error}")
                return 1
            print(f"  (transient connection error, retrying in {args.poll_interval}s: {error})")
            time.sleep(args.poll_interval)
            continue
        _print_batch_status(batch)
        if batch.processing_status == "ended":
            break
        if not args.wait:
            print("Not ended yet. Re-run later, or pass --wait to poll until it finishes.")
            return 0
        if time.time() >= deadline:
            print(f"Still not ended after {args.max_wait_minutes} min. Re-run `collect` later.")
            return 1
        time.sleep(args.poll_interval)

    pearls = load_json(PEARLS_FILE, [])
    trials = load_json(TRIALS_FILE, [])
    fingerprint = job.get("fingerprint") or {}
    if fingerprint and (fingerprint.get("pearls") != len(pearls) or fingerprint.get("trials") != len(trials)):
        print("WARNING: pearls.json/trials.json changed since submit; index mapping may be off. "
              "Consider re-submitting rather than trusting these links.")

    pearls_by_ep = group_by_episode(pearls)
    trials_by_ep = group_by_episode(trials)
    custom_map = job["custom_map"]
    model = job.get("model")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Replace links for the episodes in this batch; keep everything else.
    batch_urls = set(custom_map.values())
    existing = load_json(LINKS_FILE, [])
    kept = [r for r in existing if r["episode_url"] not in batch_urls]

    linked_pearls = 0
    episodes_ok = 0
    errored = 0
    total_dropped = 0
    for result in client.messages.batches.results(batch_id):
        url = custom_map.get(result.custom_id)
        if url is None:
            continue
        if result.result.type != "succeeded":
            errored += 1
            print(f"  {result.custom_id} ({result.result.type}): skipped")
            continue
        message = result.result.message
        raw = next((b.text for b in message.content if b.type == "text"), "")
        parsed = parse_json_response(raw)
        ep_pearls = pearls_by_ep.get(url, [])
        pool = episode_trial_pool(trials_by_ep.get(url, []))
        records, dropped = build_link_records(parsed, ep_pearls, pool, model, now)
        kept.extend(records)
        linked_pearls += len(records)
        total_dropped += dropped
        episodes_ok += 1

    save_json(LINKS_FILE, kept)
    print(f"\nDone. {episodes_ok} episode(s) processed, {linked_pearls} pearls linked -> {LINKS_FILE}.")
    if errored:
        print(f"{errored} request(s) errored and were skipped.")
    if total_dropped:
        print(f"Dropped {total_dropped} out-of-range index reference(s) the model returned.")
    print("Review links (set review_status to \"approved\"), then run: "
          "python scripts/link_pearls_evidence.py apply")
    return 0


def cmd_report(args) -> int:
    from collections import Counter

    pearls = load_json(PEARLS_FILE, [])
    links = load_json(LINKS_FILE, [])
    if not links:
        print(f"No links yet ({LINKS_FILE} is empty). Run generate first.")
        return 0

    links_by_key = {(r["episode_url"], r["pearl_key"]): r for r in links}
    covered_episodes = {r["episode_url"] for r in links}

    # Compare, over just the episodes we've processed, model coverage vs the
    # deterministic term-overlap baseline already in pearls.json.
    det_linked = model_linked = both = only_model = only_det = 0
    for pearl in pearls:
        url = pearl.get("episode_url")
        if url not in covered_episodes:
            continue
        det = bool(pearl.get("supporting_citations"))
        rec = links_by_key.get((url, _pearl_dedupe_key(pearl.get("pearl", ""))))
        mod = bool(rec and rec.get("links"))
        det_linked += det
        model_linked += mod
        both += det and mod
        only_model += mod and not det
        only_det += det and not mod

    status = Counter(r.get("review_status") for r in links)
    direct = sum(1 for r in links for l in r["links"] if l.get("support") == "direct")
    total_links = sum(len(r["links"]) for r in links)

    print("=== Pearl evidence linking ===")
    print(f"  Episodes processed:        {len(covered_episodes)}")
    print(f"  Pearls the model linked:   {len(links)}  ({total_links} links, {direct} direct)")
    print(f"  Review status:             {dict(status)}")
    print("\n  Over the processed episodes, vs the term-overlap baseline:")
    print(f"    pearls linked by baseline: {det_linked}")
    print(f"    pearls linked by model:    {model_linked}")
    print(f"    both agree link exists:    {both}")
    print(f"    NEW (model only):          {only_model}   <- coverage recovered")
    print(f"    baseline only (model none):{only_det}   <- baseline links the model did not confirm")
    return 0


def cmd_apply(args) -> int:
    """Merge reviewed links onto a copy of the pearls, into a separate artifact."""
    pearls = load_json(PEARLS_FILE, [])
    links = load_json(LINKS_FILE, [])
    if not links:
        print(f"No links to apply ({LINKS_FILE} is empty).")
        return 1

    accepted = ("approved",) if not args.include_pending else ("approved", "pending")
    links_by_key = {
        (r["episode_url"], r["pearl_key"]): r
        for r in links
        if r.get("review_status") in accepted
    }
    if not links_by_key:
        print("No links with review_status in "
              f"{accepted}. Mark links \"approved\" (or pass --include-pending).")
        return 1

    applied = 0
    out = []
    for pearl in pearls:
        pearl = dict(pearl)
        rec = links_by_key.get((pearl.get("episode_url"), _pearl_dedupe_key(pearl.get("pearl", ""))))
        if rec:
            pearl["evidence_links"] = rec["links"]
            applied += 1
        out.append(pearl)

    save_json(LINKED_PEARLS_FILE, out)
    print(f"Applied model links to {applied} pearl(s) -> {LINKED_PEARLS_FILE}")
    print(f"(data/pearls.json is left untouched by design.)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="Draft model evidence links for pearls (synchronous)")
    g.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model (default claude-opus-4-8)")
    g.add_argument("--episode", type=int, default=None, help="Only this episode number")
    g.add_argument("--limit", type=int, default=None, help="At most N eligible episodes")
    g.add_argument("--refresh", action="store_true", help="Regenerate episodes that already have links")
    g.set_defaults(func=cmd_generate)

    s = sub.add_parser("submit-batch", help="Submit all eligible episodes via the Batch API (50% cheaper)")
    s.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model (default claude-opus-4-8)")
    s.add_argument("--episode", type=int, default=None, help="Only this episode number")
    s.add_argument("--limit", type=int, default=None, help="At most N eligible episodes")
    s.add_argument("--refresh", action="store_true", help="Include episodes that already have links")
    s.set_defaults(func=cmd_submit_batch)

    c = sub.add_parser("collect", help="Retrieve batch results and write the links sidecar")
    c.add_argument("--wait", action="store_true", help="Poll until the batch ends instead of reporting once")
    c.add_argument("--poll-interval", type=int, default=60, help="Seconds between polls when --wait")
    c.add_argument("--max-wait-minutes", type=int, default=120, help="Give up waiting after this many minutes")
    c.set_defaults(func=cmd_collect)

    r = sub.add_parser("report", help="Coverage lift vs the term-overlap baseline")
    r.set_defaults(func=cmd_report)

    a = sub.add_parser("apply", help="Merge reviewed links into data/pearls_linked.json")
    a.add_argument("--include-pending", action="store_true",
                   help="Also apply links still marked pending (default: approved only)")
    a.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
