# Automating the weekly ingest

`scripts/ingest.py` picks up new Curbsiders episodes and rebuilds the deterministic
layers: enrichment, pearls, the show-note evidence hyperlink inventory, and the
published site JSON. It is **idempotent and safe to run on a timer**: scraping and
transcript fetching are free, and the paid trial-extraction stage is skipped entirely
when no new episode is pending — so a run that finds nothing new spends ~0 tokens.

Everything here drives that from a schedule. `run_ingest.sh` is the entry point:
it resolves the repo, loads `.env` (for `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`),
requires and executes `.venv/bin/python`, takes a lock so runs can't overlap, and
appends to `ingest.log`. It fails visibly if the virtual environment or dependencies
are absent; set `CURBSIDERS_ALLOW_SYSTEM_PYTHON=1` only for an intentional override.

## Quick test (no schedule, no spend)

```bash
automation/run_ingest.sh --dry-run      # detect new episodes, do nothing
tail -f ingest.log
```

Pass extra flags straight through, or set `INGEST_ARGS`:

```bash
INGEST_ARGS="--backend anthropic --workers 4" automation/run_ingest.sh
```

## macOS — launchd (recommended)

1. Point the template at your checkout and install it:

   ```bash
   REPO="$(pwd)"
   mkdir -p ~/Library/LaunchAgents
   sed "s#__REPO__#${REPO}#g" automation/com.curbsiders.ingest.plist \
     > ~/Library/LaunchAgents/com.curbsiders.ingest.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.curbsiders.ingest.plist
   ```

   (On older macOS: `launchctl load ~/Library/LaunchAgents/com.curbsiders.ingest.plist`.)

2. It runs every **Sunday 06:00** local time. Trigger a run now to verify:

   ```bash
   launchctl kickstart -k gui/$(id -u)/com.curbsiders.ingest
   ```

3. Remove it:

   ```bash
   launchctl bootout gui/$(id -u)/com.curbsiders.ingest
   rm ~/Library/LaunchAgents/com.curbsiders.ingest.plist
   ```

Note: launchd jobs run in a minimal environment. Keep API keys in the repo's
`.env` (loaded by `run_ingest.sh`), not in your interactive shell profile.

## Linux / cron

```cron
# Sunday 06:00 — weekly incremental ingest
0 6 * * 0  /path/to/curbsiders_to_trials/automation/run_ingest.sh
```

## What the schedule does *not* do

Model **generation** for pearl→evidence links stays owner-gated — it spends tokens
and is not scheduled. `ingest.py` does re-apply already attributable approvals so
published sidecars cannot go stale. After a run brings in a new episode, generate
and adjudicate deliberately:

```bash
python scripts/link_pearls_evidence.py generate --episode <N>   # or submit-batch
python scripts/link_pearls_evidence.py adjudicate --episode <N> --trial "<bad study>" --reject
python scripts/link_pearls_evidence.py adjudicate --episode <N> --record --approve --reviewer "<name>"
python scripts/link_pearls_evidence.py apply
```

`ingest.log` and the lock files are git-ignored.
