"""
Extract fields from EVERY Sales Arrangement PDF and write them into ONE
consolidated Excel.

Unlike build_excels.py (which samples 10 newest PDFs per dev_id into separate
files), this script processes all ~15,900 PDFs and produces a single workbook:

    all_sa_info.xlsx
        dev_id | property_name | issue_date/revision_date | sa_no

Each row is one PDF. Scanned/image-only PDFs that pdfplumber cannot read are
left empty (dev_id still recorded) and counted in the log.

Runs across multiple processes (default 5). Progress is logged periodically.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from multiprocessing import Pool
from pathlib import Path
from typing import List, Tuple

import openpyxl

import config
from excel_utils import read_dev_ids
from extract_links import load_cache
from extract_pdf_info import extract_from_pdf

log = logging.getLogger("build_all")

OUTPUT_FILE = config.BASE_DIR / "all_sa_info.xlsx"
COLS = ["dev_id", "property_name", "issue_date/revision_date", "sa_no", "sa_no_source", "file_name"]


def all_pdfs_for(dev_id: str) -> List[Path]:
    """All Sales Arrangement PDFs for a dev_id, newest first."""
    folder = config.BASE_DIR / "downloads" / dev_id
    if not folder.exists():
        return []
    return sorted(folder.glob("*.pdf"), reverse=True)


def process_dev(dev_id: str) -> Tuple[str, List[dict], int]:
    """Extract every PDF for one dev_id.

    Returns (dev_id, rows, error_count) where each row is a dict with the
    final column values plus the source filename (for debugging).
    """
    pdfs = all_pdfs_for(dev_id)
    rows: List[dict] = []
    errors = 0
    for pdf_path in pdfs:
        info = extract_from_pdf(str(pdf_path))
        if info["error"]:
            errors += 1
        rows.append({
            "dev_id": dev_id,
            "property_name": info["property_name"] or "",
            "date": info["date"] or "",          # revision_date if present else issue_date
            "sa_no": info["sa_no"] or "",
            "sa_no_source": info["sa_no_source"] or "",
            "file_name": pdf_path.name,
            "error": info["error"] or "",
        })
    return dev_id, rows, errors


def _worker(task):
    wid, dev_id = task
    logging.basicConfig(
        level=logging.INFO,
        format=f"[w{wid}] %(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        dev_id, rows, errors = process_dev(dev_id)
        log.info("[%s] %d PDFs (%d errors)", dev_id, len(rows), errors)
        return dev_id, rows, errors
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] FAILED: %r", dev_id, exc)
        return dev_id, [], 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Extract ALL PDFs into one Excel.")
    ap.add_argument("--workers", type=int, default=config.NUM_WORKERS,
                    help="parallel processes (default 5)")
    ap.add_argument("--only", nargs="*", default=None,
                    help="only process these dev_ids")
    ap.add_argument("--limit", type=int, default=None,
                    help="only process the first N eligible dev_ids")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
                        datefmt="%H:%M:%S")

    # Eligible dev_ids = those with cached Sales Arrangement files.
    cache = load_cache()
    if args.only:
        eligible = [d for d in args.only if cache.get(d)]
    else:
        eligible = [d for d in read_dev_ids() if cache.get(d)]
    if args.limit:
        eligible = eligible[:args.limit]
    log.info("processing %d dev_id(s), ALL pdfs each", len(eligible))

    tasks = [(i % max(1, args.workers), d) for i, d in enumerate(eligible)]

    # Collect results, then write once at the end (single workbook).
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "SA_info"
    ws.append(COLS)

    total_rows = 0
    total_errors = 0
    done = 0
    t0 = time.time()
    with Pool(processes=max(1, args.workers)) as pool:
        for dev_id, rows, errors in pool.imap_unordered(_worker, tasks):
            for r in rows:
                ws.append([r["dev_id"], r["property_name"], r["date"], r["sa_no"], r["sa_no_source"], r["file_name"]])
            total_rows += len(rows)
            total_errors += errors
            done += 1
            if done % 25 == 0 or done == len(tasks):
                log.info("progress %d/%d devs | rows=%d errors=%d | %.0fs",
                         done, len(tasks), total_rows, total_errors, time.time() - t0)

    # Freeze the header row + enable autofilter for easy checking.
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    wb.save(OUTPUT_FILE)
    log.info("SAVED %s | %d rows | %d errors | %.1fs",
             OUTPUT_FILE.name, total_rows, total_errors, time.time() - t0)
    print(f"\n✓ Done: {OUTPUT_FILE.name} — {total_rows} rows, {total_errors} unreadable PDFs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
