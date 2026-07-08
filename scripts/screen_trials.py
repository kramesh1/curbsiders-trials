"""
Owner-gated research-screening pass: a structured PICO + clinical-bottom-line
summary for each cited trial, grounded in the real PubMed abstract or, when
open-access, the full paper.

scripts/trial_detail_utils.py deliberately defers PICO (population /
intervention / comparator / outcome) extraction to "a future model-backed
pass," to avoid inventing clinical detail the show notes never stated. This
is that pass. Fenced the same way as the rest of the model work in this repo:

  1. GROUNDED WHERE POSSIBLE. When a citation resolves to a PubMed ID
     (scripts/pubmed_utils.resolve_pmid), the model is given the real fetched
     text and asked to summarize ONLY that text -- the open-access full paper
     from PubMed Central when one resolves (scripts/pubmed_utils.resolve_pmcid
     + fetch_pmc_fulltext), else the abstract, else (when no PMID resolves at
     all) the podcast's own show-notes gloss with an explicit instruction to
     be more conservative. Every record carries a grounded_in flag
     ("pmc_fulltext" | "pubmed_abstract" | "show_notes_only") so the site can
     show which is which.
  2. NULL DISCIPLINE. The prompt requires null (not a guess) for any PICO or
     clinical_bottom_line field the source text doesn't state.
  3. OWNER-GATED. Output goes to its own sidecar, data/trial_screening.json,
     with review_status="pending". It NEVER writes docs/data/trials.json.
     Not part of ingest.py -- it spends tokens and makes external network
     calls to NCBI, and must be run deliberately. `apply` copies
     review_status="approved" records to data/trial_screening_approved.json;
     build_site.py picks that up if present.

Two ways to run the model pass: `generate` (synchronous, one call per trial)
or `submit-batch` + `collect` (Anthropic Message Batches API, 50% cheaper,
recommended for anything beyond a small pilot -- see submit-batch's --help).

Model defaults to claude-sonnet-5 (this pass runs at the scale of thousands of
trials, so cost matters more than squeezing out the last bit of quality);
override with --model claude-opus-4-8 for higher-stakes spot checks. Requires
ANTHROPIC_API_KEY.

Usage:
  python scripts/screen_trials.py generate --limit 5           # first 5 eligible trials
  python scripts/screen_trials.py generate --trial <canonical_key>
  python scripts/screen_trials.py generate --source pubmed      # skip un-groundable trials
  python scripts/screen_trials.py generate --source show_notes  # force the fallback (spot-check it)
  python scripts/screen_trials.py generate --no-fulltext        # abstract-only, skip PMC lookup
  python scripts/screen_trials.py submit-batch --limit 50       # same pool, Batch API (50% cheaper)
  python scripts/screen_trials.py collect --wait                # retrieve batch results
  python scripts/screen_trials.py report
  python scripts/screen_trials.py adjudicate --trial "SPRINT" --approve
  python scripts/screen_trials.py apply
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

try:
    from scripts.extract_trials import DATA_DIR, load_json, save_json
    from scripts.trial_utils import build_canonical_trial_records, clean_text
    from scripts.pubmed_utils import resolve_pmid, fetch_pubmed_abstract, resolve_pmcid, fetch_pmc_fulltext
except ImportError:
    from extract_trials import DATA_DIR, load_json, save_json
    from trial_utils import build_canonical_trial_records, clean_text
    from pubmed_utils import resolve_pmid, fetch_pubmed_abstract, resolve_pmcid, fetch_pmc_fulltext

TRIALS_FILE = DATA_DIR / "trials.json"
SCREENING_FILE = DATA_DIR / "trial_screening.json"
APPROVED_FILE = DATA_DIR / "trial_screening_approved.json"
BATCH_JOB_FILE = DATA_DIR / "trial_screening_batch.json"
DEFAULT_MODEL = "claude-sonnet-5"
# Generous margin: Claude Sonnet 5 runs adaptive thinking by default, and
# thinking tokens draw from this same budget -- a tight cap here truncates the
# JSON response mid-string on trials needing more reasoning (dense abstracts,
# multi-outcome meta-analyses), producing an unparseable response that silently
# becomes an all-null record. Confirmed via a pilot batch: 2/23 trials hit
# stop_reason="max_tokens" at the old 1200 cap.
MAX_TOKENS = 4096

SYSTEM_PROMPT = """\
You are an evidence-based-medicine reviewer summarizing a clinical study for a bedside \
teaching reference used by residents making inpatient management decisions.

Rules that matter more than anything else:
- Decompose the study into PICO: population, intervention, comparator, outcome. Use only \
what the given text actually states.
- Add a "clinical_bottom_line": one or two sentences on the practical inpatient-management \
takeaway a resident could act on -- what changes at the bedside, what threshold or drug \
choice it supports, or what practice it argues against. Base this ONLY on the outcome/result \
the text actually reports, not on what the topic is generally "about." If the text doesn't \
report a result specific enough to support a concrete action, return null rather than a \
generic restatement of the intervention.
- If the given text does not state a detail, return null for that field. Do NOT infer, \
assume, or fill in a typical/expected value -- an absent detail must stay null.
- Note a quality/limitation signal only if the text itself states it (e.g. open-label vs \
blinded, funding source, an explicit limitation the authors name, early stopping). Do not \
invent a generic caveat that isn't actually in the text.
- Do not add drug names, numbers, or claims not present in the given text.

Return ONLY a JSON object of the form:
  {"population": "...", "intervention": "...", "comparator": "...", "outcome": "...", \
"clinical_bottom_line": "...", "study_quality_limitations": "...", "confidence": "high|medium|low"}
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


def resolve_grounding(trial: dict, *, source: str, no_fulltext: bool) -> tuple[str | None, dict | None, str | None]:
    """Resolve (pmid, abstract, fulltext) for one trial. fulltext is the PMC
    open-access body text when one resolves; abstract is always fetched (even
    when fulltext resolves) since it carries title/journal/year metadata the
    PMC XML doesn't cleanly expose. Rate-limited to NCBI's shared throttle."""
    if source not in ("all", "pubmed"):
        return None, None, None
    pmid = resolve_pmid(trial.get("pubmed_url"))
    if not pmid:
        return None, None, None
    abstract = fetch_pubmed_abstract(pmid)
    fulltext = None
    if abstract and not no_fulltext:
        pmcid = resolve_pmcid(pmid)
        if pmcid:
            fulltext = fetch_pmc_fulltext(pmcid)
    return pmid, abstract, fulltext


def build_screening_prompt(trial: dict, abstract: dict | None, fulltext: str | None) -> tuple[str, str]:
    """Return (user_content, grounded_in) for one trial's screening request."""
    if abstract or fulltext:
        meta = [m for m in (abstract.get("journal"), str(abstract["year"]) if abstract.get("year") else None,
                            ", ".join(abstract.get("publication_types") or [])) if m] if abstract else []
        header = (abstract or {}).get("title") or clean_text(trial.get("paper_title")) or clean_text(trial.get("citation_label"))
        if fulltext:
            body_label = "Full text (open-access, via PubMed Central)"
            body = fulltext
            grounded_in = "pmc_fulltext"
        else:
            body_label = "Published abstract"
            body = abstract["abstract"]
            grounded_in = "pubmed_abstract"
        content = (
            f"Study: {header}\n"
            f"({'; '.join(meta)})\n\n"
            f"{body_label}:\n{body}\n\n"
            "Summarize this study's PICO, clinical bottom line, and any stated quality signals, "
            "using only the text above."
        )
        return content, grounded_in

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


def build_screening_record(fields: dict, *, canonical_key: str, citation_label: str, grounded_in: str,
                            pmid: str | None, model: str, generated_at: str) -> dict:
    """Turn one model response's parsed fields into a screening record. Shared
    by the synchronous and batch paths, so both produce identical output."""
    confidence = fields.get("confidence")
    return {
        "canonical_key": canonical_key,
        "citation_label": citation_label,
        "grounded_in": grounded_in,
        "pmid": pmid,
        "population": clean_text(fields.get("population")),
        "intervention": clean_text(fields.get("intervention")),
        "comparator": clean_text(fields.get("comparator")),
        "outcome": clean_text(fields.get("outcome")),
        "clinical_bottom_line": clean_text(fields.get("clinical_bottom_line")),
        "study_quality_limitations": clean_text(fields.get("study_quality_limitations")),
        "confidence": confidence if confidence in ("high", "medium", "low") else None,
        "generated_by": model,
        "generated_at": generated_at,
        "review_status": "pending",
    }


class ScreeningParseError(Exception):
    """The model's response wasn't parseable as a JSON object at all -- usually
    a truncated response (stop_reason="max_tokens"). Distinct from a legitimate
    all-null record, which still parses as a well-formed object; raising here
    (instead of silently writing an all-null record) means a truncated response
    shows up as a retriable failure rather than masquerading as reviewed data."""


def generate_for_trial(client, model: str, trial: dict, abstract: dict | None, fulltext: str | None, pmid: str | None) -> dict:
    content, grounded_in = build_screening_prompt(trial, abstract, fulltext)
    message = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    raw = next((b.text for b in message.content if b.type == "text"), "")
    fields = _parse_json_object(raw)
    if not fields:
        raise ScreeningParseError(
            f"unparseable response ({len(raw)} chars, stop_reason={message.stop_reason})"
        )
    return build_screening_record(
        fields,
        canonical_key=trial["canonical_key"],
        citation_label=clean_text(trial.get("citation_label")),
        grounded_in=grounded_in,
        pmid=pmid,
        model=model,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


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
        pmid, abstract, fulltext = resolve_grounding(trial, source=args.source, no_fulltext=args.no_fulltext)
        if args.source == "pubmed" and abstract is None:
            skipped_no_source += 1
            print(f"  [{i+1}/{len(canonical)}] {key}: no PubMed abstract resolvable, skipped (--source pubmed)")
            continue
        try:
            record = generate_for_trial(client, args.model, trial, abstract, fulltext, pmid)
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


def build_batch_requests(canonical: list[dict], args) -> tuple[list[dict], dict]:
    """One Messages-API batch request per trial, plus a custom_id -> record-context
    map. Resolves PubMed/PMC grounding synchronously first (rate-limited to NCBI's
    3/sec), same as the sync path, so batch and sync produce identical prompts."""
    requests = []
    custom_map: dict[str, dict] = {}
    skipped = 0
    for i, trial in enumerate(canonical):
        pmid, abstract, fulltext = resolve_grounding(trial, source=args.source, no_fulltext=args.no_fulltext)
        if args.source == "pubmed" and abstract is None:
            skipped += 1
            continue
        content, grounded_in = build_screening_prompt(trial, abstract, fulltext)
        custom_id = f"trial-{i:05d}"
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": args.model,
                "max_tokens": MAX_TOKENS,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": content}],
            },
        })
        custom_map[custom_id] = {
            "canonical_key": trial["canonical_key"],
            "citation_label": clean_text(trial.get("citation_label")),
            "grounded_in": grounded_in,
            "pmid": pmid,
        }
        if (i + 1) % 25 == 0:
            print(f"  resolved grounding for {i+1}/{len(canonical)}...")
    if skipped:
        print(f"Skipped {skipped} trial(s) with no resolvable PubMed abstract (--source pubmed).")
    return requests, custom_map


def cmd_submit_batch(args) -> int:
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
        print("Nothing to submit (all eligible trials already screened).")
        return 0

    print(f"Resolving PubMed/PMC grounding for {len(canonical)} trial(s) "
          f"(rate-limited to NCBI's 3/sec -- this can take a while for large batches)...")
    requests, custom_map = build_batch_requests(canonical, args)
    if not requests:
        print("Nothing to submit after grounding resolution.")
        return 0

    try:
        import anthropic
    except ImportError:
        print("Error: the anthropic package is required (pip install anthropic).")
        return 1
    client = anthropic.Anthropic()

    print(f"Submitting a batch of {len(requests)} trial(s) at 50% Batch-API pricing with {args.model}.")
    batch = client.messages.batches.create(requests=requests)

    job = {
        "batch_id": batch.id,
        "model": args.model,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "custom_map": custom_map,
        # Sanity check for collect: only valid if trials.json hasn't changed
        # between submit and collect.
        "fingerprint": {"trials": len(trials)},
    }
    save_json(BATCH_JOB_FILE, job)

    print(f"  batch id:          {batch.id}")
    print(f"  processing status: {batch.processing_status}")
    print(f"  job saved to:      {BATCH_JOB_FILE}")
    print("\nCollect results when the batch ends (usually <1h) with:")
    print("  python scripts/screen_trials.py collect --wait")
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

    trials = load_json(TRIALS_FILE, [])
    fingerprint = job.get("fingerprint") or {}
    if fingerprint and fingerprint.get("trials") != len(trials):
        print("WARNING: trials.json changed since submit; canonical_key mapping may be off. "
              "Consider re-submitting rather than trusting these results.")

    custom_map = job["custom_map"]
    model = job.get("model")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    batch_keys = {entry["canonical_key"] for entry in custom_map.values()}
    existing = load_json(SCREENING_FILE, [])
    kept = [r for r in existing if r["canonical_key"] not in batch_keys]

    added = 0
    errored = 0
    for result in client.messages.batches.results(batch_id):
        entry = custom_map.get(result.custom_id)
        if entry is None:
            continue
        if result.result.type != "succeeded":
            errored += 1
            print(f"  {result.custom_id} ({result.result.type}): skipped")
            continue
        message = result.result.message
        raw = next((b.text for b in message.content if b.type == "text"), "")
        fields = _parse_json_object(raw)
        if not fields:
            errored += 1
            print(f"  {result.custom_id} ({entry['canonical_key']}): unparseable response "
                  f"({len(raw)} chars, stop_reason={message.stop_reason}), skipped")
            continue
        record = build_screening_record(
            fields,
            canonical_key=entry["canonical_key"],
            citation_label=entry["citation_label"],
            grounded_in=entry["grounded_in"],
            pmid=entry["pmid"],
            model=model,
            generated_at=now,
        )
        kept.append(record)
        added += 1

    save_json(SCREENING_FILE, kept)
    print(f"\nDone. {added} trial(s) screened, written to {SCREENING_FILE}.")
    if errored:
        print(f"{errored} request(s) errored and were skipped.")
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
    bottom_lined = sum(1 for r in records if r.get("clinical_bottom_line"))

    print("=== Trial screening ===")
    print(f"  Screened:             {len(records)}/{len(canonical)} canonical trials")
    print(f"  Grounded in:          {dict(grounded)}")
    print(f"  With clinical bottom line: {bottom_lined}/{len(records)}")
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

    def add_selection_args(sp):
        sp.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model (default claude-sonnet-5)")
        sp.add_argument("--trial", default=None, help="Only this canonical_key")
        sp.add_argument("--limit", type=int, default=None, help="At most N eligible trials")
        sp.add_argument("--source", choices=["all", "pubmed", "show_notes"], default="all",
                       help="all=PMC full text or abstract when resolvable else show-notes fallback (default); "
                            "pubmed=skip trials with no resolvable abstract; "
                            "show_notes=force the fallback even when a PMID resolves")
        sp.add_argument("--no-fulltext", action="store_true",
                       help="Abstract only -- skip the PMC open-access full-text lookup")
        sp.add_argument("--refresh", action="store_true", help="Regenerate trials that already have a screening record")

    g = sub.add_parser("generate", help="Draft PICO/clinical-bottom-line screening for canonical trials")
    add_selection_args(g)
    g.set_defaults(func=cmd_generate)

    s = sub.add_parser("submit-batch", help="Submit the same eligible pool via the Batch API (50%% cheaper)")
    add_selection_args(s)
    s.set_defaults(func=cmd_submit_batch)

    c = sub.add_parser("collect", help="Retrieve batch results and write them to trial_screening.json")
    c.add_argument("--wait", action="store_true", help="Poll until the batch ends instead of reporting once")
    c.add_argument("--poll-interval", type=int, default=60, help="Seconds between polls when --wait")
    c.add_argument("--max-wait-minutes", type=int, default=120, help="Give up waiting after this many minutes")
    c.set_defaults(func=cmd_collect)

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
