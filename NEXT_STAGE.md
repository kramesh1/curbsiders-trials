# Next Stage

This repository is now past the extraction/backfill phase. The next stage is QA,
reconciliation, enrichment, and curation for teaching use.

## Current State

- `555` Curbsiders episodes are scraped and marked completed.
- `6793` model-extracted trial/evidence mentions are stored in `data/trials.json`.
- `6243` model-extracted canonical records are merged with the deterministic
  show-note hyperlink layer into `6785` published evidence records in
  `docs/data/trials.json`.
- `data/show_note_evidence.json` contains `5887` canonical cited evidence links
  from actual show-note hyperlinks, representing `9225` hyperlink mentions across
  `525` episodes.
- Of those show-note evidence records, `5345` match existing extracted records and
  `542` are show-note-only records that the model extractor missed.
- `2089` raw show-note pearls canonicalize to `2062` published pearls in
  `docs/data/pearls.json`.
- `372` canonical pearls have reviewed evidence links, and `471` evidence records
  have reverse linked-pearl backlinks in the evidence browser.
- Validation passes, with a soft warning for `7` vague citation labels that remain
  a review queue.

## Core Architecture

### Evidence layer

There are now two evidence sources:

1. `data/trials.json`
   Model-extracted episode-level evidence mentions, with summaries, study types,
   topics, specialties, and episode context.

2. `data/show_note_evidence.json`
   Deterministic inventory of likely clinical-evidence hyperlinks actually present
   in show notes. Stable keys are based on PMID, DOI, PMCID, NCT ID, or normalized
   URL. This layer is the source of truth for "what Curbsiders linked to."

`scripts/build_site.py` merges both into `docs/data/trials.json`. Records can carry
`show_note_citations`, `source_layers`, and `linked_pearls`.

### Pearl layer

`scripts/extract_pearls.py` deterministically extracts verbatim show-note pearls
into `data/pearls.json`. Its `supporting_citations` field is only a term-overlap
audit aid; it is not treated as reviewed teaching evidence.

`scripts/link_pearls_evidence.py` owns the model-assisted, owner-gated
pearl-to-evidence sidecar:

- Draft links live in `data/pearl_evidence_links.json`.
- Reviewed/published links are applied into `data/pearls_linked.json`.
- `apply` publishes only record-approved, direct, non-low-confidence links by
  default.
- `build_site.py` canonicalizes the linked pearls and also repairs stale reviewed
  canonical keys at publish time when they match current evidence records.

### Browser behavior

The static site now supports both directions:

- `Teaching pearls` shows pearl -> reviewed evidence links.
- `Evidence browser` shows evidence -> reviewed teaching pearl backlinks.
- Show-note-only evidence records still appear in the evidence browser with source
  links and episode backlinks, even when the model extractor did not summarize
  them originally.

## Recommended QA Sequence

1. Review newest episodes first.
   Start with episodes `530` through `521`. Compare show notes, `data/trials.json`,
   `data/show_note_evidence.json`, and the published `docs/data/trials.json`.

2. Reconcile show-note-only records.
   The `542` `source_layers: ["show_notes_links"]` records are the highest-value
   gap queue. Decide whether each should remain source-only, merge into an existing
   canonical record, or be enriched with a better PMID/DOI/title.

3. Review evidence records linked to pearls.
   The browser now surfaces `471` evidence records with linked pearls. These are
   high leverage because they directly affect teaching workflows.

4. Tighten `study_type = "other"`.
   Current published distribution still has many `other` records. Some are real
   background sources, but many should likely become guideline, observational,
   RCT, systematic review, or meta-analysis after review.

5. Fix vague citation labels.
   `scripts/validate_repository.py` warns about 7 citation labels that are not
   recognizable to a clinician. This is a small, concrete cleanup queue.

6. Inspect canonical merge quality.
   Look especially at records with high `mention_count`, multiple URLs, or both a
   PubMed and publisher/DOI representation.

## Owner-Gated Queues

### Pearl evidence adjudication

Use:

```bash
python scripts/link_pearls_evidence.py report
python scripts/link_pearls_evidence.py adjudicate --episode <N> --trial "<label>" --reject --note "off-topic"
python scripts/link_pearls_evidence.py adjudicate --episode <N> --record --approve --note "checked vs show notes"
python scripts/link_pearls_evidence.py apply
python scripts/build_site.py
```

Current state: `398` model-drafted records have record-level approval and are
eligible for publication under the strict `apply` defaults; `447` remain pending.

### Candidate transcript pearls

`data/candidate_pearls.json` contains `3016` quote-verified candidates, all still
pending review. Nothing is promoted into `data/pearls.json` unless a reviewer
approves candidates and runs the promotion/merge steps described in `README.md`.

### Research screening

`data/trial_screening.json` currently holds a 23-record pilot batch, all pending.
No PICO/clinical-bottom-line screening is published until records are approved and
applied into `data/trial_screening_approved.json`.

## Commands For Handoff

Rebuild deterministic artifacts:

```bash
python scripts/extract_show_note_evidence.py
python scripts/build_site.py
```

Validate:

```bash
python -m py_compile scripts/*.py tests/*.py
python -m unittest discover -s tests
python scripts/validate_repository.py
```

Preview site locally:

```bash
python -m http.server 8765 --directory docs
```

Run incremental ingest:

```bash
python scripts/ingest.py --dry-run
python scripts/ingest.py
```

## Publishing

GitHub Pages serves the `docs/` directory from `main`. A push to `main` that changes
`docs/` should redeploy the public site automatically.

Before handing off, confirm:

```bash
git status --short
python scripts/validate_repository.py
```

The expected validation state is pass, with the current soft warning about 7 vague
labels unless that review queue has been cleaned up.
