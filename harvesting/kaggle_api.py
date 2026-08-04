"""Harvest social-media text datasets from Kaggle.

Queries the Kaggle API (``/dataset_list``) for datasets mentioning a social-media
platform together with a content term. Kaggle's search does not support Boolean
operators, so the Cartesian product of platform and content terms is issued
(644 queries).

Results are sorted by update date, which lets pagination stop as soon as datasets
older than ``MIN_CREATED_DATE`` appear. Because Kaggle's search matches loosely,
a client-side keyword filter is applied afterwards. Each surviving dataset's file
list is fetched to drop those without a relevant data file. Kaggle publishes no
language metadata, so English filtering relies entirely on fastText. Results are
written to ``kaggle.csv``.

Authentication follows the usual Kaggle mechanism: either ``~/.kaggle/kaggle.json``
or the ``KAGGLE_USERNAME`` and ``KAGGLE_KEY`` environment variables.

Note that Kaggle uses "subtitle" for what the other repositories call a
description; it is mapped accordingly and is noticeably shorter than elsewhere.
"""

# Make the shared modules in the parent directory importable when this script is
# executed directly from within ``harvesting/``.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
import random
import re
import time
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Optional

import pandas as pd
from kaggle.api.kaggle_api_extended import KaggleApi
from requests.exceptions import HTTPError

from harvest_common import (
    QUERY_DELAY_S,
    REQUEST_DELAY_S,
    finalise,
    standardise,
    write_output,
)
from query_specs import MIN_CREATED_DATE, PLATFORMS, TEXT_TERMS, is_relevant_file

OUTPUT_FILE = "kaggle.csv"
RETRY_STATUSES = (429, 500, 502, 503, 504)

RENAME = {
    "subtitle": "description",
    "lastupdated": "updated",
    "totalbytes": "total_file_size",
}


def _setup_logger(name: str = "kaggle_search", level: int = logging.INFO) -> logging.Logger:
    """Create a console logger, avoiding duplicate handlers on re-import.

    Args:
        name: Logger name.
        level: Logging level; ``logging.DEBUG`` for per-record output.

    Returns:
        The configured logger.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if logger.handlers:
        return logger

    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(funcName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(handler)
    return logger


log = _setup_logger()


def _retry_delay(attempt: int, base_delay: float, backoff_factor: float,
                 max_delay: float, retry_after_header: Optional[str]) -> float:
    """Compute how long to wait before the next attempt.

    A ``Retry-After`` header wins if present and parseable; otherwise the delay
    grows exponentially, with jitter to avoid synchronised retries.

    Args:
        attempt: Zero-based attempt counter.
        base_delay: Delay for the first retry.
        backoff_factor: Multiplier per attempt.
        max_delay: Upper bound on the delay.
        retry_after_header: Raw ``Retry-After`` header, if any.

    Returns:
        Seconds to sleep.
    """
    if retry_after_header:
        try:
            return min(float(retry_after_header), max_delay)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after_header)
                seconds = max(0.0, (parsed - pd.Timestamp.utcnow().to_pydatetime()).total_seconds())
                return min(seconds, max_delay)
            except (TypeError, ValueError):
                log.debug("Could not parse Retry-After header: %r", retry_after_header)

    delay = min(max_delay, base_delay * (backoff_factor ** attempt))
    return delay + random.uniform(0, min(1.0, delay * 0.25))


def _call_with_retry(func, *args, description: str, max_retries: int = 6,
                     base_delay: float = 1.0, backoff_factor: float = 2.0,
                     max_delay: float = 60.0, **kwargs):
    """Call a Kaggle client method, retrying on throttling and transient errors.

    Args:
        func: The bound client method to call.
        *args: Positional arguments for ``func``.
        description: What is being fetched, used in log messages.
        max_retries: Attempts before giving up.
        base_delay: Delay for the first retry.
        backoff_factor: Multiplier per attempt.
        max_delay: Upper bound on the delay.
        **kwargs: Keyword arguments for ``func``.

    Returns:
        Whatever ``func`` returns.

    Raises:
        Exception: The last error, once the retries are exhausted.
    """
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except HTTPError as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if status in RETRY_STATUSES and attempt < max_retries:
                retry_after = response.headers.get("Retry-After") if response is not None else None
                sleep_s = _retry_delay(attempt, base_delay, backoff_factor, max_delay, retry_after)
                log.warning("HTTP %s for %s (attempt %d/%d), waiting %.1fs",
                            status, description, attempt + 1, max_retries, sleep_s)
                time.sleep(sleep_s)
                attempt += 1
                continue
            log.error("Unrecoverable HTTP error for %s (status %s): %s", description, status, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - the Kaggle client raises broadly
            if attempt < max_retries:
                sleep_s = _retry_delay(attempt, base_delay, backoff_factor, max_delay, None)
                log.warning("Unexpected error for %s (attempt %d/%d): %s, waiting %.1fs",
                            description, attempt + 1, max_retries, exc, sleep_s)
                time.sleep(sleep_s)
                attempt += 1
                continue
            log.exception("%s not retrievable after %d attempts.", description, max_retries)
            raise


def _get_last_updated(record) -> Any:
    """Read the last-update timestamp from a Kaggle result object.

    The client exposes this inconsistently across versions, so several spellings
    are tried, including the private attributes.

    Args:
        record: A Kaggle dataset object.

    Returns:
        The timestamp, or None if no variant is present.
    """
    for attribute in ("lastUpdated", "last_updated"):
        value = getattr(record, attribute, None)
        if value is not None:
            return value

    attributes = getattr(record, "__dict__", {}) or {}
    for key in ("_lastUpdated", "lastUpdated", "_last_updated", "last_updated"):
        if attributes.get(key) is not None:
            return attributes[key]
    return None


def _fetch_files(api: KaggleApi, dataset_ref: str) -> list:
    """Return the file list of a Kaggle dataset.

    Args:
        api: An authenticated Kaggle client.
        dataset_ref: Dataset reference in ``owner/name`` form.

    Returns:
        The file objects reported by the API.
    """
    return _call_with_retry(
        api.dataset_list_files, dataset_ref,
        description=f"file list of {dataset_ref}",
    ).files


def _keyword_filter(df: pd.DataFrame, column: str = "description") -> pd.DataFrame:
    """Drop rows that mention none of the search terms.

    Kaggle's search matches loosely, so the query terms are re-checked against
    the text client-side.

    Args:
        df: The table to filter.
        column: Text column to search.

    Returns:
        The filtered table.
    """
    if df.empty:
        return df

    pattern = re.compile("|".join(map(re.escape, PLATFORMS + TEXT_TERMS)), re.IGNORECASE)
    text = df[column].fillna("").astype(str)
    return df[text.str.len().gt(0) & text.str.contains(pattern, na=False)].reset_index(drop=True)


def search_records(
    query: str,
    *,
    api: Optional[KaggleApi] = None,
    sort_by: str = "updated",
    max_pages: int = 1000,
    delay_s: float = REQUEST_DELAY_S,
    min_created_date: str = MIN_CREATED_DATE,
    check_files: bool = True,
) -> pd.DataFrame:
    """Search Kaggle for a single query and return the matching datasets.

    Args:
        query: A single search query.
        api: An authenticated client; one is created if omitted.
        sort_by: Sort order. Early pagination stop requires ``"updated"``.
        max_pages: Page limit.
        delay_s: Pause between pages.
        min_created_date: Lower bound on the last-update date.
        check_files: Fetch each dataset's file list and drop those without a
            relevant data file. Slow, but it is the only reliable filter Kaggle
            offers.

    Returns:
        A table in the common output schema.
    """
    if api is None:
        api = KaggleApi()
        api.authenticate()

    min_date = pd.to_datetime(min_created_date, utc=True) if min_created_date else None
    if min_date is not None and sort_by != "updated":
        log.warning("Early pagination stop needs sort_by='updated' (got %r).", sort_by)
        min_date = None

    rows: list[dict] = []
    page = 1

    while page <= max_pages:
        results = _call_with_retry(
            api.dataset_list,
            description=f"page {page} of {query!r}",
            search=query, sort_by=sort_by, page=page,
        )
        if not results:
            break

        stop = False
        for record in results:
            # Results are sorted newest first, so the first stale record ends it.
            if min_date is not None:
                updated = pd.to_datetime(_get_last_updated(record), errors="coerce", utc=True)
                if pd.notna(updated) and updated < min_date:
                    stop = True
                    break
            rows.append(getattr(record, "__dict__", {}) or {})

        if stop:
            break

        page += 1
        if delay_s:
            time.sleep(delay_s)

    if not rows:
        return standardise(pd.DataFrame())

    df = pd.json_normalize(rows, sep=".")
    df.columns = df.columns.str.replace("_", "", regex=False)

    if "title" not in df.columns and "ref" in df.columns:
        df["title"] = df["ref"].str.split("/", n=1).str[1]

    df = df.rename(columns=RENAME)
    df = _keyword_filter(df, "description")

    if check_files and not df.empty:
        keep = []
        for i, ref in enumerate(df.get("ref", pd.Series(dtype=str)).tolist()):
            if not isinstance(ref, str) or "/" not in ref:
                log.warning("Skipping invalid ref %r (expected 'owner/name').", ref)
                continue
            try:
                files = _fetch_files(api, ref)
                if any(is_relevant_file(f.name.lower()) for f in files):
                    df.loc[df.index[i], "file_types"] = " ".join(sorted({
                        f.name.rsplit(".", 1)[-1].lower() for f in files if "." in f.name
                    }))
                    keep.append(df.index[i])
            except Exception:  # noqa: BLE001 - a single unreadable dataset is not fatal
                log.warning("Skipping %s: file list could not be fetched.", ref)

            if delay_s:
                time.sleep(delay_s)

        df = df.loc[keep].reset_index(drop=True)

    return standardise(df)


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
    api = KaggleApi()
    api.authenticate()
    frames = []

    for i, query in enumerate(queries):
        df = search_records(query, api=api, delay_s=delay_s)
        print(f"[{i + 1}/{len(queries)}] {query[:60]}... -> {len(df)} records")
        if not df.empty:
            frames.append(df)

        if inter_query_delay and i < len(queries) - 1:
            time.sleep(inter_query_delay)

    if not frames:
        return standardise(pd.DataFrame())
    return pd.concat(frames, ignore_index=True)


def main() -> int:
    """Run the full Kaggle harvest and write ``kaggle.csv``."""
    queries = [f"{platform} {term}" for platform in PLATFORMS for term in TEXT_TERMS]
    df = search_all(queries)
    df = finalise(df, deduplicate_by="id")
    write_output(df, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
