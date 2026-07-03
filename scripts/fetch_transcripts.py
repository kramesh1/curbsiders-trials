"""
Harvest official full-episode transcripts.

Many Curbsiders episodes (~94/555, concentrated in the CME era ~#247-424) publish
an official transcript PDF/DOCX on the show's own WordPress uploads, linked from the
show notes. These are human/CME-reviewed, so they are the highest-fidelity
full-episode text obtainable — and, unlike YouTube auto-captions or Whisper, they
carry no ASR error risk. That matters here: the project's whole point is teaching
content trustworthy enough for the bedside, so transcripts derived from speech
recognition should never be confused with these.

This script downloads each linked transcript (through the same WAF-bypassing Chrome
impersonation the scraper uses), extracts its text, and stores it to
data/transcripts.json keyed by episode url. It is resumable — already-fetched
transcripts are skipped — and spends no model tokens.

The harvested text is intended as a *search / context corpus* and as input to an
owner-gated candidate-pearl pass, NOT as a source for auto-published verbatim
pearls; the deterministic pearl layer stays anchored to the show notes.

Usage:
  python scripts/fetch_transcripts.py            # fetch transcripts we don't have yet
  python scripts/fetch_transcripts.py --refresh  # re-fetch everything
  python scripts/fetch_transcripts.py --limit 5  # fetch at most 5 (for testing)
  python scripts/fetch_transcripts.py --report   # print coverage and exit
"""

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

try:
    from scripts.scrape_episodes import extract_transcript_url, fetch, make_session
    from scripts.extract_trials import (
        DATA_DIR,
        EPISODES_FILE,
        load_json,
        save_json,
    )
except ImportError:
    from scrape_episodes import extract_transcript_url, fetch, make_session
    from extract_trials import DATA_DIR, EPISODES_FILE, load_json, save_json

TRANSCRIPTS_FILE = DATA_DIR / "transcripts.json"
REQUEST_DELAY = 1.0  # seconds between downloads, matching the scraper


def transcript_url_for(episode: dict) -> str | None:
    """The episode's official transcript URL, preferring the stored field but
    falling back to re-deriving it from show notes for records scraped before the
    transcript_url field existed."""
    return episode.get("transcript_url") or extract_transcript_url(episode.get("show_notes", ""))


# A few "official" transcripts (e.g. some Hotcakes recaps) are actually the show's
# own AI-generated transcripts, which carry a disclaimer near the top. They still
# beat scraping captions ourselves, but a consumer weighing fidelity should know one
# is ASR-derived — so we flag it rather than treating every official file as reviewed.
_AI_DISCLAIMER_RE = re.compile(r"ai[\s-]generated|automated transcript", re.IGNORECASE)


def looks_ai_generated(text: str) -> bool:
    """True if the transcript's opening carries an AI-generated/automated disclaimer."""
    return bool(_AI_DISCLAIMER_RE.search(text[:800]))


def clean_text(text: str) -> str:
    """Collapse the ragged whitespace PDF/DOCX extraction leaves behind."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)      # trailing spaces on lines
    text = re.sub(r"\n{3,}", "\n\n", text)      # runs of blank lines
    return text.strip()


def extract_pdf_text(content: bytes) -> str:
    import fitz  # PyMuPDF; imported lazily so --report needs no PDF deps

    with fitz.open(stream=content, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def extract_docx_text(content: bytes) -> str:
    import docx  # python-docx

    document = docx.Document(BytesIO(content))
    return "\n".join(p.text for p in document.paragraphs)


def extract_text(url: str, content: bytes) -> str:
    """Extract plain text from a downloaded transcript by file type.

    Nearly all official transcripts are PDFs (some named *.docx.pdf). We branch on
    the real trailing extension and, when a .docx download is actually a PDF, fall
    back to PDF parsing so a mislabeled URL doesn't lose an episode.
    """
    ext = url.lower().split("?", 1)[0].rsplit(".", 1)[-1]
    if ext == "pdf":
        return clean_text(extract_pdf_text(content))
    if ext in ("docx", "doc"):
        if content[:4] == b"%PDF":  # mislabeled: it's really a PDF
            return clean_text(extract_pdf_text(content))
        return clean_text(extract_docx_text(content))
    # Unknown extension: sniff the magic bytes.
    if content[:4] == b"%PDF":
        return clean_text(extract_pdf_text(content))
    raise ValueError(f"Unsupported transcript file type: {url}")


def build_report(episodes: list[dict], transcripts: dict[str, dict]) -> str:
    from collections import Counter

    linked = [e for e in episodes if transcript_url_for(e)]
    fetched = [e for e in linked if transcripts.get(e["url"], {}).get("text")]
    by_bucket = Counter(
        (e.get("episode_number") // 100) * 100
        for e in fetched
        if e.get("episode_number")
    )
    lines = [
        "=== Transcript coverage ===",
        f"  Episodes total:            {len(episodes)}",
        f"  Episodes linking a transcript: {len(linked)}",
        f"  Transcripts fetched:       {len(fetched)}/{len(linked)}",
    ]
    for bucket in sorted(by_bucket):
        lines.append(f"    #{bucket}-{bucket + 99}: {by_bucket[bucket]}")
    missing = [e for e in linked if not transcripts.get(e["url"], {}).get("text")]
    if missing:
        lines.append(f"  Not yet fetched:           {len(missing)}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Re-fetch transcripts we already have")
    parser.add_argument("--limit", type=int, default=None, help="Fetch at most N transcripts (for testing)")
    parser.add_argument("--report", action="store_true", help="Print coverage and exit without fetching")
    args = parser.parse_args()

    if not EPISODES_FILE.exists():
        print(f"Error: {EPISODES_FILE} not found. Run scrape_episodes.py first.")
        return 1

    episodes = load_json(EPISODES_FILE, [])
    existing = {row["episode_url"]: row for row in load_json(TRANSCRIPTS_FILE, [])}

    # Backfill the ai_generated flag on records saved before it existed (no network).
    backfilled = False
    for row in existing.values():
        if "ai_generated" not in row and row.get("text"):
            row["ai_generated"] = looks_ai_generated(row["text"])
            backfilled = True
    if backfilled and not args.report:
        save_json(TRANSCRIPTS_FILE, _sorted_rows(existing))
        print("Backfilled ai_generated flag on existing transcripts.")

    if args.report:
        print(build_report(episodes, existing))
        return 0

    # Episodes that link a transcript we don't already have text for.
    pending = []
    for episode in episodes:
        url = transcript_url_for(episode)
        if not url:
            continue
        if not args.refresh and existing.get(episode["url"], {}).get("text"):
            continue
        pending.append((episode, url))

    if args.limit is not None:
        pending = pending[: args.limit]

    linked_total = sum(1 for e in episodes if transcript_url_for(e))
    print(f"Episodes linking a transcript: {linked_total} | to fetch now: {len(pending)}")
    if not pending:
        print("Nothing to fetch. All linked transcripts are already harvested.")
        print("\n" + build_report(episodes, existing))
        return 0

    session = make_session()
    results = dict(existing)
    fetched = 0
    failed = 0

    for i, (episode, url) in enumerate(pending):
        ep_num = episode.get("episode_number")
        label = f"#{ep_num if ep_num is not None else '?'}"
        try:
            resp = fetch(session, url)
            if resp is None:
                print(f"  [{i+1}/{len(pending)}] {label}: no page ({url})")
                failed += 1
                continue
            text = extract_text(url, resp.content)
            if not text:
                print(f"  [{i+1}/{len(pending)}] {label}: empty extraction ({url})")
                failed += 1
                continue
            results[episode["url"]] = {
                "episode_url": episode["url"],
                "episode_number": ep_num,
                "title": episode.get("title", ""),
                "source": "official",
                "ai_generated": looks_ai_generated(text),
                "transcript_url": url,
                "char_count": len(text),
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "text": text,
            }
            fetched += 1
            print(f"  [{i+1}/{len(pending)}] {label}: {len(text)} chars")

            if fetched % 10 == 0:
                save_json(TRANSCRIPTS_FILE, _sorted_rows(results))
                print(f"    -> Progress saved ({len(results)} total)")
            time.sleep(REQUEST_DELAY)
        except Exception as error:  # noqa: BLE001 - keep harvesting the rest
            print(f"    -> Error on {url}: {type(error).__name__}: {error}")
            failed += 1
            time.sleep(2)

    save_json(TRANSCRIPTS_FILE, _sorted_rows(results))
    print(f"\nDone. {fetched} fetched, {failed} failed, {len(results)} transcripts total.")
    print(f"Saved to {TRANSCRIPTS_FILE}")
    print("\n" + build_report(episodes, results))
    return 0


def _sorted_rows(results: dict[str, dict]) -> list[dict]:
    return sorted(
        results.values(),
        key=lambda row: (-(row.get("episode_number") or 0), row.get("title") or ""),
    )


if __name__ == "__main__":
    sys.exit(main())
