"""Harvest social-media text datasets from Harvard Dataverse.

Queries the Dataverse search API (``/api/search``) for datasets mentioning a
social-media platform together with a content term. Dataverse supports Boolean
operators, so one query per platform is issued (23 in total), with the date bound
pushed into the server-side ``fq`` filter.

For each hit the latest published version is fetched via ``/datasets``, because
the search response carries no file list. Datasets with restricted files or no
public version are dropped, as are those without a relevant data file. Dataverse
returns no language metadata, so English filtering relies entirely on fastText.
Results are written to ``dataverse.csv``.

An optional ``DATAVERSE_TOKEN`` (read from the environment) raises the rate limit.
Harvard throttles aggressively: the delays below are deliberately far longer than
in the other scripts, and server-side search limits were still reached during the
collection reported in the paper, on the largest queries.
"""

# Make the shared modules in the parent directory importable when this script is
# executed directly from within ``harvesting/``.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import time
from typing import Iterable, Optional

import pandas as pd
import requests
from tqdm import tqdm

from harvest_common import (
    build_queries,
    finalise,
    make_session,
    standardise,
    summarise_files,
    write_output,
)
from query_specs import MIN_CREATED_DATE, PLATFORMS, TEXT_TERMS

BASE_URL = "https://dataverse.harvard.edu"
SEARCH_URL = f"{BASE_URL}/api/search"
VERSION_URL = f"{BASE_URL}/api/datasets/:persistentId/versions/:latest-published"
OUTPUT_FILE = "dataverse.csv"

# Harvard throttles harder than the other repositories, hence the long pauses.
REQUEST_DELAY_S = 1.5
QUERY_DELAY_S = 30.0

RENAME = {
    "updatedAt": "updated",
    "global_id": "doi",
    "name": "title",
}


def _make_dataverse_session(token: Optional[str] = None) -> requests.Session:
    """Build a session for Dataverse, which expects an ``X-Dataverse-key`` header.

    Args:
        token: API token; read from ``DATAVERSE_TOKEN`` if omitted.

    Returns:
        A configured session.
    """
    session = make_session()
    token = token or os.getenv("DATAVERSE_TOKEN")
    if token:
        session.headers["X-Dataverse-key"] = token
    return session


def _fetch_files(session, persistent_id: str, timeout=(5, 180)) -> Optional[list[dict]]:
    """Return the file list of a dataset's latest published version.

    Args:
        session: Session to use.
        persistent_id: The dataset's persistent identifier (its DOI).
        timeout: ``(connect, read)`` timeout in seconds.

    Returns:
        The file records, or None if the dataset is restricted, still a draft, or
        otherwise not publicly accessible.
    """
    try:
        response = session.get(
            VERSION_URL, params={"persistentId": persistent_id}, timeout=timeout
        )
        if response.status_code in (401, 403, 404):
            return None
        response.raise_for_status()
        return response.json().get("data", {}).get("files") or []
    except requests.RequestException as exc:
        print(f"   warning: {persistent_id}: {exc}")
        return None


def search_records(
    query: str,
    *,
    per_page: int = 100,
    max_records: Optional[int] = None,
    delay_s: float = REQUEST_DELAY_S,
    timeout: tuple = (5, 180),
    session=None,
    published_after: str = MIN_CREATED_DATE,
) -> pd.DataFrame:
    """Search Harvard Dataverse for a single query and return the matching datasets.

    Args:
        query: A single search query.
        per_page: Page size.
        max_records: Stop after this many records, or None for all of them.
        delay_s: Pause between pages.
        timeout: ``(connect, read)`` timeout in seconds.
        session: Reusable session; one is created if omitted.
        published_after: Lower bound on the publication date, applied server-side.

    Returns:
        A table in the common output schema.
    """
    session = session or _make_dataverse_session()
    hits: list[dict] = []
    start = 0

    while True:
        params = {
            "q": query,
            "type": "dataset",
            "start": start,
            "per_page": per_page,
            "sort": "date",
            "order": "desc",
        }
        if published_after:
            # Curly braces make the lower bound exclusive.
            params["fq"] = f"dateSort:{{{published_after}T23:59:59Z TO *]"

        try:
            response = session.get(SEARCH_URL, params=params, timeout=timeout)
            response.raise_for_status()
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in (403, 429):
                print(f"   throttled ({code}), waiting 60s before one retry")
                time.sleep(60)
                try:
                    response = session.get(SEARCH_URL, params=params, timeout=timeout)
                    response.raise_for_status()
                except requests.HTTPError:
                    print("   giving up on this query after retry")
                    break
            else:
                print(f"   skipping at start={start}: {exc}")
                break

        data = response.json().get("data", {})
        items = data.get("items") or []
        if not items:
            break

        hits.extend(items)

        if max_records is not None and len(hits) >= max_records:
            hits = hits[:max_records]
            break
        if len(hits) >= data.get("total_count", 0):
            break

        start += per_page
        if delay_s:
            time.sleep(delay_s)

    if not hits:
        return standardise(pd.DataFrame())

    df = pd.json_normalize(hits, sep=".")
    df["total_file_size"] = 0
    df["file_types"] = ""
    df["language"] = ""

    keep = []
    for i in tqdm(range(len(df)), desc="   fetching versions", unit="ds"):
        persistent_id = df.loc[i, "global_id"] if "global_id" in df.columns else None
        if not persistent_id:
            continue

        files = _fetch_files(session, persistent_id, timeout=timeout)
        if files is None:
            continue
        if any(f.get("restricted", False) for f in files):
            continue

        summary = summarise_files(
            [(f.get("dataFile") or {}).get("filename", "") for f in files],
            [(f.get("dataFile") or {}).get("filesize", 0) for f in files],
        )
        if not summary["has_relevant_file"]:
            continue

        df.loc[i, "total_file_size"] = summary["total_file_size"]
        df.loc[i, "file_types"] = summary["file_types"]
        keep.append(i)

        if delay_s:
            time.sleep(delay_s / 2)

    df = df.loc[keep].reset_index(drop=True)

    # Dataverse identifies datasets by DOI; derive a short id from it.
    if "global_id" in df.columns:
        df["id"] = df["global_id"].astype(str).str.replace("^doi:", "", regex=True)
        df["global_id"] = df["id"]

    return standardise(df, RENAME)


def search_all(
    queries: Iterable[str],
    *,
    inter_query_delay: float = QUERY_DELAY_S,
) -> pd.DataFrame:
    """Run every query in turn and concatenate the results.

    Args:
        queries: The search queries to run.
        inter_query_delay: Pause between queries. Long by default, because
            Harvard returns HTTP 403 rate-limit responses readily.

    Returns:
        The concatenated, unfiltered results in the common output schema.
    """
    queries = list(queries)
    session = _make_dataverse_session()
    frames = []

    for i, query in enumerate(queries):
        df = search_records(query, session=session)
        print(f"[{i + 1}/{len(queries)}] {query[:60]}... -> {len(df)} records")
        if not df.empty:
            frames.append(df)

        if inter_query_delay and i < len(queries) - 1:
            time.sleep(inter_query_delay)

    if not frames:
        return standardise(pd.DataFrame())
    return pd.concat(frames, ignore_index=True)


def main() -> int:
    """Run the full Harvard Dataverse harvest and write ``dataverse.csv``."""
    queries = build_queries(PLATFORMS, TEXT_TERMS, boolean=True)
    df = search_all(queries)
    df = finalise(df, deduplicate_by="doi")
    write_output(df, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
