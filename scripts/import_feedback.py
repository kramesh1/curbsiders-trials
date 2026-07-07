"""
Import and adjudicate visitor-submitted feedback on pearls and evidence links.

Site visitors can flag a pearl or a pearl->trial evidence link (see
docs/app.js) with a structured reason code (inaccurate/outdated/wrong_citation/
unclear/other) plus an optional comment. Submissions POST to a small
Cloudflare Worker (worker/, backed by D1 -- see the README's feedback section
for the one-time `wrangler` deploy). This script pulls from that Worker and
folds the result into the same owner-gated review pattern used everywhere
else in this repo:

  1. FETCH. `fetch` pulls new rows (cursor-tracked by id in
     data/feedback_import_state.json; never re-fetches or deletes remote rows)
     from the Worker's admin-only GET /feedback endpoint into
     data/pearl_feedback.json, each with review_status: "pending".
  2. ADJUDICATE. A human marks rows approved/rejected/reset via CLI selectors
     (no free-text browsing needed for bulk triage).
  3. APPLY. Only rows marked "approved" are aggregated -- counts per reason
     code, grouped by pearl (and, for link-level feedback, by pearl+trial) --
     into data/pearl_feedback_approved.json. This is the only feedback
     artifact build_site.py reads; no raw/unmoderated submission is ever
     published.

Not part of ingest.py -- it makes an external network call and needs
credentials (FEEDBACK_WORKER_URL, CLOUDFLARE_FEEDBACK_ADMIN_TOKEN in .env).

Usage:
  python scripts/import_feedback.py fetch                        # pull new rows
  python scripts/import_feedback.py report                       # counts by status/reason
  python scripts/import_feedback.py adjudicate --id 42 --approve
  python scripts/import_feedback.py adjudicate --pearl "SPRINT" --reject --note "wrong trial, not applicable"
  python scripts/import_feedback.py apply                        # -> data/pearl_feedback_approved.json
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

from dotenv import load_dotenv

try:
    from scripts.extract_trials import DATA_DIR, load_json, save_json
except ImportError:
    from extract_trials import DATA_DIR, load_json, save_json

load_dotenv()

FEEDBACK_FILE = DATA_DIR / "pearl_feedback.json"
FEEDBACK_APPROVED_FILE = DATA_DIR / "pearl_feedback_approved.json"
IMPORT_STATE_FILE = DATA_DIR / "feedback_import_state.json"

REASON_CODES = ("inaccurate", "outdated", "wrong_citation", "unclear", "other")


def _worker_url() -> str | None:
    return os.environ.get("FEEDBACK_WORKER_URL")


def _admin_token() -> str | None:
    return os.environ.get("CLOUDFLARE_FEEDBACK_ADMIN_TOKEN")


def _normalize_row(raw: dict, *, imported_at: str) -> dict:
    """Turn one Worker-returned row into a sidecar record."""
    return {
        "id": raw.get("id"),
        "submitted_at": raw.get("submitted_at"),
        "target_type": raw.get("target_type"),
        "pearl_key": raw.get("pearl_key"),
        "pearl_text_snapshot": raw.get("pearl_text_snapshot"),
        "canonical_key": raw.get("canonical_key"),
        "reason_code": raw.get("reason_code"),
        "comment": raw.get("comment"),
        "episode_url": raw.get("episode_url"),
        "review_status": "pending",
        "imported_at": imported_at,
    }


def fetch_new_rows(worker_url: str, admin_token: str, since_id: int) -> list[dict]:
    """GET rows with id > since_id from the Worker's admin endpoint."""
    query = urllib.parse.urlencode({"since_id": since_id})
    request = urllib.request.Request(
        f"{worker_url.rstrip('/')}/feedback?{query}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
    return payload.get("rows", [])


def cmd_fetch(args) -> int:
    worker_url = _worker_url()
    admin_token = _admin_token()
    if not worker_url or not admin_token:
        print("Set FEEDBACK_WORKER_URL and CLOUDFLARE_FEEDBACK_ADMIN_TOKEN in .env first.")
        return 1

    state = load_json(IMPORT_STATE_FILE, {"last_id": 0})
    since_id = state.get("last_id", 0)

    try:
        rows = fetch_new_rows(worker_url, admin_token, since_id)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        print(f"Fetch failed: {type(error).__name__}: {error}")
        return 1

    if not rows:
        print(f"No new feedback since id {since_id}.")
        return 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = load_json(FEEDBACK_FILE, [])
    existing_ids = {row["id"] for row in existing}
    new_records = [_normalize_row(row, imported_at=now) for row in rows if row.get("id") not in existing_ids]
    existing.extend(new_records)
    save_json(FEEDBACK_FILE, existing)

    max_id = max((row.get("id") or 0) for row in rows)
    save_json(IMPORT_STATE_FILE, {"last_id": max(max_id, since_id)})

    print(f"Imported {len(new_records)} new feedback row(s) -> {FEEDBACK_FILE}")
    print(f"Cursor advanced to id {max(max_id, since_id)}.")
    return 0


def cmd_report(args) -> int:
    rows = load_json(FEEDBACK_FILE, [])
    if not rows:
        print(f"No feedback yet ({FEEDBACK_FILE} is empty). Run fetch first.")
        return 0

    status = Counter(row.get("review_status") for row in rows)
    target = Counter(row.get("target_type") for row in rows)
    reason = Counter(row.get("reason_code") for row in rows)
    approved = load_json(FEEDBACK_APPROVED_FILE, [])

    print("=== Visitor feedback ===")
    print(f"  Total rows:          {len(rows)}")
    print(f"  Review status:       {dict(status)}")
    print(f"  Target type:         {dict(target)}")
    print(f"  Reason codes:        {dict(reason)}")
    print(f"  Aggregated/applied:  {len(approved)} pearl/link group(s) in {FEEDBACK_APPROVED_FILE}")
    return 0


def _row_matches(row: dict, sel: dict) -> bool:
    if "id" in sel and row.get("id") != sel["id"]:
        return False
    if "canonical_key" in sel and row.get("canonical_key") != sel["canonical_key"]:
        return False
    if "reason" in sel and row.get("reason_code") != sel["reason"]:
        return False
    if "pearl" in sel:
        hay = f"{row.get('pearl_text_snapshot', '')}\n{row.get('pearl_key', '')}".lower()
        if sel["pearl"].lower() not in hay:
            return False
    return True


def cmd_adjudicate(args) -> int:
    rows = load_json(FEEDBACK_FILE, [])
    if not rows:
        print(f"No feedback to adjudicate ({FEEDBACK_FILE} is empty). Run fetch first.")
        return 1

    sel = {}
    if args.id is not None:
        sel["id"] = args.id
    if args.pearl is not None:
        sel["pearl"] = args.pearl
    if args.canonical_key is not None:
        sel["canonical_key"] = args.canonical_key
    if args.reason is not None:
        sel["reason"] = args.reason

    if args.action is None:
        print("Pass an action: --approve, --reject, or --reset.")
        return 1
    if not sel:
        print("Refusing to adjudicate every row: pass at least one selector "
              "(--id/--pearl/--canonical-key/--reason).")
        return 1

    reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    touched = 0
    for row in rows:
        if not _row_matches(row, sel):
            continue
        touched += 1
        if args.dry_run:
            continue
        if args.action == "reset":
            row["review_status"] = "pending"
            row.pop("reviewed_at", None)
            row.pop("review_note", None)
        else:
            row["review_status"] = args.action
            row["reviewed_at"] = reviewed_at
            if args.note is not None:
                row["review_note"] = args.note

    verb = "Would update" if args.dry_run else "Updated"
    print(f"{verb} {touched} row(s).")
    if touched and not args.dry_run:
        save_json(FEEDBACK_FILE, rows)
        print(f"Wrote {FEEDBACK_FILE}. Re-run `apply` to refresh {FEEDBACK_APPROVED_FILE}.")
    status = Counter(row.get("review_status") for row in rows)
    print(f"Review status now: {dict(status)}")
    return 0


def aggregate_approved(rows: list[dict]) -> list[dict]:
    """Group approved rows into per-(pearl_key[, canonical_key]) flag_summary counts."""
    groups: dict[tuple, Counter] = {}
    for row in rows:
        if row.get("review_status") != "approved":
            continue
        pearl_key = row.get("pearl_key")
        if not pearl_key:
            continue
        canonical_key = row.get("canonical_key") if row.get("target_type") == "pearl_link" else None
        reason = row.get("reason_code") or "other"
        groups.setdefault((pearl_key, canonical_key), Counter())[reason] += 1

    return [
        {"pearl_key": pearl_key, "canonical_key": canonical_key, "flag_summary": dict(counts)}
        for (pearl_key, canonical_key), counts in groups.items()
    ]


def cmd_apply(args) -> int:
    rows = load_json(FEEDBACK_FILE, [])
    if not rows:
        print(f"No feedback to apply ({FEEDBACK_FILE} is empty).")
        return 1

    aggregated = aggregate_approved(rows)
    if not aggregated:
        print('No rows with review_status "approved". Adjudicate some first.')
        return 1

    save_json(FEEDBACK_APPROVED_FILE, aggregated)
    total_flags = sum(count for row in aggregated for count in row["flag_summary"].values())
    print(f"Applied {total_flags} approved flag(s) across {len(aggregated)} pearl/link group(s) "
          f"-> {FEEDBACK_APPROVED_FILE}")
    print("Re-run build_site.py to publish updated flag badges.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="Pull new feedback rows from the Worker into the sidecar")
    f.set_defaults(func=cmd_fetch)

    r = sub.add_parser("report", help="Counts by review status / target type / reason code")
    r.set_defaults(func=cmd_report)

    d = sub.add_parser("adjudicate", help="Approve/reject/reset feedback rows from selectors")
    d.add_argument("--id", type=int, default=None, help="Only the row with this exact id")
    d.add_argument("--pearl", default=None,
                   help="Only rows whose pearl_text_snapshot/pearl_key contains this substring")
    d.add_argument("--canonical-key", dest="canonical_key", default=None,
                   help="Only rows with this exact trial canonical_key")
    d.add_argument("--reason", choices=REASON_CODES, default=None, help="Only rows with this reason_code")
    action = d.add_mutually_exclusive_group()
    action.add_argument("--approve", dest="action", action="store_const", const="approved",
                        help="Approve the matching rows")
    action.add_argument("--reject", dest="action", action="store_const", const="rejected",
                        help="Reject the matching rows (excluded by apply)")
    action.add_argument("--reset", dest="action", action="store_const", const="reset",
                        help="Clear review status back to pending")
    d.add_argument("--note", default=None, help="Free-text review note stored on each touched row")
    d.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    d.set_defaults(func=cmd_adjudicate, action=None)

    a = sub.add_parser("apply", help="Aggregate approved rows into data/pearl_feedback_approved.json")
    a.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
