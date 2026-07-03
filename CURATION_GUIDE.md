# Curbsiders Evidence Curation Guide

This repository is currently strong enough to browse, search, and begin human review. Treat it as an extracted evidence map, not yet as a fully adjudicated clinical reference.

## Teaching Use

Use the site for two teaching workflows:

1. **Prepare a chalk talk**
   Search the `Evidence browser` by condition, drug, or trial name. Filter by `RCT`, `systematic review`, `meta-analysis`, or `guideline` to separate primary evidence from synthesis and current-practice sources.

2. **Trace a Curbsiders citation**
   Open a record, follow the episode backlink, and compare the record summary against the original show-note context before using it in teaching.

## Review Priorities

Review in this order:

1. **Newest episodes**
   Start with episodes `530` through `521` because these are highest-value for current teaching.

2. **High-impact topics**
   Review hypertension, diabetes/cardiorenal protection, anticoagulation, antibiotic duration, ASCVD prevention, obesity/nutrition, and screening.

3. **Records labeled `other`**
   `other` is the noisiest study-type bucket. Many records may be better classified as guideline, review, observational, or background article.

4. **Canonical merges**
   Check records with high `mention_count` or many `episode_titles`. These are useful when correct, but false merges have larger teaching impact.

5. **Missing identifiers**
   Prioritize adding PMID, DOI, or NCT IDs when a record has no outbound link or only a publisher URL.

## Reviewer Checklist

For each reviewed record, confirm:

- The cited paper/trial is actually present in the Curbsiders show notes.
- `citation_label` is recognizable to a clinician.
- `paper_title` is not invented if the show notes do not supply it.
- `study_type` matches the cited source.
- `brief_summary` is supported by the show notes and does not overstate clinical impact.
- `specialty_tags` are useful for discovery.
- Episode backlinks point to the right source episode.

## Local QA Commands

```bash
python scripts/build_site.py
python scripts/validate_repository.py
python -m unittest discover -s tests
python -m py_compile scripts/*.py tests/*.py
```

Run `build_site.py` after any change to `data/trials.json`; otherwise the browser may show stale canonical data.

## Known Limitations

- The extracted summaries are model-generated from show notes and need human review before being treated as authoritative.
- Episode dates are currently missing from all canonical episode backlinks; the live show-note markup did not expose a parseable date, so the scraper left `date` empty. `needs_refresh()` no longer treats a missing date as a reason to re-fetch.
- `study_type = "other"` remains overused and should be tightened during review.
- Curbsiders show notes vary in citation detail, so not every record can be fully enriched without external lookup.
