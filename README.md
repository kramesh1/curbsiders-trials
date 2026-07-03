# Curbsiders Trial Repository

This repository builds a searchable teaching reference of clinical trials, observational studies, systematic reviews, meta-analyses, and guidelines mentioned on The Curbsiders Internal Medicine Podcast.

The static site has two working modes:

- **Teaching pearls** (default): verbatim clinical pearls pulled from the show-note `Pearls` sections, each linked to the trials, guidelines, and reviews cited in the same episode. This is the fastest path to a quick teaching point plus its evidence.
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

As of July 3, 2026, the extraction pipeline has been run across the full scraped episode set, the teaching-pearls layer was added, a full-episode transcript corpus was harvested, and the site dataset was rebuilt.

- `555` episodes marked completed in [data/extraction_state.json](data/extraction_state.json)
- `0` failed episodes
- `6797` extracted trial mentions in [data/trials.json](data/trials.json)
- `6230` canonical trial records in [docs/data/trials.json](docs/data/trials.json)
- `533` episodes with at least one extracted literature mention
- `5903` canonical records with an outbound literature link
- `1271` canonical teaching pearls in [docs/data/pearls.json](docs/data/pearls.json), of which `892` link to at least one supporting citation
- `448` full-episode transcripts in [data/transcripts.json](data/transcripts.json): `94` official (human/CME-reviewed) and `354` YouTube auto-captions filling the gaps (`ai_generated`)

The missing `22` episodes are currently zero-trial episodes, not ingestion failures.

The transcript corpus and the owner-gated candidate-pearl pass are a context/search layer, deliberately kept out of the auto-published verbatim pearl path (see below). `data/candidate_pearls.json` currently holds a small seeded batch (`13` quote-verified candidates from one episode) as a worked example; nothing has been promoted into `data/approved_pearls.json` yet.

## Data artifacts

- [data/episodes.json](data/episodes.json)
  Scraped Curbsiders metadata and show notes. Each row also carries `transcript_url` when the show notes link an official transcript.

- [data/transcripts.json](data/transcripts.json)
  Full-episode text, one record per episode (`448` total), tagged by `source`. `source: "official"` (`94` episodes, mostly #247–424) is text from the transcript PDFs the show publishes — highest-fidelity, human/CME-reviewed, no ASR. `source: "youtube"` (`354` episodes) fills the gaps from the channel's auto-captions (`ai_generated: true`), which are speech recognition and carry the usual ASR error risk. Both are intended as a search/context corpus and input to the owner-gated candidate-pearl pass — **not** as a source for auto-published verbatim pearls (the deterministic pearl layer stays anchored to the show notes).

- [data/candidate_pearls.json](data/candidate_pearls.json) / [data/approved_pearls.json](data/approved_pearls.json)
  Model-drafted teaching pearls from transcripts (candidate_pearls), each with a `supporting_quote` verified verbatim against the transcript and a `review_status`. A human sets `review_status: "approved"` and runs the `promote` step to copy those into approved_pearls; nothing here is ever written into `data/pearls.json`. See the owner-gated pass below.

- [data/extraction_state.json](data/extraction_state.json)
  Per-episode processing manifest with completion state, chunk counts, and errors.

- [data/trials.json](data/trials.json)
  Episode-level extracted trial mentions after within-episode normalization and deduplication.

- [data/pearls.json](data/pearls.json)
  Episode-level teaching pearls extracted verbatim from show-note `Pearls` sections, each with the supporting citations found in the same episode.

- [docs/data/trials.json](docs/data/trials.json)
  Canonicalized site dataset with one record per trial or paper plus backlinks to all episodes mentioning it.

- [docs/data/pearls.json](docs/data/pearls.json)
  Canonicalized pearls for the site: one record per unique pearl, with episode backlinks and links (by `canonical_key`) into the trial records that support it.

- `data/batches/`
  Optional local OpenAI Batch API inputs and outputs. These are ignored for sharing because they can contain request payloads, provider object IDs, and machine-specific paths.

## Pipeline

1. `python scripts/scrape_episodes.py`

   Scrapes Curbsiders episode pages into [data/episodes.json](data/episodes.json). The scraper refreshes incomplete cached rows and captures episode dates when the source page exposes them.

2. `python scripts/fetch_transcripts.py`

   Downloads the official transcript file linked by each episode's show notes, extracts its text, and writes [data/transcripts.json](data/transcripts.json). Resumable (skips already-fetched) and spends no model tokens. `--report` prints coverage; `--refresh` re-fetches everything.

   Optional gap-fill: `python scripts/harvest_youtube_captions.py` matches episodes without an official transcript to the show's YouTube videos (by episode number in the title) and stores the auto-captions as `source: "youtube"`. Needs `yt-dlp`; spends no model tokens; tagged `ai_generated` since captions are ASR.

3. `python scripts/extract_trials.py --backend openai --workers 8`

   Synchronous extractor for spot checks, small reruns, and prompt iteration. It writes episode-level mentions to [data/trials.json](data/trials.json) and per-episode state to [data/extraction_state.json](data/extraction_state.json).

4. `python scripts/extract_trials_batch.py ...`

   Preferred workflow for larger reruns and backfills. It builds a saved local batch directory under `data/batches/`, submits one request per show-note chunk, then downloads and merges results into the local dataset.

5. `python scripts/extract_pearls.py`

   Deterministically extracts the show-note `Pearls` sections into [data/pearls.json](data/pearls.json) and links each pearl to the episode's already-extracted trial mentions. No model calls, so it is cheap and safe to re-run any time.

6. `python scripts/build_site.py`

   Canonicalizes duplicate trial mentions across episodes and rewrites [docs/data/trials.json](docs/data/trials.json), and canonicalizes pearls into [docs/data/pearls.json](docs/data/pearls.json), for the browser UI.

The site is a static app rooted at [docs/index.html](docs/index.html).

## Candidate pearls from transcripts (owner-gated)

`scripts/generate_candidate_pearls.py` drafts additional teaching pearls from the full
episode transcripts — the points a whole episode makes that the show-note `Pearls`
summary doesn't. Because this needs a model and model paraphrase is the hallucination
risk the project is built to avoid, it is deliberately fenced:

- It **never** writes to `data/pearls.json`. Candidates go to `data/candidate_pearls.json`.
- Every candidate must carry a **verbatim `supporting_quote`**, which is then verified
  to actually appear in the transcript. Unsupported candidates are dropped — the model
  can't smuggle in a claim, because its evidence is checkable.
- Nothing is published until a human sets `review_status: "approved"` and runs `promote`.
- It is **not** part of `ingest.py` — it spends tokens and is run deliberately.

```bash
python scripts/generate_candidate_pearls.py generate --episode 347   # one episode
python scripts/generate_candidate_pearls.py generate --limit 5       # first 5 eligible
python scripts/generate_candidate_pearls.py report                   # counts + review status
# (review data/candidate_pearls.json, set review_status: "approved" on the good ones)
python scripts/generate_candidate_pearls.py promote                  # -> data/approved_pearls.json
```

Defaults to `claude-opus-4-8` (override with `--model`) and to the high-fidelity
official transcripts only (`--source`/`--include-ai` to widen). Requires `ANTHROPIC_API_KEY`.

## Incremental ingest (recommended for new episodes)

Instead of running the steps above by hand, use the orchestrator, which does model
work only on episodes that are new since the last run and then rebuilds the
deterministic layers (pearls + site) and validates:

```bash
python scripts/ingest.py                 # scrape → extract new → pearls → site → validate
python scripts/ingest.py --dry-run       # report which episodes are new, change nothing
python scripts/ingest.py --skip-scrape   # reuse the current episodes.json
python scripts/ingest.py --backend batch # extract new episodes via the OpenAI Batch API
```

Because Curbsiders publishes roughly weekly, each incremental run typically
extracts only one or two episodes. The extractor is resumable and pearls are
always rebuilt, so linking picks up any newly extracted trials.

### Scheduling weekly ingest

`scripts/ingest.py` is safe to run on a timer in an environment that has
`OPENAI_API_KEY` and network access. A weekly cron entry (Sundays 06:00), logging
to the repo:

```cron
0 6 * * 0  cd /path/to/curbsiders_to_trials && /path/to/.venv/bin/python scripts/ingest.py >> ingest.log 2>&1
```

On macOS you can use the same command via `launchd` or `cron`. The run is
idempotent: with no new episodes it validates and exits without spending tokens.

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
