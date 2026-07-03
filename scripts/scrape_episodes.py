"""
Step 1: Scrape all Curbsiders episode URLs and show notes.

thecurbsiders.com sits behind a WAF that returns HTTP 403 to ordinary HTTP
clients (plain requests/curl) even with a browser User-Agent, because it
fingerprints the TLS handshake. We use curl_cffi with Chrome impersonation,
which presents a real Chrome TLS fingerprint and passes the block.

Saves to data/episodes.json. Resumable — skips already-scraped episodes.

Usage: python scripts/scrape_episodes.py
"""

import json
import re
import time
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests as cr

BASE_URL = "https://thecurbsiders.com"
SITEMAP_INDEX = f"{BASE_URL}/sitemap_index.xml"
IMPERSONATE = "chrome"  # TLS fingerprint that passes the site's WAF
DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "episodes.json"
REQUEST_DELAY = 1.0  # seconds between requests
MAX_RETRIES = 3

# Episode posts live under /curbsiders-podcast/, in several historical forms:
#   /curbsiders-podcast/530-nutrition-...        (recent)
#   /curbsiders-podcast/endocrine/520-...        (nested under a specialty)
#   /curbsiders-podcast/285                        (older, bare number)
# Since we read the WordPress *post* sitemap (episodes only, no taxonomy),
# we accept anything under that prefix and just drop embed/feed variants.
EPISODE_URL_RE = re.compile(r"^https://thecurbsiders\.com/curbsiders-podcast/[^?#]+$")
EXCLUDE_SUFFIXES = ("/embed", "/feed")

# Many episodes (~94/555, concentrated in the CME era ~#247-424) link an official
# transcript PDF/DOCX hosted on the Curbsiders' own WordPress uploads. These are the
# highest-fidelity full-episode text available, so we capture the link here for the
# transcript harvester (scripts/fetch_transcripts.py). A link qualifies when it is
# on thecurbsiders.com, points at a .pdf/.docx, and says "transcript" in either the
# link text or the filename. (Note: URLs appear with both http:// and https://.)
TRANSCRIPT_LINK_RE = re.compile(
    r"\[([^\]]*)\]\((https?://(?:www\.)?thecurbsiders\.com/[^)]+\.(?:pdf|docx?))\)",
    re.IGNORECASE,
)


def extract_transcript_url(show_notes: str) -> str | None:
    """Return the URL of the episode's official transcript file, or None.

    Picks the first qualifying link; episodes carry at most one distinct transcript.
    """
    for text, url in TRANSCRIPT_LINK_RE.findall(show_notes or ""):
        if "transcript" in text.lower() or "transcript" in url.lower():
            return url
    return None


def make_session() -> cr.Session:
    return cr.Session(impersonate=IMPERSONATE, timeout=30)


def fetch(session: cr.Session, url: str) -> cr.Response | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 404:
                return None
            if resp.status_code in (403, 429, 503) and attempt < MAX_RETRIES:
                time.sleep(5 * attempt)
                continue
            resp.raise_for_status()
        except Exception:
            if attempt == MAX_RETRIES:
                raise
            time.sleep(3 * attempt)
    return None


def normalize_url(href: str) -> str:
    href = href.split("?", 1)[0].split("#", 1)[0]
    if not href.startswith("http"):
        href = BASE_URL + href
    href = href.replace("http://", "https://")
    return href.rstrip("/")


def locs_from_xml(xml: str) -> list[str]:
    """Extract <loc> URLs from a sitemap, handling CDATA wrapping."""
    return re.findall(r"<loc>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</loc>", xml)


def discover_episode_urls(session: cr.Session) -> list[str]:
    """Enumerate every episode URL from the WordPress post sitemap(s).

    The sitemap is far more complete than the paginated category archive
    (~555 episodes vs ~204), so it's the source of truth for the URL list.
    """
    resp = fetch(session, SITEMAP_INDEX)
    if resp is None:
        return []
    sub_sitemaps = [u for u in locs_from_xml(resp.text) if "post-sitemap" in u]
    if not sub_sitemaps:  # fall back to a single conventional name
        sub_sitemaps = [f"{BASE_URL}/post-sitemap.xml"]

    episodes: list[str] = []
    seen: set[str] = set()
    for sm in sub_sitemaps:
        r = fetch(session, sm)
        if r is None:
            continue
        for loc in locs_from_xml(r.text):
            href = normalize_url(loc)
            if href.endswith(EXCLUDE_SUFFIXES):
                continue
            if EPISODE_URL_RE.match(href) and href not in seen:
                seen.add(href)
                episodes.append(href)
        time.sleep(REQUEST_DELAY)
    return episodes


def html_to_text_with_links(element) -> str:
    """Convert a BeautifulSoup element to text, preserving links as [text](url)."""
    for a in element.find_all("a"):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        if href and text:
            a.replace_with(f"[{text}]({href}) ")
    return element.get_text(separator="\n", strip=True)


def parse_episode_date(soup: BeautifulSoup) -> str:
    candidates = []
    for meta in soup.find_all("meta"):
        attr_name = (meta.get("property") or meta.get("name") or "").strip().lower()
        if attr_name in {"article:published_time", "og:published_time"}:
            candidates.append(meta)
    candidates.extend(soup.find_all("time"))

    for candidate in candidates:
        raw = candidate.get("content") or candidate.get("datetime") or candidate.get_text(strip=True)
        if not raw:
            continue
        match = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
        if match:
            return match.group(1)
    return ""


def parse_episode(html: str) -> tuple[str, str, str]:
    """Return (title, show_notes_text, episode_date). Handles the site's different themes."""
    soup = BeautifulSoup(html, "lxml")

    title_el = soup.find("h1") or soup.find("title")
    title = title_el.get_text(strip=True) if title_el else ""

    candidates = [
        soup.find("div", class_="entry-content"),
        soup.find("div", class_="post-content"),
        soup.find(id="columns_main"),
        soup.find("div", class_="content"),
        soup.find("main"),
        soup.find("article"),
    ]
    candidates = [c for c in candidates if c is not None]
    content = max(candidates, key=lambda c: len(c.get_text(strip=True)), default=None)
    notes = html_to_text_with_links(content) if content else ""
    return title, notes, parse_episode_date(soup)


def extract_episode_number(title: str, url: str) -> int | None:
    for field in (title, url):
        m = re.search(r"#?(\d{1,3})", field)
        if m:
            return int(m.group(1))
    return None


def needs_refresh(entry: dict) -> bool:
    # Only show_notes is essential. Episode dates are frequently absent from the
    # source markup, so a missing date must not force a full re-fetch of every
    # already-scraped episode.
    return not entry.get("show_notes")


def sorted_episode_rows(results: dict[str, dict]) -> list[dict]:
    return sorted(
        results.values(),
        key=lambda entry: (-(entry.get("episode_number") or 0), entry.get("title") or ""),
    )


def main():
    DATA_DIR.mkdir(exist_ok=True)

    existing: dict[str, dict] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE) as f:
            data = json.load(f)
        existing = {e["url"]: e for e in data}
        print(f"Loaded {len(existing)} previously scraped episodes")

    session = make_session()

    # Phase 1: collect all episode URLs from the sitemap.
    print("\nPhase 1: Collecting episode URLs from sitemap...")
    all_urls = discover_episode_urls(session)
    print(f"Found {len(all_urls)} unique episodes")

    # Phase 2: fetch show notes for episodes we don't already have.
    print("\nPhase 2: Fetching show notes...")
    results: dict[str, dict] = dict(existing)
    new_count = 0
    to_fetch = [u for u in all_urls if u not in existing or needs_refresh(existing[u])]

    for i, url in enumerate(to_fetch):
        try:
            resp = fetch(session, url)
            if resp is None:
                print(f"  [{i+1}/{len(to_fetch)}] skipped (no page): {url}")
                continue
            title, show_notes, episode_date = parse_episode(resp.text)
            ep_num = extract_episode_number(title, url)
            print(f"  [{i+1}/{len(to_fetch)}] #{ep_num or '?'}: {title[:50]} "
                  f"({len(show_notes)} chars)")

            results[url] = {
                "url": url,
                "title": title,
                "date": episode_date,
                "episode_number": ep_num,
                "show_notes": show_notes,
                "show_notes_length": len(show_notes),
                "transcript_url": extract_transcript_url(show_notes),
            }
            new_count += 1

            if new_count % 10 == 0:
                with open(OUTPUT_FILE, "w") as f:
                    json.dump(sorted_episode_rows(results), f, indent=2, ensure_ascii=False)
                print(f"    -> Progress saved ({len(results)} total)")

            time.sleep(REQUEST_DELAY)
        except Exception as e:
            print(f"    -> Error on {url}: {type(e).__name__}: {e}")
            time.sleep(2)

    with open(OUTPUT_FILE, "w") as f:
        json.dump(sorted_episode_rows(results), f, indent=2, ensure_ascii=False)

    print(f"\nDone. {len(results)} episodes saved to {OUTPUT_FILE}")
    print(f"({new_count} newly scraped, {len(existing)} from cache)")


if __name__ == "__main__":
    main()
