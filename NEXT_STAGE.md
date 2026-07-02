# Next Stage

This repository is now past the extraction/backfill phase. The next stage is QA, enrichment, and preparation for teaching use.

## Teaching-pearls layer (added July 2026)

A deterministic pearls layer now sits on top of the trial extraction:

- `scripts/extract_pearls.py` pulls verbatim pearls from the show-note `Pearls`
  sections into `data/pearls.json` and links each to the episode's trial mentions.
- `scripts/build_site.py` canonicalizes them into `docs/data/pearls.json`.
- The site's default **Teaching pearls** view surfaces them with their evidence.
- `scripts/ingest.py` is the incremental orchestrator for new episodes.

Open follow-ups for this layer: tune the pearl→trial link threshold in
`pearl_utils.DEFAULT_MIN_LINK_SCORE`, and consider a reviewer pass over pearls
whose linked evidence looks off-topic.

## Current baseline

- Full scrape completed in [data/episodes.json](data/episodes.json)
- Full extraction completed in [data/trials.json](data/trials.json)
- Full canonical rebuild completed in [docs/data/trials.json](docs/data/trials.json)
- Per-episode processing state recorded in [data/extraction_state.json](data/extraction_state.json)

## Immediate objectives

1. Verify extraction quality on a human-reviewable sample.
2. Identify where canonicalization merged distinct studies incorrectly or failed to merge obvious duplicates.
3. Decide what additional metadata is required for teaching use.
4. Define a curation workflow for ongoing updates.

## Recommended QA sequence

1. Review the 10 most recent episodes first.

   Use [data/trials.json](data/trials.json) and filter by `episode_number` `530` down through `521`.

2. Compare mention-level extraction to canonical site records.

   For a few recent episodes, check that every important cited trial appears both:
   - as an episode-level mention in [data/trials.json](data/trials.json)
   - as a canonical trial record in [docs/data/trials.json](docs/data/trials.json)

3. Inspect zero-trial episodes.

   There are currently `22` completed episodes with no extracted trial mentions. These may be legitimate zero-trial episodes, but they are worth a quick sanity pass before treating the repository as complete.

4. Inspect `study_type = "other"`.

   The canonical distribution still contains many `other` labels. This likely marks the highest-yield prompt-improvement area if the goal is a stronger teaching dataset.

## Suggested enrichment fields

If the repository is intended for durable future teaching use, the most useful next fields are:

- `pmid`
- `doi`
- `nct_id`
- `journal`
- `first_author`
- `publication_type` or a tightened `study_type`
- `key_outcome`
- `population`
- `intervention`
- `comparator`

These do not need to be added in one pass, but `pmid` and `nct_id` are the highest-value identifiers for canonicalization.

## Decision points before more coding

Before changing the schema again, decide:

- Is this mainly a “what trials were mentioned on Curbsiders?” archive?
- Or is it becoming a structured educational trial library?

If it is the second, the next coding phase should shift from extraction to metadata enrichment and reviewer tooling.

## Good next implementation candidates

If QA finds the extraction acceptable, the next technical work should probably be one of these:

1. PubMed enrichment pass keyed by URL/title/label.
2. Reviewer report for suspicious merges, missing identifiers, and `other` study types.
3. Episode-level QA dashboard or CSV export for manual review.
4. Improved site filters for specialty, study type, and identifier presence.

## Commands you may still need

Rebuild canonical dataset after any changes to [data/trials.json](data/trials.json):

```bash
python scripts/build_site.py
```

Check batch status:

```bash
python scripts/extract_trials_batch.py status --batch-dir data/batches/<batch_name>
```

Download a completed batch:

```bash
python scripts/extract_trials_batch.py download --batch-dir data/batches/<batch_name>
```

Run tests:

```bash
python -m unittest discover -s tests
python -m py_compile scripts/*.py tests/*.py
```
