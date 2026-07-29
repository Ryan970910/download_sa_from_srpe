# download_sa_from_srpe

Download all **Sales Arrangement** PDFs for every `dev_id` listed in
`all_subfolders.xlsx` from the Hong Kong SRPE platform
(`https://www.srpe.gov.hk/opip/selected_dev_all_development?devId=...`)
and save them into per-`dev_id` folders.

The site is a React single-page app, so the PDF links are injected by JavaScript
and are **not** present in the raw HTML. This project therefore runs in two
stages:

1. **Extract** — a headless Chromium (Playwright) renders each dev page, waits
   for the *Sales Arrangement* section, and reads the download links out of the
   DOM. Links are cached in `cache/links.json` so a page is never rendered twice.
2. **Download** — `requests` fetches every cached link in parallel (default
   **5 processes**) into `downloads/<dev_id>/`.

Downloads are resumable: a file that already exists with a matching size is
skipped, so interrupting and re-running picks up exactly where it left off.

## Install

```bash
py -m pip install openpyxl requests playwright
py -m playwright install chromium
```

## Usage

```bash
# everything: extract links for all 552 dev_ids, then download all files
py main.py

# quick test on the first 10 dev_ids
py main.py --limit 10

# only specific dev_ids
py main.py --only 10005 10025

# change parallelism (default 5)
py main.py --workers 5

# run just one stage
py main.py --skip-download      # only extract links
py main.py --skip-extract       # only download from the existing cache
```

## Output

```
downloads/
  10005/
    2024-06-07_170100_19837240607002SA.pdf
    2024-08-07_165759_19837240807001SA.pdf
    ...
  10025/
    ...
cache/
  links.json        # { "10005": [ {href, date, size}, ... ], ... }
```

File names are `YYYY-MM-DD_HHMMSS_<original>.pdf` — the upload date/time is
prefixed so files sort chronologically, and the original server filename (e.g.
`19837240607002SA.pdf`) is preserved. A `dev_id` with no Sales Arrangement files
simply produces no folder.

## Files

| File               | Role                                                        |
|--------------------|-------------------------------------------------------------|
| `config.py`        | Paths, site URL, cookie, concurrency, folder helpers        |
| `excel_utils.py`   | Reads `dev_id` values from column A of the Excel file       |
| `extract_links.py` | Stage 1 — Playwright renders pages, caches the PDF links    |
| `download_files.py`| Stage 2 — multiprocess `requests` downloads into folders    |
| `main.py`          | Orchestrates both stages (run this)                         |

## Notes

- The download URLs are public (no login required); the only cookie needed is
  `srpe_public_terms_accepted=1`, which Playwright sets automatically.
- The "File Size" shown on the site is rounded to 2 decimals, so the byte count
  rarely matches exactly. Downloads are accepted when they are ≥ 99% of the
  displayed size (i.e. not truncated).
- `dev_id` 10065 (and similar) correctly produces no folder — that development
  genuinely has no Sales Arrangement documents on the platform.
