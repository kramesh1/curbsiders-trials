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

## Model-assisted evidence linking + adjudication (added July 2026)

On top of the deterministic term-overlap linker, `scripts/link_pearls_evidence.py`
asks a model which of an episode's own extracted trials support each pearl (grounded,
verifiable, owner-gated — see the README). `845` pearls now carry model `evidence_links`.

- **Adjudication is per individual link.** `link_pearls_evidence.py adjudicate` sets a
  per-link `review_status` (`approved`/`rejected`/`reset`) from CLI selectors or a
  `--from-file` feedback list; `apply` drops rejected links while keeping their siblings.
  This is the "capture user feedback and re-apply" loop.

`evidence_links` now flows through: `pearl_utils.attach_evidence_links` merges the
`pearls_linked.json` sidecar onto `data/pearls.json` by (episode_url, pearl_key) before
canonicalization (so pearls added since the last `apply` run just show no links yet,
rather than breaking the build), and `build_canonical_pearls` merges links across
episodes per trial, keeping the highest-ranked (`direct` > `background`, then confidence)
when the same pearl/trial pair recurs. The Teaching-pearls view now renders an "Evidence
for this pearl" block with support/confidence badges and the model's rationale, ahead of
any remaining term-overlap-only citations under "Also cited in this episode"; the
"With evidence only" filter and pearl search now also account for `evidence_links`.

Open follow-ups: work a first review queue over the `129` low-confidence / background
links (the adjudication tooling exists in `link_pearls_evidence.py` but hasn't been run
against real reviewer judgment yet).

## Local ingest automation (added July 2026)

The [`automation/`](automation/) directory schedules the incremental ingest:
`run_ingest.sh` (locked, logged wrapper), a launchd template, and install docs. A run
with no new episode spends ~0 tokens. Linking stays manual/owner-gated.

## Episode-level pearl coverage (added July 2026 — was a "next candidate" below)

`scripts/pearl_coverage.py` reveals the `301` episodes with no extracted pearls,
annotated with transcript availability (`236` are transcript-backed and feedable to the
candidate-pearl generator), and writes `data/pearls_coverage_gap.json`. The count also
shows in `ingest.py --report`. This closes implementation candidate #3 below.

## Transcript corpus + candidate pearls (added July 2026)

A full-episode transcript layer now backs the project as a context/search corpus,
kept strictly separate from the auto-published verbatim pearl path:

- `scripts/fetch_transcripts.py` harvests the `94` official (human/CME-reviewed)
  transcript files the show links from its notes; `scripts/harvest_youtube_captions.py`
  fills the remaining gaps with the channel's YouTube auto-captions (`354` episodes,
  tagged `ai_generated`). Both write `data/transcripts.json` and spend no model tokens.
  `scripts/ingest.py` now runs the official-transcript harvest as an incremental phase.
- `scripts/generate_candidate_pearls.py` is an **owner-gated** pass that drafts extra
  teaching pearls from transcripts. Every candidate carries a verbatim `supporting_quote`
  that is deterministically checked against the transcript; unsupported ones are dropped.
  Nothing reaches `data/pearls.json` — candidates land in `data/candidate_pearls.json`,
  and a human must approve + `promote` them into `data/approved_pearls.json`.

Open follow-ups: review the seeded candidate batch in `data/candidate_pearls.json`,
decide whether/how approved transcript pearls surface in the site, and consider
widening candidate generation beyond official transcripts once fidelity is trusted.

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
3. ~~Episode-level QA dashboard or CSV export for manual review.~~ Done: `scripts/pearl_coverage.py` (episodes-without-pearls report).
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
