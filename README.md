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

As of July 4, 2026, the extraction pipeline has been run across the full scraped episode set, the teaching-pearls layer was added, a model-assisted pearl→evidence linking layer was added, a full-episode transcript corpus was harvested, and the site dataset was rebuilt.

- `555` episodes marked completed in [data/extraction_state.json](data/extraction_state.json)
- `0` failed episodes
- `6797` extracted trial mentions in [data/trials.json](data/trials.json)
- `6230` canonical trial records in [docs/data/trials.json](docs/data/trials.json)
- `533` episodes with at least one extracted literature mention
- `5903` canonical records with an outbound literature link
- `1287` episode-level teaching pearls in [data/pearls.json](data/pearls.json) across `254` of `555` episodes; the remaining `301` episodes have no recognizable show-note `Pearls` section (see [pearl coverage](#pearl-coverage-gap) — `236` of those have a transcript and are feedable to the candidate-pearl generator)
- `845` of those pearls now carry model-authored `evidence_links` (`1537` links across `227` episodes) in [data/pearl_evidence_links.json](data/pearl_evidence_links.json), applied onto [data/pearls_linked.json](data/pearls_linked.json)
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

- [data/pearl_evidence_links.json](data/pearl_evidence_links.json) / [data/pearls_linked.json](data/pearls_linked.json)
  The model-assisted pearl→evidence linking layer (owner-gated). The sidecar (`pearl_evidence_links`) holds, per pearl, the episode's own trials the model judged to support it — each link with `support`, `confidence`, a `rationale`, and a per-link `review_status` for adjudication. `apply` merges the reviewed (non-rejected) links onto a copy of the pearls as `evidence_links` in `pearls_linked.json`, leaving `data/pearls.json` untouched. See the linking section below.

- [data/pearls_coverage_gap.json](data/pearls_coverage_gap.json)
  The list of episodes with no extracted pearls yet, each annotated with whether a transcript exists (and its source). Generated by `scripts/pearl_coverage.py`.

- [docs/data/trials.json](docs/data/trials.json)
  Canonicalized site dataset with one record per trial or paper plus backlinks to all episodes mentioning it.

- [docs/data/pearls.json](docs/data/pearls.json)
  Canonicalized pearls for the site: one record per unique pearl, with episode backlinks, term-overlap citations, and (when present) model-authored `evidence_links` merged across episodes, keeping the highest-ranked link per trial. The Teaching-pearls view renders these ahead of any remaining term-overlap-only citations.

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

   Deterministically extracts the show-note `Pearls` sections into [data/pearls.json](data/pearls.json) and links each pearl to the episode's already-extracted trial mentions. No model calls, so it is cheap and safe to re-run any time. To see which episodes yielded no pearls, run `python scripts/pearl_coverage.py` (see [Pearl coverage gap](#pearl-coverage-gap)).

6. `python scripts/build_site.py`

   Canonicalizes duplicate trial mentions across episodes and rewrites [docs/data/trials.json](docs/data/trials.json), and canonicalizes pearls into [docs/data/pearls.json](docs/data/pearls.json), for the browser UI. If [data/pearls_linked.json](data/pearls_linked.json) exists, its `evidence_links` are merged onto the pearls first (by episode + pearl text) so the site can render them.

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

## Model-assisted pearl→evidence linking (owner-gated)

The deterministic linker in step 5 links pearls to trials by term overlap, which is
lossy and imprecise. `scripts/link_pearls_evidence.py` upgrades this: it asks a model,
one episode at a time, which of that episode's **own already-extracted trials** support
each pearl. It is fenced the same way the candidate-pearl pass is — the model may only
refer to the supplied trials by index (it cannot cite a paper we didn't extract), every
index is range-checked, and output goes to a sidecar (`data/pearl_evidence_links.json`),
never `data/pearls.json`. It is not part of `ingest.py`; it spends tokens and is run
deliberately.

```bash
python scripts/link_pearls_evidence.py generate --episode 500   # one episode (synchronous)
python scripts/link_pearls_evidence.py submit-batch             # all eligible via Batch API (50% cheaper)
python scripts/link_pearls_evidence.py collect --wait           # retrieve batch results
python scripts/link_pearls_evidence.py report                   # coverage lift vs term-overlap + adjudication counts
python scripts/link_pearls_evidence.py apply                    # merge reviewed links -> data/pearls_linked.json
```

### Adjudication loop

Review is **per individual link**, so one bad trial link can be rejected without
discarding the pearl's good links. `adjudicate` sets a per-link `review_status`
(`approved` / `rejected` / `reset`) on the links matching its selectors; `apply` then
drops any link marked `rejected` while keeping its siblings. Links generated before
adjudication existed inherit their record's status, so nothing changes until you act.

```bash
# Reject one off-topic study on episode 500 (preview first with --dry-run):
python scripts/link_pearls_evidence.py adjudicate --episode 500 --trial "SPRINT" --reject --note "off-topic"
python scripts/link_pearls_evidence.py apply        # re-apply so pearls_linked.json reflects it

# Other selectors: --pearl <substr>, --canonical-key <key>, --confidence low, --support background
# Reset a link back to inherited status:
python scripts/link_pearls_evidence.py adjudicate --canonical-key "<key>" --reset
```

To adjudicate in bulk from captured user feedback, pass a JSON list of decision
objects (`{episode_number, canonical_key | trial, decision, note}`) via
`--from-file feedback.json`, then `apply`.

## Pearl coverage gap

`scripts/pearl_coverage.py` reveals which episodes still have **zero** extracted pearls
(the deterministic layer only emits pearls for episodes with a recognizable
`<Topic> Pearls` heading, and silently skips the rest). It joins episodes to
`data/transcripts.json` so you can see which pearl-less episodes have a transcript and
are therefore feedable to the owner-gated candidate-pearl generator.

```bash
python scripts/pearl_coverage.py    # print the summary + list, write data/pearls_coverage_gap.json
```

The same gap count also appears in `python scripts/ingest.py --report`.

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

### Scheduling weekly ingest (automation)

`scripts/ingest.py` is safe to run on a timer: the run is idempotent, and with no
new episodes the paid extraction stage is skipped entirely, so it validates and
exits without spending tokens. The [`automation/`](automation/) directory drives this
from a schedule:

- `automation/run_ingest.sh` — the scheduled entry point. Resolves the repo, loads
  `.env` (API keys), activates `.venv`, takes a lock so runs can't overlap, and logs
  to `ingest.log`. Test it now with `automation/run_ingest.sh --dry-run`.
- `automation/com.curbsiders.ingest.plist` — a macOS **launchd** template (weekly,
  Sundays 06:00) that runs the wrapper.
- [`automation/README.md`](automation/README.md) — install steps for launchd (macOS,
  recommended) and a cron one-liner for Linux.

The model-assisted pearl→evidence linking step stays owner-gated and out of the
scheduled path by design; run it deliberately after a new episode lands (see
`automation/README.md`).

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
