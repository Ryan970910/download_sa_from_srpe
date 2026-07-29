"""
Stage 2 - Download the extracted Sales Arrangement PDFs into per-dev_id folders.

Reads the link cache produced by extract_links.py and fetches every file with
`requests`. Downloads run across multiple processes (default 5) for speed, and
each file is skipped if a complete copy already exists on disk (size match), so
re-running the script resumes cleanly after interruptions.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from multiprocessing import Pool
from typing import Dict, List

import requests

import config
from extract_links import load_cache

log = logging.getLogger("download")

# A shared session per worker process for connection reuse.
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        _session = s
    return _session


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------
# The site exposes the real filename in the URL path, e.g. ".../19837240607002SA.pdf".
_FILENAME_RE = re.compile(r"/([^/]+\.pdf)(?:/|$)", re.IGNORECASE)
# Date column looks like "07 Jun 2024 05:01:00 PM". Make it filesystem-safe and
# sortable: "2024-06-07_170100".
_DATE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM)"
)
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _parse_date_sortkey(date_str: str) -> str:
    """Turn the upload-date column into a sortable 'YYYY-MM-DD_HHMMSS' string."""
    m = _DATE_RE.search(date_str or "")
    if not m:
        return ""
    day, mon, year, hh, mm, ss, ap = m.groups()
    month = _MONTHS.get(mon[:3].title(), 0)
    if not month:
        return ""
    hour = int(hh) % 12 + (12 if ap.upper() == "PM" else 0)
    return f"{int(year):04d}-{month:02d}-{int(day):02d}_{hour:02d}{int(mm):02d}{int(ss):02d}"


def _build_filename(row: Dict[str, str], dev_id: str) -> str:
    """Build a unique, English, sortable filename for one file."""
    m = _FILENAME_RE.search(row.get("href", ""))
    base = m.group(1) if m else f"{dev_id}.pdf"
    prefix = _parse_date_sortkey(row.get("date", ""))
    name = f"{prefix}_{base}" if prefix else base
    return _BAD_CHARS.sub("_", name)


# ---------------------------------------------------------------------------
# Download logic
# ---------------------------------------------------------------------------
def _already_complete(path, expected_size: int | None) -> bool:
    """True if `path` exists and looks complete (size within rounding tolerance)."""
    if not path.exists():
        return False
    actual = path.stat().st_size
    if expected_size and expected_size > 0:
        return _within_rounding_tolerance(actual, expected_size)
    return actual > 0


def _bytes_from_size_label(label: str) -> int | None:
    """Parse a 'File Size' cell like '639.14 KB' into bytes (best effort)."""
    if not label:
        return None
    m = re.search(r"([\d.]+)\s*([KMGT]?B)", label, re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).upper()
    factor = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[unit]
    return int(round(value * factor))


def _within_rounding_tolerance(actual: int, expected: int) -> bool:
    """Accept a download whose byte count differs from the displayed size.

    The site's "File Size" column is rounded to 2 decimals (e.g. '639.14 KB' =
    654479.36 bytes), so the true byte count almost never matches it exactly.
    We accept the file when it is at least 99% of the expected size (i.e. not
    truncated); a few extra bytes from rounding are fine.
    """
    if expected <= 0:
        return True
    return actual >= int(expected * 0.99)


def _looks_like_error_response(path) -> bool:
    """True if the downloaded bytes are an API error JSON, not a real PDF.

    A valid PDF starts with '%PDF'. The SRPE download endpoint occasionally
    answers HTTP 200 with a small JSON body like
    {"code":-99,"remarks":"Error encountered...",...} when the source file on
    the server is broken. We catch that here so it is never stored as a .pdf.
    """
    try:
        with path.open("rb") as f:
            head = f.read(256)
    except OSError:
        return False
    if head.startswith(b"%PDF"):
        return False
    # Not a PDF — inspect whether it looks like the known error envelope.
    try:
        text = head.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return False
    return '"code"' in text and ('Error' in text or 'resultData' in text)


def download_one(dev_id: str, row: Dict[str, str]) -> tuple[str, str, str]:
    """Download a single file. Returns (dev_id, status, detail).

    status is one of: 'ok', 'skip', 'error'.
    """
    href = row.get("href", "")
    if not href:
        return dev_id, "error", "no href"

    url = config.SITE_BASE + href
    folder = config.dev_folder(dev_id)
    filename = _build_filename(row, dev_id)
    target = folder / filename
    expected = _bytes_from_size_label(row.get("size", ""))

    if _already_complete(target, expected):
        return dev_id, "skip", filename

    session = _get_session()
    last_err = ""
    for attempt in range(1, config.HTTP_RETRIES + 1):
        try:
            with session.get(url, timeout=config.HTTP_TIMEOUT, stream=True) as r:
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    if 400 <= r.status_code < 500 and r.status_code != 429:
                        break  # won't fix itself on retry
                    raise RuntimeError(last_err)
                tmp = target.with_suffix(target.suffix + ".part")
                written = 0
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                # The download endpoint sometimes returns HTTP 200 with a JSON
                # error body (e.g. {"code":-99,"remarks":"Error encountered..."})
                # instead of the PDF. Detect and reject those so we never store a
                # fake ".pdf" that is really an API error.
                if _looks_like_error_response(tmp):
                    last_err = "server returned error JSON instead of PDF"
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(last_err)
                # Verify we got the whole file. The displayed size is rounded,
                # so we accept anything within rounding tolerance and only reject
                # clearly truncated downloads (< 99% of expected).
                if expected and not _within_rounding_tolerance(written, expected):
                    last_err = f"truncated {written}/{expected}"
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(last_err)
                tmp.replace(target)
                return dev_id, "ok", filename
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(config.HTTP_RETRY_BACKOFF ** attempt)

    log.error("[%s] FAILED %s -> %s", dev_id, filename, last_err)
    return dev_id, "error", last_err


def _worker_file(task):
    """Pool worker: download a single file.

    `task` is (worker_id, dev_id, row). Splitting work to one-file-per-task keeps
    the pool perfectly balanced even when some dev_ids have far more files.
    """
    worker_id, dev_id, row = task
    logging.basicConfig(
        level=logging.INFO,
        format=f"[w{worker_id}] %(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    _, status, detail = download_one(dev_id, row)
    if status == "ok":
        log.info("[%s] downloaded %s", dev_id, detail)
    elif status == "skip":
        log.debug("[%s] skipped %s (already exists)", dev_id, detail)
    return dev_id, 1 if status == "ok" else 0, 1 if status == "skip" else 0, 1 if status == "error" else 0


def download_all(workers: int, dev_filter=None) -> Dict[str, tuple]:
    """Download every cached file. Returns {dev_id: (ok, skip, err)}."""
    cache = load_cache()
    # Spread work across workers at the file level: flatten to (dev,row) tasks
    # so a dev with many files doesn't block one whole worker. We bucket by dev
    # only for nicer logging. Flattening gives the best load balance.
    tasks = []
    wid = 0
    for dev_id, rows in cache.items():
        if dev_filter and dev_id not in dev_filter:
            continue
        if not rows:
            continue
        for row in rows:
            tasks.append((wid % max(1, workers), dev_id, row))
            wid += 1
    if not tasks:
        log.info("no files to download")
        return {}

    log.info("downloading %d file(s) across %d worker(s)", len(tasks), workers)
    totals = {"ok": 0, "skip": 0, "err": 0}
    per_dev: Dict[str, list] = {}
    t0 = time.time()
    with Pool(processes=workers) as pool:
        # imap over single-file tasks for fine-grained progress + balance
        for dev_id, ok, skip, err in pool.imap_unordered(_worker_file, tasks):
            per_dev.setdefault(dev_id, [0, 0, 0])
            per_dev[dev_id][0] += ok
            per_dev[dev_id][1] += skip
            per_dev[dev_id][2] += err
            totals["ok"] += ok
            totals["skip"] += skip
            totals["err"] += err
            done = sum(totals.values())
            if done % 25 == 0 or done == len(tasks):
                log.info("progress %d/%d (ok=%d skip=%d err=%d)",
                         done, len(tasks), totals["ok"], totals["skip"], totals["err"])
    log.info("finished in %.1fs: ok=%d skip=%d err=%d",
             time.time() - t0, totals["ok"], totals["skip"], totals["err"])
    return {d: tuple(v) for d, v in per_dev.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download cached Sales Arrangement PDFs.")
    ap.add_argument("--workers", type=int, default=config.NUM_WORKERS,
                    help="number of parallel download processes")
    ap.add_argument("--only", nargs="*", default=None,
                    help="only download these dev_ids")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    download_all(args.workers, dev_filter=set(args.only) if args.only else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
