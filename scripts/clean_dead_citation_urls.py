"""
One-off cleanup: null out pubmed_url values in data/trials.json that are dead
placeholders rather than real citation links -- the bare PubMed homepage
(no article id), the literal string "null" (not a JSON null), or a URL whose
hostname is a bare, dotless fragment (a garbled/truncated scrape artifact).

Usage:
  python scripts/clean_dead_citation_urls.py
"""

import json
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path(__file__).parent.parent / "data"
TRIALS_FILE = DATA_DIR / "trials.json"

DEAD_URL_LITERALS = {
    "https://pubmed.ncbi.nlm.nih.gov",
    "https://pubmed.ncbi.nlm.nih.gov/",
    "null",
    "none",
}


def is_dead_url(url) -> bool:
    if not url:
        return False
    cleaned = url.strip()
    if cleaned.lower() in DEAD_URL_LITERALS:
        return True
    host = urlparse(cleaned).netloc
    return bool(host) and "." not in host


def main():
    trials = json.loads(TRIALS_FILE.read_text())
    cleared = 0
    for trial in trials:
        if is_dead_url(trial.get("pubmed_url")):
            trial["pubmed_url"] = None
            cleared += 1

    TRIALS_FILE.write_text(json.dumps(trials, indent=2, ensure_ascii=False))
    print(f"Cleared {cleared} dead pubmed_url placeholders out of {len(trials)} trials")


if __name__ == "__main__":
    main()
