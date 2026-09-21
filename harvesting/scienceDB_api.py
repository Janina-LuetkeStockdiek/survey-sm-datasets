"""Harvest social-media text datasets from Science Data Bank (scidb.cn).

Queries the Science Data Bank query service for each platform, keeping only
public datasets not mirrored from Zenodo/Figshare/OSF, created on or after
``MIN_CREATED_DATE`` and above a minimum size. Because the query API does not
support boolean operators, the "platform AND content term" condition is enforced
client-side in ``_relevance_filter``. Results are language-filtered to English
(API field plus fastText fallback) and written to ``science_db.csv``.

The search endpoint occasionally returns 500 errors; the pager degrades the
request (dropping the publishDate filter, then shrinking the page size) before
giving up on a query.
"""


# Make the shared modules in the parent directory (config.py, query_specs.py)
# importable when this script is executed directly from within ``harvesting/``.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import re
import time
from typing import Iterable, Optional

import pandas as pd
import requests
from tqdm import tqdm

from config import data_path
from harvest_common import (
    QUERY_DELAY_S,
    finalise,
    make_session,
    standardise,
    write_output,
)
from query_specs import MIN_CREATED_DATE, PLATFORMS, TEXT_TERMS

SCIENCEDB_BASE = "https://www.scidb.cn"
SEARCH_API = f"{SCIENCEDB_BASE}/api/sdb-query-service/query"
OUTPUT_FILE = "science_db.csv"
CHECKPOINT_FILE = "science_db_checkpoint.csv"
_MIN_CREATED_TS = pd.to_datetime(MIN_CREATED_DATE, errors="coerce", utc=True)
_MIN_CREATED_API = (
    _MIN_CREATED_TS.strftime("%Y-%m-%d") if pd.notna(_MIN_CREATED_TS) else ""
)
# Range end: the server rejects an empty "" end date (-> 500),
# so fill it with today's date.
_TODAY_API = pd.Timestamp.utcnow().strftime("%Y-%m-%d")
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: Optional[str]) -> str:
    """Remove HTML tags from a string."""
    if not text:
        return ""
    return _TAG_RE.sub("", text).strip()



def search_records(
        query: str,
        per_page: int = 50,
        max_records: Optional[int] = None,
        delay_s: float = 0.5,
        timeout: tuple = (5, 180),
        session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    """Search Science Data Bank for one query and return a filtered DataFrame.

    Degrades the request on persistent 500 errors (drop publishDate filter, then
    shrink the page size) and keeps only public, recent, non-mirrored datasets.
    """
    s = session or make_session()
    all_hits: list[dict] = []
    page = 1
    pbar = None
    total = None

    # Effective parameters – trimmed down on persistent 500 on page 1.
    eff_size = per_page
    use_publish_date = bool(_MIN_CREATED_API)
    downgrade_step = 0  # 0 = normal, 1 = without publishDate, 2 = additionally size=10

    while True:
        body = {
            "fileType": [],
            "dataSetStatus": [],
            "copyrightCode": [],
            "publishDate": [_MIN_CREATED_API, _TODAY_API] if use_publish_date else [],
            # ─────────────────────────────────────────────────────────────────
            "ordernum": "6",  # 6 = by date
            "rorId": [],
            "ror": "",
            "taxonomyEn": [],
            "journalNameEn": [],
            "page": page,
            "size": eff_size,
        }
        params = {"queryCode": "", "q": query}

        try:
            resp = s.post(SEARCH_API, params=params, json=body, timeout=timeout)
            resp.raise_for_status()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            # Log the server message – it usually reveals the real cause.
            body_txt = ""
            if e.response is not None:
                body_txt = (e.response.text or "").strip().replace("\n", " ")[:300]
            print(f"  [http {status}] page={page} step={downgrade_step} body={body_txt!r}")

            # No hits yet -> it is the first (or an early) page.
            # Further pages would fail the same way with this query, so we first
            # try to trim the request down.
            if not all_hits:
                if downgrade_step == 0 and use_publish_date:
                    downgrade_step = 1
                    use_publish_date = False
                    print("  [downgrade] removing publishDate filter and retrying")
                    if delay_s:
                        time.sleep(delay_s * 2)
                    continue  # same page again
                if downgrade_step <= 1 and eff_size > 10:
                    downgrade_step = 2
                    eff_size = 10
                    print(f"  [downgrade] reducing size to {eff_size} and retrying")
                    if delay_s:
                        time.sleep(delay_s * 2)
                    continue  # same page again
                # All trim-down variants exhausted -> give up on the query.
                print("  [abort query] page 1 keeps failing – skipping query")
                break

            # We already had hits -> transient error mid-pagination:
            # skip the page and continue.
            print("  [skip page] skipping and moving on")
            page += 1
            if delay_s:
                time.sleep(delay_s * 2)  # brief breather
            if page > 200:
                print("  [abort] too many pages / persistent errors")
                break
            continue
        except Exception as e:
            print(f"  [skip] error on page={page}: {e}")
            break

        payload = resp.json()
        if payload.get("code") not in (20000, 0):
            print(f"  [skip] API code {payload.get('code')}: {payload.get('messageEn')}")
            break

        data = payload.get("data", {}) or {}
        items = data.get("data") or []

        if total is None:
            total = data.get("total") or 0
            # warning if more hits exist than are being loaded
            if max_records is not None and total and total > max_records:
                print(
                    f"  [WARN] Query '{query}' has {total} hits, "
                    f"but only {max_records} are being loaded "
                    f"({total - max_records} discarded)! "
                    f"-> raise max_records or set it to None to load everything."
                )

        if pbar is None:
            target = min(total, max_records) if (total and max_records) else (total or None)
            pbar = tqdm(total=target, desc=f"  search '{query[:40]}'", unit="rec")

        if not items:
            break

        all_hits.extend(items)
        pbar.update(len(items))

        if max_records is not None and len(all_hits) >= max_records:
            all_hits = all_hits[:max_records]
            break

        if total and len(all_hits) >= total:
            break

        # safety net: fewer than requested -> last page
        if len(items) < per_page:
            break

        page += 1
        if delay_s:
            time.sleep(delay_s)

    if pbar:
        pbar.close()

    print(f"  -> {len(all_hits)} datasets retrieved from search")

    if not all_hits:
        return pd.DataFrame()

    rows = []
    for it in all_hits:

        other_platforms = ["zenodo", "figshare", "osf", "dryad"]
        if any(p.lower() in it.get("doi").lower() for p in other_platforms):
            continue

        if it.get("shareStatus") != "PUBLIC":
            continue

        # ── only datasets after MIN_CREATED_DATE ─────────────────────────────
        if _MIN_CREATED_TS is not None:
            created_raw = (
                    it.get("dataSetPublishDate")
                    or it.get("createTime")
                    or it.get("publishDate")
            )
            created_ts = pd.to_datetime(created_raw, errors="coerce", utc=True)
            if pd.isna(created_ts) or created_ts < _MIN_CREATED_TS:
                continue
        # ─────────────────────────────────────────────────────────────────────

        size_bytes = it.get("size") or 0
        if size_bytes == 0:
            continue

        ds_id = it.get("dataSetId") or it.get("id") or ""
        title = _strip_html(it.get("titleEn") or it.get("titleZh"))
        desc = _strip_html(
            it.get("description")
            or it.get("descriptionEn")
            or it.get("introductionEn")
            or it.get("introduction")
            or ""
        )
        doi = (
                it.get("doi")
                or it.get("identifier")
                or (it.get("copyRight", {}) or {}).get("doi")
                or ""
        )

        lang = it.get("language") or ""

        rows.append({
            "id": ds_id,
            "doi": doi,
            "url": f"{SCIENCEDB_BASE}/en/detail?dataSetId={ds_id}" if ds_id else "",
            "updated": it.get("dataSetPublishDate"),
            "title": title,
            "description": desc,
            "language": lang,
            "total_file_size": size_bytes,
            "file_types": " ".join(sorted(set(it.get("fileType") or []))),
        })

    return standardise(pd.DataFrame(rows))


def search_all(
        queries: Iterable[str],
        *,
        per_page: int = 50,
        delay_seconds: float = 0.5,
        inter_query_delay: float = 2.0,
        deduplicate_by: Optional[str] = "id",
) -> pd.DataFrame:
    """Run several queries, concatenate the results and deduplicate them."""
    queries = list(queries)
    frames: list[pd.DataFrame] = []
    session = make_session()

    print(f"\n=== Starting {len(queries)} queries ===\n")

    for i, q in enumerate(tqdm(queries, desc="Queries", unit="query"), start=1):
        print(f"\n[{i}/{len(queries)}] Query: {q}")
        df_q = search_records(
            query=q,
            per_page=per_page,
            max_records=None,
            delay_s=delay_seconds,
            session=session,
        )
        if not df_q.empty:
            frames.append(df_q)

        running_total = sum(len(f) for f in frames)
        print(f"  [running total: {running_total} datasets across {len(frames)} queries]")

        if inter_query_delay and i < len(queries):
            time.sleep(inter_query_delay)

    if not frames:
        print("\n No results at all!")
        return pd.DataFrame()

    df_all = pd.concat(frames, ignore_index=True)
    before = len(df_all)

    dedup_key = deduplicate_by
    if dedup_key == "doi" and ("doi" not in df_all.columns or df_all["doi"].eq("").all()):
        dedup_key = "id"
    if dedup_key and dedup_key in df_all.columns:
        df_all = df_all.drop_duplicates(subset=dedup_key, keep="last")
        print(f"\n Deduplication ({dedup_key}): {before} -> {len(df_all)} "
              f"({before - len(df_all)} duplicates removed)")

    if "updated" in df_all.columns:
        df_all["updated"] = pd.to_datetime(df_all["updated"], errors="coerce", utc=True)
        df_all = df_all.sort_values(by="updated")

    df_all = df_all.drop_duplicates(subset="description", keep="last")

    df_all.reset_index(drop=True, inplace=True)
    print(f"\n=== Done: {len(df_all)} unique datasets ===\n")
    return df_all


def _relevance_filter(df: pd.DataFrame, platform_term: str,
                      content_terms: Iterable[str]) -> pd.DataFrame:
    """Enforce the "platform AND content term" condition client-side.

    The ScienceDB query API does not support Boolean operators: a string such as
    ``"Twitter AND (post OR ...)"`` is treated as a loose OR search over all
    words and returns a very large number of irrelevant hits. Each query
    therefore searches for the platform term alone — every dataset that could
    pass this filter mentions the platform — and the conjunction is applied here.

    Args:
        df: A table in the common output schema.
        platform_term: The platform term the query searched for.
        content_terms: The content terms, of which at least one must occur.

    Returns:
        The filtered table.
    """
    if df.empty:
        return df

    haystack = (df["title"].fillna("") + " " + df["description"].fillna("")).str.lower()
    platform_pattern = re.escape(platform_term.lower())
    content_pattern = "|".join(re.escape(t.lower()) for t in content_terms)

    mask = (haystack.str.contains(platform_pattern, regex=True)
            & haystack.str.contains(content_pattern, regex=True))
    return df[mask].reset_index(drop=True)


def main() -> int:
    """Run the full Science Data Bank harvest and write ``science_db.csv``."""
    session = make_session()
    checkpoint = data_path(CHECKPOINT_FILE)
    frames = []

    for i, platform in enumerate(PLATFORMS):
        df = search_records(query=platform, per_page=50, delay_s=1.0, session=session)
        df = _relevance_filter(df, platform, TEXT_TERMS)
        print(f"[{i + 1}/{len(PLATFORMS)}] {platform} -> {len(df)} records")

        if not df.empty:
            frames.append(df)
            # ScienceDB aborts long runs with Elasticsearch errors, so progress
            # is checkpointed after every platform.
            pd.concat(frames, ignore_index=True).to_csv(
                checkpoint, index=False, sep=";", encoding="utf-8-sig"
            )

        if i < len(PLATFORMS) - 1:
            time.sleep(QUERY_DELAY_S)

    df_all = pd.concat(frames, ignore_index=True) if frames else standardise(pd.DataFrame())
    df_all = finalise(df_all, deduplicate_by="id")
    write_output(df_all, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
