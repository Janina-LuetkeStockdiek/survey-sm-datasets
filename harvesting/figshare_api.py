"""Harvest social-media text datasets from Figshare.

Queries the Figshare search API (``/articles/search``) for articles of type
dataset or fileset whose title or description mentions a social-media platform
together with a content term. Figshare's search does not accept the Boolean form
used elsewhere, so the Cartesian product of platform and content terms is issued
(644 queries).

Each hit is then fetched individually via ``/articles/{id}``, because the search
response carries neither the description, the file list nor the embargo flags.
Embargoed, confidential and inaccessible articles are dropped, as are articles
without a relevant data file. Results are language-filtered to English (Figshare's
own language metadata plus a fastText fallback) and written to ``figshare.csv``.

An optional ``FIGSHARE_TOKEN`` (read from the environment) raises the rate limit.
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
    QUERY_DELAY_S,
    REQUEST_DELAY_S,
    finalise,
    make_session,
    standardise,
    summarise_files,
    write_output,
)
from query_specs import MIN_CREATED_DATE, PLATFORMS, TEXT_TERMS

SEARCH_URL = "https://api.figshare.com/v2/articles/search"
ARTICLE_URL = "https://api.figshare.com/v2/articles"
OUTPUT_FILE = "figshare.csv"

# Figshare item types: 3 = dataset, 4 = fileset.
ITEM_TYPES = [3, 4]

# Figshare refuses to page beyond roughly 9,000 records per item type.
DEEP_PAGING_LIMIT = 9000

RENAME = {
    "modified_date": "updated",
    "url_public_html": "url",
}


def _make_figshare_session(token: Optional[str] = None) -> requests.Session:
    """Build a session for Figshare, which expects ``Authorization: token ...``.

    Args:
        token: API token; read from ``FIGSHARE_TOKEN`` if omitted.

    Returns:
        A configured session.
    """
    session = make_session(headers={"Content-Type": "application/json"})
    token = token or os.getenv("FIGSHARE_TOKEN")
    if token:
        session.headers["Authorization"] = f"token {token}"
    return session


def _fetch_article(session, article_id: int, timeout=(5, 180)) -> Optional[dict]:
    """Fetch one article's full metadata, backing off on rate limits.

    Args:
        session: Session to use.
        article_id: Figshare article ID.
        timeout: ``(connect, read)`` timeout in seconds.

    Returns:
        The article record, or None if it is inaccessible or the retries ran out.
    """
    for attempt in range(1, 6):
        try:
            response = session.get(f"{ARTICLE_URL}/{article_id}", timeout=timeout)
            if response.status_code in (403, 429):
                wait = min(30 * attempt, 180)
                print(f"   rate limit on article {article_id}, waiting {wait}s")
                time.sleep(wait)
                continue
            if response.status_code in (401, 404):
                return None
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            print(f"   warning: article {article_id}: {exc}")
            return None
    return None


def _field_or(terms: str, fields=("title", "description")) -> str:
    """Build an OR group over several fields for one term group.

    Args:
        terms: The term or parenthesised term group to search for.
        fields: Metadata fields to search.

    Returns:
        A Figshare query fragment, e.g. ``(:title: Reddit OR :description: Reddit)``.
    """
    return "(" + " OR ".join(f":{field}: {terms}" for field in fields) + ")"


def build_query(platform_term: str, content_term: str) -> str:
    """Build one Figshare query from a platform and a content term.

    Args:
        platform_term: A single platform search term.
        content_term: A single content search term.

    Returns:
        A query requiring both terms in the title or description.
    """
    return f"{_field_or(platform_term)} AND {_field_or(content_term)}"


def search_records(
    query: str,
    *,
    page_size: int = 100,
    max_records: Optional[int] = None,
    delay_s: float = REQUEST_DELAY_S,
    timeout: tuple = (5, 180),
    session=None,
    min_created_date: str = MIN_CREATED_DATE,
) -> pd.DataFrame:
    """Search Figshare for a single query and return the matching articles.

    Args:
        query: A single Figshare query.
        page_size: Page size, capped at 1,000 by the API.
        max_records: Stop after this many records, or None for all of them.
        delay_s: Pause between pages.
        timeout: ``(connect, read)`` timeout in seconds.
        session: Reusable session; one is created if omitted.
        min_created_date: Lower bound on the publication date.

    Returns:
        A table in the common output schema.
    """
    session = session or _make_figshare_session()
    page_size = min(page_size, 1000)
    hits: list[dict] = []

    for item_type in ITEM_TYPES:
        page = 1
        while True:
            body = {
                "search_for": query,
                "item_type": item_type,
                "page": page,
                "page_size": page_size,
                "order": "published_date",
                "order_direction": "desc",
            }
            if min_created_date:
                body["published_since"] = min_created_date

            response = None
            for attempt in range(1, 6):
                response = session.post(SEARCH_URL, json=body, timeout=timeout)
                if response.status_code in (403, 429):
                    wait = min(60 * attempt, 300)
                    print(f"   rate limit on page {page}, type {item_type}, waiting {wait}s")
                    time.sleep(wait)
                    continue
                break

            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                print(f"   skipping page {page}, type {item_type}: {exc}")
                break

            items = response.json() or []
            if not items:
                break

            hits.extend(items)

            if max_records is not None and len(hits) >= max_records:
                hits = hits[:max_records]
                break
            if len(items) < page_size:
                break
            if page * page_size >= DEEP_PAGING_LIMIT:
                print(f"   deep-paging limit reached for type {item_type}")
                break

            page += 1
            if delay_s:
                time.sleep(delay_s)

        if max_records is not None and len(hits) >= max_records:
            break

    if not hits:
        return standardise(pd.DataFrame())

    df = pd.json_normalize(hits, sep=".")
    df["description"] = ""
    df["language"] = ""
    df["total_file_size"] = 0
    df["file_types"] = ""

    # The search response carries no description, file list or embargo flags, so
    # each article has to be fetched individually.
    keep = []
    for i in tqdm(range(len(df)), desc="   fetching articles", unit="art"):
        article_id = df.loc[i, "id"] if "id" in df.columns else None
        if pd.isna(article_id):
            continue

        article = _fetch_article(session, int(article_id), timeout=timeout)
        if article is None:
            continue
        if article.get("is_embargoed") or article.get("is_confidential"):
            continue

        files = article.get("files") or []
        summary = summarise_files(
            [f.get("name", "") for f in files],
            [f.get("size", 0) for f in files],
        )
        if not summary["has_relevant_file"]:
            continue

        df.loc[i, "total_file_size"] = summary["total_file_size"]
        df.loc[i, "file_types"] = summary["file_types"]
        df.loc[i, "description"] = article.get("description") or ""
        df.loc[i, "language"] = article.get("language") or ""
        keep.append(i)

        if delay_s:
            time.sleep(delay_s / 2)

    df = df.loc[keep].reset_index(drop=True)
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
    session = _make_figshare_session()
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
    """Run the full Figshare harvest and write ``figshare.csv``."""
    queries = [build_query(platform, term)
               for platform in PLATFORMS for term in TEXT_TERMS]
    df = search_all(queries)
    df = finalise(df, deduplicate_by="id")
    write_output(df, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
