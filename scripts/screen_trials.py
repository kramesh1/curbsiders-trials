"""
Owner-gated research-screening pass: a structured PICO + quality summary for
each cited trial, grounded in the real PubMed abstract when one resolves.

scripts/trial_detail_utils.py deliberately defers PICO (population /
intervention / comparator / outcome) extraction to "a future model-backed
pass," to avoid inventing clinical detail the show notes never stated. This
is that pass. Fenced the same way as the rest of the model work in this repo:

  1. GROUNDED WHERE POSSIBLE. When a citation resolves to a PubMed ID
     (scripts/pubmed_utils.resolve_pmid), the model is given the real fetched
     abstract and asked to summarize ONLY that text. When no PMID resolves,
     it falls back to the podcast's own show-notes gloss and is explicitly
     told to be more conservative -- every record carries a grounded_in flag
     ("pubmed_abstract" | "show_notes_only") so the site can show which is
     which.
  2. NULL DISCIPLINE. The prompt requires null (not a guess) for any PICO
     field the source text doesn't state.
  3. OWNER-GATED. Output goes to its own sidecar, data/trial_screening.json,
     with review_status="pending". It NEVER writes docs/data/trials.json.
     Not part of ingest.py -- it spends tokens and makes external network
     calls to NCBI, and must be run deliberately. `apply` copies
     review_status="approved" records to data/trial_screening_approved.json;
     build_site.py picks that up if present.

Model defaults to claude-opus-4-8; override with --model. Requires ANTHROPIC_API_KEY.

Usage:
  python scripts/screen_trials.py generate --limit 5           # first 5 eligible trials
  python scripts/screen_trials.py generate --trial <canonical_key>
  python scripts/screen_trials.py generate --source pubmed      # skip un-groundable trials
  python scripts/screen_trials.py generate --source show_notes  # force the fallback (spot-check it)
  python scripts/screen_trials.py report
  python scripts/screen_trials.py adjudicate --trial "SPRINT" --approve
  python scripts/screen_trials.py apply
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone

try:
    from scripts.extract_trials import DATA_DIR, load_json, save_json
    from scripts.trial_utils import build_canonical_trial_records, clean_text
    from scripts.pubmed_utils import resolve_pmid, fetch_pubmed_abstract
except ImportError:
    from extract_trials import DATA_DIR, load_json, save_json
    from trial_utils import build_canonical_trial_records, clean_text
    from pubmed_utils import resolve_pmid, fetch_pubmed_abstract

TRIALS_FILE = DATA_DIR / "trials.json"
SCREENING_FILE = DATA_DIR / "trial_screening.json"
APPROVED_FILE = DATA_DIR / "trial_screening_approved.json"
DEFAULT_MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = """\
You are an evidence-based-medicine reviewer summarizing a clinical study for a bedside \
teaching reference used by residents.

Rules that matter more than anything else:
- Decompose the study into PICO: population, intervention, comparator, outcome. Use only \
what the given text actually states.
- If the given text does not state a detail, return null for that field. Do NOT infer, \
assume, or fill in a typical/expected value -- an absent detail must stay null.
- Note a quality/limitation signal only if the text itself states it (e.g. open-label vs \
blinded, funding source, an explicit limitation the authors name, early stopping). Do not \
invent a generic caveat that isn't actually in the text.
- Do not add drug names, numbers, or claims not present in the given text.

Return ONLY a JSON object of the form:
  {"population": "...", "intervention": "...", "comparator": "...", "outcome": "...", \
"study_quality_limitations": "...", "confidence": "high|medium|low"}
Use null (not an empty string) for any field the text does not support.
No prose before or after the JSON.
"""


def _parse_json_object(text: str) -> dict:
    """Parse a single JSON object from a model response, tolerating extra wrapping."""
    text = (text or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return {}
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def build_screening_prompt(trial: dict, abstract: dict | None) -> tuple[str, str]:
    """Return (user_content, grounded_in) for one trial's screening request."""
    if abstract:
        meta = [m for m in (abstract.get("journal"), str(abstract["year"]) if abstract.get("year") else None,
                            ", ".join(abstract.get("publication_types") or [])) if m]
        header = abstract.get("title") or clean_text(trial.get("paper_title")) or clean_text(trial.get("citation_label"))
        content = (
            f"Study: {header}\n"
            f"({'; '.join(meta)})\n\n"
            f"Published abstract:\n{abstract['abstract']}\n\n"
            "Summarize this study's PICO and any stated quality signals, using only the abstract above."
        )
        return content, "pubmed_abstract"

    bits = []
    label = clean_text(trial.get("citation_label"))
    if label:
        bits.append(f"Citation: {label}")
    title = clean_text(trial.get("paper_title"))
    if title:
        bits.append(f"Title: {title}")
    summary = clean_text(trial.get("brief_summary"))
    if summary:
        bits.append(f"Podcast's own gloss on this study: {summary}")
    topic = clean_text(trial.get("context_topic"))
    if topic:
        bits.append(f"Discussed in context of: {topic}")
    if trial.get("study_type"):
        bits.append(f"Study type (as tagged): {trial['study_type']}")
    content = (
        "\n".join(bits) + "\n\n"
        "NOTE: no PubMed abstract was available for this citation -- the text above is only the "
        "podcast's own secondhand description, not the actual paper. Be maximally conservative: "
        "most fields should likely be null unless the text above genuinely states that detail. "
        "Do not fill in what a study \"probably\" found or used."
    )
    return content, "show_notes_only"


def generate_for_trial(client, model: str, trial: dict, abstract: dict | None, pmid: str | None) -> dict:
    content, grounded_in = build_screening_prompt(trial, abstract)
    message = client.messages.create(
        model=model,
        max_tokens=1200,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    raw = next((b.text for b in message.content if b.type == "text"), "")
    fields = _parse_json_object(raw)
    confidence = fields.get("confidence")
    return {
        "canonical_key": trial["canonical_key"],
        "citation_label": clean_text(trial.get("citation_label")),
        "grounded_in": grounded_in,
        "pmid": pmid,
        "population": clean_text(fields.get("population")),
        "intervention": clean_text(fields.get("intervention")),
        "comparator": clean_text(fields.get("comparator")),
        "outcome": clean_text(fields.get("outcome")),
        "study_quality_limitations": clean_text(fields.get("study_quality_limitations")),
        "confidence": confidence if confidence in ("high", "medium", "low") else None,
        "generated_by": model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "review_status": "pending",
    }


def cmd_generate(args) -> int:
    trials = load_json(TRIALS_FILE, [])
    if not trials:
        print(f"No trials in {TRIALS_FILE}. Run extract_trials.py first.")
        return 1

    canonical = [t for t in build_canonical_trial_records(trials) if t.get("canonical_key")]
    if args.trial is not None:
        canonical = [t for t in canonical if t["canonical_key"] == args.trial]
        if not canonical:
            print(f"No canonical trial with key {args.trial!r}.")
            return 1

    existing = load_json(SCREENING_FILE, [])
    done_keys = {r["canonical_key"] for r in existing}
    if not args.refresh:
        canonical = [t for t in canonical if t["canonical_key"] not in done_keys]
    if args.limit is not None:
        canonical = canonical[: args.limit]

    if not canonical:
        print("Nothing to generate (all eligible trials already screened).")
        return 0

    try:
        import anthropic
    except ImportError:
        print("Error: the anthropic package is required (pip install anthropic).")
        return 1
    client = anthropic.Anthropic()

    print(f"Screening {len(canonical)} trial(s) with {args.model} (source={args.source}).")
    print("Owner-gated: writes a sidecar, never docs/data/trials.json directly.\n")

    regenerated = {t["canonical_key"] for t in canonical}
    kept = [r for r in existing if r["canonical_key"] not in regenerated]
    added = 0
    skipped_no_source = 0
    for i, trial in enumerate(canonical):
        key = trial["canonical_key"]
        pmid = None
        abstract = None
        if args.source in ("all", "pubmed"):
            pmid = resolve_pmid(trial.get("pubmed_url"))
            if pmid:
                abstract = fetch_pubmed_abstract(pmid)
        if args.source == "pubmed" and abstract is None:
            skipped_no_source += 1
            print(f"  [{i+1}/{len(canonical)}] {key}: no PubMed abstract resolvable, skipped (--source pubmed)")
            continue
        try:
            record = generate_for_trial(client, args.model, trial, abstract, pmid)
        except Exception as error:  # noqa: BLE001 - keep processing the rest
            print(f"  [{i+1}/{len(canonical)}] {key}: error {type(error).__name__}: {error}")
            continue
        kept.append(record)
        added += 1
        print(f"  [{i+1}/{len(canonical)}] {key}: grounded_in={record['grounded_in']}")
        save_json(SCREENING_FILE, kept)

    print(f"\nDone. {added} trial(s) screened, written to {SCREENING_FILE}.")
    if skipped_no_source:
        print(f"Skipped {skipped_no_source} trial(s) with no resolvable PubMed abstract.")
    print("Review them (set review_status to \"approved\"), then run: python scripts/screen_trials.py apply")
    return 0


def cmd_report(args) -> int:
    from collections import Counter

    records = load_json(SCREENING_FILE, [])
    trials = load_json(TRIALS_FILE, [])
    canonical = [t for t in build_canonical_trial_records(trials) if t.get("canonical_key")]
    if not records:
        print(f"No screening records yet ({SCREENING_FILE} is empty). Run generate first.")
        resolvable = sum(1 for t in canonical if resolve_pmid(t.get("pubmed_url")))
        print(f"PMID-resolvable pool: {resolvable}/{len(canonical)} canonical trials.")
        return 0

    status = Counter(r.get("review_status") for r in records)
    grounded = Counter(r.get("grounded_in") for r in records)
    resolvable = sum(1 for t in canonical if resolve_pmid(t.get("pubmed_url")))

    print("=== Trial screening ===")
    print(f"  Screened:             {len(records)}/{len(canonical)} canonical trials")
    print(f"  Grounded in:          {dict(grounded)}")
    print(f"  Review status:        {dict(status)}")
    print(f"  PMID-resolvable pool: {resolvable}/{len(canonical)}")
    approved = load_json(APPROVED_FILE, [])
    print(f"  Applied (approved):   {len(approved)} in {APPROVED_FILE.name}")
    return 0


def cmd_adjudicate(args) -> int:
    records = load_json(SCREENING_FILE, [])
    if not records:
        print(f"No screening records to adjudicate ({SCREENING_FILE} is empty). Run generate first.")
        return 1
    if args.action is None:
        print("Pass an action: --approve, --reject, or --reset.")
        return 1
    if not args.trial and not args.canonical_key:
        print("Pass --trial <substring> or --canonical-key <exact key> to select records.")
        return 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    touched = 0
    for record in records:
        if args.canonical_key is not None and record.get("canonical_key") != args.canonical_key:
            continue
        if args.trial is not None:
            haystack = f"{record.get('citation_label', '')}\n{record.get('canonical_key', '')}".lower()
            if args.trial.lower() not in haystack:
                continue
        touched += 1
        if args.action == "reset":
            record["review_status"] = "pending"
            record.pop("reviewed_at", None)
        else:
            record["review_status"] = args.action
            record["reviewed_at"] = now

    if touched:
        save_json(SCREENING_FILE, records)
    print(f"Updated {touched} record(s).")
    return 0


def cmd_apply(args) -> int:
    records = load_json(SCREENING_FILE, [])
    approved = [r for r in records if r.get("review_status") == "approved"]
    if not approved:
        print("No records marked review_status=\"approved\". Nothing to apply.")
        return 0
    save_json(APPROVED_FILE, approved)
    print(f"Applied {len(approved)} approved screening record(s) -> {APPROVED_FILE}")
    print("Run scripts/build_site.py to surface them on the site.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="Draft PICO/quality screening for canonical trials")
    g.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model (default claude-opus-4-8)")
    g.add_argument("--trial", default=None, help="Only this canonical_key")
    g.add_argument("--limit", type=int, default=None, help="At most N eligible trials")
    g.add_argument("--source", choices=["all", "pubmed", "show_notes"], default="all",
                   help="all=abstract when resolvable else show-notes fallback (default); "
                        "pubmed=skip trials with no resolvable abstract; "
                        "show_notes=force the fallback even when a PMID resolves")
    g.add_argument("--refresh", action="store_true", help="Regenerate trials that already have a screening record")
    g.set_defaults(func=cmd_generate)

    r = sub.add_parser("report", help="Print screening counts and review status")
    r.set_defaults(func=cmd_report)

    a = sub.add_parser("adjudicate", help="Approve/reject/reset individual screening records")
    a.add_argument("--trial", default=None, help="Only records whose citation_label/canonical_key contains this")
    a.add_argument("--canonical-key", dest="canonical_key", default=None, help="Only the record with this exact key")
    action = a.add_mutually_exclusive_group()
    action.add_argument("--approve", dest="action", action="store_const", const="approved")
    action.add_argument("--reject", dest="action", action="store_const", const="rejected")
    action.add_argument("--reset", dest="action", action="store_const", const="reset")
    a.set_defaults(func=cmd_adjudicate, action=None)

    p = sub.add_parser("apply", help="Copy approved records to data/trial_screening_approved.json")
    p.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
