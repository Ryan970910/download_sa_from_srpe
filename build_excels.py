"""
Sample the 10 newest Sales Arrangement PDFs per dev_id, extract their fields
with pdfplumber, and write one Excel per dev_id.

Output:  extracted/<dev_id>.xlsx
Columns: property_name | issue_date/revision_date | sa_no

Rules:
  - Only dev_ids that actually have downloaded PDFs are processed.
  - For each dev_id, take the 10 newest files (by filename date prefix);
    fewer than 10 -> take all.
  - revision_date takes precedence over issue_date in the date column.
  - Missing values are left empty.
  - Runs across multiple processes (default 5) for speed.
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

log = logging.getLogger("build_excels")

OUTPUT_DIR = config.BASE_DIR / "extracted"
SAMPLE_SIZE = 10  # default; overridden by --sample in main()
COLS = ["property_name", "issue_date/revision_date", "sa_no"]


def newest_pdfs(dev_id: str, n: int) -> List[Path]:
    """Return the `n` newest PDFs for a dev_id (by filename date, descending)."""
    folder = config.BASE_DIR / "downloads" / dev_id
    if not folder.exists():
        return []
    pdfs = sorted(folder.glob("*.pdf"), reverse=True)  # filenames start YYYY-MM-DD
    return pdfs[:n]


def process_dev(dev_id: str, sample_size: int = SAMPLE_SIZE) -> Tuple[str, int, int, List[str]]:
    """Process one dev_id: extract fields and write its Excel.

    Returns (dev_id, rows_written, errors, error_messages).
    """
    pdfs = newest_pdfs(dev_id, sample_size)
    if not pdfs:
        return dev_id, 0, 0, []

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "SA"
    ws.append(COLS)

    errors: List[str] = []
    for pdf_path in pdfs:
        info = extract_from_pdf(str(pdf_path))
        if info["error"]:
            errors.append(f"{pdf_path.name}: {info['error']}")
        ws.append([
            info["property_name"] or "",
            info["date"] or "",          # revision_date if present else issue_date
            info["sa_no"] or "",
        ])

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{dev_id}.xlsx"
    wb.save(out_path)
    return dev_id, len(pdfs), len(errors), errors


def _worker(task):
    wid, dev_id, sample_size = task
    logging.basicConfig(
        level=logging.INFO,
        format=f"[w{wid}] %(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        dev_id, n, nerr, errs = process_dev(dev_id, sample_size)
        log.info("[%s] wrote %d rows (%d errors) -> extracted/%s.xlsx",
                 dev_id, n, nerr, dev_id)
        return dev_id, n, nerr, errs
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] FAILED: %r", dev_id, exc)
        return dev_id, 0, 1, [f"{dev_id}: {type(exc).__name__}: {exc}"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build per-dev_id Excel files from sampled PDFs.")
    ap.add_argument("--workers", type=int, default=config.NUM_WORKERS,
                    help="parallel processes")
    ap.add_argument("--only", nargs="*", default=None,
                    help="only process these dev_ids")
    ap.add_argument("--limit", type=int, default=None,
                    help="only process the first N eligible dev_ids")
    ap.add_argument("--sample", type=int, default=SAMPLE_SIZE,
                    help="number of newest PDFs per dev_id (default 10)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
                        datefmt="%H:%M:%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Eligible dev_ids = those with cached Sales Arrangement files.
    cache = load_cache()
    if args.only:
        eligible = [d for d in args.only if cache.get(d)]
    else:
        eligible = [d for d in read_dev_ids() if cache.get(d)]
    if args.limit:
        eligible = eligible[:args.limit]
    log.info("processing %d dev_id(s), sampling %d PDFs each",
             len(eligible), args.sample)

    tasks = [(i % max(1, args.workers), d, args.sample) for i, d in enumerate(eligible)]
    totals = {"rows": 0, "errs": 0, "devs": 0}
    t0 = time.time()
    with Pool(processes=max(1, args.workers)) as pool:
        for dev_id, n, nerr, errs in pool.imap_unordered(_worker, tasks):
            totals["rows"] += n
            totals["errs"] += nerr
            totals["devs"] += 1
            for e in errs:
                log.warning("  err: %s", e)
            done = totals["devs"]
            if done % 25 == 0 or done == len(tasks):
                log.info("progress %d/%d devs (rows=%d errors=%d)",
                         done, len(tasks), totals["rows"], totals["errs"])
    log.info("done in %.1fs: %d dev Excels, %d rows, %d errors",
             time.time() - t0, totals["devs"], totals["rows"], totals["errs"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
