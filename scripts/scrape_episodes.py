"""Discover Curbsiders episodes from the official RSS feed and refresh show notes.

The WordPress sitemap is intermittently blocked by SiteGround and is not a safe
automation boundary.  Audioboom's official Curbsiders RSS feed is used for
discovery; existing older episodes remain in the cache.  Episode pages are fetched
directly first and, when the public WAF serves an interstitial, through the optional
Jina Reader proxy.  Empty or anomalously small discoveries fail loudly.

Usage:
  python scripts/scrape_episodes.py
  python scripts/scrape_episodes.py --dry-run
  python scripts/scrape_episodes.py --refresh-recent 20
"""

import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests as cr

BASE_URL = "https://thecurbsiders.com"
RSS_URL = "https://audioboom.com/channels/5034728.rss"
READER_BASE = os.getenv("CURBSIDERS_READER_BASE", "https://r.jina.ai/http://")
IMPERSONATE = "chrome"
DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "episodes.json"
REQUEST_DELAY = 1.0
MAX_RETRIES = 3
MIN_RSS_ITEMS = 100
MIN_SHOW_NOTES_CHARS = 500

TRANSCRIPT_LINK_RE = re.compile(
    r"\[([^\]]*)\]\((https?://(?:www\.)?thecurbsiders\.com/[^)]+\.(?:pdf|docx?))\)",
    re.IGNORECASE,
)


def extract_transcript_url(show_notes: str) -> str | None:
    for text, url in TRANSCRIPT_LINK_RE.findall(show_notes or ""):
        if "transcript" in text.lower() or "transcript" in url.lower():
            return url
    return None


def make_session() -> cr.Session:
    return cr.Session(impersonate=IMPERSONATE, timeout=30)


def _blocked_response(resp: cr.Response) -> bool:
    sample = (resp.text or "")[:5000].lower()
    return resp.status_code == 202 or "sgcaptcha" in sample or "siteground" in sample and "captcha" in sample


def fetch(session: cr.Session, url: str) -> cr.Response | None:
    """Fetch a URL, returning None only for a real 404 and raising otherwise."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url)
            if resp.status_code == 404:
                return None
            if resp.status_code == 200 and not _blocked_response(resp):
                return resp
            if _blocked_response(resp):
                raise RuntimeError(f"source blocked by WAF (HTTP {resp.status_code})")
            resp.raise_for_status()
            raise RuntimeError(f"unexpected HTTP {resp.status_code}")
        except Exception as exc:
            last_error = exc
            if "blocked by WAF" in str(exc):
                break
            if attempt < MAX_RETRIES:
                time.sleep(3 * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def normalize_url(href: str) -> str:
    href = href.split("?", 1)[0].split("#", 1)[0]
    if not href.startswith("http"):
        href = BASE_URL + href
    return href.replace("http://", "https://").rstrip("/")


def extract_episode_number(title: str, url: str = "") -> int | None:
    # Prefer an explicit #NNN in the title; URL digits can include years.
    match = re.search(r"#(\d{1,4})\b", title or "")
    if match:
        return int(match.group(1))
    for field in (title, url):
        match = re.search(r"(?:^|/)(\d{1,4})(?:-|/|$)", field or "")
        if match:
            return int(match.group(1))
    return None


def extract_episode_numbers(title: str, url: str = "") -> set[int]:
    """All explicitly numbered episodes represented by a page (e.g. #330 & #331)."""
    numbers = {int(value) for value in re.findall(r"#(\d{1,4})\b", title or "")}
    primary = extract_episode_number(title, url)
    if primary is not None:
        numbers.add(primary)
    return numbers


def episode_url_from_title(title: str, episode_number: int) -> str:
    suffix = re.sub(r"^.*?#\d{1,4}\s*[:\-–—]?\s*", "", title).strip()
    ascii_text = unicodedata.normalize("NFKD", suffix).encode("ascii", "ignore").decode()
    ascii_text = ascii_text.replace("&", " and ").replace("'", "")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return f"{BASE_URL}/curbsiders-podcast/{episode_number}-{slug}"


def _rss_date(raw: str) -> str:
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        match = re.search(r"(\d{4}-\d{2}-\d{2})", raw or "")
        return match.group(1) if match else ""


def discover_rss_episodes(session: cr.Session) -> list[dict]:
    """Return numbered episode descriptors from the official RSS feed."""
    resp = fetch(session, RSS_URL)
    if resp is None:
        raise RuntimeError("official RSS feed returned 404")
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise RuntimeError(f"official RSS feed is not valid XML: {exc}") from exc

    items = root.findall("./channel/item")
    if len(items) < MIN_RSS_ITEMS:
        raise RuntimeError(
            f"RSS discovery returned only {len(items)} items; refusing a partial/empty ingest"
        )

    discovered: dict[int, dict] = {}
    for item in items:
        title = html.unescape((item.findtext("title") or "").strip())
        number = extract_episode_number(title)
        if number is None:
            continue
        # Reboots repeat an existing episode number. Prefer the original numbered
        # item and never create a duplicate episode from a reboot feed entry.
        is_reboot = "reboot" in title.lower()
        if number in discovered and is_reboot:
            continue
        row = {
            "episode_number": number,
            "title": title,
            "date": _rss_date(item.findtext("pubDate") or ""),
            "audio_url": (item.findtext("link") or "").strip(),
            "url": episode_url_from_title(title, number),
            "is_reboot": is_reboot,
        }
        if number not in discovered or not is_reboot:
            discovered[number] = row
    if not discovered:
        raise RuntimeError("RSS feed contained no numbered Curbsiders episodes")
    return sorted(discovered.values(), key=lambda row: -row["episode_number"])


def html_to_text_with_links(element) -> str:
    for anchor in element.find_all("a"):
        href = anchor.get("href", "")
        text = anchor.get_text(strip=True)
        if href and text:
            anchor.replace_with(f"[{text}]({href}) ")
    return element.get_text(separator="\n", strip=True)


def parse_episode_date(soup: BeautifulSoup) -> str:
    candidates = []
    for meta in soup.find_all("meta"):
        name = (meta.get("property") or meta.get("name") or "").strip().lower()
        if name in {"article:published_time", "og:published_time"}:
            candidates.append(meta)
    candidates.extend(soup.find_all("time"))
    for candidate in candidates:
        raw = candidate.get("content") or candidate.get("datetime") or candidate.get_text(strip=True)
        match = re.search(r"(\d{4}-\d{2}-\d{2})", raw or "")
        if match:
            return match.group(1)
    return ""


def parse_episode(page: str) -> tuple[str, str, str]:
    soup = BeautifulSoup(page, "lxml")
    title_el = soup.find("h1") or soup.find("title")
    title = title_el.get_text(strip=True) if title_el else ""
    candidates = [
        soup.find("div", class_="entry-content"), soup.find("div", class_="post-content"),
        soup.find(id="columns_main"), soup.find("div", class_="content"), soup.find("main"),
        soup.find("article"),
    ]
    candidates = [candidate for candidate in candidates if candidate is not None]
    content = max(candidates, key=lambda candidate: len(candidate.get_text(strip=True)), default=None)
    return title, html_to_text_with_links(content) if content else "", parse_episode_date(soup)


def _clean_reader_markdown(markdown: str) -> str:
    lines = []
    for line in markdown.splitlines():
        if re.match(r"^!\[[^]]*\]\([^)]+\)\s*$", line.strip()):
            continue
        line = re.sub(r"^\s*#{1,6}\s*", "", line)
        line = re.sub(r"^\s*\*\s{2,}", "- ", line)
        line = line.replace("**", "")
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_reader_page(text: str) -> tuple[str, str, str]:
    title_match = re.search(r"^Title:\s*(.+)$", text, re.MULTILINE)
    date_match = re.search(r"^Published Time:\s*(.+)$", text, re.MULTILINE)
    marker = "Markdown Content:"
    if marker not in text:
        raise ValueError("reader response omitted Markdown Content")
    title = (title_match.group(1) if title_match else "").removesuffix(" - The Curbsiders").strip()
    date_value = date_match.group(1) if date_match else ""
    date_match_value = re.search(r"(\d{4}-\d{2}-\d{2})", date_value)
    return title, _clean_reader_markdown(text.split(marker, 1)[1]), date_match_value.group(1) if date_match_value else ""


def fetch_episode_page(session: cr.Session, url: str) -> tuple[str, str, str, str]:
    direct_error: Exception | None = None
    try:
        response = fetch(session, url)
        if response is not None:
            title, notes, date = parse_episode(response.text)
            if len(notes) >= MIN_SHOW_NOTES_CHARS:
                return title, notes, date, "website"
            direct_error = RuntimeError(f"direct page contained only {len(notes)} note characters")
    except Exception as exc:
        direct_error = exc

    if not READER_BASE:
        raise RuntimeError(f"direct episode fetch failed and reader fallback is disabled: {direct_error}")
    reader_url = f"{READER_BASE}{url.removeprefix('https://').removeprefix('http://')}"
    response = fetch(session, reader_url)
    if response is None:
        raise RuntimeError(f"reader returned 404 after direct fetch failed: {direct_error}")
    title, notes, date = parse_reader_page(response.text)
    if len(notes) < MIN_SHOW_NOTES_CHARS:
        raise RuntimeError(f"reader page contained only {len(notes)} note characters")
    return title, notes, date, "jina_reader"


def needs_refresh(entry: dict) -> bool:
    return not entry.get("show_notes")


def sorted_episode_rows(results: dict[str, dict]) -> list[dict]:
    return sorted(results.values(), key=lambda row: (-(row.get("episode_number") or 0), row.get("title") or ""))


def _load_existing() -> list[dict]:
    if not OUTPUT_FILE.exists():
        return []
    with OUTPUT_FILE.open() as handle:
        return json.load(handle)


def run(*, dry_run: bool = False, refresh_recent: int = 12) -> int:
    DATA_DIR.mkdir(exist_ok=True)
    existing_rows = _load_existing()
    existing_by_number = {}
    for row in existing_rows:
        for number in extract_episode_numbers(row.get("title", ""), row.get("url", "")):
            existing_by_number.setdefault(number, row)
    print(f"Loaded {len(existing_rows)} cached episodes")

    session = make_session()
    print(f"Discovering episodes from official RSS: {RSS_URL}")
    inventory = discover_rss_episodes(session)
    print(f"RSS returned {len(inventory)} unique numbered episodes")

    for descriptor in inventory:
        cached = existing_by_number.get(descriptor["episode_number"])
        if cached:
            descriptor["url"] = cached["url"]

    new_rows = [row for row in inventory if row["episode_number"] not in existing_by_number and not row["is_reboot"]]
    recent = inventory[:max(refresh_recent, 0)]
    refresh_numbers = {row["episode_number"] for row in recent}
    fetch_rows = [
        row for row in inventory
        if row in new_rows or row["episode_number"] in refresh_numbers
        or needs_refresh(existing_by_number.get(row["episode_number"], {}))
    ]

    print(f"New episodes: {len(new_rows)} | pages to fetch/refresh: {len(fetch_rows)}")
    for row in new_rows:
        print(f"  NEW #{row['episode_number']} {row['title']} ({row['date']})")
    if dry_run:
        print("Dry run: live discovery completed; no files changed.")
        return 0

    results = {row["url"]: dict(row) for row in existing_rows}
    # RSS is authoritative for dates and feed titles even when a WAF prevents a
    # page refresh. Preserve richer website titles already in the cache.
    for descriptor in inventory:
        cached = existing_by_number.get(descriptor["episode_number"])
        if cached:
            cached_copy = dict(results.get(cached["url"], cached))
            candidate_dates = [value for value in (cached_copy.get("date"), descriptor["date"]) if value]
            cached_copy["date"] = max(candidate_dates) if candidate_dates else ""
            if not cached_copy.get("audio_url"):
                cached_copy["audio_url"] = descriptor["audio_url"]
            results[cached_copy["url"]] = cached_copy

    new_numbers = {row["episode_number"] for row in new_rows}
    new_failures = []
    refresh_failures = []
    for index, descriptor in enumerate(fetch_rows, 1):
        try:
            title, notes, page_date, source = fetch_episode_page(session, descriptor["url"])
            number = descriptor["episode_number"]
            results[descriptor["url"]] = {
                "url": descriptor["url"],
                "title": title or descriptor["title"],
                "date": page_date or descriptor["date"],
                "episode_number": number,
                "show_notes": notes,
                "show_notes_length": len(notes),
                "transcript_url": extract_transcript_url(notes),
                "audio_url": descriptor["audio_url"],
                "show_notes_source": source,
                "last_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            print(f"  [{index}/{len(fetch_rows)}] #{number}: {len(notes)} chars via {source}")
        except Exception as exc:
            target = new_failures if descriptor["episode_number"] in new_numbers else refresh_failures
            target.append((descriptor, exc))
            print(f"  [{index}/{len(fetch_rows)}] ERROR #{descriptor['episode_number']}: {exc}")
        time.sleep(REQUEST_DELAY)

    if new_failures:
        print(f"Refusing to write: {len(new_failures)} newly discovered episode page(s) failed.", file=sys.stderr)
        return 1
    if fetch_rows and len(refresh_failures) == len(fetch_rows):
        print("Refusing to write: every scheduled page refresh failed.", file=sys.stderr)
        return 1

    with OUTPUT_FILE.open("w") as handle:
        json.dump(sorted_episode_rows(results), handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(f"Saved {len(results)} episodes to {OUTPUT_FILE} ({len(refresh_failures)} refresh warnings)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Perform live discovery but do not write")
    parser.add_argument("--refresh-recent", type=int, default=12, help="Refresh this many newest RSS episodes")
    args = parser.parse_args()
    return run(dry_run=args.dry_run, refresh_recent=args.refresh_recent)


if __name__ == "__main__":
    raise SystemExit(main())
