"""
End-to-end pipeline:

    Excel (dev_ids)
        │
        ▼  Stage 1 — Playwright renders each dev page, caches the PDF links
    cache/links.json
        │
        ▼  Stage 2 — multiprocess requests download every file
    downloads/<dev_id>/*.pdf

Run with no arguments to process everything:

    py main.py

Useful flags:
    py main.py --limit 10            # try the first 10 dev_ids only
    py main.py --workers 5           # 5 parallel processes in each stage
    py main.py --skip-extract        # only download from the existing cache
    py main.py --skip-download       # only (re)extract links
    py main.py --only 10005 10025    # process specific dev_ids
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

import config
from excel_utils import read_dev_ids

log = logging.getLogger("main")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download Sales Arrangement files from SRPE.")
    ap.add_argument("--workers", type=int, default=config.NUM_WORKERS,
                    help="parallel processes for each stage (default 5)")
    ap.add_argument("--limit", type=int, default=None,
                    help="process only the first N dev_ids")
    ap.add_argument("--only", nargs="*", default=None,
                    help="process only these dev_ids")
    ap.add_argument("--skip-extract", action="store_true",
                    help="do not extract links; download from existing cache")
    ap.add_argument("--skip-download", action="store_true",
                    help="do not download; only extract links")
    ap.add_argument("--force-extract", action="store_true",
                    help="re-render pages even if a dev_id is already cached")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
                        datefmt="%H:%M:%S")

    # ---- resolve the dev_id list ---------------------------------------
    if args.only:
        dev_ids = args.only
    else:
        dev_ids = read_dev_ids()
    if args.limit:
        dev_ids = dev_ids[:args.limit]
    log.info("pipeline targets %d dev_id(s)", len(dev_ids))

    # ---- Stage 1: extract links ---------------------------------------
    if not args.skip_extract:
        import extract_links
        cache = extract_links.load_cache()
        todo = dev_ids if args.force_extract else [d for d in dev_ids if d not in cache]
        if todo:
            log.info("=== Stage 1: extract links (%d to do) ===", len(todo))
            t0 = time.time()
            result = extract_links.extract_all(todo, args.workers)
            cache.update(result)
            extract_links.save_cache(cache)
            total = sum(len(v) for v in result.values())
            log.info("stage 1 done: %d files for %d dev_ids in %.1fs",
                     total, len(result), time.time() - t0)
        else:
            log.info("stage 1 skipped: all dev_ids already cached")
    else:
        log.info("=== Stage 1 skipped (--skip-extract) ===")

    # ---- Stage 2: download files --------------------------------------
    if not args.skip_download:
        import download_files
        log.info("=== Stage 2: download files ===")
        t0 = time.time()
        download_files.download_all(args.workers,
                                    dev_filter=set(args.only) if args.only else None)
        log.info("stage 2 done in %.1fs", time.time() - t0)
    else:
        log.info("=== Stage 2 skipped (--skip-download) ===")

    log.info("pipeline complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
