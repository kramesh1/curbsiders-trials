"""
Owner-gated candidate teaching-pearl generation over full-episode transcripts.

The deterministic pearl layer (scripts/extract_pearls.py) extracts pearls verbatim
from the show-note "Pearls" sections. Whole episodes contain many more teaching
points than the notes summarize — but pulling them out needs a model, and model
paraphrase is exactly the hallucination risk this project exists to avoid. So this
pass is deliberately fenced:

  1. It NEVER writes to data/pearls.json. Candidates go to their own file.
  2. Every candidate must carry a **verbatim supporting quote** from the transcript.
     We then deterministically verify that quote actually appears in the transcript
     and drop (or flag) any that don't — the model cannot smuggle in an unsupported
     claim, because its evidence is checkable.
  3. Nothing is published until a human sets review_status="approved" and runs the
     `promote` step. That is the owner gate.
  4. It is not part of ingest.py — it spends tokens and must be run deliberately.
  5. Only episodes with zero deterministic pearls (per pearl_coverage.compute_pearl_gap)
     are ever eligible, even with --refresh -- an episode with real show-notes pearls
     is never sent through this path.

By default it only reads high-fidelity official (human/CME-reviewed) transcripts and
skips AI-generated ones (see the ai_generated flag), since ASR text is a poor source
for teaching claims.

Usage:
  python scripts/generate_candidate_pearls.py generate --episode 347   # one episode
  python scripts/generate_candidate_pearls.py generate --limit 5       # first 5 eligible
  python scripts/generate_candidate_pearls.py generate                 # all eligible (spends tokens!)
  python scripts/generate_candidate_pearls.py submit-batch              # same pool, Batch API (50% cheaper)
  python scripts/generate_candidate_pearls.py collect --wait            # retrieve batch results
  python scripts/generate_candidate_pearls.py report                   # counts + review status
  python scripts/generate_candidate_pearls.py promote                  # approved -> approved_pearls.json

Model defaults to claude-opus-4-8; override with --model. Requires ANTHROPIC_API_KEY.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.extract_trials import DATA_DIR, load_json, save_json, parse_json_response
    from scripts.fetch_transcripts import TRANSCRIPTS_FILE
    from scripts.extract_pearls import EPISODES_FILE, PEARLS_FILE
    from scripts.pearl_coverage import compute_pearl_gap
except ImportError:
    from extract_trials import DATA_DIR, load_json, save_json, parse_json_response
    from fetch_transcripts import TRANSCRIPTS_FILE
    from extract_pearls import EPISODES_FILE, PEARLS_FILE
    from pearl_coverage import compute_pearl_gap

CANDIDATES_FILE = DATA_DIR / "candidate_pearls.json"
PRIVATE_CANDIDATES_FILE = DATA_DIR / "private" / "candidate_pearls.json"
APPROVED_FILE = DATA_DIR / "approved_pearls.json"
BATCH_JOB_FILE = DATA_DIR / "candidate_pearls_batch.json"
DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 8000
MIN_QUOTE_CHARS = 15  # a verifiable quote has to be substantial


def _public_candidate(record: dict) -> dict:
    """Tracked review metadata without republishing transcript excerpts."""
    public = dict(record)
    quote = public.pop("supporting_quote", "") or ""
    public["supporting_quote_sha256"] = hashlib.sha256(quote.encode("utf-8")).hexdigest() if quote else None
    public["supporting_quote_char_count"] = len(quote)
    return public


def load_full_candidates() -> list[dict]:
    private = load_json(PRIVATE_CANDIDATES_FILE, [])
    if private:
        return private
    # Backward-compatible migration path for repositories that still have quotes
    # in the tracked artifact. `migrate-private` rewrites it safely.
    return load_json(CANDIDATES_FILE, [])


def save_candidate_files(records: list[dict]) -> None:
    PRIVATE_CANDIDATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    save_json(PRIVATE_CANDIDATES_FILE, records)
    save_json(CANDIDATES_FILE, [_public_candidate(record) for record in records])


def content_fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

SYSTEM_PROMPT = """\
You are a careful internal-medicine educator extracting a small number of concise, \
high-yield teaching pearls from a podcast episode transcript, for use as quick reference \
at the bedside.

Rules that matter more than anything else:
- Ground every pearl in the transcript. For each pearl, quote the exact span of the \
transcript it comes from, copied VERBATIM (character for character) into \
"supporting_quote". Do not paraphrase the quote, fix its grammar, or merge \
non-adjacent spans. If you cannot support a statement with a verbatim quote, do not \
include it.
- Prefer specific, actionable clinical points that connect evidence to a practice-changing \
idea: management thresholds, drug choices, dosing, test interpretation, outcome tradeoffs, \
harms, or guideline changes. Strong candidates usually mention a trial, guideline, \
systematic review, quantified outcome, or explicit recommendation in the quoted text.
- Do not extract every interesting quote. Omit generic background, pathophysiology, \
definitions, anecdotes, and broad advice unless the quoted text ties it to clinical \
evidence or a clear change in practice.
- Keep each "statement" to one or two sentences, phrased as a clean teaching point.
- Do NOT invent numbers, drug names, doses, or trial names not present in the quote.
- It is better to return fewer, well-supported pearls than many weak ones. Return an \
empty list if the transcript has no solid teaching points.

Return ONLY a JSON object of the form:
  {"pearls": [{"statement": "...", "supporting_quote": "...", "topic": "...", \
"confidence": "high|medium|low"}, ...]}
No prose before or after the JSON.
"""


def _normalize(text: str) -> str:
    """Whitespace- and case-insensitive form for verbatim-quote checking."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def quote_is_verbatim(quote: str, transcript: str, *, _cache: dict = {}) -> bool:
    """True if the quote really appears in the transcript (whitespace/case tolerant)."""
    if not quote or len(quote.strip()) < MIN_QUOTE_CHARS:
        return False
    norm_transcript = _cache.get(id(transcript))
    if norm_transcript is None:
        norm_transcript = _normalize(transcript)
        _cache[id(transcript)] = norm_transcript
    return _normalize(quote) in norm_transcript


def eligible_transcripts(transcripts: list[dict], *, include_ai: bool, source: str) -> list[dict]:
    rows = []
    for t in transcripts:
        if not t.get("text"):
            continue
        if source != "all" and t.get("source") != source:
            continue
        if t.get("ai_generated") and not include_ai:
            continue
        rows.append(t)
    return sorted(rows, key=lambda r: -(r.get("episode_number") or 0))


def build_transcript_prompt(transcript: dict) -> str:
    return (
        f"Episode #{transcript.get('episode_number')}: {transcript.get('title','')}\n\n"
        f"Transcript:\n{transcript['text']}"
    )


def build_candidate_records(raw: str, transcript: dict, model: str, generated_at: str) -> list[dict]:
    """Turn one model response into candidate-pearl records. Shared by the
    synchronous and batch paths, so both produce identical output."""
    # parse_json_response tolerates code fences and returns the list inside a
    # {"pearls": [...]} wrapper object.
    pearls = parse_json_response(raw)
    text = transcript["text"]

    records = []
    for p in pearls:
        quote = (p.get("supporting_quote") or "").strip()
        records.append({
            "episode_url": transcript["episode_url"],
            "episode_number": transcript.get("episode_number"),
            "episode_title": transcript.get("title", ""),
            "source": transcript.get("source"),
            "statement": (p.get("statement") or "").strip(),
            "supporting_quote": quote,
            "topic": (p.get("topic") or "").strip(),
            "confidence": p.get("confidence"),
            "quote_verified": quote_is_verbatim(quote, text),
            "review_status": "pending",
            "generated_by": model,
            "generated_at": generated_at,
        })
    return records


def generate_for_transcript(client, model: str, transcript: dict) -> list[dict]:
    message = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_transcript_prompt(transcript)}],
    )
    raw = next((b.text for b in message.content if b.type == "text"), "")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return build_candidate_records(raw, transcript, model, now)


def restrict_to_pearl_gap(pool: list[dict], episodes: list[dict], pearls: list[dict], transcripts: list[dict]) -> list[dict]:
    """Drop any transcript whose episode already has deterministic show-notes pearls.

    Never overridable by --refresh: --refresh means "redo candidates for a gap
    episode," not "reach into an episode that already has real pearls."
    """
    gap_urls = {g["episode_url"] for g in compute_pearl_gap(episodes, pearls, transcripts)}
    return [t for t in pool if t["episode_url"] in gap_urls]


def build_pool(args, transcripts: list[dict]) -> list[dict]:
    """Shared eligible-transcript-pool logic for the sync and batch commands."""
    pool = eligible_transcripts(transcripts, include_ai=args.include_ai, source=args.source)
    episodes = load_json(EPISODES_FILE, [])
    pearls = load_json(PEARLS_FILE, [])
    pool = restrict_to_pearl_gap(pool, episodes, pearls, transcripts)

    if args.episode is not None:
        pool = [t for t in pool if t.get("episode_number") == args.episode]

    existing = load_full_candidates()
    done_urls = {c["episode_url"] for c in existing}
    if not args.refresh:
        pool = [t for t in pool if t["episode_url"] not in done_urls]
    if args.limit is not None:
        pool = pool[: args.limit]
    return pool


def cmd_generate(args) -> int:
    transcripts = load_json(TRANSCRIPTS_FILE, [])
    if not transcripts:
        print(f"No transcripts in {TRANSCRIPTS_FILE}. Run fetch_transcripts.py first.")
        return 1

    pool = build_pool(args, transcripts)
    if args.episode is not None and not pool:
        print(f"No eligible transcript for episode #{args.episode}.")
        return 1

    if not pool:
        print("Nothing to generate (all eligible transcripts already have candidates).")
        return 0

    try:
        import anthropic
    except ImportError:
        print("Error: the anthropic package is required (pip install anthropic).")
        return 1
    client = anthropic.Anthropic()

    print(f"Generating candidate pearls for {len(pool)} episode(s) with {args.model}.")
    print("This spends tokens and is owner-gated — nothing is published to pearls.json.\n")

    # Keep candidates for episodes not in this run; replace those we regenerate.
    existing = load_full_candidates()
    regenerated_urls = {t["episode_url"] for t in pool}
    kept = [c for c in existing if c["episode_url"] not in regenerated_urls]
    added = 0
    dropped_unverified = 0
    for i, transcript in enumerate(pool):
        num = transcript.get("episode_number")
        try:
            records = generate_for_transcript(client, args.model, transcript)
        except Exception as error:  # noqa: BLE001
            print(f"  [{i+1}/{len(pool)}] #{num}: error {type(error).__name__}: {error}")
            continue
        verified = [r for r in records if r["quote_verified"]]
        dropped = len(records) - len(verified)
        dropped_unverified += dropped
        emit = records if args.keep_unverified else verified
        kept.extend(emit)
        added += len(emit)
        print(f"  [{i+1}/{len(pool)}] #{num}: {len(verified)} verified"
              f"{f', {dropped} unverified' + (' dropped' if not args.keep_unverified else ' kept') if dropped else ''}")
        save_candidate_files(kept)

    print(f"\nDone. {added} candidate pearls written to {CANDIDATES_FILE}.")
    if dropped_unverified and not args.keep_unverified:
        print(f"Dropped {dropped_unverified} candidate(s) whose quote wasn't verbatim in the transcript.")
    print("Review them (set review_status to \"approved\"), then run: "
          "python scripts/generate_candidate_pearls.py promote")
    return 0


def build_batch_requests(pool: list[dict], model: str) -> tuple[list[dict], dict]:
    """One Messages-API request per transcript, plus a custom_id -> episode_url map.

    custom_id is a short index (batch custom_ids are length-limited); the map lets
    `collect` re-attach each result to its episode. The prompt is built exactly as
    in the synchronous path, so batch and sync produce identical candidates.
    """
    requests = []
    custom_map: dict[str, str] = {}
    for i, transcript in enumerate(pool):
        custom_id = f"ep-{i:04d}"
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": model,
                "max_tokens": MAX_TOKENS,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": build_transcript_prompt(transcript)}],
            },
        })
        custom_map[custom_id] = transcript["episode_url"]
    return requests, custom_map


def cmd_submit_batch(args) -> int:
    transcripts = load_json(TRANSCRIPTS_FILE, [])
    if not transcripts:
        print(f"No transcripts in {TRANSCRIPTS_FILE}. Run fetch_transcripts.py first.")
        return 1

    pool = build_pool(args, transcripts)
    if not pool:
        print("Nothing to submit (all eligible transcripts already have candidates).")
        return 0

    requests, custom_map = build_batch_requests(pool, args.model)

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
        # Sanity check for collect: only valid if transcripts.json/pearls.json
        # haven't changed between submit and collect.
        "fingerprint": {
            "transcripts_sha256": content_fingerprint(transcripts),
            "pool_episode_urls_sha256": content_fingerprint(sorted(custom_map.values())),
        },
    }
    save_json(BATCH_JOB_FILE, job)

    print(f"  batch id:          {batch.id}")
    print(f"  processing status: {batch.processing_status}")
    print(f"  job saved to:      {BATCH_JOB_FILE}")
    print("\nCollect results when the batch ends (usually <1h) with:")
    print("  python scripts/generate_candidate_pearls.py collect --wait")
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

    transcripts = load_json(TRANSCRIPTS_FILE, [])
    fingerprint = job.get("fingerprint") or {}
    current_fingerprint = content_fingerprint(transcripts)
    if fingerprint and fingerprint.get("transcripts_sha256") != current_fingerprint:
        print("Refusing to collect: transcripts.json content changed since submit. Re-submit the batch.")
        return 1

    transcripts_by_url = {t["episode_url"]: t for t in transcripts if t.get("episode_url")}
    custom_map = job["custom_map"]
    model = job.get("model")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    batch_urls = set(custom_map.values())
    existing = load_full_candidates()
    kept = [c for c in existing if c["episode_url"] not in batch_urls]

    added = 0
    episodes_ok = 0
    errored = 0
    dropped_unverified = 0
    for result in client.messages.batches.results(batch_id):
        url = custom_map.get(result.custom_id)
        transcript = transcripts_by_url.get(url)
        if url is None or transcript is None:
            continue
        if result.result.type != "succeeded":
            errored += 1
            print(f"  {result.custom_id} ({result.result.type}): skipped")
            continue
        message = result.result.message
        raw = next((b.text for b in message.content if b.type == "text"), "")
        records = build_candidate_records(raw, transcript, model, now)
        verified = [r for r in records if r["quote_verified"]]
        dropped = len(records) - len(verified)
        dropped_unverified += dropped
        emit = records if args.keep_unverified else verified
        kept.extend(emit)
        added += len(emit)
        episodes_ok += 1

    save_candidate_files(kept)
    print(f"\nDone. {episodes_ok} episode(s) processed, {added} candidate pearls written to {CANDIDATES_FILE}.")
    if errored:
        print(f"{errored} request(s) errored and were skipped.")
    if dropped_unverified and not args.keep_unverified:
        print(f"Dropped {dropped_unverified} candidate(s) whose quote wasn't verbatim in the transcript.")
    print("Review them (set review_status to \"approved\"), then run: "
          "python scripts/generate_candidate_pearls.py promote")
    return 0


def cmd_report(args) -> int:
    from collections import Counter

    candidates = load_full_candidates()
    if not candidates:
        print(f"No candidates yet ({CANDIDATES_FILE} is empty).")
        return 0
    status = Counter(c.get("review_status") for c in candidates)
    conf = Counter(c.get("confidence") for c in candidates)
    verified = sum(1 for c in candidates if c.get("quote_verified"))
    episodes = len({c["episode_url"] for c in candidates})
    print("=== Candidate pearls ===")
    print(f"  Total candidates:     {len(candidates)} across {episodes} episode(s)")
    print(f"  Quote-verified:       {verified}/{len(candidates)}")
    print(f"  Review status:        {dict(status)}")
    print(f"  Confidence:           {dict(conf)}")
    approved = load_json(APPROVED_FILE, [])
    print(f"  Promoted (approved):  {len(approved)} in {APPROVED_FILE.name}")
    return 0


def cmd_promote(args) -> int:
    candidates = load_full_candidates()
    approved = [
        c for c in candidates if c.get("review_status") == "approved" and c.get("reviewed_by")
    ]
    if not approved:
        print("No candidates marked review_status=\"approved\". Nothing to promote.")
        return 0
    # Guard rails: never promote something whose quote isn't verbatim.
    unverified = [c for c in approved if not c.get("quote_verified")]
    if unverified and not args.allow_unverified:
        print(f"Refusing to promote {len(unverified)} approved candidate(s) with an unverified "
              f"quote. Re-review, or pass --allow-unverified to override.")
        return 1
    save_json(APPROVED_FILE, [_public_candidate(candidate) for candidate in approved])
    print(f"Promoted {len(approved)} approved pearl(s) to {APPROVED_FILE}.")
    print("These stay separate from the deterministic data/pearls.json by design.")
    return 0


def cmd_adjudicate(args) -> int:
    candidates = load_full_candidates()
    if args.action == "approved" and not args.reviewer:
        print("Approval requires --reviewer so human sign-off is attributable.")
        return 1
    if args.episode is None and not args.statement:
        print("Pass --episode or --statement to select candidates; refusing a bulk decision.")
        return 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    touched = 0
    for candidate in candidates:
        if args.episode is not None and candidate.get("episode_number") != args.episode:
            continue
        if args.statement and args.statement.lower() not in candidate.get("statement", "").lower():
            continue
        touched += 1
        if args.action == "reset":
            candidate["review_status"] = "pending"
            candidate.pop("reviewed_at", None)
            candidate.pop("reviewed_by", None)
            candidate.pop("review_note", None)
        else:
            candidate["review_status"] = args.action
            candidate["reviewed_at"] = now
            if args.reviewer:
                candidate["reviewed_by"] = args.reviewer
            if args.note:
                candidate["review_note"] = args.note
    if touched:
        save_candidate_files(candidates)
    print(f"Updated {touched} candidate(s) in the private review sidecar.")
    return 0


def cmd_migrate_private(args) -> int:
    candidates = load_json(CANDIDATES_FILE, [])
    if not candidates:
        print(f"No candidates in {CANDIDATES_FILE}.")
        return 0
    if not any(candidate.get("supporting_quote") for candidate in candidates):
        print("Tracked candidates are already sanitized.")
        return 0
    save_candidate_files(candidates)
    print(f"Moved full candidate records to ignored {PRIVATE_CANDIDATES_FILE}.")
    print(f"Sanitized {CANDIDATES_FILE} now contains quote hashes/lengths only.")
    return 0


def cmd_prune_gap(args) -> int:
    candidates = load_full_candidates()
    episodes = load_json(EPISODES_FILE, [])
    pearls = load_json(PEARLS_FILE, [])
    transcripts = load_json(TRANSCRIPTS_FILE, [])
    gap_urls = {row["episode_url"] for row in compute_pearl_gap(episodes, pearls, transcripts)}
    kept = [candidate for candidate in candidates if candidate.get("episode_url") in gap_urls]
    save_candidate_files(kept)
    print(f"Kept {len(kept)} candidates for current zero-pearl episodes; pruned {len(candidates) - len(kept)} stale candidates.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="Draft candidate pearls from transcripts")
    g.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model (default claude-opus-4-8)")
    g.add_argument("--episode", type=int, default=None, help="Only this episode number")
    g.add_argument("--limit", type=int, default=None, help="At most N eligible episodes")
    g.add_argument("--source", choices=["official", "youtube", "all"], default="official",
                   help="Which transcript source to read (default official, highest fidelity)")
    g.add_argument("--include-ai", action="store_true", help="Also use AI-generated transcripts (risky)")
    g.add_argument("--keep-unverified", action="store_true",
                   help="Keep candidates whose quote isn't verbatim (default drops them)")
    g.add_argument("--refresh", action="store_true", help="Regenerate episodes that already have candidates")
    g.set_defaults(func=cmd_generate)

    s = sub.add_parser("submit-batch", help="Submit the same eligible pool via the Batch API (50% cheaper)")
    s.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model (default claude-opus-4-8)")
    s.add_argument("--episode", type=int, default=None, help="Only this episode number")
    s.add_argument("--limit", type=int, default=None, help="At most N eligible episodes")
    s.add_argument("--source", choices=["official", "youtube", "all"], default="official",
                   help="Which transcript source to read (default official, highest fidelity)")
    s.add_argument("--include-ai", action="store_true", help="Also use AI-generated transcripts (risky)")
    s.add_argument("--refresh", action="store_true", help="Include episodes that already have candidates")
    s.set_defaults(func=cmd_submit_batch)

    c = sub.add_parser("collect", help="Retrieve batch results and write them to candidate_pearls.json")
    c.add_argument("--wait", action="store_true", help="Poll until the batch ends instead of reporting once")
    c.add_argument("--poll-interval", type=int, default=60, help="Seconds between polls when --wait")
    c.add_argument("--max-wait-minutes", type=int, default=120, help="Give up waiting after this many minutes")
    c.add_argument("--keep-unverified", action="store_true",
                   help="Keep candidates whose quote isn't verbatim (default drops them)")
    c.set_defaults(func=cmd_collect)

    r = sub.add_parser("report", help="Print candidate counts and review status")
    r.set_defaults(func=cmd_report)

    a = sub.add_parser("adjudicate", help="Approve/reject/reset candidates in the private review sidecar")
    a.add_argument("--episode", type=int, default=None)
    a.add_argument("--statement", default=None, help="Statement substring selector")
    action = a.add_mutually_exclusive_group(required=True)
    action.add_argument("--approve", dest="action", action="store_const", const="approved")
    action.add_argument("--reject", dest="action", action="store_const", const="rejected")
    action.add_argument("--reset", dest="action", action="store_const", const="reset")
    a.add_argument("--reviewer", default=None, help="Reviewer name/handle (required for approval)")
    a.add_argument("--note", default=None)
    a.set_defaults(func=cmd_adjudicate)

    p = sub.add_parser("promote", help="Copy approved candidates to approved_pearls.json")
    p.add_argument("--allow-unverified", action="store_true",
                   help="Allow promoting approved candidates whose quote wasn't verified")
    p.set_defaults(func=cmd_promote)

    m = sub.add_parser("migrate-private", help="Move tracked transcript quotes into ignored private review data")
    m.set_defaults(func=cmd_migrate_private)

    q = sub.add_parser("prune-gap", help="Remove candidates for episodes that now have show-note pearls")
    q.set_defaults(func=cmd_prune_gap)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
