"""Project-wide configuration and shared helpers."""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
EXCEL_FILE = BASE_DIR / "all_subfolders.xlsx"

# Output root: every dev_id gets its own sub-folder under here.
OUTPUT_DIR = BASE_DIR / "downloads"

# Cache of extracted links so we never re-render a page we already scraped.
CACHE_DIR = BASE_DIR / "cache"
LINKS_CACHE = CACHE_DIR / "links.json"

# Run / log directories
LOG_DIR = BASE_DIR / "logs"

for _d in (OUTPUT_DIR, CACHE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Site
# ---------------------------------------------------------------------------
SITE_BASE = "https://www.srpe.gov.hk"
# The SPA page that renders Sales Arrangement for a given dev_id.
PAGE_URL = SITE_BASE + "/opip/selected_dev_all_development?devId={dev_id}"
# Public cookie set after accepting the Terms & Conditions. Without it the site
# bounces to the disclaimer page, so we set it up-front in every context.
TERMS_COOKIE = {"name": "srpe_public_terms_accepted", "value": "1"}

# Selector that confirms the Sales Arrangement block has rendered. We wait for
# this <h1> instead of a fixed sleep so slow pages still work and fast pages
# are not slowed down.
SA_HEADING_SELECTOR = 'h1:has-text("Sales Arrangement")'

# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
# How many dev_ids to process at the same time.
NUM_WORKERS = 5

# Per-request HTTP settings for the download stage.
HTTP_TIMEOUT = 120          # seconds
HTTP_RETRIES = 4            # retry attempts on transient errors
HTTP_RETRY_BACKOFF = 2.0    # exponential base (2, 4, 8, 16 ... seconds)


def dev_folder(dev_id: str) -> Path:
    """Return (creating if needed) the folder for a single dev_id."""
    folder = OUTPUT_DIR / str(dev_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder
