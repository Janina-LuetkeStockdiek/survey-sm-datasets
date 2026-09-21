"""Harvest social-media text datasets from data.gov (US).

Queries the data.gov catalog search (JSON endpoint) for each platform + content
term, keeps datasets created on or after ``MIN_CREATED_DATE`` whose description
mentions the platform, parses their distributions for file count/size/formats,
language-filters to English via fastText and writes ``datagov.csv``.

Note: data.gov's ``byteSize`` is frequently missing, so ``total_file_size`` is
often 0 and the size filter is intentionally disabled in ``main``.
"""


# Make the shared modules in the parent directory (config.py, query_specs.py)
# importable when this script is executed directly from within ``harvesting/``.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import re
import time
import json
from urllib.parse import urlparse
import requests
import pandas as pd
from tqdm import tqdm
from harvest_common import (
    QUERY_DELAY_S,
    build_queries,
    finalise,
    make_session,
    standardise,
    write_output,
)
from query_specs import MIN_CREATED_DATE, PLATFORMS, TEXT_TERMS

BASE = "https://catalog.data.gov"
SEARCH_URL = f"{BASE}/search"
OUTPUT_FILE = "datagov.csv"

PER_PAGE = 100  # web UI uses 20; the JSON endpoint accepts more -> fewer requests
SORT = "metadata_created desc"
REQUEST_TIMEOUT = 30
SLEEP_BETWEEN = QUERY_DELAY_S
MAX_RECORDS_PER_QUERY = 2000  # safety limit per query

QUERIES = build_queries(PLATFORMS, TEXT_TERMS, boolean=True)

# filter out X-ray / X-Men etc. false positives from the X query
X_FALSE_POSITIVE = re.compile(
    r'\b(x[\s\-]?ray|x[\s\-]?rays|x[\s\-]?men|x[\s\-]?box|'
    r'x[\s\-]?axis|x[\s\-]?factor|x[\s\-]?chromosome|'
    r'x[\s\-]?coordinate|matrix|excel|max|tax|index|complex|'
    r'exos|oxide|toxic)\b',
    re.IGNORECASE,
)



# ----------------------------------------------------------------------------- #
# Parsing helpers
# ----------------------------------------------------------------------------- #
_EXT_RE = re.compile(r'\.([A-Za-z0-9]{1,6})(?:\?|#|$)')


def _guess_format(dist):
    """Derive a format from mediaType/format, else guess from the URL extension."""
    fmt = dist.get("format") or dist.get("mediaType")
    if fmt:
        return str(fmt).strip().lower()
    url = dist.get("downloadURL") or dist.get("accessURL") or ""
    m = _EXT_RE.search(urlparse(url).path)
    if m:
        ext = m.group(1).lower()
        # ignore obvious non-file extensions (e.g. "com", "gov")
        if ext not in {"com", "gov", "org", "net", "html", "htm", "php", "aspx"}:
            return ext
    return None


def _parse_distributions(distributions):
    """Return ``(n_files, total_bytes, file_types_str)``.

    ``byteSize`` is often missing, so ``total_bytes`` is frequently 0.
    """
    if not distributions:
        return 0, 0, ""

    n_files = len(distributions)
    total_bytes = 0
    fmts = []

    for dist in distributions:
        if not isinstance(dist, dict):
            continue
        bs = dist.get("byteSize")
        if bs is not None:
            try:
                total_bytes += int(float(bs))
            except (ValueError, TypeError):
                pass
        fmt = _guess_format(dist)
        if fmt:
            fmts.append(fmt)

    # unique formats, stable order
    seen = set()
    uniq = []
    for f in fmts:
        if f not in seen:
            seen.add(f)
            uniq.append(f)

    return n_files, total_bytes, ", ".join(uniq)


def _build_url(dcat, identifier):
    """Prefer landingPage, otherwise construct a URL from the identifier."""
    lp = dcat.get("landingPage")
    if lp:
        return lp
    if identifier:
        # if the identifier is already a URL
        if identifier.startswith("http"):
            return identifier
        return f"{BASE}/dataset/{identifier}"
    return ""


def _record_to_row(record):
    """Convert one catalog record into a flat output row (or None)."""
    dcat = record.get("dcat") or {}
    if not dcat:
        return None

    title = (dcat.get("title") or "").strip()
    description = (dcat.get("description") or "").strip()
    identifier = dcat.get("identifier") or ""

    n_files, total_bytes, file_types = _parse_distributions(dcat.get("distribution"))

    return {
        "id": identifier,
        "doi": identifier if str(identifier).startswith("10.") else "",
        "url": _build_url(dcat, identifier),
        "updated": dcat.get("modified", "") or dcat.get("issued", "") or "",
        "title": title,
        "description": description,
        "language": "",  # data.gov publishes none; filled in by fastText
        "total_file_size": total_bytes,
        "file_types": file_types,
    }


# ----------------------------------------------------------------------------- #
# Search (one query, with cursor pagination)
# ----------------------------------------------------------------------------- #
def search_records(session, query, min_date):
    """Page through one query and return rows created on/after ``min_date``.

    Results are date-sorted descending, so pagination stops as soon as records
    older than ``min_date`` appear. Only rows whose description mentions the
    platform are kept.
    """
    rows = []
    after = None
    collected = 0

    while True:
        params = {
            "q": query,
            "per_page": PER_PAGE,
            "sort": SORT,
        }
        if after:
            params["after"] = after
            params["results"] = collected + PER_PAGE

        try:
            resp = session.get(SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            print(f"  [WARN] Request failed for '{query}' (after={after}): {e}")
            break

        results = data.get("results") or []
        if not results:
            break

        stop = False  # flag for early termination
        for rec in results:
            row = _record_to_row(rec)
            if not row:
                continue

            # --- check creation date -------------------------------------- #
            created = pd.to_datetime(row.get("created", ""), errors="coerce", utc=True)
            if pd.isna(created):
                # no parseable date: skip, but do NOT stop
                # (sorting assumes date values).
                continue
            if created < min_date:
                # from here on only older records follow -> stop
                stop = True
                break

            # --- platform check (as before) ------------------------------- #
            vor_klammer = query.split('(')[0]
            woerter = [wort for wort in vor_klammer.split() if wort != 'AND']
            platform = ' '.join(woerter)
            if platform.lower() in row["description"].lower():
                rows.append(row)

        collected += len(results)
        after = data.get("after")

        if stop or not after or len(results) < PER_PAGE or collected >= MAX_RECORDS_PER_QUERY:
            break

        time.sleep(SLEEP_BETWEEN)

    return rows


def search_all(session, queries, min_date):
    """Run all queries and return the concatenated rows (with duplicates)."""
    all_rows = []
    for q in tqdm(queries, desc="Queries"):
        rows = search_records(session, q, min_date)
        for r in rows:
            r["_query"] = q
        all_rows.extend(rows)
        tqdm.write(f"  '{q[:40]}...' → {len(rows)} hits")
        time.sleep(SLEEP_BETWEEN)
    return all_rows


# ----------------------------------------------------------------------------- #
# Post-processing
# ----------------------------------------------------------------------------- #

def _platform_from_query(query):
    # e.g. "Twitter AND (...)" -> "Twitter"
    m = re.match(r'\s*([A-Za-z0-9]+)', query)
    return m.group(1) if m else ""


def _mentions_platform(row, platform):
    if not platform:
        return True
    blob = f"{row.get('title', '')} {row.get('description', '')}".lower()
    return platform.lower() in blob


def main() -> int:
    """Run the full data.gov harvest and write ``datagov.csv``."""
    session = make_session()
    rows = search_all(session, QUERIES, pd.Timestamp(MIN_CREATED_DATE, tz="UTC"))
    print(f"Raw hits across all queries: {len(rows)}")

    df = standardise(pd.DataFrame(rows))
    # data.gov reports no file sizes, so the size threshold cannot be applied.
    df = finalise(df, deduplicate_by="id", apply_size_filter=False)
    write_output(df, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
