"""
Fill transcript coverage gaps with YouTube auto-captions.

Only ~94/555 episodes publish an official transcript (see scripts/fetch_transcripts.py).
For the rest, the show's YouTube channel often has the full episode video with
auto-generated captions. This script enumerates the channel, matches videos to
episodes by episode number in the title, downloads the English auto-captions for
episodes that have NO transcript yet, and stores them into data/transcripts.json
with source "youtube".

Fidelity caveat: YouTube auto-captions are speech-recognition (ASR) output — they
misrender drug names, doses, and numbers. They are tagged source "youtube" and
ai_generated=True so nothing downstream confuses them with the reviewed official
transcripts. Like everything in the transcript layer, this text is a search/context
corpus and candidate-pearl input, NOT a source for auto-published verbatim pearls.

Requires yt-dlp (pip install yt-dlp).

Usage:
  python scripts/harvest_youtube_captions.py            # fill gaps
  python scripts/harvest_youtube_captions.py --limit 5  # at most 5 (for testing)
  python scripts/harvest_youtube_captions.py --report   # coverage by source, no fetching
  python scripts/harvest_youtube_captions.py --channel https://www.youtube.com/@thecurbsiders/videos
"""

import argparse
import html
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.fetch_transcripts import TRANSCRIPTS_FILE, transcript_url_for, _sorted_rows
    from scripts.extract_trials import EPISODES_FILE, load_json, save_json
except ImportError:
    from fetch_transcripts import TRANSCRIPTS_FILE, transcript_url_for, _sorted_rows
    from extract_trials import EPISODES_FILE, load_json, save_json

# The show's canonical channel. The @thecurbsiders7326 handle in the show notes is
# stale in yt-dlp (404s); the channel ID is the stable identifier.
DEFAULT_CHANNEL = "https://www.youtube.com/channel/UCHGfC9YOG2NUMHlf7uhEd8g/videos"
REQUEST_DELAY = 1.0
_EP_NUM_RE = re.compile(r"#(\d{2,3})\b")


def episode_number_from_title(title: str) -> int | None:
    """The '#NNN' episode number a channel video title advertises, if any."""
    m = _EP_NUM_RE.search(title or "")
    return int(m.group(1)) if m else None


def build_video_index(channel_url: str, ydl) -> dict[int, str]:
    """Map episode_number -> YouTube video id, from the channel's flat listing.

    Titles carry '#NNN'. If several videos share a number (re-uploads/clips), the
    first (newest) wins — good enough since gaps are filled, not overwritten.
    """
    info = ydl.extract_info(channel_url, download=False)
    index: dict[int, str] = {}
    for entry in info.get("entries") or []:
        num = episode_number_from_title(entry.get("title", ""))
        vid = entry.get("id")
        if num is not None and vid and num not in index:
            index[num] = vid
    return index


def _strip_vtt_tags(text: str) -> str:
    text = re.sub(r"<\d{2}:\d{2}:\d{2}\.\d{3}>", "", text)  # inline word timestamps
    text = re.sub(r"</?c[^>]*>", "", text)                   # <c> karaoke spans
    text = html.unescape(text)                               # &gt;&gt; -> >>, &amp; -> &
    text = re.sub(r"^\s*(>>\s*)+", "", text)                 # YouTube speaker-change markers
    return text.strip()


def parse_vtt(vtt: str) -> str:
    """Turn a WebVTT auto-caption file into clean prose.

    YouTube auto-captions roll: each cue repeats the previous cue's tail plus a few
    new words, so a naive join triples the text. We keep only lines that add new
    content by dropping any line that the last kept line already ends with.
    """
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if (
            not line
            or line == "WEBVTT"
            or line.startswith(("Kind:", "Language:", "NOTE"))
            or "-->" in line
            or line.isdigit()
        ):
            continue
        cleaned = _strip_vtt_tags(line)
        if not cleaned:
            continue
        if lines and (cleaned == lines[-1] or lines[-1].endswith(cleaned)):
            continue
        lines.append(cleaned)
    return "\n".join(lines).strip()


def fetch_caption_text(video_id: str, ydl, opener) -> str | None:
    """Download the English auto-caption track for a video and return its text."""
    info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    tracks = (info.get("automatic_captions") or {}).get("en") or []
    # Prefer VTT; fall back to whatever is offered.
    track = next((t for t in tracks if t.get("ext") == "vtt"), tracks[0] if tracks else None)
    if not track or not track.get("url"):
        return None
    resp = opener.get(track["url"])
    if resp.status_code != 200:
        return None
    text = parse_vtt(resp.text)
    return text or None


def build_report(episodes: list[dict], transcripts: dict[str, dict]) -> str:
    from collections import Counter

    by_source = Counter(r.get("source") for r in transcripts.values())
    linked_official = sum(1 for e in episodes if transcript_url_for(e))
    covered = len(transcripts)
    lines = [
        "=== Transcript sources ===",
        f"  Episodes total:          {len(episodes)}",
        f"  Episodes with any transcript: {covered}/{len(episodes)}",
        f"    official: {by_source.get('official', 0)}",
        f"    youtube:  {by_source.get('youtube', 0)}",
        f"  Still uncovered:         {len(episodes) - covered}",
        f"  (official transcript links available: {linked_official})",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, help="Channel /videos URL to enumerate")
    parser.add_argument("--limit", type=int, default=None, help="Fetch at most N captions (for testing)")
    parser.add_argument("--report", action="store_true", help="Print source coverage and exit")
    args = parser.parse_args()

    if not EPISODES_FILE.exists():
        print(f"Error: {EPISODES_FILE} not found. Run scrape_episodes.py first.")
        return 1

    episodes = load_json(EPISODES_FILE, [])
    existing = {row["episode_url"]: row for row in load_json(TRANSCRIPTS_FILE, [])}

    if args.report:
        print(build_report(episodes, existing))
        return 0

    # Gap = an episode we have no transcript for at all. (Episodes with an official
    # transcript are handled by fetch_transcripts.py and skipped here.)
    gaps = [e for e in episodes if e.get("episode_number") and e["url"] not in existing]
    if not gaps:
        print("No gaps: every episode already has a transcript.")
        print("\n" + build_report(episodes, existing))
        return 0

    try:
        import yt_dlp
    except ImportError:
        print("Error: yt-dlp is required. Install it with: pip install yt-dlp")
        return 1
    from curl_cffi import requests as cr

    opener = cr.Session(impersonate="chrome", timeout=60)
    flat = yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "skip_download": True})
    detail = yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "writeautomaticsub": True})

    print(f"Enumerating channel videos: {args.channel}")
    try:
        video_index = build_video_index(args.channel, flat)
    except Exception as error:  # noqa: BLE001
        print(f"Error enumerating channel: {type(error).__name__}: {error}")
        return 1
    print(f"Found {len(video_index)} numbered videos on the channel.")

    # Episodes we can actually fill: a gap that matches a channel video.
    fillable = [(e, video_index[e["episode_number"]]) for e in gaps if e["episode_number"] in video_index]
    print(f"Gaps: {len(gaps)} | matched to a video: {len(fillable)}")
    if args.limit is not None:
        fillable = fillable[: args.limit]

    results = dict(existing)
    fetched = 0
    failed = 0
    for i, (episode, video_id) in enumerate(fillable):
        num = episode["episode_number"]
        try:
            text = fetch_caption_text(video_id, detail, opener)
            if not text:
                print(f"  [{i+1}/{len(fillable)}] #{num}: no English auto-captions ({video_id})")
                failed += 1
                continue
            results[episode["url"]] = {
                "episode_url": episode["url"],
                "episode_number": num,
                "title": episode.get("title", ""),
                "source": "youtube",
                "ai_generated": True,  # ASR captions
                "transcript_url": f"https://www.youtube.com/watch?v={video_id}",
                "char_count": len(text),
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "text": text,
            }
            fetched += 1
            print(f"  [{i+1}/{len(fillable)}] #{num}: {len(text)} chars ({video_id})")
            if fetched % 10 == 0:
                save_json(TRANSCRIPTS_FILE, _sorted_rows(results))
                print(f"    -> Progress saved ({len(results)} total)")
            time.sleep(REQUEST_DELAY)
        except Exception as error:  # noqa: BLE001
            print(f"    -> Error on #{num} ({video_id}): {type(error).__name__}: {error}")
            failed += 1
            time.sleep(2)

    save_json(TRANSCRIPTS_FILE, _sorted_rows(results))
    print(f"\nDone. {fetched} captions fetched, {failed} failed, {len(results)} transcripts total.")
    print("\n" + build_report(episodes, results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
