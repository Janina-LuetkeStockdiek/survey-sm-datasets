"""Harvest social-media text datasets from GitHub.

Searches the GitHub repository search API for repositories whose description
mentions a platform term, a content term and the word "dataset", created on or
after ``MIN_CREATED_DATE``. GitHub's search does not support the Boolean form the
other APIs accept, so the Cartesian product of platform and content terms is used
(644 queries). "dataset" is appended because GitHub offers no content-type filter
and the search would otherwise return mostly code repositories.

Each candidate repository's file tree is inspected, and only repositories holding
at least one relevant data file are kept. Results are language-filtered to
English via fastText on the description and written to ``github.csv``.

A ``GITHUB_TOKEN`` (read from the environment) is optional but strongly
recommended: it raises the search rate limit from 10 to 30 requests per minute.
"""

# Make the shared modules in the parent directory importable when this script is
# executed directly from within ``harvesting/``.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import time
from typing import Iterable

import pandas as pd

from harvest_common import (
    QUERY_DELAY_S,
    build_queries,
    finalise,
    make_session,
    standardise,
    write_output,
)
from query_specs import MIN_CREATED_DATE, PLATFORMS, TEXT_TERMS, is_relevant_file

SEARCH_URL = "https://api.github.com/search/repositories"
TREE_URL = "https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}"
OUTPUT_FILE = "github.csv"

# GitHub's search API caps a result set at 1,000 items, i.e. 10 pages of 100.
MAX_PAGES = 10

API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2026-03-10",
}

# GitHub repository field -> common schema name. Note that the repository's own
# "language" field is the *programming* language and is deliberately not mapped:
# the natural language is determined by fastText in the finalise step.
RENAME = {
    "html_url": "url",
    "name": "title",
    "updated_at": "updated",
}

# A repository tree is fetched at most once, even when several queries match it.
_tree_cache: dict[str, set[str]] = {}


def _handle_rate_limit(response) -> bool:
    """Sleep until the rate-limit window resets.

    Args:
        response: The response to inspect.

    Returns:
        True if the caller should retry the request, False otherwise.
    """
    if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
        reset_ts = int(response.headers.get("X-RateLimit-Reset", "0"))
        sleep_s = max(1, reset_ts - int(time.time()) + 1)
        print(f"   rate limit reached, waiting {sleep_s}s", file=sys.stderr)
        time.sleep(sleep_s)
        return True
    return False


def _fetch_files(session, owner: str, repo: str, branch: str) -> set[str]:
    """Return the relevant data-file extensions found in a repository tree.

    Args:
        session: Session to use.
        owner: Repository owner.
        repo: Repository name.
        branch: Branch to inspect, usually the default branch.

    Returns:
        The set of matching extensions; empty if the repository holds no relevant
        data file or could not be read.
    """
    cache_key = f"{owner}/{repo}"
    if cache_key in _tree_cache:
        return _tree_cache[cache_key]

    url = TREE_URL.format(owner=owner, repo=repo, branch=branch)

    while True:
        response = session.get(url, params={"recursive": "1"}, timeout=30)
        if _handle_rate_limit(response):
            continue
        # Empty, deleted or access-restricted repositories are skipped quietly.
        if response.status_code in (404, 409, 451):
            _tree_cache[cache_key] = set()
            return set()
        response.raise_for_status()
        break

    extensions = set()
    for entry in response.json().get("tree", []):
        if entry.get("type") != "blob":
            continue
        path = entry.get("path", "")
        if is_relevant_file(path):
            ext = os.path.splitext(path)[1].lower().lstrip(".")
            if ext:
                extensions.add(ext)

    _tree_cache[cache_key] = extensions
    return extensions


def search_records(
    query: str,
    *,
    per_page: int = 100,
    max_pages: int = MAX_PAGES,
    created_after: str = MIN_CREATED_DATE,
    session=None,
) -> pd.DataFrame:
    """Search GitHub for a single query and return the matching repositories.

    Args:
        query: A single search query.
        per_page: Page size, capped at 100 by the API.
        max_pages: Page limit; the API refuses to page beyond 1,000 results.
        created_after: Lower bound on the repository creation date.
        session: Reusable session; one is created if omitted.

    Returns:
        A table in the common output schema.
    """
    session = session or make_session(token_env="GITHUB_TOKEN", headers=API_HEADERS)

    if created_after:
        query = f"{query} created:>={created_after}"

    items: list[dict] = []
    page = 1

    while page <= max_pages:
        params = {"q": query, "sort": "stars", "order": "desc",
                  "per_page": per_page, "page": page}
        response = session.get(SEARCH_URL, params=params, timeout=30)
        if _handle_rate_limit(response):
            continue
        response.raise_for_status()

        page_items = response.json().get("items", [])
        if not page_items:
            break

        for item in page_items:
            owner = item.get("owner", {}).get("login")
            name = item.get("name")
            if not (owner and name):
                continue
            extensions = _fetch_files(session, owner, name, item.get("default_branch") or "HEAD")
            if extensions:
                item["file_types"] = " ".join(sorted(extensions))
                items.append(item)

        if "next" not in response.links:
            break
        page += 1

    if not items:
        return standardise(pd.DataFrame())

    df = pd.json_normalize(items, sep=".")
    df = df.drop(columns=[c for c in ("language",) if c in df.columns])

    # GitHub reports repository size in kilobytes, not bytes.
    if "size" in df.columns:
        df["total_file_size"] = pd.to_numeric(df["size"], errors="coerce") * 1024

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
    session = make_session(token_env="GITHUB_TOKEN", headers=API_HEADERS)
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
    """Run the full GitHub harvest and write ``github.csv``."""
    # GitHub has no content-type filter, so "dataset" is appended to raise the
    # share of actual data repositories among the results.
    queries = [f"{q} dataset in:description"
               for q in build_queries(PLATFORMS, TEXT_TERMS, boolean=False)]
    df = search_all(queries)
    df = finalise(df, deduplicate_by="id")
    write_output(df, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
