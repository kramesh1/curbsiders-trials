# Curbsiders Trial Repository

This repository builds a searchable teaching reference of clinical trials, observational studies, systematic reviews, meta-analyses, and guidelines mentioned on The Curbsiders Internal Medicine Podcast.

**Live site:** https://kramesh1.github.io/curbsiders-trials/ (public, unaffiliated with The Curbsiders; see [Publishing](#publishing-github-pages) and the site footer for disclaimers).

The static site has two working modes:

- **Teaching pearls** (default): verbatim clinical pearls pulled from the show-note `Pearls` sections, with reviewed `evidence_links` shown when a trial, guideline, review, or meta-analysis directly supports a practice-changing teaching point.
- **Evidence browser**: searchable/filterable canonical records with backlinks to the Curbsiders episodes where each paper or trial was cited, plus reviewed pearl backlinks when an evidence record supports a teaching point.

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

As of July 13, 2026, the repository is mechanically complete and ready for clinical review, but it does **not** claim Curbsiders approval.

- `558` episodes are scraped and extraction-complete (`0` failed), including newly recovered #381, #531, and #532.
- Official Audioboom RSS is the discovery source; `432/558` cached pages now have dates. Older/non-numbered archive entries remain date-unknown rather than guessed.
- `6817` episode-level trial/evidence mentions produce `6771` canonical evidence records; `6362` have outbound source links and `537` episodes have evidence.
- `5869` deterministic show-note evidence identities represent `9065` hyperlink occurrences; `5324` match extracted records and `545` are show-note-only records.
- `2672` show-note teaching pearls across `377` episodes canonicalize to `2647` site records.
- `845` model-drafted pearl-link records (`1537` links) are withheld. `447` are `pending`; `398` that were previously labeled approved after an automated rule plus a 25-record spot check are now honestly labeled `auto_triaged`. There are currently **zero attributable human-approved links on the public site**.
- `449` local-only transcripts: `95` official and `354` YouTube/ASR fallbacks. An official transcript now replaces a YouTube fallback whenever it becomes available.
- `2558` transcript-derived candidate pearls remain pending. The tracked file contains quote hashes and lengths only; full excerpts live in ignored `data/private/candidate_pearls.json` for local review.
- The 23-record trial-screening pilot remains pending and unpublished. Screening and linking now share the same `6771`-record evidence universe, including show-note-only citations; `3598` currently resolve directly to a PMID.
- Visitor feedback controls are hidden until a real deployed endpoint is configured.

## Data artifacts

- [data/episodes.json](data/episodes.json)
  Scraped Curbsiders metadata and show notes. Each row also carries `transcript_url` when the show notes link an official transcript.

- `data/transcripts.json` **(local-only, not tracked in git)**
  Full-episode text, one record per episode (`449` total), tagged by `source`. `source: "official"` (`95` episodes) is text from transcript files the show publishes; `source: "youtube"` (`354` episodes) fills gaps from auto-captions (`ai_generated: true`). Both are local search/context inputs, not auto-published pearl sources. Because this is full verbatim commercial-podcast text, it is deliberately kept out of the tracked/shared repo.

- [data/candidate_pearls.json](data/candidate_pearls.json) / `data/private/candidate_pearls.json` / `data/approved_pearls.json`
  The tracked candidate file contains statements, review metadata, and SHA-256/length proof for a locally verified quote, but no transcript excerpt. The full quote-bearing review file is local-only under ignored `data/private/`. Human approval is attributable (`reviewed_by`) and promotion emits a sanitized approved artifact.

- `data/candidate_pearls_batch.json`
  Bookkeeping for an in-flight or most-recently-collected Batch API submission from `generate_candidate_pearls.py submit-batch` (batch ID, model, per-episode `custom_id` map, a fingerprint used to sanity-check `collect`). Safe to commit — no secrets, just a job reference.

- [data/trial_screening.json](data/trial_screening.json) / [data/trial_screening_approved.json](data/trial_screening_approved.json)
  Owner-gated PICO, applicability, and clinical-bottom-line screening grounded in PMC full text, PubMed abstract, or conservatively in show notes. `apply` publishes only records with `review_status: "approved"` and `reviewed_by`; the current approved artifact is intentionally empty.

- `data/trial_screening_batch.json`
  Bookkeeping for an in-flight or most-recently-collected Batch API submission from `screen_trials.py submit-batch` (batch ID, model, per-trial `custom_id` map, a fingerprint used to sanity-check `collect`). Safe to commit — no secrets, just a job reference.

- [data/extraction_state.json](data/extraction_state.json)
  Per-episode processing manifest with completion state, chunk counts, and errors.

- [data/trials.json](data/trials.json)
  Episode-level extracted trial mentions after within-episode normalization and deduplication.

- [data/show_note_evidence.json](data/show_note_evidence.json)
  Deterministic inventory of likely clinical-evidence hyperlinks actually present in Curbsiders show notes. Each record has a stable `evidence_key` based on PMID, DOI, PMCID, NCT ID, or normalized URL, episode backlinks, source labels, and a `canonical_key` when it matches the model-extracted evidence browser. `build_site.py` merges this layer into [docs/data/trials.json](docs/data/trials.json), adding show-note-only records for cited evidence the model extractor missed.

- [data/pearls.json](data/pearls.json)
  Episode-level teaching pearls extracted verbatim from show-note `Pearls` sections. The `supporting_citations` field is a deterministic same-episode term-overlap aid for audit/review, not a teaching-grade evidence claim.

- [data/pearl_evidence_links.json](data/pearl_evidence_links.json) / [data/pearls_linked.json](data/pearls_linked.json)
  The model-assisted pearl→evidence linking layer (owner-gated). `pending` and `auto_triaged` are never publishable. `apply` requires record-level `approved` plus `reviewed_by`, drops rejected links, and publishes only direct, non-low-confidence links by default. The current `pearls_linked.json` deliberately contains no model links pending Curbsiders-team review.

- [data/pearls_coverage_gap.json](data/pearls_coverage_gap.json)
  The list of episodes with no extracted pearls yet, each annotated with whether a transcript exists (and its source). Generated by `scripts/pearl_coverage.py`.

- [docs/data/trials.json](docs/data/trials.json)
  Canonicalized site dataset with one record per trial, paper, guideline, or cited evidence hyperlink plus backlinks to all episodes citing it. Records can include `show_note_citations` from the deterministic hyperlink layer and `linked_pearls` reverse backlinks from reviewed pearl→evidence links.

- [docs/data/pearls.json](docs/data/pearls.json)
  Canonicalized pearls for the site: one record per unique pearl, with episode backlinks, heuristic term-overlap citations retained for audit, and reviewed `evidence_links` merged across episodes. The Teaching-pearls view treats only `evidence_links` as linked clinical evidence.

- `data/batches/`
  Optional local OpenAI Batch API inputs and outputs. These are ignored for sharing because they can contain request payloads, provider object IDs, and machine-specific paths.

- [data/pearl_feedback.json](data/pearl_feedback.json) / [data/pearl_feedback_approved.json](data/pearl_feedback_approved.json)
  Visitor-submitted feedback on pearls and pearl→evidence links (owner-gated). The sidecar (`pearl_feedback`) holds every imported row (reason code, optional comment, per-row `review_status`); `apply` aggregates only `"approved"` rows into per-pearl (and per-pearl-link) flag counts in `pearl_feedback_approved.json`, the only feedback artifact `build_site.py` reads. See the visitor feedback section below.

## Pipeline

1. `python scripts/scrape_episodes.py`

   Discovers numbered episodes from the official Audioboom RSS feed, unions them with the older local archive, and refreshes recent pages into [data/episodes.json](data/episodes.json). It fetches the website directly first and uses a configurable public reader fallback when SiteGround serves its WAF interstitial. Empty/anomalously small discovery and failed new pages are hard errors. `--dry-run` performs real live discovery without writing; `--refresh-recent N` controls refresh depth.

2. `python scripts/fetch_transcripts.py`

   Downloads the official transcript file linked by each episode's show notes, extracts its text, and writes `data/transcripts.json` (local-only, untracked). A WAF-blocked public document uses the same reader fallback. Resumable, but an existing YouTube record is replaced when an official transcript becomes available.

   Gap-fill: `python scripts/harvest_youtube_captions.py` matches episodes without an official transcript to the show's YouTube videos (by episode number in the title) and stores the auto-captions as `source: "youtube"`. Needs `yt-dlp`; spends no model tokens; tagged `ai_generated` since captions are ASR. `scripts/ingest.py` now runs this automatically as phase 2b (after scraping, before extraction) unless `--skip-youtube-transcripts` is passed.

3. `python scripts/extract_trials.py --backend openai --workers 8`

   Synchronous extractor for spot checks, small reruns, and prompt iteration. It writes episode-level mentions to [data/trials.json](data/trials.json) and per-episode state to [data/extraction_state.json](data/extraction_state.json).

4. `python scripts/extract_trials_batch.py ...`

   Preferred workflow for larger reruns and backfills. It builds a saved local batch directory under `data/batches/`, submits one request per show-note chunk, then downloads and merges results into the local dataset.

5. `python scripts/extract_show_note_evidence.py`

   Deterministically harvests likely clinical-evidence hyperlinks from [data/episodes.json](data/episodes.json) into [data/show_note_evidence.json](data/show_note_evidence.json). It normalizes PMID/DOI/PMCID/NCT/URL identities and annotates records that already match the model-extracted canonical trial layer. `build_site.py` also regenerates this artifact, so this standalone step is mainly for auditing counts and gaps.

6. `python scripts/extract_pearls.py`

   Deterministically extracts the show-note `Pearls` sections into [data/pearls.json](data/pearls.json) and attaches heuristic same-episode citations by term overlap. No model calls, so it is cheap and safe to re-run any time, but these overlap citations are not considered reviewed teaching evidence. To see which episodes yielded no pearls, run `python scripts/pearl_coverage.py` (see [Pearl coverage gap](#pearl-coverage-gap)).

7. `python scripts/link_pearls_evidence.py apply && python scripts/screen_trials.py apply`

   Re-compose the two owner-gated publication sidecars. These are safe deterministic steps: only attributable human approvals survive, and stale published data is cleared when none exist. `ingest.py` runs both automatically.

8. `python scripts/build_site.py`

   Canonicalizes duplicate trial mentions across episodes, merges the deterministic show-note evidence layer, repairs stale reviewed pearl evidence keys when they can be matched to current canonical records, rewrites [docs/data/trials.json](docs/data/trials.json), and canonicalizes pearls into [docs/data/pearls.json](docs/data/pearls.json). If [data/pearls_linked.json](data/pearls_linked.json) exists, its `evidence_links` are merged onto the pearls first (by episode + pearl text) so the site can render pearl→evidence links and evidence→pearl backlinks.

The site is a static app rooted at [docs/index.html](docs/index.html).

## Candidate pearls from transcripts (owner-gated)

`scripts/generate_candidate_pearls.py` drafts additional teaching pearls from the full
episode transcripts — the points a whole episode makes that the show-note `Pearls`
summary doesn't. Because this needs a model and model paraphrase is the hallucination
risk the project is built to avoid, it is deliberately fenced:

- It **never** writes directly to `data/pearls.json`.
- Every candidate must carry a verbatim quote which is checked locally. Full quotes are written only to ignored `data/private/candidate_pearls.json`; the tracked candidate file receives a hash and character count.
- Nothing is published until an attributable human approval (`--reviewer`) and `promote`.
- It is **not** part of `ingest.py` — it spends tokens and is run deliberately.
- Only episodes with **zero** deterministic show-notes pearls are ever eligible (per
  `pearl_coverage.compute_pearl_gap`), even with `--refresh` — an episode that already
  has real pearls is never sent through this path.

```bash
python scripts/generate_candidate_pearls.py generate --episode 347   # one episode
python scripts/generate_candidate_pearls.py generate --limit 5       # first 5 eligible
python scripts/generate_candidate_pearls.py submit-batch             # same pool, Batch API (50% cheaper)
python scripts/generate_candidate_pearls.py collect --wait           # retrieve batch results
python scripts/generate_candidate_pearls.py report                   # counts + review status
python scripts/generate_candidate_pearls.py adjudicate --episode 347 --statement "<substring>" --approve --reviewer "<name>"
python scripts/generate_candidate_pearls.py promote                  # -> data/approved_pearls.json
python scripts/merge_approved_pearls.py                              # -> merges into data/pearls.json
python scripts/build_site.py
```

Defaults to `claude-opus-4-8` (override with `--model`) and to the high-fidelity
official transcripts only (`--source`/`--include-ai` to widen). Requires `ANTHROPIC_API_KEY`.

`collect` refuses stale batch results when the transcript content hash differs from submission. `promote` writes a quote-free `data/approved_pearls.json`; nothing downstream reads that file
by default. `scripts/merge_approved_pearls.py` is the missing link — it maps each
approved candidate into the same record shape `extract_pearls.py` produces (running it
through the same deterministic linking/segment/category pipeline) and merges it into
`data/pearls.json`, deduped against the episode's existing show-notes pearls so a
candidate never shadows a real one. It is deliberately **not** part of `ingest.py`: a
plain `ingest.py`/`extract_pearls.py` run regenerates `pearls.json` from show notes only
and would silently drop any merged-in candidates — re-run `merge_approved_pearls.py`
after each such rebuild, then `build_site.py` again.

## Model-assisted pearl→evidence linking (owner-gated)

The deterministic linker in step 6 links pearls to trials by term overlap, which is
lossy and imprecise and should be treated as an audit aid only. `scripts/link_pearls_evidence.py` upgrades this: it asks a model,
one episode at a time, which of that episode's records in the **shared canonical evidence universe** support
each pearl with direct, teaching-worthy evidence. It is fenced the same way the candidate-pearl pass is — the model may only
refer to the supplied trials by index (it cannot cite a paper we didn't extract), every
index is range-checked, and output goes to a sidecar (`data/pearl_evidence_links.json`),
never `data/pearls.json`. It is not part of `ingest.py`; it spends tokens and is run
deliberately. Where an approved trial screening exists, its grounded outcome, bottom line,
and applicability are included in the linker prompt; this is the reverse-linking path from
paper summary back to an appropriate pearl. Batch collection refuses content-hash drift.

```bash
python scripts/link_pearls_evidence.py generate --episode 500   # one episode (synchronous)
python scripts/link_pearls_evidence.py submit-batch             # all eligible via Batch API (50% cheaper)
python scripts/link_pearls_evidence.py collect --wait           # retrieve batch results
python scripts/link_pearls_evidence.py report                   # coverage lift vs term-overlap + adjudication counts
python scripts/link_pearls_evidence.py apply                    # merge reviewed direct/high-confidence links -> data/pearls_linked.json
```

### Adjudication loop

Review happens at **two levels**. Individual links are curated first (one bad trial
link can be rejected without discarding the pearl's good links), then the whole record
is explicitly signed off — `apply` only merges records marked `"approved"` with a
`reviewed_by` identity. `auto_triaged` and `pending` can never reach the site.

```bash
# 1. Reject one off-topic study on episode 500 (preview first with --dry-run):
python scripts/link_pearls_evidence.py adjudicate --episode 500 --trial "SPRINT" --reject --note "off-topic"
# 2. Once the pearl's surviving links check out against the show notes, sign off the record:
python scripts/link_pearls_evidence.py adjudicate --episode 500 --record --approve --reviewer "<name>" --note "checked vs primary source and show notes"
python scripts/link_pearls_evidence.py apply        # re-apply so pearls_linked.json reflects it

# Other link-level selectors: --pearl <substr>, --canonical-key <key>, --confidence low, --support background
# Apply defaults to direct, non-low-confidence links; pass --include-background or
# --include-low-confidence only for review/debug artifacts, not the public teaching view.
# Reset a link (or, with --record, the whole record) back to pending:
python scripts/link_pearls_evidence.py adjudicate --canonical-key "<key>" --reset
```

To adjudicate in bulk from captured user feedback, pass a JSON list of decision
objects (`{episode_number, canonical_key | trial, decision, note, scope}`, where
`scope: "record"` signs off the whole record instead of one link) via
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

## Research screening (owner-gated)

`scripts/trial_detail_utils.py` deliberately defers PICO (population / intervention /
comparator / outcome) extraction to "a future model-backed pass," to avoid inventing
clinical detail the show notes never stated. `scripts/screen_trials.py` is that pass —
it also drafts a **clinical bottom line and applicability boundary** for inpatient,
outpatient, preventive, diagnostic, or population-care evidence without forcing an
inpatient recommendation. Observational association is explicitly not promoted into a
recommendation. The fields stay null when source text does not support a concrete claim.
Fenced the same way as every other model-touching step in this repo:

- **Grounded in the real paper where possible, not just the abstract.** When a
  citation resolves to a PubMed ID (`scripts/pubmed_utils.resolve_pmid`), the pass
  tries to resolve an open-access full text via PubMed Central
  (`scripts/pubmed_utils.resolve_pmcid` + `fetch_pmc_fulltext`, via NCBI's free
  E-utilities, no key required) and gives the model that instead of just the
  abstract — richer grounding for both PICO and the bottom line, since abstracts
  often omit the numbers a bedside recommendation needs. Falls back to the abstract
  when no open-access full text exists, and to the podcast's own show-notes gloss
  (with an explicit instruction to be more conservative) when no PMID resolves at
  all. Every record carries a `grounded_in` flag (`"pmc_fulltext"` |
  `"pubmed_abstract"` | `"show_notes_only"`) so the site can show which is which.
- **Null discipline.** The prompt requires `null` (not a guess) for any PICO or
  clinical-bottom-line field the source text doesn't state — no filling in what a
  study "probably" found, and no generic restatement of the intervention standing in
  for an actual bottom line.
- **Owner-gated.** Output goes to `data/trial_screening.json` with
  `review_status: "pending"`. Approval requires an attributable `reviewed_by`.
  It never writes `docs/data/trials.json` directly.
  Not part of `ingest.py` — it spends tokens and makes external network calls to NCBI.
- **One evidence universe.** Screening uses the same canonical model-extracted plus
  show-note-only records as the site and linker. Batch collection is refused if the
  source-content fingerprint changed after submission.

```bash
python scripts/screen_trials.py generate --limit 5             # first 5 eligible trials, synchronous
python scripts/screen_trials.py generate --source pubmed       # skip un-groundable trials
python scripts/screen_trials.py generate --no-fulltext         # abstract-only, skip the PMC lookup
python scripts/screen_trials.py submit-batch --limit 50        # same pool via the Batch API (50% cheaper)
python scripts/screen_trials.py collect --wait                 # retrieve batch results (usually <1h)
python scripts/screen_trials.py report                          # counts + review status
python scripts/screen_trials.py adjudicate --trial "SPRINT" --approve --reviewer "<name>"
python scripts/screen_trials.py apply                            # -> data/trial_screening_approved.json
python scripts/build_site.py                                    # publish PICO/bottom-line fields
```

Defaults to `claude-sonnet-5` (override with `--model claude-opus-4-8` for
higher-stakes spot checks) — at the scale of thousands of trials this pass runs
against, cost matters more than squeezing out the last bit of model quality, and
grounding real paper text keeps quality high even on the cheaper model. Prefer
`submit-batch` + `collect` over `generate` for anything beyond a small pilot: same
output, half the price, and it doesn't tie up a terminal. Requires
`ANTHROPIC_API_KEY`.

## Visitor feedback (owner-gated)

The site is static with no backend of its own, so visitor feedback is collected by a
small self-hosted Cloudflare Worker + D1 database (`worker/`) rather than a
third-party form service — this keeps submitted data under your control instead of
a SaaS relay's. Visitors can flag a pearl or a specific pearl→trial evidence link
with a structured reason code (inaccurate / outdated / wrong citation / unclear /
other) plus an optional comment — no name/email is collected, and the caller's IP
is only hashed transiently for rate-limiting, never stored raw.

**One-time setup** (you need a free Cloudflare account):

```bash
cd worker
npx wrangler login
npx wrangler d1 create curbsiders-feedback   # copy the returned database_id into wrangler.toml
npx wrangler d1 execute curbsiders-feedback --file=schema.sql
npx wrangler secret put ADMIN_TOKEN          # a long random string; also goes in .env below
npx wrangler secret put IP_HASH_SALT         # another random string
npx wrangler deploy
```

Then:
- Put the deployed Worker's `/feedback` URL in `docs/index.html`'s
  `curbsiders-feedback-endpoint` meta tag. Until it is a valid non-placeholder HTTPS
  endpoint, the site does not render feedback controls.
- Put `FEEDBACK_WORKER_URL` (the Worker's base URL) and `CLOUDFLARE_FEEDBACK_ADMIN_TOKEN`
  (the same value as `ADMIN_TOKEN` above) into `.env`.
- Update `worker/wrangler.toml`'s `ALLOWED_ORIGIN` to your actual GitHub Pages URL.

Just like the model-assisted passes, nothing a visitor submits reaches the site
directly — it goes through the same sidecar → adjudicate → apply gate:

```bash
python scripts/import_feedback.py fetch                          # pull new rows from the Worker
python scripts/import_feedback.py report                         # counts by status/reason
python scripts/import_feedback.py adjudicate --id 42 --approve
python scripts/import_feedback.py adjudicate --pearl "SPRINT" --reject --note "not applicable, off-topic flag"
python scripts/import_feedback.py apply                           # -> data/pearl_feedback_approved.json
python scripts/build_site.py                                      # publish updated flag badges
```

`fetch` is cursor-tracked (`data/feedback_import_state.json`) and never deletes or
re-imports rows from D1, so it's safe to run repeatedly. Not part of `ingest.py` —
it's an external network call and needs credentials.

## Incremental ingest (recommended for new episodes)

Instead of running the steps above by hand, use the orchestrator, which does model
work only on episodes that are new since the last run and then rebuilds the
deterministic layers (pearls + site) and validates:

```bash
python scripts/ingest.py                            # scrape → youtube gap-fill → extract new → pearls → site → validate
python scripts/ingest.py --dry-run                  # report which episodes are new, change nothing
python scripts/ingest.py --skip-scrape               # reuse the current episodes.json
python scripts/ingest.py --skip-youtube-transcripts  # don't fill transcript gaps from YouTube captions
python scripts/ingest.py --backend batch             # extract new episodes via the OpenAI Batch API
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
  `.env` (API keys), requires `.venv` and its dependencies, takes a lock so runs can't
  overlap, and logs to `ingest.log`. Test it with `automation/run_ingest.sh --dry-run`.
- `automation/com.curbsiders.ingest.plist` — a macOS **launchd** template (weekly,
  Sundays 06:00) that runs the wrapper.
- [`automation/README.md`](automation/README.md) — install steps for launchd (macOS,
  recommended) and a cron one-liner for Linux.

The model-assisted pearl→evidence linking step stays owner-gated and out of the
scheduled path by design; run it deliberately after a new episode lands (see
`automation/README.md`).

GitHub Actions also runs a weekly live source-health check. The main CI gate remains
offline/reproducible and verifies compilation, unit tests, deterministic rebuild drift,
and repository invariants.

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

## Curbsiders-team review handoff

Start with [REVIEW_HANDOFF.md](REVIEW_HANDOFF.md) and [CURATION_GUIDE.md](CURATION_GUIDE.md).
The next implementation decision is clinical governance, not another automatic approval:

- name reviewers and choose a representative evidence-link queue;
- review links against the cited source plus show-note claim, then sign records with `--reviewer`;
- review the 23-screening pilot for PICO fidelity, applicability, and observational overreach;
- decide whether the transcript-derived candidate-pearl pass should remain private-only;
- only then expand screening/link generation and enable public feedback.

## Publishing (GitHub Pages)

The site is the `docs/` folder, published live at
**https://kramesh1.github.io/curbsiders-trials/**. Both the repository and the
GitHub Pages deployment are **public**. Pages is configured under **Settings →
Pages** with **Source: Deploy from a branch**, branch **`main`**, folder
**`/docs`** — every push to `main` that touches `docs/` redeploys the live
site automatically (usually within a minute or two; check
**Settings → Pages** or `gh api repos/<owner>/<repo>/pages/builds/latest` for
build status).

Because the repo and site are public:

- Treat everything under `data/` and `docs/data/` as world-readable. Don't add
  secrets, tokens, or non-public data to tracked files — use `.env` (already
  git-ignored) for credentials, matching `.env.example`.
- `worker/wrangler.toml` is committed and public; it holds config only
  (`ALLOWED_ORIGIN`, a placeholder D1 `database_id`), never secrets. Real
  secrets (`ADMIN_TOKEN`, `IP_HASH_SALT`) are set via `wrangler secret put`
  and never touch the repo.
- `data/transcripts.json` (full-episode transcripts) is intentionally
  git-ignored and never published — see the LICENSE data note for why.
- The site footer carries a visible AI-generation disclaimer; keep it intact
  in any template changes since the pearls/evidence links are model-assisted
  and not clinically warranted.
- `docs/.nojekyll` is present so GitHub serves the files verbatim (no Jekyll).
- The page loads `docs/data/*.json` via a relative path and pulls Fuse.js
  (pinned with an SRI hash) and Google Fonts from CDNs, so it needs network
  access at load time.

## Tests

Run:

```bash
python -m unittest discover -s tests
python scripts/validate_repository.py
python -m py_compile scripts/*.py tests/*.py
```
