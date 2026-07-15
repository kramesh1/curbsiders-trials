# Clinical Review Handoff

## What is safe to review now

The automated ingestion and deterministic build are operational and reproducible. The public site currently contains show-note pearls, extracted evidence records, and source backlinks. It intentionally contains no model-suggested pearl-to-evidence link and no trial-screening summary presented as human-reviewed.

This project is independent and is not currently endorsed or reviewed by The Curbsiders.

## Review states

| Status | Meaning | Publicly publishable |
|---|---|---|
| `pending` | Model output has not been clinically triaged | No |
| `auto_triaged` | Passed an automated rule or limited spot check | No |
| `rejected` | Reviewer rejected the record/link | No |
| `approved` + `reviewed_by` | Attributable human review completed | Yes, subject to quality filters |

The historical `398` bulk "approved" pearl-link records were migrated to
`auto_triaged`; the prior process was an automated high-confidence/direct-support
rule with a 25-record spot check, not record-by-record human sign-off.

## Suggested first review packet

1. Select 25-50 pearls across inpatient, outpatient, preventive, diagnostic, and guideline topics.
2. For each proposed link, read the primary source (or at minimum its abstract) and the episode show-note context.
3. Reject topical-but-not-supportive links. Background association should not be treated as direct practice evidence.
4. Sign a whole pearl record only after every surviving link is acceptable:

```bash
python scripts/link_pearls_evidence.py adjudicate --episode 500 --trial "<citation>" --reject --note "<reason>"
python scripts/link_pearls_evidence.py adjudicate --episode 500 --record --approve --reviewer "<name>" --note "checked against source and show notes"
python scripts/link_pearls_evidence.py apply
python scripts/build_site.py
python scripts/validate_repository.py
```

## Trial-Summary Pilot Review

The 23 existing screening records are pending and unpublished. Review population, intervention, comparator, outcome, applicability, limitations, and bottom line. In particular, reject recommendations extrapolated from observational associations or from a different care setting.

```bash
python scripts/screen_trials.py report
python scripts/screen_trials.py adjudicate --trial "<citation>" --approve --reviewer "<name>"
python scripts/screen_trials.py apply
python scripts/build_site.py
```

Approved trial summaries feed the next pearl-linking run, giving the model the paper-grounded outcome and applicability rather than only the original extraction gloss.

## Operational Checks

```bash
python scripts/ingest.py --dry-run       # performs real RSS discovery
python -m unittest discover -s tests
python -m compileall -q scripts tests
python scripts/validate_repository.py
```

The scheduled wrapper requires a populated `.venv`; install the launchd/cron job
separately using [automation/README.md](automation/README.md). Public feedback
remains hidden until the Worker/D1 endpoint is deployed and configured.

## Known Curation Debt

- `3185/6771` evidence records remain `study_type: other`.
- Only `1265/6817` extracted mentions have a journal and `2/6817` have a parsed sample size; paper-grounded screening is the intended higher-quality enrichment path.
- `126/558` episode dates are still unknown, concentrated in older/non-numbered pages outside the current RSS window.
- Seven citation labels remain too generic for clinician recognition; validation reports them as warnings.
- `181/558` episodes have no recognizable show-note pearl section; 148 of those have a transcript and their candidates remain private and unapproved.

These are visible review queues, not publication blockers or claims of clinical validation.
