# Curbsiders Trial Repository

This project builds a searchable, static teaching reference for clinical trials,
observational studies, systematic reviews, meta-analyses, and guidelines
mentioned on [The Curbsiders Internal Medicine Podcast](https://thecurbsiders.com/category/curbsiders-podcast).

**Live site:** https://kramesh1.github.io/curbsiders-trials/

This is an independent project. It is not affiliated with, endorsed by, or
reviewed by The Curbsiders. The site is for education and source discovery only;
it is not medical advice and should not be used for patient-care decisions.

## What the site does

The GitHub Pages site in [`docs/`](docs/) has two modes:

- **Teaching pearls:** verbatim teaching points extracted from show-note
  `Pearls` sections. Reviewed `evidence_links`, when present, connect a pearl to
  a specific trial, guideline, review, or meta-analysis.
- **Evidence browser:** canonical evidence records with links back to the
  Curbsiders episodes where each source was cited. The browser also shows source
  links from show notes and reviewed backlinks from evidence records to pearls.

The public site deliberately withholds model-suggested pearl-to-evidence links
and trial-screening summaries unless they have attributable human approval.

## Current dataset

As of July 15, 2026, the deterministic pipeline is complete and ready for
clinical review, but the project does **not** claim Curbsiders approval or
clinical validation.

- `558` episodes are scraped and extraction-complete (`0` failed).
- `432/558` cached episodes have dates from the official Audioboom RSS feed;
  older/non-numbered archive entries remain date-unknown rather than guessed.
- `6817` episode-level evidence mentions produce `6771` canonical site records.
- `6362/6771` site records have an outbound source link.
- `537` episodes have at least one evidence record.
- `5869` deterministic show-note evidence identities represent `9065` link
  occurrences; `5324` match model-extracted records and `545` are show-note-only
  evidence records.
- `2672` show-note pearls across `377` episodes canonicalize to `2647` site
  records.
- `845` model-drafted pearl-link records (`1537` links) are withheld:
  `447` are `pending` and `398` are `auto_triaged`. There are currently
  **zero attributable human-approved pearl-to-evidence links** on the public
  site.
- `23` trial-screening records are pending and unpublished.
- `2558` transcript-derived candidate pearls remain pending and unpublished.
- Visitor feedback controls are hidden until a real Worker/D1 endpoint is
  deployed and configured.

## Local quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/build_site.py
python -m http.server 8765 --directory docs
```

Then open [http://127.0.0.1:8765/](http://127.0.0.1:8765/).

The site is static, but it must be served over HTTP so the browser can fetch
`docs/data/*.json`. Opening `docs/index.html` directly from the filesystem will
usually fail because of browser file-access restrictions.

## Repository map

- [`docs/`](docs/) - the public static site and published JSON data.
- [`data/`](data/) - tracked source and intermediate data used to build the
  site. Treat tracked data as public.
- [`scripts/`](scripts/) - scraping, extraction, review, validation, and site
  build commands.
- [`tests/`](tests/) - unit tests for deterministic pipeline behavior.
- [`automation/`](automation/) - optional launchd/cron wrapper for weekly
  incremental ingest.
- [`worker/`](worker/) - optional Cloudflare Worker/D1 feedback endpoint.
- [`CURATION_GUIDE.md`](CURATION_GUIDE.md) - reviewer criteria and adjudication
  workflow.
- [`REVIEW_HANDOFF.md`](REVIEW_HANDOFF.md) - current clinical-review handoff.
- [`NEXT_STAGE.md`](NEXT_STAGE.md) - short project status and next decision.

## Data artifacts

- [`data/episodes.json`](data/episodes.json) - scraped Curbsiders metadata and
  show notes.
- `data/transcripts.json` - local-only full-episode transcript cache. This file
  is git-ignored and must not be redistributed.
- [`data/trials.json`](data/trials.json) - episode-level extracted evidence
  mentions before site canonicalization.
- [`data/show_note_evidence.json`](data/show_note_evidence.json) -
  deterministic inventory of likely evidence hyperlinks from show notes, keyed
  by PMID, DOI, PMCID, NCT ID, or normalized URL.
- [`data/pearls.json`](data/pearls.json) - episode-level pearls extracted from
  show-note `Pearls` sections. The `supporting_citations` field is a heuristic
  audit aid, not reviewed evidence.
- [`data/pearl_evidence_links.json`](data/pearl_evidence_links.json) and
  [`data/pearls_linked.json`](data/pearls_linked.json) - owner-gated
  pearl-to-evidence review sidecars. Only `approved` records with `reviewed_by`
  can publish.
- [`data/trial_screening.json`](data/trial_screening.json) and
  [`data/trial_screening_approved.json`](data/trial_screening_approved.json) -
  owner-gated PICO, applicability, and bottom-line screening sidecars.
- [`docs/data/trials.json`](docs/data/trials.json) and
  [`docs/data/pearls.json`](docs/data/pearls.json) - canonicalized datasets read
  by the public site.
- `data/private/` and `data/batches/` - ignored local working directories that
  may contain full quotes, provider payloads, object IDs, or machine-specific
  paths.

## Pipeline

For a normal rebuild from existing local data:

```bash
python scripts/enrich_trials.py
python scripts/extract_pearls.py
python scripts/extract_show_note_evidence.py
python scripts/link_pearls_evidence.py apply
python scripts/screen_trials.py apply
python scripts/build_site.py
python scripts/validate_repository.py
```

For incremental updates after new episodes are published, use the orchestrator:

```bash
python scripts/ingest.py                            # scrape, extract new episodes, rebuild, validate
python scripts/ingest.py --dry-run                  # live discovery only; write nothing
python scripts/ingest.py --skip-scrape              # reuse current data/episodes.json
python scripts/ingest.py --skip-youtube-transcripts # do not fill transcript gaps from YouTube captions
python scripts/ingest.py --backend batch            # use the OpenAI Batch API for extraction
python scripts/ingest.py --enrich-only              # deterministic rebuild only
```

The orchestrator runs model extraction only for pending episodes. It always
rebuilds deterministic layers and re-applies owner-gated sidecars so stale
published approvals are cleared safely.

## Model-assisted steps

Several optional workflows use a model and are intentionally not automatic:

- `scripts/extract_trials.py` and `scripts/extract_trials_batch.py` extract
  evidence mentions from show notes.
- `scripts/generate_candidate_pearls.py` drafts candidate pearls from local
  transcript text for episodes without show-note pearls.
- `scripts/link_pearls_evidence.py generate|submit-batch|collect` drafts
  pearl-to-evidence links.
- `scripts/screen_trials.py generate|submit-batch|collect` drafts PICO,
  applicability, and bottom-line summaries from PMC full text, PubMed abstracts,
  or show notes.

Draft outputs are sidecars. They do not publish unless an attributable reviewer
marks the record `approved` and provides `reviewed_by`; `pending` and
`auto_triaged` records are withheld.

See [`CURATION_GUIDE.md`](CURATION_GUIDE.md) for reviewer criteria and
[`REVIEW_HANDOFF.md`](REVIEW_HANDOFF.md) for the current review queue.

## Visitor feedback

The static site has no backend by default. Optional feedback can be collected
with the Cloudflare Worker in [`worker/`](worker/), but controls remain hidden
until `docs/index.html` contains a real HTTPS `/feedback` endpoint.

Feedback also follows the sidecar review pattern:

```bash
python scripts/import_feedback.py fetch
python scripts/import_feedback.py report
python scripts/import_feedback.py adjudicate --id 42 --approve
python scripts/import_feedback.py apply
python scripts/build_site.py
```

Nothing submitted by a visitor reaches the public site without review.

## Automation

The wrapper in [`automation/`](automation/) can run weekly incremental ingest
from launchd or cron. It requires a populated `.venv`, loads `.env`, takes a
lock to avoid overlapping runs, and writes to `ingest.log`.

See [`automation/README.md`](automation/README.md) for installation and removal
steps.

## Publishing

GitHub Pages serves the `docs/` folder from `main`:

- Source: **Deploy from a branch**
- Branch: **`main`**
- Folder: **`/docs`**

Every push to `main` that changes `docs/` redeploys the public site. Keep the
site footer disclaimer intact. The repository and Pages deployment are public,
so never commit credentials, non-public transcripts, or raw provider payloads.
Use `.env` for local credentials; `.env.example` documents expected variable
names.

## Validation

Run these before publishing data or site changes:

```bash
python scripts/build_site.py
python scripts/validate_repository.py
python -m unittest discover -s tests
python -m py_compile scripts/*.py tests/*.py
```

## License and data note

Source code is MIT licensed. The derived datasets under `data/` and
`docs/data/` are built from public show notes and are provided for educational
and research use only. See [`LICENSE`](LICENSE) for the full data note,
including the restriction on redistributing full-episode transcripts.
