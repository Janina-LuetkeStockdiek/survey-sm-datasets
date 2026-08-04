"""Harvest social-media text datasets from the EU Open Data Portal.

Queries the data.europa.eu search hub for each platform + content term, keeps
public datasets with at least one distribution, newer than ``MIN_CREATED_DATE``,
and summarizes their distributions (size / formats) from the search response
(no extra request). Results are whole-word filtered on platform/content terms
(with an X-ray/X-Men style negative filter), language-filtered to English via
fastText and written to ``eu_opendata.csv``.
"""


# Make the shared modules in the parent directory (config.py, query_specs.py)
# importable when this script is executed directly from within ``harvesting/``.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Optional, Iterable, List
import re
import time
import pandas as pd
import requests
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
from datetime import datetime, timezone

EU_PORTAL_BASE = "https://data.europa.eu/api/hub/search"
SEARCH_API = f"{EU_PORTAL_BASE}/search"
OUTPUT_FILE = "eu_opendata.csv"

LANG_CODE_MAP = {
    "ENG": "en", "DEU": "de", "FRA": "fr", "ITA": "it", "SPA": "es",
    "POR": "pt", "NLD": "nl", "POL": "pl", "SWE": "sv", "DAN": "da",
    "FIN": "fi", "EST": "et", "LAV": "lv", "LIT": "lt", "HUN": "hu",
    "CES": "cs", "SLK": "sk", "SLV": "sl", "BUL": "bg", "RON": "ro",
    "HRV": "hr", "MLT": "mt", "ELL": "el", "GLE": "ga",
}



def _extract_multilang(field, preferred: str = "en") -> str:
    """
    Extract a string from a multilingual API field.

    Supported forms:
      • plain str
      • dict {lang_code: str | [str, ...]}
      • list [str, ...]
    Fallback: first non-empty value in any language.
    """
    if not field:
        return ""
    if isinstance(field, str):
        return field
    if isinstance(field, list):
        return str(field[0]) if field else ""
    if isinstance(field, dict):
        for lang in (preferred, "en", "de", "fr", "it", "es", "pt", "nl", "pl"):
            val = field.get(lang)
            if val:
                return val[0] if isinstance(val, list) else str(val)
        for v in field.values():  # absolute fallback
            if v:
                return v[0] if isinstance(v, list) else str(v)
    return ""


def find_newest_date(data):
    newest = None

    def parse(s):
        try:
            d = datetime.fromisoformat(s.replace('Z', '+00:00'))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d
        except (ValueError, AttributeError):
            return None

    def walk(obj):
        nonlocal newest
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, (list, tuple, set)):
            for v in obj:
                walk(v)
        elif isinstance(obj, str):
            d = parse(obj)
            if d and (newest is None or d > newest):
                newest = d

    walk(data)
    return newest


def _parse_language(lang_field) -> str:
    if not lang_field:
        return ""
    if isinstance(lang_field, str):
        lang_field = [lang_field]
    if isinstance(lang_field, dict):
        code = (lang_field.get("id") or lang_field.get("label") or "").upper()
        return LANG_CODE_MAP.get(code, code.lower()[:2])
    if isinstance(lang_field, list):
        for item in lang_field:
            if isinstance(item, str):
                code = item.rstrip("/").split("/")[-1].upper()
                return LANG_CODE_MAP.get(code, code.lower()[:2])
            if isinstance(item, dict):
                code = (item.get("id") or item.get("label") or "").upper()
                return LANG_CODE_MAP.get(code, code.lower()[:2])
    return ""


def _parse_distributions(distributions: list) -> dict:
    """
    Summarize distribution metadata:
    total size in bytes / MB and unique format labels.
    Corresponds to _fetch_dataset_files() from the Dataverse script –
    here without an extra request, since distributions are in the search response.
    """
    if not distributions:
        return {"total_file_size": 0, "file_types": ""}

    total_bytes = 0
    formats = set()

    for dist in distributions:
        if not isinstance(dist, dict):
            continue

        # file size (different spellings per API version)
        for size_key in ("file_size", "byte_size", "byteSize"):
            val = dist.get(size_key)
            if val is not None:
                try:
                    total_bytes += int(val)
                except (TypeError, ValueError):
                    pass
                break

        # Format label
        fmt = (
                dist.get("format")
                or dist.get("media_type")
                or dist.get("mediaType")
                or ""
        )
        if isinstance(fmt, dict):
            label = fmt.get("label") or fmt.get("id", "")
            if label:
                formats.add(str(label).strip().upper())
        elif isinstance(fmt, str) and fmt:
            ext = fmt.rstrip("/").split("/")[-1].split("+")[0].upper()
            formats.add(ext)

    return {
        "total_file_size": total_bytes,
        "file_types": " ".join(sorted(formats)),
    }


def _is_public(item: dict) -> bool:
    """Return True if the dataset is publicly accessible."""
    ar = item.get("access_rights") or item.get("accessRights") or {}
    if isinstance(ar, dict):
        ar = ar.get("id") or ar.get("label") or ""
    ar = str(ar).upper()
    return not any(t in ar for t in ("RESTRICT", "NON_PUBLIC", "NONPUBLIC"))


def search_records(
        query: str,
        per_page: int = 100,
        max_records: Optional[int] = None,
        delay_s: float = 0.5,
        timeout: tuple = (5, 180),
        session: Optional[requests.Session] = None,
        fetch_files: bool = True,
        min_date: Optional[datetime] = None,   # added
) -> pd.DataFrame:
    s = session or make_session()

    # bring min_date to UTC robustly so comparisons work
    if min_date is not None and min_date.tzinfo is None:
        min_date = min_date.replace(tzinfo=timezone.utc)

    all_items: List[dict] = []
    offset = 0
    pbar = None

    while True:
        params = {
            "q": query,
            "filter": "dataset",  # important: restrict to datasets
            "limit": per_page,
            "page": offset // per_page,  # 0,1,2,...
        }

        try:
            resp = s.get(SEARCH_API, params=params, timeout=timeout)
            resp.raise_for_status()
        except requests.HTTPError as e:
            print(f"  [HTTP {e.response.status_code}] {e.response.text[:300]}")
            raise
        except Exception as e:
            print(f"  [error] {e}")
            break

        payload = resp.json()

        # typical response: {"success": true, "result": {"count": N, "results": [...]}}
        data = payload.get("result") or payload
        items = data.get("results") or []
        total = int(data.get("count", 0))

        if pbar is None:
            target = min(total, max_records) if max_records else total
            pbar = tqdm(total=target, desc=f"  search '{query[:40]}...'", unit="rec")

        if not items:
            break

        all_items.extend(items)
        pbar.update(len(items))

        if max_records and len(all_items) >= max_records:
            all_items = all_items[:max_records]
            break
        if len(all_items) >= total:
            break

        offset += per_page
        if delay_s:
            time.sleep(delay_s)

    if pbar:
        pbar.close()

    print(f"  -> {len(all_items)} datasets retrieved from search")

    if not all_items:
        return pd.DataFrame()

    # ── build rows ───────────────────────────────────────────────────────────
    rows: List[dict] = []
    n_restricted = 0
    n_no_dist = 0
    n_too_old = 0  # added

    for item in tqdm(all_items, desc="  processing datasets", unit="ds"):
        distributions = item.get("distributions") or item.get("distribution") or []
        if not isinstance(distributions, list):
            distributions = []
        if len(distributions) < 1:
            n_no_dist += 1
            continue

        if not _is_public(item):
            n_restricted += 1
            continue

        # ── date filter ──────────────────────────────────────────────────────
        newest = find_newest_date(item)  # added
        if min_date is not None:
            if newest is None or newest < min_date:
                n_too_old += 1
                continue
        # ─────────────────────────────────────────────────────────────────────

        ds_id = item.get("id", "")
        uri = item.get("uri", "")

        row = {
            "id": ds_id,
            "doi": "",  # the EU Open Data Portal mints no DOIs
            "url": uri or (
                f"https://data.europa.eu/data/datasets/{ds_id}" if ds_id else ""
            ),
            "updated": str(newest),
            "title": _extract_multilang(item.get("title")),
            "description": _extract_multilang(item.get("description")),
            "language": _parse_language(item.get("language")),
        }

        if fetch_files:
            row.update(_parse_distributions(distributions))
        else:
            row.update({"total_file_size": 0, "file_types": ""})

        rows.append(row)

    print(f"  -> dropped {n_restricted} restricted datasets")
    print(f"  -> dropped {n_no_dist} datasets with no distributions")
    if min_date is not None:  # added
        print(f"  -> dropped {n_too_old} datasets older than {min_date.date()}")
    print(f"  -> {len(rows)} public datasets remaining")

    if not rows:
        return pd.DataFrame()

    return standardise(pd.DataFrame(rows))


def search_all(
        queries: Iterable[str],
        *,
        per_page: int = 100,
        delay_seconds: float = 0.5,
        inter_query_delay: float = 2.0,
        deduplicate_by: Optional[str] = "id",
        fetch_files: bool = True,
        min_date: Optional[datetime] = None,       # added
) -> pd.DataFrame:
    queries = list(queries)
    frames: List[pd.DataFrame] = []
    session = make_session()

    print(f"\n=== Starting {len(queries)} queries ===\n")

    for idx, q in enumerate(tqdm(queries, desc="Queries", unit="query"), start=1):
        print(f"\n[{idx}/{len(queries)}] Query: {q}")
        df_q = search_records(
            query=q,
            per_page=per_page,
            delay_s=delay_seconds,
            session=session,
            fetch_files=fetch_files,
            min_date=min_date,
        )
        if not df_q.empty:
            frames.append(df_q)

        running_total = sum(len(f) for f in frames)
        print(f"  [running total: {running_total} datasets across {len(frames)} queries]")

        if inter_query_delay and idx < len(queries):
            time.sleep(inter_query_delay)

    if not frames:
        print("\n  No results at all!")
        return pd.DataFrame()

    df_all = pd.concat(frames, ignore_index=True)
    before = len(df_all)

    if deduplicate_by and deduplicate_by in df_all.columns:
        df_all = df_all.drop_duplicates(subset=deduplicate_by, keep="last")
        print(
            f"\n  Deduplication: {before} → {len(df_all)} "
            f"({before - len(df_all)} duplicates removed)"
        )

    if "updated" in df_all.columns:
        df_all["updated"] = pd.to_datetime(df_all["updated"], errors="coerce", utc=True)
        df_all = df_all.sort_values("updated").reset_index(drop=True)

    print(f"\n=== Done: {len(df_all)} unique public datasets ===\n")
    return df_all


def _build_wholeword_pattern(terms: Iterable[str]) -> re.Pattern:
    """Build a regex that matches the terms only as whole words."""
    escaped = [re.escape(t) for t in terms]
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


def filter_wholeword(df: pd.DataFrame, terms: Iterable[str],
                     fields=("title", "description")) -> pd.DataFrame:
    """Keep only rows where a term appears as a standalone word."""
    pat = _build_wholeword_pattern(terms)

    def row_matches(row) -> bool:
        text = " ".join(str(row.get(f, "") or "") for f in fields)
        return bool(pat.search(text))

    mask = df.apply(row_matches, axis=1)
    print(f"  whole-word filter: {len(df)} → {int(mask.sum())}")
    return df[mask].reset_index(drop=True)


def main() -> int:
    """Run the full EU Open Data Portal harvest and write ``eu_opendata.csv``."""
    queries = build_queries(PLATFORMS, TEXT_TERMS, boolean=True)
    df = search_all(
        queries=queries,
        per_page=100,
        inter_query_delay=QUERY_DELAY_S,
        deduplicate_by="id",
        fetch_files=True,
        min_date=datetime.fromisoformat(MIN_CREATED_DATE),
    )

    # The portal's search matches substrings, so the terms are re-checked as
    # whole words, and the most common "X" false positives are excluded.
    df = filter_wholeword(df, PLATFORMS + TEXT_TERMS)
    false_positives = re.compile(r"x[- ]?ray|x[- ]?men|x[- ]?achse|axis|vitamin", re.IGNORECASE)
    df = df[~df["description"].fillna("").str.contains(false_positives)]

    # Records mirrored from Zenodo are collected by the Zenodo script instead.
    df = df[~df["id"].str.contains("zenodo", case=False, na=False)]

    df = finalise(df, deduplicate_by="id")
    write_output(df, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
