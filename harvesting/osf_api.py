"""Harvest social-media text datasets from OSF via the Trove index (share.osf.io).

Background: the old OSF search API (https://api.osf.io/v2/search/...) has been
shut down (all category paths return 404). OSF now exposes full-text search
through the Trove index:

    https://share.osf.io/trove/index-card-search
        ?cardSearchText=<query>
        &cardSearchFilter[resourceType]=Project        # or ProjectComponent, File
        &cardSearchFilter[dateCreated][after]=YYYY-MM-DD
        &page[size]=100
        &acceptMediatype=application/json

The 'application/json' format returns one flat "index-card" per hit. The shape
depends on the resource type:

- Project / ProjectComponent cards carry @id (osf.io URL), title, description,
  dateCreated, dateModified and identifier (incl. DOI, if present). They are
  containers, so we additionally query the OSF API v2 to confirm that actual
  data files are attached (see ``list_node_data_files``).
- File cards carry fileName, filePath, the file size (``dcterms:extent`` inside
  ``osf:hasFileVersion``) and the parent project via ``isContainedBy``. Here the
  file check is done directly on the card – no extra API call needed.

Note on resource types: OSF/Trove does NOT expose a "Dataset" resourceType
(the SHARE vocabulary only knows Project, ProjectComponent, Registration,
RegistrationComponent, Preprint, File, Agent, ...), so it is not queried.

Results are relevance-filtered (platform AND content term), file-checked against
the DATA_FORMATS / MIN_SIZE_BYTES rules from ``query_specs``, language-filtered
to English via fastText, and written to ``osf.csv``.
"""


# Make the shared modules in the parent directory (config.py, query_specs.py)
# importable when this script is executed directly from within ``harvesting/``.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Optional, Iterable, List, Tuple
import re
import time
import random
import os
import threading
import concurrent.futures as cf
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tqdm.auto import tqdm
from harvest_common import (
    finalise,
    standardise,
    write_output,
)
from query_specs import (MIN_CREATED_DATE, MIN_SIZE_BYTES, PLATFORMS, TEXT_TERMS,
                         is_relevant_file)
from config import data_path, get_token

TROVE_SEARCH = "https://share.osf.io/trove/index-card-search"
OSF_API = "https://api.osf.io/v2"
OUTPUT_FILE = "osf.csv"

# Which OSF object types are searched. Besides top-level projects and their
# sub-components (datasets often live as a component of a project) we also query
# the File type directly – file cards already carry name/size/parent project.
# "Dataset" is intentionally omitted: OSF/Trove returns nothing for it.
RESOURCE_TYPES = ["Project", "ProjectComponent", "File"]

# Upper bound on pages per query (safety net against endless pagination).
MAX_PAGES = 500
# Trove is a shared service with a strict rate limit (429).
MAX_HTTP_ATTEMPTS = 8          # attempts per page before it is given up
MAX_BACKOFF_S = 120            # upper bound of the backoff wait time

# ── Global throttling (applies to EVERY request on a session) ────────────────
# Chosen conservatively for Trove, which documents no limit and sends no
# Retry-After. Observed: ~10 requests were enough to hit the burst limit.
# Hence: generous spacing + frequent pause. Increase on persistent 429s.
# The OSF API (api.osf.io) is a different host with a laxer limit, so the file
# checker uses a session with relaxed pacing (see _make_osf_session).
MIN_REQUEST_INTERVAL_S = 6.0   # minimum gap between two requests (~10/min)
PAUSE_EVERY_N_REQUESTS = 10    # after every N requests …
PAUSE_SECONDS = 45             # … a longer breather

# Safety bounds for the recursive file listing of a single node.
MAX_FILES_PER_NODE = 5000
MAX_FOLDER_DEPTH = 8

# File check parallelism. Each worker gets its own OSF session (own throttle),
# so effective throughput ≈ FILECHECK_WORKERS / OSF_MIN_INTERVAL_S per second.
FILECHECK_WORKERS = 12
OSF_MIN_INTERVAL_S = 0.3        # per-thread pacing for api.osf.io


def _throttle(session) -> None:
    """Enforce a minimum gap between all requests and, after every
    PAUSE_EVERY_N_REQUESTS requests, insert a longer pause. State and the pacing
    parameters are attached to the session, so a run's Trove session and the
    file-checker's OSF session can move at different speeds."""
    min_interval = getattr(session, "_min_interval", MIN_REQUEST_INTERVAL_S)
    pause_every = getattr(session, "_pause_every", PAUSE_EVERY_N_REQUESTS)
    pause_secs = getattr(session, "_pause_secs", PAUSE_SECONDS)

    last = getattr(session, "_last_req_ts", 0.0)
    gap = time.monotonic() - last
    if gap < min_interval:
        time.sleep(min_interval - gap)

    n = getattr(session, "_req_count", 0) + 1
    session._req_count = n
    if pause_every and n % pause_every == 0:
        time.sleep(pause_secs)

    session._last_req_ts = time.monotonic()


def _make_session() -> requests.Session:
    """Build a session for the Trove index.

    This does not use ``harvest_common.make_session``: Trove's ``Retry-After``
    headers need to be honoured exactly, so ``status_forcelist`` is left empty
    and 429/5xx responses are handled explicitly in :func:`_get` instead of by
    urllib3. Connection-level errors are still retried by urllib3.

    Returns:
        A configured session.
    """
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s = requests.Session()
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": "osf-trove-client/0.1 (research data collection)",
        "Accept": "application/json",
    })
    return s


def _make_osf_session() -> requests.Session:
    """Session for the OSF API v2 file checks. Same retry/backoff machinery as
    the Trove session, but with much lighter pacing – api.osf.io tolerates a
    faster cadence than the shared Trove index. An OSF_TOKEN (if set) is sent as
    a bearer token, which raises the API rate limit and is worth having under
    the parallel file check."""
    s = _make_session()
    s._min_interval = OSF_MIN_INTERVAL_S
    s._pause_every = 0         # no periodic long pause
    s._pause_secs = 0
    token = get_token("OSF_TOKEN")
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def _get(session, url, *, params=None, timeout=(5, 60),
         max_attempts=MAX_HTTP_ATTEMPTS, log_fn=print, ctx="") -> Optional[requests.Response]:
    """
    GET with robust handling of rate limits. On 429 the Retry-After header is
    respected (otherwise exponential backoff with jitter), on 5xx it waits
    exponentially. Returns the successful response or None after giving up.
    """
    for attempt in range(1, max_attempts + 1):
        _throttle(session)
        try:
            resp = session.get(url, params=params, timeout=timeout)
        except requests.exceptions.RequestException as e:
            wait = min(MAX_BACKOFF_S, 2 ** attempt)
            log_fn(f"  [retry] {ctx} connection error ({e}) – waiting {wait}s "
                   f"(attempt {attempt}/{max_attempts}).")
            time.sleep(wait)
            continue

        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            try:
                wait = float(ra) if ra else min(MAX_BACKOFF_S, 5 * 2 ** attempt)
            except ValueError:
                wait = min(MAX_BACKOFF_S, 5 * 2 ** attempt)
            wait = min(MAX_BACKOFF_S, wait) + random.uniform(0, 2)
            log_fn(f"  [429] {ctx} rate-limit – waiting {wait:.0f}s "
                   f"(attempt {attempt}/{max_attempts}).")
            time.sleep(wait)
            continue

        if resp.status_code in (500, 502, 503, 504):
            wait = min(MAX_BACKOFF_S, 2 ** attempt)
            log_fn(f"  [{resp.status_code}] {ctx} server error – waiting {wait}s "
                   f"(attempt {attempt}/{max_attempts}).")
            time.sleep(wait)
            continue

        return resp

    log_fn(f"  [WARN] {ctx} gave up after {max_attempts} attempts.")
    return None


# ── Extraction from a Trove index-card ────────────────────────────────────────

def _first_value(card: dict, key: str) -> str:
    """First @value for a property key (Trove fields are lists of objects)."""
    vals = card.get(key)
    if isinstance(vals, list) and vals:
        v = vals[0]
        if isinstance(v, dict):
            return (v.get("@value") or "").strip()
    return ""


def _resource_type(card: dict) -> str:
    """The card's own resourceType @id (e.g. 'Project', 'File')."""
    rt = card.get("resourceType")
    if isinstance(rt, list) and rt:
        first = rt[0]
        if isinstance(first, dict):
            return (first.get("@id") or "").strip()
    return ""


def _osf_url(card: dict) -> str:
    url = card.get("@id") or ""
    if not url:
        # Fallback: identifier that points to osf.io
        for ident in card.get("identifier", []) or []:
            val = ident.get("@value", "") if isinstance(ident, dict) else ""
            if "osf.io" in val:
                url = val
                break
    return url.strip()


def _osf_id(url: str) -> str:
    # https://osf.io/wbtc8  ->  wbtc8
    m = re.search(r"osf\.io/([^/?#]+)", url or "")
    return m.group(1) if m else ""


_DOI_RE = re.compile(r"10\.\d{4,9}/\S+")


def _extract_doi(card: dict) -> str:
    """Look for a DOI in the identifier values (if the object has one)."""
    for ident in card.get("identifier", []) or []:
        val = ident.get("@value", "") if isinstance(ident, dict) else ""
        if "doi.org" in val or "doi:" in val.lower():
            m = _DOI_RE.search(val)
            return m.group(0) if m else val.strip()
    return ""


# ── File-size helpers ─────────────────────────────────────────────────────────

_SIZE_UNITS = {
    "B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4,
    "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4,
}


def _extent_to_bytes(text: str) -> Optional[int]:
    """Convert a Trove ``dcterms:extent`` string like '1.3 MB' to bytes."""
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)\s*$", text or "")
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    factor = _SIZE_UNITS.get(m.group(2).upper())
    if factor is None:
        return None
    return int(value * factor)


def _file_size_bytes(card: dict) -> Optional[int]:
    """Size of a File card, taken from the first file version's extent."""
    for fv in card.get("osf:hasFileVersion", []) or []:
        if isinstance(fv, dict):
            b = _extent_to_bytes(_first_value(fv, "dcterms:extent"))
            if b is not None:
                return b
    return None


# ── OSF API v2: does a node actually carry usable data files? ──────────────────

def list_node_data_files(
        session: requests.Session,
        node_id: str,
        *,
        max_files: int = MAX_FILES_PER_NODE,
        max_depth: int = MAX_FOLDER_DEPTH,
        timeout: tuple = (5, 60),
        log_fn=print,
        relevant_only: bool = False,
        stop_at_bytes: Optional[int] = None,
) -> List[Tuple[str, int]]:
    """List files (name, size_bytes) under a node's osfstorage provider.

    Walks the OSF API v2 file tree, recursing into folders up to ``max_depth``
    and stopping after ``max_files`` entries. Returns [] on error or empty node.

    Early-exit options (used by the file check to save API calls):
    - ``relevant_only``: only collect files that pass ``is_relevant_file``.
    - ``stop_at_bytes``: once the cumulative size of collected files reaches this
      threshold, stop walking immediately (further folders are not fetched).
    """
    if not node_id:
        return []
    out: List[Tuple[str, int]] = []
    collected_bytes = 0
    # (url, depth) work stack; start at the node's osfstorage root.
    stack: List[Tuple[str, int]] = [
        (f"{OSF_API}/nodes/{node_id}/files/osfstorage/", 0)
    ]

    while stack and len(out) < max_files:
        url, depth = stack.pop()
        next_url = url
        while next_url and len(out) < max_files:
            resp = _get(session, next_url, timeout=timeout,
                        ctx=f"files {node_id} (d{depth})", log_fn=log_fn)
            if resp is None:
                break
            try:
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log_fn(f"  [warn] file list {node_id}: {e}")
                break

            for item in data.get("data", []) or []:
                attr = item.get("attributes", {}) or {}
                kind = attr.get("kind")
                if kind == "file":
                    name = attr.get("name", "") or ""
                    if relevant_only and not is_relevant_file(name):
                        continue
                    size = attr.get("size")
                    size = int(size) if isinstance(size, (int, float)) else 0
                    out.append((name, size))
                    collected_bytes += size
                    if stop_at_bytes is not None and collected_bytes >= stop_at_bytes:
                        return out
                elif kind == "folder" and depth < max_depth:
                    rel = (((item.get("relationships", {}) or {}).get("files", {}) or {})
                           .get("links", {}) or {}).get("related", {})
                    href = rel.get("href") if isinstance(rel, dict) else None
                    if href:
                        stack.append((href, depth + 1))

            next_url = (data.get("links") or {}).get("next")

    return out


def node_relevant_data(
        session: requests.Session,
        node_id: str,
        *,
        min_size: int = MIN_SIZE_BYTES,
        log_fn=print,
) -> Tuple[int, int]:
    """Return (number_of_relevant_data_files, total_bytes_of_those_files) for a
    node, using the DATA_FORMATS / boilerplate rules from ``query_specs``.

    Uses early exit: the walk stops as soon as the relevant payload clears
    ``min_size``. The returned counts are therefore lower bounds for nodes with
    plenty of data (enough to answer "does this node carry usable data?"), and
    exact for nodes that fall short of the threshold."""
    relevant = list_node_data_files(
        session, node_id, log_fn=log_fn,
        relevant_only=True, stop_at_bytes=min_size,
    )
    total = sum(size for _, size in relevant)
    # Only count as "has data" when the relevant payload clears the size floor.
    if total < min_size:
        return 0, total
    return len(relevant), total


# ── One query against Trove ───────────────────────────────────────────────────

def search_records(
        query: str,
        resource_type: str = "Project",
        *,
        min_created: Optional[str] = None,
        page_size: int = 100,
        max_records: Optional[int] = None,
        delay_s: float = 0.5,
        timeout: tuple = (5, 60),
        session: Optional[requests.Session] = None,
        show_progress: bool = True,
) -> pd.DataFrame:
    """Search Trove for one query/resource type and return a DataFrame of cards.

    The column set is the same regardless of resource type; File-specific fields
    (``file_name``, ``file_bytes``, ``parent_url``) are empty for container types.
    """
    s = session or _make_session()

    params = {
        "cardSearchText": query,
        "cardSearchFilter[resourceType]": resource_type,
        "page[size]": page_size,
        "acceptMediatype": "application/json",
    }
    if min_created:
        params["cardSearchFilter[dateCreated][after]"] = min_created

    rows: List[dict] = []
    total = None
    next_url = TROVE_SEARCH
    use_params = params
    page = 0
    bar = None

    while next_url and page < MAX_PAGES:
        resp = _get(s, next_url, params=use_params, timeout=timeout,
                    ctx=f"{resource_type} '{query[:30]}' page {page + 1}")
        if resp is None:
            print(f"  [WARN] {resource_type} '{query[:30]}' page {page + 1} permanently "
                  f"failed – query ends with the {len(rows)} hits collected so far.")
            break
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [WARN] {resource_type} '{query[:30]}' page {page + 1} invalid: {e}")
            break

        # After the first page the full URL (incl. cursor) is in links.next.
        use_params = None
        page += 1

        cards = data.get("data") or []
        if total is None:
            total = (data.get("meta") or {}).get("total")
            if show_progress:
                bar = tqdm(total=total, desc=f"  [{resource_type}] '{query[:30]}'",
                           unit="rec", leave=False)

        if not cards:
            break

        for card in cards:
            rows.append(_parse_card(card, fallback_type=resource_type))

        if bar is not None:
            bar.update(len(cards))

        if max_records is not None and len(rows) >= max_records:
            rows = rows[:max_records]
            break

        next_url = (data.get("links") or {}).get("next")
        if delay_s and next_url:
            time.sleep(delay_s)

    if bar is not None:
        bar.close()

    return pd.DataFrame(rows)


def _parse_card(card: dict, *, fallback_type: str = "") -> dict:
    """Turn one Trove index-card into a flat row. File cards get their name,
    size and parent-project metadata; container cards get title/description."""
    rtype = _resource_type(card) or fallback_type

    if rtype == "File":
        url = _osf_url(card)
        parent = (card.get("isContainedBy") or [{}])
        parent = parent[0] if isinstance(parent, list) and parent else {}
        parent_url = parent.get("@id", "") if isinstance(parent, dict) else ""
        file_name = _first_value(card, "fileName")
        file_path = _first_value(card, "filePath")
        return {
            "url": url,
            "id": _osf_id(url),
            "resource_type": "File",
            "doi": _extract_doi(parent) if isinstance(parent, dict) else "",
            "updated": _first_value(card, "dateModified"),
            # Parent title as the human label; file path doubles as searchable text.
            "title": _first_value(parent, "title") if isinstance(parent, dict) else "",
            "description": file_path,
            "file_name": file_name,
            "file_bytes": _file_size_bytes(card),
            "parent_url": parent_url,
            "created": _first_value(card, "dateCreated"),
        }

    # Project / ProjectComponent (and any other container type).
    url = _osf_url(card)
    return {
        "url": url,
        "id": _osf_id(url),
        "resource_type": rtype,
        "doi": _extract_doi(card),
        "updated": _first_value(card, "dateModified"),
        "title": _first_value(card, "title"),
        "description": _first_value(card, "description"),
        "file_name": "",
        "file_bytes": None,
        "parent_url": "",
        "created": _first_value(card, "dateCreated"),
    }


# ── Relevance filter: platform term AND content term ──────────────────────────

def _relevance_filter(df: pd.DataFrame, platform_term: str,
                      content_terms: List[str]) -> pd.DataFrame:
    """Keep rows whose text (title + description + file name) contains the
    platform term AND at least one content term."""
    if df.empty:
        return df
    hay = (
        df["title"].fillna("") + " "
        + df["description"].fillna("") + " "
        + df.get("file_name", pd.Series("", index=df.index)).fillna("")
    ).str.lower()
    platform_pat = re.escape(platform_term.lower())
    content_pat = "|".join(re.escape(t.lower()) for t in content_terms)
    mask = hay.str.contains(platform_pat, regex=True) & hay.str.contains(content_pat, regex=True)
    return df[mask].reset_index(drop=True)


# ── File check: keep only hits that carry usable data files ───────────────────

def _check_one_row(row, min_size: int, session_getter, log_fn) -> Tuple[int, int, bool]:
    """File check for a single hit → (n_data_files, total_bytes, keep)."""
    rtype = getattr(row, "resource_type", "")
    if rtype == "File":
        name = getattr(row, "file_name", "") or ""
        size = getattr(row, "file_bytes", None)
        # Unknown size (unparseable extent) is given the benefit of the doubt.
        ok = is_relevant_file(name) and (size is None or size >= min_size)
        return (1 if ok else 0,
                int(size) if isinstance(size, (int, float)) else 0,
                bool(ok))
    node_id = getattr(row, "id", "") or ""
    n, tot = node_relevant_data(session_getter(), node_id,
                                min_size=min_size, log_fn=log_fn)
    return n, tot, n > 0


def _apply_file_check(
        df: pd.DataFrame,
        osf_session: Optional[requests.Session] = None,
        *,
        min_size: int = MIN_SIZE_BYTES,
        log_fn=print,
        save_path: Optional[str] = None,
        save_every: int = 25,
        max_workers: int = FILECHECK_WORKERS,
) -> pd.DataFrame:
    """Drop hits without real data files and annotate the survivors.

    - File rows: checked directly against ``is_relevant_file`` + ``min_size``
      using the size already present on the card (no API call).
    - Container rows (Project/ProjectComponent): the node's osfstorage tree is
      listed via the OSF API and checked with the same rules.

    Container checks run in a thread pool (``max_workers``); each worker gets its
    own OSF session, so per-session throttle state is never shared across
    threads. Adds ``n_data_files`` and ``total_data_bytes`` columns. When
    ``save_path`` is given, the survivors checked so far are written every
    ``save_every`` hits, so a crash mid-check does not lose progress.

    ``osf_session`` is accepted for backwards compatibility but ignored in favour
    of per-thread sessions.
    """
    df = df.reset_index(drop=True)
    if df.empty:
        df = df.copy()
        df["n_data_files"] = pd.Series(dtype=int)
        df["total_file_size"] = pd.Series(dtype=int)
        return df

    n = len(df)
    n_files = [0] * n
    total_bytes = [0] * n
    keep = [False] * n
    processed = [False] * n

    # One OSF session per worker thread (throttle state is per-session).
    tls = threading.local()

    def _session() -> requests.Session:
        s = getattr(tls, "osf", None)
        if s is None:
            s = _make_osf_session()
            tls.osf = s
        return s

    def _flush() -> None:
        """Write all survivors processed so far (called from the main thread)."""
        if not save_path:
            return
        out = df.copy()
        out["n_data_files"] = n_files
        out["total_file_size"] = total_bytes
        mask = pd.Series([processed[j] and keep[j] for j in range(n)], index=out.index)
        out[mask].to_csv(save_path, index=False, sep=";")

    rows = list(df.itertuples(index=False))
    bar = tqdm(total=n, desc="  file check", unit="hit",
               leave=False) if log_fn is print else None

    done = 0
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_idx = {
            ex.submit(_check_one_row, row, min_size, _session, log_fn): i
            for i, row in enumerate(rows)
        }
        # Results arrive out of order; bookkeeping and saving stay on this thread.
        for fut in cf.as_completed(future_to_idx):
            i = future_to_idx[fut]
            nf, tot, ok = fut.result()
            n_files[i], total_bytes[i], keep[i] = nf, tot, ok
            processed[i] = True
            done += 1
            if bar is not None:
                bar.update(1)
            if save_every and done % save_every == 0:
                _flush()

    if bar is not None:
        bar.close()

    df = df.copy()
    df["n_data_files"] = n_files
    df["total_file_size"] = total_bytes
    df = df[pd.Series(keep, index=df.index)].reset_index(drop=True)
    if save_path:
        df.to_csv(save_path, index=False, sep=";")
    return df


def search_all(
        platforms: Iterable[str],
        content_terms: List[str],
        *,
        resource_types: Optional[List[str]] = None,
        min_created: Optional[str] = None,
        page_size: int = 100,
        max_records: Optional[int] = None,
        delay_s: float = 0.5,
        inter_query_delay: float = 1.0,
        session: Optional[requests.Session] = None,
        osf_session: Optional[requests.Session] = None,
        file_check: bool = True,
        max_workers: int = FILECHECK_WORKERS,
        save_path: Optional[str] = None,
) -> pd.DataFrame:
    """Search every platform × resource type, relevance-filter, deduplicate and
    then file-check.

    The file check runs ONCE per unique node, after all queries are collected
    and deduplicated – so a project that matches several platform queries costs
    only a single OSF API pass instead of one per query.

    Progress is persisted in two phases when ``save_path`` is given: the
    relevance-filtered hits are dumped to ``<save_path>.relevant.csv`` as the
    Trove harvest proceeds, and the file check writes surviving rows to
    ``save_path`` every few hits (see ``_apply_file_check``).
    """
    platforms = list(platforms)
    resource_types = resource_types or RESOURCE_TYPES
    session = session or _make_session()
    osf_session = osf_session or _make_osf_session()
    frames: List[pd.DataFrame] = []

    # Separate progress file for the pre-file-check harvest, so the two phases
    # never overwrite each other's schema.
    relevant_path = (save_path + ".relevant.csv") if save_path else None

    total_jobs = len(platforms) * len(resource_types)
    print(f"\n=== Trove: {len(platforms)} platforms × {len(resource_types)} types "
          f"= {total_jobs} queries ===\n")

    # ── Phase 1: harvest + relevance filter (no API file calls yet) ──────────
    for rtype in resource_types:
        for platform in platforms:
            # Search multi-word platforms as a phrase.
            q = f'"{platform}"' if " " in platform else platform
            df_q = search_records(
                q, resource_type=rtype, min_created=min_created,
                page_size=page_size, max_records=max_records,
                delay_s=delay_s, session=session,
            )
            before = len(df_q)
            df_q = _relevance_filter(df_q, platform, content_terms)
            print(f"  [{rtype}] '{platform}': {before} -> {len(df_q)} after relevance filter")

            if not df_q.empty:
                frames.append(df_q)
                if relevant_path:  # incremental harvest dump (crash resilience)
                    merged = pd.concat(frames, ignore_index=True)
                    if "id" in merged.columns:
                        merged = merged.drop_duplicates(subset="id", keep="last")
                    merged.to_csv(relevant_path, index=False, sep=";")

            if inter_query_delay:
                time.sleep(inter_query_delay)

    if not frames:
        return pd.DataFrame()

    df_all = pd.concat(frames, ignore_index=True)
    if "id" in df_all.columns:
        df_all = df_all.drop_duplicates(subset="id", keep="last")

    # Client-side date filter as a backup to the server-side filter. Done BEFORE
    # the file check so out-of-range hits never cost an API call.
    if min_created and "created" in df_all.columns:
        created = pd.to_datetime(df_all["created"], errors="coerce", utc=True)
        min_dt = pd.to_datetime(min_created, utc=True)
        df_all = df_all[created.isna() | (created >= min_dt)].reset_index(drop=True)

    # ── Phase 2: one file check per unique node ──────────────────────────────
    if file_check and not df_all.empty:
        print(f"\n=== File check on {len(df_all)} unique relevant hits "
              f"({max_workers} workers) ===")
        df_all = _apply_file_check(df_all, osf_session,
                                   max_workers=max_workers, save_path=save_path)
    elif save_path and not df_all.empty:
        df_all.to_csv(save_path, index=False, sep=";")

    return df_all.reset_index(drop=True)


def main() -> int:
    """Run the full OSF harvest and write ``osf.csv``."""
    save_path = data_path(OUTPUT_FILE)
    # The intermediate file is rewritten from scratch to avoid mixing schemas.
    if os.path.exists(save_path):
        os.remove(save_path)

    df = search_all(
        platforms=PLATFORMS,
        content_terms=TEXT_TERMS,
        resource_types=RESOURCE_TYPES,
        min_created=MIN_CREATED_DATE,
        page_size=500,        # large pages mean far fewer requests, hence fewer 429s
        max_records=None,
        delay_s=0.0,          # pacing is handled by the global throttle
        inter_query_delay=0.0,
        file_check=True,
        save_path=save_path,
    )

    df = standardise(df)
    df = finalise(df, deduplicate_by="id")
    write_output(df, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
