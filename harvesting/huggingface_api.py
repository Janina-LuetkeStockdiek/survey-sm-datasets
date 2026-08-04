"""Harvest social-media text datasets from Hugging Face.

Queries the Hub API (``/api/datasets``) for datasets mentioning a social-media
platform. The Hub's free-text search does not support Boolean operators, so one
query per platform is issued (23 in total) and the content terms are applied as a
client-side filter on title and description afterwards.

Results are sorted by creation date, which lets pagination stop as soon as
datasets older than ``MIN_CREATED_DATE`` appear. Gated, private and disabled
repositories are dropped, as are repositories without a relevant data file, which
is determined from the file listing returned by ``tree/main``. English filtering
uses the ``language`` field of the dataset card where present and falls back to
fastText. Results are written to ``huggingface.csv``.

An optional ``HF_TOKEN`` (read from the environment) raises the rate limit. A
checkpoint file is written after every query so an interrupted harvest can be
resumed cheaply.
"""

# Make the shared modules in the parent directory importable when this script is
# executed directly from within ``harvesting/``.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import re
import time
from typing import Iterable, Optional

import pandas as pd
import requests
from huggingface_hub import HfApi
from huggingface_hub.utils import GatedRepoError, HfHubHTTPError, RepositoryNotFoundError
from tqdm import tqdm

from config import data_path
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

BASE_URL = "https://huggingface.co"
SEARCH_URL = f"{BASE_URL}/api/datasets"
OUTPUT_FILE = "huggingface.csv"
CHECKPOINT_FILE = "huggingface_checkpoint.csv"

MIN_CREATED_TS = pd.to_datetime(MIN_CREATED_DATE, utc=True)

RENAME = {
    "lastModified": "updated",
}


def _created_after_min(value) -> bool:
    """Return True if a creation timestamp is at or after ``MIN_CREATED_DATE``.

    Args:
        value: A timestamp string from the API.

    Returns:
        True if the value parses and is recent enough.
    """
    timestamp = pd.to_datetime(value, errors="coerce", utc=True)
    return bool(pd.notna(timestamp) and timestamp >= MIN_CREATED_TS)


def _parse_next_link(link_header: str) -> Optional[str]:
    """Extract the ``rel="next"`` URL from an HTTP Link header.

    Args:
        link_header: The raw Link header.

    Returns:
        The next page's URL, or None if there is none.
    """
    for part in (link_header or "").split(","):
        if 'rel="next"' in part:
            match = re.search(r"<([^>]+)>", part)
            if match:
                return match.group(1)
    return None


def _is_gated(value) -> bool:
    """Return True if a dataset's ``gated`` field indicates restricted access.

    The Hub reports this as either a boolean or a string such as ``"auto"``.

    Args:
        value: The raw ``gated`` value.

    Returns:
        True if access is gated.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "none", "nan")
    return False


def _respect_rate_limit(response, attempt: int) -> float:
    """Return how long to wait after a 429 response.

    Args:
        response: The response to inspect.
        attempt: Zero-based attempt counter, used for exponential backoff.

    Returns:
        Seconds to sleep; 0.0 if the response was not rate limited.
    """
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return min(60.0, 2.0 * (2 ** attempt))
    return 0.0


def _fetch_files(api: HfApi, repo_id: str, max_retries: int = 3) -> Optional[list[dict]]:
    """Return the file listing of a dataset repository.

    Args:
        api: An authenticated ``HfApi`` client.
        repo_id: Repository identifier, e.g. ``"owner/name"``.
        max_retries: Attempts before giving up on rate limits.

    Returns:
        A list of ``{"path", "size"}`` dicts, or None if the repository is gated,
        missing or otherwise unreadable.
    """
    for _attempt in range(max_retries):
        try:
            info = api.dataset_info(repo_id, files_metadata=True, timeout=60)
            files = []
            for sibling in info.siblings:
                size = sibling.size
                if size is None and sibling.lfs is not None:
                    size = sibling.lfs.get("size")
                files.append({"path": sibling.rfilename, "size": size or 0})
            return files
        except (GatedRepoError, RepositoryNotFoundError):
            return None
        except HfHubHTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (401, 403, 404):
                return None
            if status == 429:
                retry_after = exc.response.headers.get("Retry-After")
                wait = min(float(retry_after) if retry_after and retry_after.isdigit() else 10.0, 30.0)
                print(f"   rate limit on {repo_id}, waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            print(f"   warning: {repo_id}: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001 - the Hub client raises broadly
            print(f"   warning: {repo_id}: {exc}")
            return None
    return None


def _content_filter(df: pd.DataFrame, content_terms: Iterable[str]) -> pd.DataFrame:
    """Keep only rows whose title or description contains a content term.

    The Hub's search covers the platform term only, so the content half of the
    query is applied here instead.

    Args:
        df: Table with ``title`` and ``description`` columns.
        content_terms: The content terms to require.

    Returns:
        The filtered table.
    """
    content_terms = list(content_terms)
    if df.empty or not content_terms:
        return df

    pattern = r"\b(" + "|".join(map(re.escape, content_terms)) + r")\b"
    haystack = (
        df.get("title", "").fillna("").astype(str) + " "
        + df.get("description", "").fillna("").astype(str)
    )
    return df[haystack.str.contains(pattern, case=False, regex=True, na=False)].reset_index(drop=True)


def search_records(
    query: str,
    *,
    per_page: int = 100,
    max_records: Optional[int] = None,
    delay_s: float = REQUEST_DELAY_S,
    timeout: tuple = (5, 180),
    session=None,
    token: Optional[str] = None,
) -> pd.DataFrame:
    """Search the Hub for a single query and return the matching datasets.

    Args:
        query: A single platform search term.
        per_page: Page size.
        max_records: Stop after this many records, or None for all of them.
        delay_s: Pause between pages.
        timeout: ``(connect, read)`` timeout in seconds.
        session: Reusable session; one is created if omitted.
        token: Hub token; read from ``HF_TOKEN`` if omitted.

    Returns:
        A table in the common output schema.
    """
    session = session or make_session(token=token, token_env="HF_TOKEN")
    api = HfApi(token=token or os.getenv("HF_TOKEN"))

    hits: list[dict] = []
    url = SEARCH_URL
    params = {"search": query, "limit": per_page, "full": "true",
              "sort": "createdAt", "direction": -1}

    while url:
        response = None
        for attempt in range(8):
            try:
                response = session.get(url, params=params, timeout=timeout)
            except requests.RequestException as exc:
                wait = min(60.0, 2.0 * (2 ** attempt))
                print(f"   connection error, waiting {wait:.0f}s: {exc}")
                time.sleep(wait)
                continue

            wait = _respect_rate_limit(response, attempt)
            if wait > 0:
                print(f"   rate limit, waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            break

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            print(f"   skipping: {exc}")
            break

        items = response.json()
        if not items:
            break

        fresh = [item for item in items if _created_after_min(item.get("createdAt"))]
        hits.extend(fresh)

        # Results are sorted newest first, so the first stale page ends the run.
        if len(fresh) < len(items):
            break
        if max_records is not None and len(hits) >= max_records:
            hits = hits[:max_records]
            break

        url = _parse_next_link(response.headers.get("Link", ""))
        params = None

        if delay_s:
            time.sleep(delay_s)

    if not hits:
        return standardise(pd.DataFrame())

    df = pd.json_normalize(hits, sep=".")

    # Drop everything that is not publicly readable.
    public = pd.Series(True, index=df.index)
    if "gated" in df.columns:
        public &= ~df["gated"].apply(_is_gated)
    for flag in ("private", "disabled"):
        if flag in df.columns:
            public &= ~df[flag].fillna(False).astype(bool)
    df = df[public].reset_index(drop=True)

    df["total_file_size"] = 0
    df["file_types"] = ""

    keep = []
    for i in tqdm(range(len(df)), desc="   fetching file lists", unit="ds"):
        repo_id = df.loc[i, "id"] if "id" in df.columns else None
        if not repo_id:
            continue

        files = _fetch_files(api, repo_id)
        if files is None:
            continue

        summary = summarise_files(
            [f.get("path", "") for f in files],
            [f.get("size", 0) for f in files],
        )
        df.loc[i, "total_file_size"] = summary["total_file_size"]
        df.loc[i, "file_types"] = summary["file_types"]
        if summary["has_relevant_file"]:
            keep.append(i)

        if delay_s:
            time.sleep(delay_s / 2)

    df = df.loc[keep].reset_index(drop=True)
    if df.empty:
        return standardise(pd.DataFrame())

    # The Hub has no DOIs; the repo id doubles as identifier, title and URL.
    df["title"] = df["id"].astype(str).str.split("/").str[-1]
    df["url"] = BASE_URL + "/datasets/" + df["id"].astype(str)
    if "description" not in df.columns:
        df["description"] = ""

    if "cardData.language" in df.columns:
        df["language"] = df["cardData.language"].apply(
            lambda x: ", ".join(map(str, x)) if isinstance(x, list) else (x if isinstance(x, str) else "")
        )
    else:
        df["language"] = ""

    return standardise(df, RENAME)


def search_all(
    queries: Iterable[str],
    *,
    content_terms: Iterable[str] = TEXT_TERMS,
    inter_query_delay: float = QUERY_DELAY_S,
    checkpoint_path: Optional[str] = None,
) -> pd.DataFrame:
    """Run every query in turn, apply the content filter and concatenate.

    Args:
        queries: The platform search terms to run.
        content_terms: Content terms applied client-side after each query.
        inter_query_delay: Pause between queries.
        checkpoint_path: If given, the running result is written here after every
            query so an interrupted harvest can be resumed.

    Returns:
        The concatenated, unfiltered results in the common output schema.
    """
    queries = list(queries)
    session = make_session(token_env="HF_TOKEN")
    frames = []

    for i, query in enumerate(queries):
        df = search_records(query, session=session)
        if not df.empty:
            df = _content_filter(df, content_terms)
        print(f"[{i + 1}/{len(queries)}] {query[:60]}... -> {len(df)} records")

        if not df.empty:
            frames.append(df)
            if checkpoint_path:
                pd.concat(frames, ignore_index=True).to_csv(
                    checkpoint_path, index=False, sep=";", encoding="utf-8-sig"
                )

        if inter_query_delay and i < len(queries) - 1:
            time.sleep(inter_query_delay)

    if not frames:
        return standardise(pd.DataFrame())
    return pd.concat(frames, ignore_index=True)


def main() -> int:
    """Run the full Hugging Face harvest and write ``huggingface.csv``."""
    checkpoint = data_path(CHECKPOINT_FILE)
    if os.path.exists(checkpoint):
        os.remove(checkpoint)

    df = search_all(PLATFORMS, content_terms=TEXT_TERMS, checkpoint_path=checkpoint)
    df = finalise(df, deduplicate_by="id")
    write_output(df, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
