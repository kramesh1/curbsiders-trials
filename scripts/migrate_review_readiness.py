"""Idempotent migrations for the review-readiness hardening release.

- Downgrade automated/spot-checked pearl evidence from ``approved`` to
  ``auto_triaged``.
- Remove database landing/search URLs that do not identify a publication.
- Repair pearl-link canonical keys against the cleaned episode trial pool.

This intentionally does not manufacture human approval.
"""

from collections import defaultdict

try:
    from scripts.extract_trials import DATA_DIR, load_json, save_json
    from scripts.evidence_repository import build_evidence_repository
    from scripts.link_pearls_evidence import LINKS_FILE, _citation_view
    from scripts.pearl_utils import trial_canonical_key
    from scripts.trial_utils import normalize_key_text, normalize_pubmed_url
except ImportError:
    from extract_trials import DATA_DIR, load_json, save_json
    from evidence_repository import build_evidence_repository
    from link_pearls_evidence import LINKS_FILE, _citation_view
    from pearl_utils import trial_canonical_key
    from trial_utils import normalize_key_text, normalize_pubmed_url

TRIALS_FILE = DATA_DIR / "trials.json"
EPISODES_FILE = DATA_DIR / "episodes.json"
SCREENING_FILE = DATA_DIR / "trial_screening.json"


def _candidate_matches(link: dict, trial: dict) -> bool:
    link_title = normalize_key_text(link.get("paper_title"))
    trial_title = normalize_key_text(trial.get("paper_title"))
    if link_title and trial_title and link_title == trial_title:
        return True
    link_label = normalize_key_text(link.get("citation_label"))
    trial_label = normalize_key_text(trial.get("citation_label"))
    return bool(link_label and trial_label and link_label == trial_label)


def migrate() -> dict[str, int]:
    trials = load_json(TRIALS_FILE, [])
    removed_generic_urls = 0
    for trial in trials:
        old_url = trial.get("pubmed_url")
        new_url = normalize_pubmed_url(old_url)
        if old_url and not new_url:
            removed_generic_urls += 1
        trial["pubmed_url"] = new_url
    save_json(TRIALS_FILE, trials)

    trials_by_episode: dict[str, list[dict]] = defaultdict(list)
    for trial in trials:
        if trial.get("episode_url"):
            trials_by_episode[trial["episode_url"]].append(trial)

    records = load_json(LINKS_FILE, [])
    downgraded = 0
    repaired_keys = 0
    unresolved_keys = 0
    for record in records:
        note = (record.get("review_note") or "").lower()
        if record.get("review_status") == "approved" and (
            "auto-clear" in note or "spot-check" in note or not record.get("reviewed_by")
        ):
            record["review_status"] = "auto_triaged"
            record["triaged_at"] = record.pop("reviewed_at", None)
            downgraded += 1

        pool = trials_by_episode.get(record.get("episode_url"), [])
        for link in record.get("links", []):
            matches = [trial for trial in pool if _candidate_matches(link, trial)]
            keys = {trial_canonical_key(trial) for trial in matches}
            keys.discard(None)
            if len(keys) == 1:
                trial = next(trial for trial in matches if trial_canonical_key(trial) in keys)
                old_key = link.get("canonical_key")
                preserved = {
                    key: link.get(key) for key in
                    ("support", "confidence", "rationale", "review_status", "reviewed_at", "review_note")
                    if link.get(key) is not None
                }
                link.clear()
                link.update(_citation_view(trial, next(iter(keys))))
                link.update(preserved)
                repaired_keys += old_key != link.get("canonical_key")
            elif not trial_canonical_key(link):
                unresolved_keys += 1

    save_json(LINKS_FILE, records)

    screening_keys_repaired = 0
    screening = load_json(SCREENING_FILE, [])
    canonical, _, _ = build_evidence_repository(trials, load_json(EPISODES_FILE, []))
    by_key = {row.get("canonical_key"): row for row in canonical}
    by_label: dict[str, list[dict]] = defaultdict(list)
    for row in canonical:
        label = normalize_key_text(row.get("citation_label"))
        if label:
            by_label[label].append(row)
    for record in screening:
        match = by_key.get(record.get("canonical_key"))
        if match is None and record.get("pmid"):
            match = by_key.get(f"pmid|{record['pmid']}")
        if match is None:
            label_matches = by_label.get(normalize_key_text(record.get("citation_label")), [])
            if len(label_matches) == 1:
                match = label_matches[0]
        if match and match.get("canonical_key") != record.get("canonical_key"):
            record["canonical_key"] = match["canonical_key"]
            record["citation_label"] = match.get("citation_label")
            screening_keys_repaired += 1
    if screening:
        save_json(SCREENING_FILE, screening)
    return {
        "generic_urls_removed": removed_generic_urls,
        "auto_approvals_downgraded": downgraded,
        "link_keys_repaired": repaired_keys,
        "link_keys_unresolved": unresolved_keys,
        "screening_keys_repaired": screening_keys_repaired,
    }


if __name__ == "__main__":
    for key, value in migrate().items():
        print(f"{key}: {value}")
