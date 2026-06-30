"""
Batch extraction workflow for Curbsiders trial mentions using the OpenAI Batch API.

Subcommands:
  submit    Build a batch input file, upload it, and create a batch job
  status    Check the status of an existing batch job
  download  Download completed batch outputs and merge them into local data files
  run       Submit a batch, poll until terminal state, then download results

Examples:
  python scripts/extract_trials_batch.py run --limit 10 --include-completed
  python scripts/extract_trials_batch.py status --batch-dir data/batches/trials_20260628_123456
  python scripts/extract_trials_batch.py download --batch-dir data/batches/trials_20260628_123456
"""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from openai import OpenAI

try:
    from scripts.extract_trials import (
        DATA_DIR,
        EPISODES_FILE,
        OPENAI_MODEL,
        OPENAI_TRIAL_SCHEMA,
        STATE_FILE,
        TRIALS_FILE,
        build_extraction_prompt,
        build_progress_line,
        completed_episode_urls,
        extract_episode_number,
        flatten_trials_by_episode,
        load_json,
        merge_episode_result,
        parse_json_response,
        save_json,
    )
    from scripts.trial_utils import (
        dedupe_trial_mentions,
        normalize_trial_record,
        split_show_notes_into_chunks,
    )
except ImportError:
    from extract_trials import (
        DATA_DIR,
        EPISODES_FILE,
        OPENAI_MODEL,
        OPENAI_TRIAL_SCHEMA,
        STATE_FILE,
        TRIALS_FILE,
        build_extraction_prompt,
        build_progress_line,
        completed_episode_urls,
        extract_episode_number,
        flatten_trials_by_episode,
        load_json,
        merge_episode_result,
        parse_json_response,
        save_json,
    )
    from trial_utils import dedupe_trial_mentions, normalize_trial_record, split_show_notes_into_chunks


BATCHES_DIR = DATA_DIR / "batches"
REQUESTS_FILENAME = "requests.jsonl"
MANIFEST_FILENAME = "manifest.json"
BATCH_INFO_FILENAME = "batch_info.json"
OUTPUT_FILENAME = "batch_output.jsonl"
ERROR_FILENAME = "batch_errors.jsonl"


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def load_batch_episode_urls(batch_dir: Path) -> set[str]:
    manifest_path = batch_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return set()
    manifest = load_json(manifest_path, {})
    requests = manifest.get("requests", {})
    return {
        entry["episode_url"]
        for entry in requests.values()
        if entry.get("episode_url")
    }


def collect_excluded_episode_urls(batch_dirs: list[str] | None) -> set[str]:
    excluded = set()
    for batch_dir in batch_dirs or []:
        excluded.update(load_batch_episode_urls(Path(batch_dir)))
    return excluded


def select_episodes(
    episodes: list[dict],
    state: dict,
    *,
    limit: int | None,
    include_completed: bool,
    retry_failures: bool,
    excluded_episode_urls: set[str] | None = None,
) -> list[dict]:
    excluded_episode_urls = excluded_episode_urls or set()
    if include_completed:
        selected = [
            episode for episode in episodes
            if episode.get("url", "") not in excluded_episode_urls
        ]
        selected = selected[:limit] if limit is not None else selected
        return selected

    processed = completed_episode_urls(state, retry_failures=retry_failures)
    pending = [
        episode for episode in episodes
        if episode.get("url", "") not in processed
        and episode.get("url", "") not in excluded_episode_urls
    ]
    if limit is not None:
        pending = pending[:limit]
    return pending


def slugify_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def create_batch_dir() -> Path:
    BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    batch_dir = BATCHES_DIR / f"trials_{slugify_timestamp()}"
    batch_dir.mkdir(parents=True, exist_ok=False)
    return batch_dir


def build_chat_batch_request(custom_id: str, prompt: str, model: str) -> dict:
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": OPENAI_TRIAL_SCHEMA["name"],
                    "strict": OPENAI_TRIAL_SCHEMA["strict"],
                    "schema": OPENAI_TRIAL_SCHEMA["schema"],
                },
            },
        },
    }


def write_batch_input(batch_dir: Path, episodes: list[dict], model: str) -> tuple[Path, dict]:
    requests_path = batch_dir / REQUESTS_FILENAME
    manifest = {
        "created_at": utc_timestamp(),
        "model": model,
        "episode_count": len(episodes),
        "requests": {},
    }

    with open(requests_path, "w") as f:
        request_index = 0
        for episode in episodes:
            show_notes = episode.get("show_notes", "")
            chunks = split_show_notes_into_chunks(show_notes, max_chars=6000) if show_notes else []
            total_chunks = len(chunks)
            for chunk_index, chunk_text in enumerate(chunks, start=1):
                custom_id = f"req-{request_index:05d}"
                prompt = build_extraction_prompt(episode, chunk_text, chunk_index, total_chunks)
                request = build_chat_batch_request(custom_id, prompt, model)
                f.write(json.dumps(request, ensure_ascii=False) + "\n")
                manifest["requests"][custom_id] = {
                    "episode_url": episode.get("url", ""),
                    "episode_number": extract_episode_number(episode),
                    "episode_title": episode.get("title", ""),
                    "episode_date": episode.get("date", ""),
                    "chunk_index": chunk_index,
                    "total_chunks": total_chunks,
                }
                request_index += 1

    manifest["request_count"] = len(manifest["requests"])
    save_json(batch_dir / MANIFEST_FILENAME, manifest)
    return requests_path, manifest


def save_batch_info(batch_dir: Path, payload: dict):
    save_json(batch_dir / BATCH_INFO_FILENAME, payload)


def load_batch_info(batch_dir: Path) -> dict:
    info_path = batch_dir / BATCH_INFO_FILENAME
    if not info_path.exists():
        raise FileNotFoundError(f"Missing batch info file: {info_path}")
    return load_json(info_path, {})


def latest_batch_dir() -> Path:
    if not BATCHES_DIR.exists():
        raise FileNotFoundError("No batch directory exists yet.")
    batch_dirs = [p for p in BATCHES_DIR.iterdir() if p.is_dir()]
    if not batch_dirs:
        raise FileNotFoundError("No saved batch runs found.")
    return max(batch_dirs, key=lambda p: p.stat().st_mtime)


def resolve_batch_dir(path_arg: str | None) -> Path:
    return Path(path_arg) if path_arg else latest_batch_dir()


def object_to_plain_dict(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return obj
    raise TypeError(f"Unsupported object type: {type(obj).__name__}")


def print_batch_status(batch: dict):
    counts = batch.get("request_counts") or {}
    total = counts.get("total", 0)
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    print(f"Batch: {batch.get('id')}")
    print(f"Status: {batch.get('status')}")
    if total:
        print(build_progress_line(completed + failed, total, failed=failed, trial_mentions=0))
    else:
        print("Request counts not available yet.")


def submit_batch(args) -> Path:
    client = OpenAI()
    model = args.model or OPENAI_MODEL
    episodes = load_json(EPISODES_FILE, [])
    state = load_json(STATE_FILE, {})
    excluded_episode_urls = collect_excluded_episode_urls(args.exclude_batch_dir)
    selected = select_episodes(
        episodes,
        state,
        limit=args.limit,
        include_completed=args.include_completed,
        retry_failures=args.retry_failures,
        excluded_episode_urls=excluded_episode_urls,
    )
    if not selected:
        raise ValueError("No episodes selected for batch submission.")

    batch_dir = create_batch_dir()
    requests_path, manifest = write_batch_input(batch_dir, selected, model)

    with open(requests_path, "rb") as fh:
        uploaded = client.files.create(file=fh, purpose="batch")
    uploaded_dict = object_to_plain_dict(uploaded)
    batch = client.batches.create(
        input_file_id=uploaded.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
        metadata={
            "description": f"curbsiders-trial-extraction:{batch_dir.name}",
            "episode_count": str(len(selected)),
            "request_count": str(manifest["request_count"]),
        },
    )
    batch_dict = object_to_plain_dict(batch)
    save_batch_info(
        batch_dir,
        {
            "created_at": utc_timestamp(),
            "model": model,
            "batch_dir": str(batch_dir),
            "input_file": uploaded_dict,
            "batch": batch_dict,
            "episode_count": len(selected),
            "request_count": manifest["request_count"],
            "include_completed": args.include_completed,
            "retry_failures": args.retry_failures,
            "limit": args.limit,
        },
    )

    print(f"Batch dir: {batch_dir}")
    print(f"Input requests: {manifest['request_count']} across {len(selected)} episodes")
    if excluded_episode_urls:
        print(f"Excluded episode URLs from prior batch manifests: {len(excluded_episode_urls)}")
    print_batch_status(batch_dict)
    return batch_dir


def refresh_batch(batch_dir: Path) -> dict:
    client = OpenAI()
    info = load_batch_info(batch_dir)
    batch_id = info["batch"]["id"]
    batch = client.batches.retrieve(batch_id)
    batch_dict = object_to_plain_dict(batch)
    info["batch"] = batch_dict
    info["refreshed_at"] = utc_timestamp()
    save_batch_info(batch_dir, info)
    return info


def extract_file_text(file_response) -> str:
    text = getattr(file_response, "text", None)
    if callable(text):
        return text()
    if isinstance(text, str):
        return text
    content = getattr(file_response, "content", None)
    if isinstance(content, bytes):
        return content.decode("utf-8")
    raise TypeError("Could not read text from file response.")


def download_file_if_present(client: OpenAI, file_id: str | None, destination: Path) -> str | None:
    if not file_id:
        return None
    payload = extract_file_text(client.files.content(file_id))
    destination.write_text(payload)
    return payload


def build_episode_results_from_batch(batch_dir: Path, output_payload: str | None, error_payload: str | None) -> dict[str, dict]:
    manifest = load_json(batch_dir / MANIFEST_FILENAME, {})
    requests = manifest.get("requests", {})
    by_episode: dict[str, dict] = {}

    def ensure_episode(entry: dict) -> dict:
        episode_url = entry["episode_url"]
        bucket = by_episode.setdefault(
            episode_url,
            {
                "episode_url": episode_url,
                "episode_number": entry.get("episode_number"),
                "episode_title": entry.get("episode_title", ""),
                "episode_date": entry.get("episode_date", ""),
                "expected_chunks": entry.get("total_chunks", 0),
                "received_chunks": 0,
                "raw_trials": [],
                "errors": [],
            },
        )
        return bucket

    if output_payload:
        for line in output_payload.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            manifest_entry = requests[row["custom_id"]]
            episode = ensure_episode(manifest_entry)
            body = row.get("response", {}).get("body", {})
            content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
            trials = parse_json_response(content)
            episode["received_chunks"] += 1
            for trial in trials:
                normalized = normalize_trial_record(
                    {
                        **trial,
                        "episode_number": manifest_entry.get("episode_number"),
                        "episode_title": manifest_entry.get("episode_title"),
                        "episode_url": manifest_entry.get("episode_url"),
                        "episode_date": manifest_entry.get("episode_date"),
                    }
                )
                if normalized:
                    episode["raw_trials"].append(normalized)

    if error_payload:
        for line in error_payload.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            manifest_entry = requests[row["custom_id"]]
            episode = ensure_episode(manifest_entry)
            episode["errors"].append(row.get("error") or {"message": "Unknown batch error"})

    return by_episode


def ingest_batch_results(batch_dir: Path):
    client = OpenAI()
    info = refresh_batch(batch_dir)
    batch = info["batch"]
    output_payload = download_file_if_present(client, batch.get("output_file_id"), batch_dir / OUTPUT_FILENAME)
    error_payload = download_file_if_present(client, batch.get("error_file_id"), batch_dir / ERROR_FILENAME)

    episodes = build_episode_results_from_batch(batch_dir, output_payload, error_payload)
    all_trials = load_json(TRIALS_FILE, [])
    state = load_json(STATE_FILE, {})
    all_trials_by_episode: dict[str, list[dict]] = {}
    for trial in all_trials:
        episode_url = trial.get("episode_url")
        if episode_url:
            all_trials_by_episode.setdefault(episode_url, []).append(trial)

    completed = 0
    failed = 0
    for episode_url, episode in episodes.items():
        has_errors = bool(episode["errors"])
        missing_chunks = episode["received_chunks"] != episode["expected_chunks"]
        if has_errors or missing_chunks:
            result = {
                "episode_url": episode_url,
                "episode_number": episode.get("episode_number"),
                "episode_title": episode.get("episode_title", ""),
                "status": "failed",
                "processed_at": utc_timestamp(),
                "started_at": None,
                "chunk_count": episode.get("expected_chunks", 0),
                "raw_mentions": len(episode["raw_trials"]),
                "deduped_mentions": 0,
                "error": json.dumps(
                    {
                        "missing_chunks": missing_chunks,
                        "errors": episode["errors"],
                    },
                    ensure_ascii=False,
                ),
                "trials": [],
            }
            failed += 1
        else:
            deduped = dedupe_trial_mentions(episode["raw_trials"])
            result = {
                "episode_url": episode_url,
                "episode_number": episode.get("episode_number"),
                "episode_title": episode.get("episode_title", ""),
                "status": "completed",
                "processed_at": utc_timestamp(),
                "started_at": None,
                "chunk_count": episode.get("expected_chunks", 0),
                "raw_mentions": len(episode["raw_trials"]),
                "deduped_mentions": len(deduped),
                "error": None,
                "trials": deduped,
            }
            completed += 1

        merge_episode_result(all_trials_by_episode, state, result)

    final_trials = flatten_trials_by_episode(all_trials_by_episode)
    save_json(TRIALS_FILE, final_trials)
    save_json(STATE_FILE, state)

    print(f"Ingested batch results from {batch_dir}")
    print(f"Episodes completed: {completed}")
    print(f"Episodes failed: {failed}")
    print(f"Total stored trial mentions: {len(final_trials)}")


def wait_for_batch(batch_dir: Path, poll_interval: int, max_wait_minutes: int) -> dict:
    deadline = time.time() + (max_wait_minutes * 60)
    terminal_statuses = {"completed", "failed", "expired", "cancelled"}
    while True:
        info = refresh_batch(batch_dir)
        batch = info["batch"]
        print_batch_status(batch)
        if batch.get("status") in terminal_statuses:
            return info
        if time.time() >= deadline:
            raise TimeoutError(
                f"Batch {batch.get('id')} did not reach a terminal state within {max_wait_minutes} minutes."
            )
        time.sleep(poll_interval)


def command_submit(args):
    submit_batch(args)


def command_status(args):
    batch_dir = resolve_batch_dir(args.batch_dir)
    info = refresh_batch(batch_dir)
    print(f"Batch dir: {batch_dir}")
    print_batch_status(info["batch"])


def command_download(args):
    batch_dir = resolve_batch_dir(args.batch_dir)
    info = refresh_batch(batch_dir)
    status = info["batch"].get("status")
    if status not in {"completed", "expired", "cancelled", "failed"}:
        raise RuntimeError(f"Batch is not finished yet. Current status: {status}")
    ingest_batch_results(batch_dir)


def command_run(args):
    batch_dir = submit_batch(args)
    info = wait_for_batch(batch_dir, args.poll_interval, args.max_wait_minutes)
    if info["batch"].get("status") != "completed":
        raise RuntimeError(f"Batch finished with status {info['batch'].get('status')}.")
    ingest_batch_results(batch_dir)


def build_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_selection_args(sp):
        sp.add_argument("--model", default=None, help="Model name (default from OPENAI_MODEL or gpt-4o)")
        sp.add_argument("--limit", type=int, default=None, help="Number of episodes to include")
        sp.add_argument("--include-completed", action="store_true", help="Include already completed episodes")
        sp.add_argument("--retry-failures", action="store_true", help="Retry episodes currently marked failed")
        sp.add_argument(
            "--exclude-batch-dir",
            action="append",
            default=[],
            help="Batch directory whose episode URLs should be excluded from selection. Repeatable.",
        )

    submit = subparsers.add_parser("submit")
    add_selection_args(submit)
    submit.set_defaults(func=command_submit)

    status = subparsers.add_parser("status")
    status.add_argument("--batch-dir", default=None, help="Batch directory to inspect (defaults to most recent)")
    status.set_defaults(func=command_status)

    download = subparsers.add_parser("download")
    download.add_argument("--batch-dir", default=None, help="Batch directory to download (defaults to most recent)")
    download.set_defaults(func=command_download)

    run = subparsers.add_parser("run")
    add_selection_args(run)
    run.add_argument("--poll-interval", type=int, default=30, help="Polling interval in seconds")
    run.add_argument("--max-wait-minutes", type=int, default=20, help="Maximum wait time before timing out")
    run.set_defaults(func=command_run)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
