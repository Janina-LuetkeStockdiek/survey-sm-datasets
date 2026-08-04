"""Harvest social-media text datasets from Dryad.

Queries the Dryad search API (``/search``) for datasets mentioning a social-media
platform together with a content term. Dryad's search does not accept the Boolean
form used elsewhere, so the Cartesian product of platform and content terms is
issued (644 queries).

For each hit the latest published version and its file list are fetched via
``/versions``, because the search response carries neither. Datasets whose files
are restricted or embargoed are dropped, as are those without a relevant data
file and those published before ``MIN_CREATED_DATE``. Dryad returns no language
metadata, so English filtering relies entirely on fastText. Results are written
to ``dryad.csv``.

An optional ``DRYAD_TOKEN`` (read from the environment) raises the rate limit.
"""

# Make the shared modules in the parent directory importable when this script is
# executed directly from within ``harvesting/``.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time
from typing import Iterable, Optional
from urllib.parse import quote

import pandas as pd
import requests
from tqdm import tqdm

from harvest_common import (
    QUERY_DELAY_S,
    REQUEST_DELAY_S,
    finalise,
    make_session,
    standardise,
    summarise_files,
    write_output,
)
from query_specs import MIN_CREATED_DATE, PLATFORMS, TEXT_TERMS

BASE_URL = "https://datadryad.org"
SEARCH_URL = f"{BASE_URL}/api/v2/search"
DATASET_URL = f"{BASE_URL}/api/v2/datasets"
OUTPUT_FILE = "dryad.csv"

RENAME = {
    "identifier": "id",
    "lastModificationDate": "updated",
    "abstract": "description",
    "storageSize": "total_file_size",
}


def _fetch_latest_version(session, dataset: dict, timeout=(5, 180)) -> Optional[dict]:
    """Return the latest published version of a dataset.

    Args:
        session: Session to use.
        dataset: A dataset record from the search response.
        timeout: ``(connect, read)`` timeout in seconds.

    Returns:
        The version record with the highest version number among the published
        ones, or None if the dataset is unpublished or inaccessible.
    """
    try:
        links = dataset.get("_links") or {}
        versions_link = (links.get("stash:versions") or {}).get("href")
        identifier = dataset.get("identifier")

        if versions_link:
            url = f"{BASE_URL}{versions_link}"
        elif identifier:
            url = f"{DATASET_URL}/{quote(identifier, safe='')}/versions"
        else:
            return None

        response = session.get(url, timeout=timeout)
        if response.status_code in (401, 403, 404):
            return None
        response.raise_for_status()

        versions = (response.json().get("_embedded") or {}).get("stash:versions") or []
        published = [
            v for v in versions
            if (v.get("curationStatus") or "").lower() in ("published", "embargoed")
            or (v.get("versionStatus") or "").lower() == "submitted"
        ]
        if not published:
            return None

        published.sort(key=lambda v: v.get("versionNumber", 0))
        return published[-1]
    except requests.RequestException as exc:
        print(f"   warning: version fetch failed: {exc}")
        return None


def _fetch_files(session, version: dict, timeout=(5, 180)) -> Optional[list[dict]]:
    """Return the file list of a dataset version, paging through the results.

    Args:
        session: Session to use.
        version: A version record.
        timeout: ``(connect, read)`` timeout in seconds.

    Returns:
        The file records, or None if the listing is inaccessible.
    """
    try:
        links = version.get("_links") or {}
        files_link = (links.get("stash:files") or {}).get("href")
        if not files_link:
            return None

        url = f"{BASE_URL}{files_link}"
        files: list[dict] = []
        page = 1

        while True:
            response = session.get(url, params={"page": page, "per_page": 100}, timeout=timeout)
            if response.status_code in (401, 403, 404):
                return None
            response.raise_for_status()

            payload = response.json()
            batch = (payload.get("_embedded") or {}).get("stash:files") or []
            files.extend(batch)

            if not batch or len(files) >= payload.get("total", 0):
                break
            page += 1

        return files
    except requests.RequestException as exc:
        print(f"   warning: file fetch failed: {exc}")
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
    """Search Dryad for a single query and return the matching datasets.

    Args:
        query: A single search query.
        per_page: Page size.
        max_records: Stop after this many records, or None for all of them.
        delay_s: Pause between pages.
        timeout: ``(connect, read)`` timeout in seconds.
        session: Reusable session; one is created if omitted.
        published_after: Lower bound on the version publication date.

    Returns:
        A table in the common output schema.
    """
    session = session or make_session(token_env="DRYAD_TOKEN")
    cutoff = pd.to_datetime(published_after, utc=True) if published_after else None

    hits: list[dict] = []
    page = 1

    while True:
        try:
            response = session.get(
                SEARCH_URL,
                params={"q": query, "page": page, "per_page": per_page},
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            print(f"   skipping page {page}: {exc}")
            break

        payload = response.json()
        items = (payload.get("_embedded") or {}).get("stash:datasets") or []
        if not items:
            break

        hits.extend(items)

        if max_records is not None and len(hits) >= max_records:
            hits = hits[:max_records]
            break
        if len(hits) >= payload.get("total", 0):
            break

        page += 1
        if delay_s:
            time.sleep(delay_s)

    if not hits:
        return standardise(pd.DataFrame())

    df = pd.json_normalize(hits, sep=".")
    df["url"] = ""
    df["file_types"] = ""
    df["language"] = ""

    keep = []
    for i in tqdm(range(len(df)), desc="   fetching versions", unit="ds"):
        dataset = hits[i]
        identifier = dataset.get("identifier")
        if not identifier:
            continue

        df.loc[i, "url"] = f"{BASE_URL}/dataset/doi:{identifier.replace('doi:', '')}"

        version = _fetch_latest_version(session, dataset, timeout=timeout)
        if version is None:
            continue

        if cutoff is not None:
            published = version.get("publicationDate") or version.get("lastModificationDate")
            published_dt = pd.to_datetime(published, errors="coerce", utc=True)
            if pd.isna(published_dt) or published_dt < cutoff:
                continue

        files = _fetch_files(session, version, timeout=timeout)
        if files is None:
            continue

        # Dryad marks individual files as restricted or embargoed.
        if any((f.get("status") or "").lower() == "restricted" or f.get("embargoed") is True
               for f in files):
            continue

        summary = summarise_files(
            [f.get("path", "") for f in files],
            [f.get("size", 0) for f in files],
        )
        if not summary["has_relevant_file"]:
            continue

        df.loc[i, "file_types"] = summary["file_types"]
        keep.append(i)

        if delay_s:
            time.sleep(delay_s / 2)

    df = df.loc[keep].reset_index(drop=True)
    # Dryad exposes the DOI as the identifier; keep it in both columns.
    if "identifier" in df.columns:
        df["doi"] = df["identifier"].astype(str).str.replace("^doi:", "", regex=True)

    return standardise(df, RENAME)


def search_all(
    queries: Iterable[str],
    *,
    inter_query_delay: float = QUERY_DELAY_S,
) -> pd.DataFrame:
    """Run every query in turn and concatenate the results.

    Args:
        queries: The search queries to run.
        inter_query_delay: Pause between queries.

    Returns:
        The concatenated, unfiltered results in the common output schema.
    """
    queries = list(queries)
    session = make_session(token_env="DRYAD_TOKEN")
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
    """Run the full Dryad harvest and write ``dryad.csv``."""
    queries = [f"{platform} {term}" for platform in PLATFORMS for term in TEXT_TERMS]
    df = search_all(queries)
    df = finalise(df, deduplicate_by="id")
    write_output(df, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
