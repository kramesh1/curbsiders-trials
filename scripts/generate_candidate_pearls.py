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

By default it only reads high-fidelity official (human/CME-reviewed) transcripts and
skips AI-generated ones (see the ai_generated flag), since ASR text is a poor source
for teaching claims.

Usage:
  python scripts/generate_candidate_pearls.py generate --episode 347   # one episode
  python scripts/generate_candidate_pearls.py generate --limit 5       # first 5 eligible
  python scripts/generate_candidate_pearls.py generate                 # all eligible (spends tokens!)
  python scripts/generate_candidate_pearls.py report                   # counts + review status
  python scripts/generate_candidate_pearls.py promote                  # approved -> approved_pearls.json

Model defaults to claude-opus-4-8; override with --model. Requires ANTHROPIC_API_KEY.
"""

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.extract_trials import DATA_DIR, load_json, save_json, parse_json_response
    from scripts.fetch_transcripts import TRANSCRIPTS_FILE
except ImportError:
    from extract_trials import DATA_DIR, load_json, save_json, parse_json_response
    from fetch_transcripts import TRANSCRIPTS_FILE

CANDIDATES_FILE = DATA_DIR / "candidate_pearls.json"
APPROVED_FILE = DATA_DIR / "approved_pearls.json"
DEFAULT_MODEL = "claude-opus-4-8"
MIN_QUOTE_CHARS = 15  # a verifiable quote has to be substantial

SYSTEM_PROMPT = """\
You are a careful internal-medicine educator extracting concise, high-yield teaching \
pearls from a podcast episode transcript, for use as quick reference at the bedside.

Rules that matter more than anything else:
- Ground every pearl in the transcript. For each pearl, quote the exact span of the \
transcript it comes from, copied VERBATIM (character for character) into \
"supporting_quote". Do not paraphrase the quote, fix its grammar, or merge \
non-adjacent spans. If you cannot support a statement with a verbatim quote, do not \
include it.
- Prefer specific, actionable clinical points (management thresholds, drug choices, \
dosing, test interpretation, guideline changes) over generic background.
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


def generate_for_transcript(client, model: str, transcript: dict) -> list[dict]:
    text = transcript["text"]
    message = client.messages.create(
        model=model,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Episode #{transcript.get('episode_number')}: {transcript.get('title','')}\n\n"
                f"Transcript:\n{text}"
            ),
        }],
    )
    raw = next((b.text for b in message.content if b.type == "text"), "")
    # parse_json_response tolerates code fences and returns the list inside a
    # {"pearls": [...]} wrapper object.
    pearls = parse_json_response(raw)

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
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
    return records


def cmd_generate(args) -> int:
    transcripts = load_json(TRANSCRIPTS_FILE, [])
    if not transcripts:
        print(f"No transcripts in {TRANSCRIPTS_FILE}. Run fetch_transcripts.py first.")
        return 1

    pool = eligible_transcripts(transcripts, include_ai=args.include_ai, source=args.source)
    if args.episode is not None:
        pool = [t for t in pool if t.get("episode_number") == args.episode]
        if not pool:
            print(f"No eligible transcript for episode #{args.episode}.")
            return 1

    # Don't regenerate episodes we already have candidates for (unless --refresh).
    existing = load_json(CANDIDATES_FILE, [])
    done_urls = {c["episode_url"] for c in existing}
    if not args.refresh:
        pool = [t for t in pool if t["episode_url"] not in done_urls]
    if args.limit is not None:
        pool = pool[: args.limit]

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
        save_json(CANDIDATES_FILE, kept)

    print(f"\nDone. {added} candidate pearls written to {CANDIDATES_FILE}.")
    if dropped_unverified and not args.keep_unverified:
        print(f"Dropped {dropped_unverified} candidate(s) whose quote wasn't verbatim in the transcript.")
    print("Review them (set review_status to \"approved\"), then run: "
          "python scripts/generate_candidate_pearls.py promote")
    return 0


def cmd_report(args) -> int:
    from collections import Counter

    candidates = load_json(CANDIDATES_FILE, [])
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
    candidates = load_json(CANDIDATES_FILE, [])
    approved = [c for c in candidates if c.get("review_status") == "approved"]
    if not approved:
        print("No candidates marked review_status=\"approved\". Nothing to promote.")
        return 0
    # Guard rails: never promote something whose quote isn't verbatim.
    unverified = [c for c in approved if not c.get("quote_verified")]
    if unverified and not args.allow_unverified:
        print(f"Refusing to promote {len(unverified)} approved candidate(s) with an unverified "
              f"quote. Re-review, or pass --allow-unverified to override.")
        return 1
    save_json(APPROVED_FILE, approved)
    print(f"Promoted {len(approved)} approved pearl(s) to {APPROVED_FILE}.")
    print("These stay separate from the deterministic data/pearls.json by design.")
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

    r = sub.add_parser("report", help="Print candidate counts and review status")
    r.set_defaults(func=cmd_report)

    p = sub.add_parser("promote", help="Copy approved candidates to approved_pearls.json")
    p.add_argument("--allow-unverified", action="store_true",
                   help="Allow promoting approved candidates whose quote wasn't verified")
    p.set_defaults(func=cmd_promote)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
