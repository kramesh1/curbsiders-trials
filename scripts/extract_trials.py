"""
Step 2: Extract clinical trials and papers from scraped show notes.
Saves to data/trials.json and data/extraction_state.json.

Resumable:
  - skips episodes already marked completed, even if they produced zero trials
  - records chunk counts, extracted counts, and last errors per episode

Backends:
  - openai    (default): OpenAI Responses API with structured outputs
  - anthropic: Claude API (requires ANTHROPIC_API_KEY)
  - ollama    : local model via the Ollama HTTP API

Usage:
  python scripts/extract_trials.py --backend openai --model gpt-4o --workers 8
  python scripts/extract_trials.py --backend anthropic --limit 5
  python scripts/extract_trials.py --backend ollama --model qwen2.5:7b --workers 4
  python scripts/extract_trials.py --retry-failures

Environment:
  OPENAI_API_KEY  Required for --backend openai
  OPENAI_MODEL    Optional default model for --backend openai
  OLLAMA_HOST     Override Ollama endpoint (default http://localhost:11434)
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
try:
    from scripts.trial_utils import (
        dedupe_trial_mentions,
        normalize_trial_record,
        recover_missing_urls_from_show_notes,
        split_show_notes_into_chunks,
    )
except ImportError:
    from trial_utils import (
        dedupe_trial_mentions,
        normalize_trial_record,
        recover_missing_urls_from_show_notes,
        split_show_notes_into_chunks,
    )

load_dotenv()

DATA_DIR = Path(__file__).parent.parent / "data"
EPISODES_FILE = DATA_DIR / "episodes.json"
TRIALS_FILE = DATA_DIR / "trials.json"
STATE_FILE = DATA_DIR / "extraction_state.json"

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
ANTHROPIC_MODEL = "claude-sonnet-4-6"
OLLAMA_MODEL = "qwen2.5:7b"  # override with --model to match `ollama list`
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_NUM_CTX = 8192  # must fit prompt (~3.5k tokens) + output headroom

REQUEST_DELAY = 0.2  # seconds between calls (ollama has no rate limit)
MAX_CHUNK_CHARS = 6000
OPENAI_TRIAL_SCHEMA = {
    "name": "trial_extraction",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "trials": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "citation_label": {"type": ["string", "null"]},
                        "paper_title": {"type": ["string", "null"]},
                        "pubmed_url": {"type": ["string", "null"]},
                        "year": {"type": ["integer", "null"]},
                        "brief_summary": {"type": ["string", "null"]},
                        "context_topic": {"type": ["string", "null"]},
                        "study_type": {"type": "string"},
                        "specialty_tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "citation_label",
                        "paper_title",
                        "pubmed_url",
                        "year",
                        "brief_summary",
                        "context_topic",
                        "study_type",
                        "specialty_tags",
                    ],
                },
            },
        },
        "required": ["trials"],
    },
}


def extract_episode_number(episode: dict) -> int | None:
    for field in (episode.get("title", ""), episode.get("url", "")):
        m = re.search(r"#?(\d{1,3})", field)
        if m:
            return int(m.group(1))
    return episode.get("episode_number")


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data, *, compact: bool = False):
    with open(path, "w") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, indent=2, ensure_ascii=False)


def build_progress_line(completed: int, total: int, *, failed: int, trial_mentions: int, width: int = 28) -> str:
    total = max(total, 1)
    ratio = completed / total
    filled = min(width, int(ratio * width))
    bar = "#" * filled + "-" * (width - filled)
    percent = int(ratio * 100)
    return (
        f"[{bar}] {completed}/{total} "
        f"({percent:3d}%) | failed {failed} | mentions {trial_mentions}"
    )


def emit_progress(completed: int, total: int, *, failed: int, trial_mentions: int, final: bool = False):
    line = build_progress_line(
        completed,
        total,
        failed=failed,
        trial_mentions=trial_mentions,
    )
    if sys.stdout.isatty():
        ending = "\n" if final else ""
        print(f"\r{line}", end=ending, flush=True)
    else:
        print(line, flush=True)


def parse_json_response(text: str) -> list[dict]:
    """Parse a JSON array from a model response, tolerating extra wrapping."""
    text = text.strip()
    # Strip <think>...</think> blocks emitted by reasoning models (e.g. Qwen).
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Strip markdown code fences if present.
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: grab the outermost JSON array.
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1 or end < start:
            raise
        data = json.loads(text[start:end + 1])
    # Some models wrap the array in an object, e.g. {"trials": [...]}.
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
        return []
    return data if isinstance(data, list) else []


def build_extraction_prompt(episode: dict, chunk_text: str, chunk_index: int, total_chunks: int) -> str:
    return """\
You are a medical librarian extracting clinical evidence citations from podcast show notes.

Episode: #{episode_number} — {episode_title}
URL: {episode_url}
Chunk: {chunk_index} of {total_chunks}

From the show notes chunk below, extract every clinical trial, observational study,
systematic review, meta-analysis, and clinical guideline that is explicitly referenced.

Important rules:
- Extract only literature actually mentioned in this chunk.
- If the same citation appears multiple times in this chunk, return it once.
- Do not invent missing publication details.
- Return ONLY valid JSON.
- If no relevant medical literature is found, return an empty trials list.

Return the extracted records using these fields:
- "citation_label": Name used in the show notes
- "paper_title": Full paper title if inferable, otherwise null
- "pubmed_url": Article URL if hyperlinked (PubMed or publisher link), otherwise null
- "year": Publication year if mentioned, otherwise null
- "brief_summary": 1-2 sentences on what this study found and why it matters clinically,
  using only information present in the show notes
- "context_topic": The clinical question this citation addresses
- "study_type": One of "RCT", "observational", "meta-analysis", "systematic review",
  "guideline", "case series", "other"
- "specialty_tags": JSON array drawn only from:
  cardiology, infectious disease, pulmonology, nephrology, endocrinology,
  gastroenterology, neurology, hematology, oncology, preventive medicine,
  rheumatology, dermatology, psychiatry, geriatrics, emergency medicine,
  general internal medicine

Do NOT include:
- Cross-references to other podcast episodes
- Apps, websites, social media, or news links
- Non-peer-reviewed resources

Show notes chunk:
{show_notes}
""".format(
        episode_number=extract_episode_number(episode) or "?",
        episode_title=episode.get("title", "Unknown"),
        episode_url=episode.get("url", ""),
        chunk_index=chunk_index,
        total_chunks=total_chunks,
        show_notes=chunk_text,
    )


# ── Backends ──────────────────────────────────────────────────────────────────

def call_openai(client, model: str, prompt: str) -> str:
    response = client.responses.create(
        model=model,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": OPENAI_TRIAL_SCHEMA["name"],
                "strict": OPENAI_TRIAL_SCHEMA["strict"],
                "schema": OPENAI_TRIAL_SCHEMA["schema"],
            }
        },
    )
    return response.output_text


def call_anthropic(client, model: str, prompt: str) -> str:
    # No temperature: the 4.7/4.8-era models (and Sonnet 5) reject temperature/top_p/top_k
    # with a 400, so we omit it and rely on the prompt for determinism. Older models that
    # still accept it are unaffected by its absence (they default to a low sampling temp).
    message = client.messages.create(
        model=model,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def call_ollama(model: str, prompt: str) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",  # constrain output to valid JSON
        "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())
    return data["message"]["content"]


def extract_trials_from_episode(episode: dict, call_model) -> tuple[list[dict], dict]:
    show_notes = episode.get("show_notes", "")
    if not show_notes or len(show_notes) < 50:
        return [], {"chunk_count": 0, "raw_mentions": 0}

    chunks = split_show_notes_into_chunks(show_notes, max_chars=MAX_CHUNK_CHARS)
    ep_num = extract_episode_number(episode)
    ep_title = episode.get("title", "Unknown")
    ep_url = episode.get("url", "")

    extracted = []
    for chunk_index, chunk_text in enumerate(chunks, start=1):
        prompt = build_extraction_prompt(episode, chunk_text, chunk_index, len(chunks))
        chunk_trials = parse_json_response(call_model(prompt))
        chunk_trials = recover_missing_urls_from_show_notes(chunk_trials, chunk_text)
        for trial in chunk_trials:
            if not isinstance(trial, dict):
                continue
            normalized = normalize_trial_record({
                **trial,
                "episode_number": ep_num,
                "episode_title": ep_title,
                "episode_url": ep_url,
                "episode_date": episode.get("date", ""),
            })
            if normalized:
                extracted.append(normalized)

    deduped = dedupe_trial_mentions(extracted)
    metrics = {
        "chunk_count": len(chunks),
        "raw_mentions": len(extracted),
        "deduped_mentions": len(deduped),
    }
    return deduped, metrics


def completed_episode_urls(state: dict, *, retry_failures: bool) -> set[str]:
    completed = set()
    for url, info in state.items():
        if info.get("status") == "completed":
            completed.add(url)
        elif info.get("status") == "failed" and not retry_failures:
            completed.add(url)
    return completed


def pending_episodes(episodes: list[dict], state: dict, *, retry_failures: bool, limit: int | None) -> list[dict]:
    processed_urls = completed_episode_urls(state, retry_failures=retry_failures)
    todo = [episode for episode in episodes if episode.get("url", "") not in processed_urls]
    if limit is not None:
        todo = todo[:limit]
    return todo


def process_episode(episode: dict, call_model) -> dict:
    ep_num = extract_episode_number(episode)
    started_at = utc_timestamp()

    try:
        trials, metrics = extract_trials_from_episode(episode, call_model)
        result = {
            "episode_url": episode.get("url", ""),
            "episode_number": ep_num,
            "episode_title": episode.get("title", ""),
            "status": "completed",
            "processed_at": utc_timestamp(),
            "started_at": started_at,
            "chunk_count": metrics["chunk_count"],
            "raw_mentions": metrics["raw_mentions"],
            "deduped_mentions": metrics["deduped_mentions"],
            "error": None,
            "trials": trials,
        }
    except json.JSONDecodeError as e:
        result = {
            "episode_url": episode.get("url", ""),
            "episode_number": ep_num,
            "episode_title": episode.get("title", ""),
            "status": "failed",
            "processed_at": utc_timestamp(),
            "started_at": started_at,
            "chunk_count": 0,
            "raw_mentions": 0,
            "deduped_mentions": 0,
            "error": f"JSONDecodeError: {e}",
            "trials": [],
        }
    except Exception as e:
        result = {
            "episode_url": episode.get("url", ""),
            "episode_number": ep_num,
            "episode_title": episode.get("title", ""),
            "status": "failed",
            "processed_at": utc_timestamp(),
            "started_at": started_at,
            "chunk_count": 0,
            "raw_mentions": 0,
            "deduped_mentions": 0,
            "error": f"{type(e).__name__}: {e}",
            "trials": [],
        }

    if REQUEST_DELAY:
        time.sleep(REQUEST_DELAY)
    return result


def flatten_trials_by_episode(all_trials_by_episode: dict[str, list[dict]]) -> list[dict]:
    trials = []
    for episode_url in sorted(all_trials_by_episode):
        trials.extend(all_trials_by_episode[episode_url])
    return trials


def merge_episode_result(all_trials_by_episode: dict[str, list[dict]], state: dict, result: dict) -> int:
    episode_url = result["episode_url"]
    old_count = len(all_trials_by_episode.get(episode_url, []))
    if result["status"] == "completed":
        all_trials_by_episode[episode_url] = result["trials"]

    state[episode_url] = {
        "status": result["status"],
        "episode_number": result["episode_number"],
        "episode_title": result["episode_title"],
        "processed_at": result["processed_at"],
        "started_at": result["started_at"],
        "chunk_count": result["chunk_count"],
        "raw_mentions": result["raw_mentions"],
        "deduped_mentions": result["deduped_mentions"],
        "error": result["error"],
    }
    return len(result["trials"]) - old_count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["openai", "ollama", "anthropic"], default="openai",
                        help="Which model backend to use (default: openai)")
    parser.add_argument("--model", default=None,
                        help="Model name (defaults per backend)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process this many new episodes (for testing)")
    parser.add_argument("--retry-failures", action="store_true",
                        help="Retry episodes previously marked failed")
    parser.add_argument("--workers", type=int, default=1,
                        help="Parallel episode workers (default: 1)")
    args = parser.parse_args()

    if not EPISODES_FILE.exists():
        print(f"Error: {EPISODES_FILE} not found. Run scrape_episodes.py first.")
        return
    if args.workers < 1:
        raise ValueError("--workers must be >= 1")

    # Build the model-call closure for the chosen backend.
    if args.backend == "openai":
        from openai import OpenAI
        client = OpenAI()
        model = args.model or OPENAI_MODEL
        call_model = lambda prompt: call_openai(client, model, prompt)
    elif args.backend == "anthropic":
        import anthropic
        client = anthropic.Anthropic()
        model = args.model or ANTHROPIC_MODEL
        call_model = lambda prompt: call_anthropic(client, model, prompt)
    else:
        model = args.model or OLLAMA_MODEL
        call_model = lambda prompt: call_ollama(model, prompt)
    print(f"Backend: {args.backend} | model: {model}")
    if args.backend == "ollama":
        print(f"Ollama host: {OLLAMA_HOST}")

    episodes = load_json(EPISODES_FILE, [])
    print(f"Loaded {len(episodes)} episodes")

    all_trials = load_json(TRIALS_FILE, [])
    state = load_json(STATE_FILE, {})
    processed_urls = completed_episode_urls(state, retry_failures=args.retry_failures)
    todo = pending_episodes(
        episodes,
        state,
        retry_failures=args.retry_failures,
        limit=args.limit,
    )
    all_trials_by_episode: dict[str, list[dict]] = {}
    for trial in all_trials:
        episode_url = trial.get("episode_url")
        if episode_url:
            all_trials_by_episode.setdefault(episode_url, []).append(trial)

    print(f"Resuming: {len(processed_urls)} completed episodes, {len(all_trials)} trial mentions so far")
    print(f"Pending episodes this run: {len(todo)} | workers: {args.workers}")
    emit_progress(
        0,
        len(todo),
        failed=0,
        trial_mentions=len(all_trials),
        final=False,
    )

    new_trial_count = 0
    processed_count = 0

    def handle_result(result: dict) -> None:
        nonlocal new_trial_count, processed_count
        processed_count += 1
        delta = merge_episode_result(all_trials_by_episode, state, result)
        if delta > 0:
            new_trial_count += delta
        if result["status"] != "completed":
            print(f"  -> Error: {result['error']}")
        current_mentions = sum(len(v) for v in all_trials_by_episode.values())
        save_json(TRIALS_FILE, flatten_trials_by_episode(all_trials_by_episode))
        save_json(STATE_FILE, state)
        emit_progress(
            processed_count,
            len(todo),
            failed=sum(1 for info in state.values() if info.get("status") == "failed"),
            trial_mentions=current_mentions,
            final=processed_count == len(todo),
        )

    if args.workers == 1:
        for episode in todo:
            handle_result(process_episode(episode, call_model))
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process_episode, episode, call_model): episode for episode in todo}
            for future in as_completed(futures):
                handle_result(future.result())

    final_trials = flatten_trials_by_episode(all_trials_by_episode)
    save_json(TRIALS_FILE, final_trials)
    save_json(STATE_FILE, state)

    print(f"\nDone. {len(final_trials)} total trials ({new_trial_count} new) saved to {TRIALS_FILE}")
    completed = sum(1 for info in state.values() if info.get("status") == "completed")
    failed = sum(1 for info in state.values() if info.get("status") == "failed")
    print(f"State written to {STATE_FILE} ({completed} completed, {failed} failed)")


if __name__ == "__main__":
    main()
