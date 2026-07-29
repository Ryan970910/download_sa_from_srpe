"""
Stage 1 - Extract the Sales Arrangement download links for each dev_id.

The SRPE site is a React single-page app, so the PDF links are NOT in the raw
HTML. We use a headless Chromium (Playwright) to render the page, wait for the
"Sales Arrangement" heading, then read the file rows straight from the DOM.

Each worker keeps its own long-lived browser to avoid the cost of relaunching
Chromium for every dev_id. Results are written to a shared JSON cache so the
download stage (and re-runs) never have to render a page twice.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from multiprocessing import Pool, cpu_count
from typing import Dict, List

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

import config
from excel_utils import read_dev_ids

log = logging.getLogger("extract")

# One record per Sales Arrangement file on a dev page.
#   href   : path beginning with /api/SrpeWebService/download/.../sales_arrangement/...
#   date   : "Date and Time of Uploading" column, e.g. "07 Jun 2024 05:01:00 PM"
#   size   : "File Size" column, e.g. "639.14 KB"
LinkRow = Dict[str, str]
DevResult = Dict[str, List[LinkRow]]   # {"dev_id": [ {href,date,size}, ... ]}

# JS that pulls the Sales Arrangement table rows out of the rendered page.
# Run inside the page via Playwright (a controlled, side-effect-free read).
_EXTRACT_JS = r"""
() => {
    const heading = [...document.querySelectorAll('h1')]
        .find(h => h.textContent.includes('Sales Arrangement'));
    if (!heading) return {found: false, rows: []};
    // The table is the nearest following <table> within the same section.
    let table = heading.parentElement.querySelector('table');
    if (!table) {
        let n = heading.nextElementSibling;
        while (n && n.tagName !== 'TABLE') n = n.nextElementSibling;
        table = n;
    }
    if (!table) return {found: true, rows: []};
    const rows = [...table.querySelectorAll('tbody tr')].map(tr => {
        const a = tr.querySelector('a[href*="sales_arrangement"]');
        const cells = [...tr.querySelectorAll('td')].map(td => (td.innerText || '').trim());
        return {
            href: a ? a.getAttribute('href') : null,
            date: cells[0] || '',
            size: cells[1] || ''
        };
    }).filter(r => r.href);
    return {found: true, rows};
}
"""


def _render_one(page, dev_id: str) -> DevResult:
    """Render a single dev page and return its link rows."""
    url = config.PAGE_URL.format(dev_id=dev_id)
    page.goto(url, wait_until="domcontentloaded")

    # Wait until the Sales Arrangement heading appears (or fall back after a
    # fixed budget if the dev has none / fails to load).
    found_heading = True
    try:
        page.wait_for_selector(config.SA_HEADING_SELECTOR, timeout=30000)
    except PWTimeoutError:
        found_heading = False

    # Even when the heading exists, the rows load a moment later; give the
    # table a brief chance to populate before reading.
    try:
        page.wait_for_selector('a[href*="sales_arrangement"]', timeout=15000)
    except PWTimeoutError:
        pass

    result = page.evaluate(_EXTRACT_JS)
    rows = result.get("rows", []) if result else []
    if not found_heading and not rows:
        # Page genuinely has no Sales Arrangement section.
        log.debug("[%s] no Sales Arrangement section", dev_id)
    return {dev_id: rows}


def _worker(task):
    """Pool worker: owns one browser, processes a batch of dev_ids."""
    worker_id, dev_ids = task
    logging.basicConfig(
        level=logging.INFO,
        format=f"[w{worker_id}] %(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    out: Dict[str, List[LinkRow]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies([
            {**config.TERMS_COOKIE,
             "domain": "www.srpe.gov.hk", "path": "/"}
        ])
        page = context.new_page()
        for dev_id in dev_ids:
            try:
                out.update(_render_one(page, dev_id))
                n = len(out[dev_id])
                log.info("extracted %s -> %d file(s)", dev_id, n)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                log.error("FAILED %s: %r", dev_id, exc)
                out[dev_id] = []  # mark as attempted-empty so it is not retried
        context.close()
        browser.close()
    return out


def _split_batches(items: List[str], n: int) -> List[List[str]]:
    """Split `items` into `n` roughly-equal chunks (round-robin, keeps order)."""
    batches: List[List[str]] = [[] for _ in range(n)]
    for i, item in enumerate(items):
        batches[i % n].append(item)
    return batches


def extract_all(dev_ids: List[str], workers: int) -> Dict[str, List[LinkRow]]:
    """Run `workers` browser processes in parallel and merge their results."""
    if not dev_ids:
        return {}
    n = min(workers, len(dev_ids), cpu_count() or 1)
    tasks = list(enumerate(_split_batches(dev_ids, n)))
    log.info("extracting %d dev_ids across %d worker(s)", len(dev_ids), n)

    merged: Dict[str, List[LinkRow]] = {}
    with Pool(processes=n) as pool:
        for partial in pool.imap_unordered(_worker, tasks):
            merged.update(partial)
    return merged


def load_cache() -> Dict[str, List[LinkRow]]:
    if config.LINKS_CACHE.exists():
        with config.LINKS_CACHE.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: Dict[str, List[LinkRow]]) -> None:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.LINKS_CACHE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    tmp.replace(config.LINKS_CACHE)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Extract Sales Arrangement links via Playwright.")
    ap.add_argument("--workers", type=int, default=config.NUM_WORKERS,
                    help="number of parallel browser workers")
    ap.add_argument("--limit", type=int, default=None,
                    help="only process the first N dev_ids (for testing)")
    ap.add_argument("--only", nargs="*", default=None,
                    help="explicit list of dev_ids to process")
    ap.add_argument("--force", action="store_true",
                    help="re-extract even if a dev_id is already cached")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    if args.only:
        dev_ids = args.only
    else:
        dev_ids = read_dev_ids()
    if args.limit:
        dev_ids = dev_ids[:args.limit]

    cache = load_cache()
    if args.force:
        todo = dev_ids
    else:
        todo = [d for d in dev_ids if d not in cache]
    log.info("cache has %d dev_ids, %d to do", len(cache), len(todo))

    if not todo:
        log.info("nothing to extract; cache is up to date")
    else:
        t0 = time.time()
        result = extract_all(todo, args.workers)
        cache.update(result)
        save_cache(cache)
        total_files = sum(len(v) for v in result.values())
        log.info("extracted %d files for %d dev_ids in %.1fs",
                 total_files, len(result), time.time() - t0)

    # Quick summary to stdout.
    with_files = sum(1 for v in cache.values() if v)
    print(f"Cache now holds {len(cache)} dev_ids "
          f"({with_files} have Sales Arrangement files, "
          f"{len(cache) - with_files} have none).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
