# Curbsiders Trial Repository

This repository builds a searchable teaching reference of clinical trials, observational studies, systematic reviews, meta-analyses, and guidelines mentioned on The Curbsiders Internal Medicine Podcast.

The static site has two working modes:

- **Knowledge chains**: computed teaching pathways that start with a bedside question and surface representative source records.
- **Evidence browser**: searchable/filterable canonical records with backlinks to the Curbsiders episodes where each paper or trial was mentioned.

## Local quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/build_site.py
python -m http.server 8765 --directory docs
```

Then open [http://127.0.0.1:8765/](http://127.0.0.1:8765/).

The site is static, but it must be served over HTTP so the browser can fetch `docs/data/trials.json`. Opening `docs/index.html` directly from the filesystem will usually fail because of browser file-access restrictions.

## Current status

As of June 29, 2026, the extraction pipeline has been run across the full scraped episode set and the site dataset was rebuilt.

- `555` episodes marked completed in [data/extraction_state.json](data/extraction_state.json)
- `0` failed episodes
- `6797` extracted trial mentions in [data/trials.json](data/trials.json)
- `6221` canonical trial records in [docs/data/trials.json](docs/data/trials.json)
- `533` episodes with at least one extracted literature mention
- `5904` canonical records with an outbound literature link

The missing `22` episodes are currently zero-trial episodes, not ingestion failures.

## Data artifacts

- [data/episodes.json](data/episodes.json)
  Scraped Curbsiders metadata and show notes.

- [data/extraction_state.json](data/extraction_state.json)
  Per-episode processing manifest with completion state, chunk counts, and errors.

- [data/trials.json](data/trials.json)
  Episode-level extracted trial mentions after within-episode normalization and deduplication.

- [docs/data/trials.json](docs/data/trials.json)
  Canonicalized site dataset with one record per trial or paper plus backlinks to all episodes mentioning it.

- `data/batches/`
  Optional local OpenAI Batch API inputs and outputs. These are ignored for sharing because they can contain request payloads, provider object IDs, and machine-specific paths.

## Pipeline

1. `python scripts/scrape_episodes.py`

   Scrapes Curbsiders episode pages into [data/episodes.json](data/episodes.json). The scraper refreshes incomplete cached rows and captures episode dates when the source page exposes them.

2. `python scripts/extract_trials.py --backend openai --workers 8`

   Synchronous extractor for spot checks, small reruns, and prompt iteration. It writes episode-level mentions to [data/trials.json](data/trials.json) and per-episode state to [data/extraction_state.json](data/extraction_state.json).

3. `python scripts/extract_trials_batch.py ...`

   Preferred workflow for larger reruns and backfills. It builds a saved local batch directory under `data/batches/`, submits one request per show-note chunk, then downloads and merges results into the local dataset.

4. `python scripts/build_site.py`

   Canonicalizes duplicate mentions across episodes and rewrites [docs/data/trials.json](docs/data/trials.json) for the browser UI.

The site is a static app rooted at [docs/index.html](docs/index.html).

## Batch workflow

Use the Batch API for anything larger than a small spot check.

Submit:

```bash
python scripts/extract_trials_batch.py submit --model gpt-4o
```

Check status:

```bash
python scripts/extract_trials_batch.py status --batch-dir data/batches/<batch_name>
```

Download and merge completed results:

```bash
python scripts/extract_trials_batch.py download --batch-dir data/batches/<batch_name>
```

Submit and poll in one command:

```bash
python scripts/extract_trials_batch.py run --limit 10 --include-completed
```

If you want to avoid reprocessing episodes already included in a prior batch manifest:

```bash
python scripts/extract_trials_batch.py submit \
  --model gpt-4o \
  --exclude-batch-dir data/batches/<existing_batch_name>
```

## Local batch history

- `data/batches/trials_20260628_210205`
  Local verification batch for the 10 most recent episodes.

- `data/batches/trials_20260629_013304`
  Local remaining-episodes batch using `gpt-5.4-mini`.

Batch directories are intentionally ignored for sharing. Each local batch directory contains:

- `requests.jsonl`: raw batch input
- `manifest.json`: mapping from batch request IDs to episode/chunk metadata
- `batch_info.json`: uploaded file ID, batch ID, model, and status metadata
- `batch_output.jsonl`: downloaded results when available

## Current model and extraction behavior

- Long show notes are chunked instead of truncated.
- Trial mentions are normalized and deduplicated within an episode.
- Published site data is canonicalized into one record per trial or paper, with backlinks to all episodes that referenced it.
- The extractor uses a strict JSON schema for OpenAI structured outputs.
- Zero-trial episodes are still marked completed in the state file.

## Next stage

The next stage is not more extraction. It is QA, enrichment, and curation.

Start with [NEXT_STAGE.md](NEXT_STAGE.md) and [CURATION_GUIDE.md](CURATION_GUIDE.md).

At a minimum, the next stage should include:

- spot-checking the newest episodes in [data/trials.json](data/trials.json)
- reviewing suspicious canonical merges in [docs/data/trials.json](docs/data/trials.json)
- deciding whether to enrich canonical records with PMID, DOI, and NCT identifiers
- reducing overuse of `study_type = "other"` if the current prompt is too permissive

## Publishing (GitHub Pages)

The site is the `docs/` folder and is ready to serve as a GitHub Page. It is
**not enabled yet** — the repository is intentionally kept private.

To go live later:

1. Open the repo on GitHub → **Settings** → **Pages**.
2. Under **Build and deployment**, set **Source** to **Deploy from a branch**.
3. Choose branch **`main`** and folder **`/docs`**, then **Save**.

Notes:

- GitHub Pages on a **private** repository requires a paid plan (GitHub Pro,
  Team, or Enterprise). On a free plan you must make the repo public to publish.
- Even from a private repo, the published Pages URL is publicly reachable
  (access control for Pages is Enterprise-only). Enabling Pages effectively
  makes the site public, while the source repository can stay private.
- `docs/.nojekyll` is present so GitHub serves the files verbatim (no Jekyll).
- The page loads `data/trials.json` via a relative path and pulls Fuse.js and
  Google Fonts from CDNs, so it needs network access at load time.

## Tests

Run:

```bash
python -m unittest discover -s tests
python scripts/validate_repository.py
python -m py_compile scripts/*.py tests/*.py
```
