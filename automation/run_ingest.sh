#!/usr/bin/env bash
#
# Scheduled entry point for the incremental ingest.
#
# Safe to run unattended on a timer (launchd/cron). The pipeline is idempotent:
# scraping and transcript fetching are free, and the paid extraction stage is
# skipped entirely when there is no new episode, so a run that finds nothing new
# spends ~0 tokens. Any extra args are passed straight through to ingest.py
# (e.g. --dry-run, --backend anthropic).
#
# Usage:
#   automation/run_ingest.sh                 # incremental run
#   automation/run_ingest.sh --dry-run       # detect new episodes, do nothing
#   INGEST_ARGS="--backend anthropic --workers 4" automation/run_ingest.sh
#
set -euo pipefail

# Resolve the repo root from this script's location (automation/ lives at the root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

LOG_FILE="${REPO_DIR}/ingest.log"
LOCK_FILE="${REPO_DIR}/ingest.lock"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"${LOG_FILE}"; }

# Load API keys and any config from .env if present (KEY=VALUE lines).
if [[ -f "${REPO_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${REPO_DIR}/.env"
    set +a
fi

# Prefer the project virtualenv; fall back to whatever python3 is on PATH.
if [[ -x "${REPO_DIR}/.venv/bin/python" ]]; then
    PYTHON="${REPO_DIR}/.venv/bin/python"
else
    PYTHON="$(command -v python3 || command -v python)"
fi

# Prevent overlapping runs. flock is available on Linux; macOS lacks it, so fall
# back to a mkdir-based lock there.
run_ingest() {
    log "=== ingest start (python=${PYTHON}) ==="
    if "${PYTHON}" scripts/ingest.py ${INGEST_ARGS:-} "$@" >>"${LOG_FILE}" 2>&1; then
        log "=== ingest ok ==="
    else
        code=$?
        log "=== ingest FAILED (exit ${code}) ==="
        return "${code}"
    fi
}

if command -v flock >/dev/null 2>&1; then
    exec 9>"${LOCK_FILE}"
    if ! flock -n 9; then
        log "another ingest run holds the lock; skipping this tick"
        exit 0
    fi
    run_ingest "$@"
else
    # macOS: atomic mkdir lock with a trap to release it.
    if ! mkdir "${LOCK_FILE}.d" 2>/dev/null; then
        log "another ingest run holds the lock; skipping this tick"
        exit 0
    fi
    trap 'rmdir "${LOCK_FILE}.d" 2>/dev/null || true' EXIT
    run_ingest "$@"
fi
