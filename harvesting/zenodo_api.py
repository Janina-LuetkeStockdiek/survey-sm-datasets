"""Harvest social-media text datasets from Zenodo.

Queries the Zenodo REST API (``/api/records``) for open datasets whose title or
description mentions a social-media platform together with a content term, that
contain at least one relevant data file and were created on or after
``MIN_CREATED_DATE``. Zenodo supports Boolean operators, so one query per
platform is issued (23 in total). Results are language-filtered to English
(Zenodo's own language metadata plus a fastText fallback) and written to
``zenodo.csv``.

An optional ``ZENODO_TOKEN`` (read from the environment) raises the rate limit.
"""

# Make the shared modules in the parent directory importable when this script is
# executed directly from within ``harvesting/``.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time
from typing import Iterable, Optional

import pandas as pd

from harvest_common import (
    QUERY_DELAY_S,
    REQUEST_DELAY_S,
    build_queries,
    finalise,
    make_session,
    standardise,
    summarise_files,
    write_output,
)
from query_specs import MIN_CREATED_DATE, PLATFORMS, TEXT_TERMS

API_URL = "https://zenodo.org/api/records"
OUTPUT_FILE = "zenodo.csv"

# Zenodo column name -> common schema name.
RENAME = {
    "metadata.description": "description",
    "metadata.language": "language",
}


def search_records(
    query: str,
    *,
    size: int = 100,
    max_records: Optional[int] = None,
    delay_s: float = REQUEST_DELAY_S,
    timeout: tuple = (5, 180),
    session=None,
    token: Optional[str] = None,
) -> pd.DataFrame:
    """Search Zenodo for a single query and return the matching records.

    Pages through the search results, aggregates each record's file list into the
    shared filter fields, and drops records without a relevant data file. The
    creation-date bound is pushed into the query itself rather than filtered
    afterwards, which keeps the result sets small.

    Args:
        query: A single search query, e.g. ``"Reddit AND (text OR posts OR ...)"``.
        size: Page size. Below 200 to reduce server-side timeouts.
        max_records: Stop after this many records, or None for all of them.
        delay_s: Pause between pages.
        timeout: ``(connect, read)`` timeout in seconds.
        session: Reusable session; one is created if omitted.
        token: Bearer token; read from ``ZENODO_TOKEN`` if omitted.

    Returns:
        A table in the common output schema.
    """
    session = session or make_session(token=token, token_env="ZENODO_TOKEN")
    min_created = pd.to_datetime(MIN_CREATED_DATE, utc=True).strftime("%Y-%m-%d")

    hits: list[dict] = []
    page = 1

    while True:
        params = [
            ("type", "dataset"),
            ("q", f"(title:{query} OR description:{query}) AND created:[{min_created} TO *]"),
            ("access_right", "open"),
            ("all_versions", "false"),
            ("page", page),
            ("size", size),
            ("sort", "mostrecent"),
        ]
        response = session.get(API_URL, params=params, timeout=timeout)
        response.raise_for_status()

        page_hits = response.json().get("hits", {}).get("hits", [])
        if not page_hits:
            break

        hits.extend(page_hits)

        if max_records is not None and len(hits) >= max_records:
            hits = hits[:max_records]
            break
        if len(page_hits) < size:
            break

        page += 1
        if delay_s:
            time.sleep(delay_s)

    if not hits:
        return standardise(pd.DataFrame())

    df = pd.json_normalize(hits, sep=".")

    keep = []
    for i in range(len(df)):
        files = df.loc[i, "files"] if "files" in df.columns else []
        files = files if isinstance(files, list) else []
        summary = summarise_files(
            [f.get("key", "") for f in files],
            [f.get("size", 0) for f in files],
        )
        df.loc[i, "total_file_size"] = summary["total_file_size"]
        df.loc[i, "file_types"] = summary["file_types"]
        if summary["has_relevant_file"]:
            keep.append(i)

    df = df.loc[keep].reset_index(drop=True)
    return standardise(df, RENAME)


def search_all(
    queries: Iterable[str],
    *,
    delay_s: float = REQUEST_DELAY_S,
    inter_query_delay: float = QUERY_DELAY_S,
) -> pd.DataFrame:
    """Run every query in turn and concatenate the results.

    Args:
        queries: The search queries to run.
        delay_s: Pause between pages within a query.
        inter_query_delay: Pause between queries.

    Returns:
        The concatenated, unfiltered results in the common output schema.
    """
    queries = list(queries)
    session = make_session(token_env="ZENODO_TOKEN")
    frames = []

    for i, query in enumerate(queries):
        df = search_records(query, delay_s=delay_s, session=session)
        print(f"[{i + 1}/{len(queries)}] {query[:60]}... -> {len(df)} records")
        if not df.empty:
            frames.append(df)

        if inter_query_delay and i < len(queries) - 1:
            time.sleep(inter_query_delay)

    if not frames:
        return standardise(pd.DataFrame())
    return pd.concat(frames, ignore_index=True)


def main() -> int:
    """Run the full Zenodo harvest and write ``zenodo.csv``."""
    queries = build_queries(PLATFORMS, TEXT_TERMS, boolean=True)
    df = search_all(queries)
    df = finalise(df, deduplicate_by="id")
    write_output(df, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
