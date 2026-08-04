"""Harvest social-media text datasets from Mendeley Data (OAI-PMH).

Harvests the Mendeley Data OAI-PMH repository in monthly slices (with automatic
slice-splitting and retries around Cloudflare/500 errors, via curl_cffi to pass
Cloudflare's browser check), parses Dublin Core records, then filters the
harvested records against boolean "platform AND (content terms)" queries using a
small AND/OR/NOT query parser. Results are date- and language-filtered to
English and written to ``mendeley_oai.csv``.
"""


# Make the shared modules in the parent directory (config.py, query_specs.py)
# importable when this script is executed directly from within ``harvesting/``.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Optional, Iterable, List, Dict, Any
import re
import time
import os
import pandas as pd
from curl_cffi import requests as cffi_requests
from xml.etree import ElementTree as ET
from tqdm import tqdm
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from harvest_common import (
    build_queries,
    finalise,
    standardise,
    write_output,
)
from query_specs import MIN_CREATED_DATE, PLATFORMS, TEXT_TERMS

OAI_BASE = "https://data.mendeley.com/oai"
OUTPUT_FILE = "mendeley_oai.csv"

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}


class _RetryableOAIError(Exception):
    pass


def _make_session():
    """Build a curl_cffi session impersonating Chrome (to pass Cloudflare)."""
    s = cffi_requests.Session(impersonate="chrome124")
    s.headers.update({
        "Accept": "application/xml,text/xml,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


# ----------------------------------------------------------------------------
# OAI-PMH Harvesting
# ----------------------------------------------------------------------------

def _parse_record(record_el) -> Optional[Dict[str, Any]]:
    """Parse one OAI record element into a flat dict (or None if unusable)."""
    header = record_el.find("oai:header", NS)
    if header is None or header.get("status") == "deleted":
        return None

    identifier = header.findtext("oai:identifier", default="", namespaces=NS)
    datestamp = header.findtext("oai:datestamp", default="", namespaces=NS)
    set_specs = [e.text for e in header.findall("oai:setSpec", NS) if e.text]

    metadata = record_el.find("oai:metadata", NS)
    if metadata is None:
        return None
    dc = metadata.find("oai_dc:dc", NS)
    if dc is None:
        return None

    def collect(tag):
        return [e.text for e in dc.findall(f"dc:{tag}", NS) if e.text]

    titles = collect("title")
    creators = collect("creator")
    subjects = collect("subject")
    descriptions = collect("description")
    publishers = collect("publisher")
    contributors = collect("contributor")
    dates = collect("date")
    formats = collect("format")
    identifiers = collect("identifier")
    languages = collect("language")
    rights = collect("rights")

    # extract DOI + URL from identifiers
    doi, url = "", ""
    for ident in identifiers:
        m = re.search(r'(10\.\d{4,9}/\S+)', ident)
        if m and not doi:
            doi = m.group(1).rstrip(".,;")
        elif ident.startswith("http") and not url:
            url = ident

    title_str = " ".join(titles)
    desc_str = " ".join(descriptions)
    subj_str = " ".join(subjects)

    return {
        "id": identifier,
        "doi": doi,
        "url": url,
        "updated": dates[0] if dates else datestamp,
        "title": title_str,
        "description": desc_str,
        "language": languages[0] if languages else "",
        # search text for the boolean filter:
        "_search_text": " ".join(
            [title_str, desc_str, subj_str, ", ".join(creators)]
        ).lower(),
    }


def _fetch_with_retries(
        session,
        params: dict,
        *,
        timeout: tuple = (10, 300),
        max_attempts: int = 6,
        backoff_base: float = 2.0,
):
    """
    Fetch one OAI response with retries. Raises RuntimeError if all attempts
    fail. Otherwise returns the response object.
    """
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(OAI_BASE, params=params, timeout=timeout)
            status = resp.status_code
            cf_block = b"Just a moment" in resp.content[:2000]

            # OAI also returns 200 on "errors" (e.g. badResumptionToken).
            # We only retry on real transport/server errors and Cloudflare.
            if status == 200 and not cf_block:
                return resp

            if cf_block:
                last_err = f"cloudflare challenge (status {status})"
            elif 500 <= status < 600 or status in (408, 425, 429):
                last_err = f"http {status}"
            else:
                # 4xx (except 408/425/429) are usually not retryable
                # -> still show the last body and abort
                print(f"  [non-retryable http {status}] {resp.url}")
                print(resp.text[:500])
                raise RuntimeError(f"non-retryable http {status}")

        except RuntimeError:
            raise
        except Exception as e:
            last_err = f"request error: {e}"

        sleep_for = backoff_base ** attempt
        print(f"  [retry {attempt}/{max_attempts}] {last_err} "
              f"-> sleeping {sleep_for:.1f}s")
        time.sleep(sleep_for)

    raise RuntimeError(f"OAI request failed after {max_attempts} attempts: {last_err}")


def harvest_records(
        metadata_prefix: str = "oai_dc",
        from_date: Optional[str] = None,
        until_date: Optional[str] = None,
        set_spec: Optional[str] = None,
        session: Optional = None,
        delay_s: float = 0.5,
        timeout: tuple = (10, 300),
        max_records: Optional[int] = None,
        max_attempts: int = 6,
) -> List[Dict[str, Any]]:
    """Harvest OAI records for a date window via resumption tokens."""
    s = session or _make_session()

    params = {"verb": "ListRecords", "metadataPrefix": metadata_prefix}
    if from_date:  params["from"] = from_date
    if until_date: params["until"] = until_date
    if set_spec:   params["set"] = set_spec

    records: List[Dict[str, Any]] = []
    pbar = tqdm(desc="OAI harvest", unit="rec")
    page = 0
    expected_total: Optional[int] = None

    while True:
        page += 1

        # --- per page: HTTP + parse + OAI error with retry ---
        attempt = 0
        root = None
        while True:
            attempt += 1
            try:
                resp = _fetch_with_retries(
                    s, params, timeout=timeout, max_attempts=max_attempts
                )
                root = ET.fromstring(resp.content)

                err = root.find("oai:error", NS)
                if err is not None:
                    code = (err.get("code") or "?").strip()
                    msg = (err.text or "").strip()
                    if code == "noRecordsMatch":
                        pbar.close()
                        return records
                    # Mendeley returns generic 500s as OAI errors
                    if ("internal server error" in msg.lower()
                            or code in ("?", "")):
                        raise _RetryableOAIError(f"{code or '?'}: {msg}")
                    pbar.close()
                    raise RuntimeError(f"OAI error on page {page}: {code}: {msg}")

                break  # success

            except (_RetryableOAIError, ET.ParseError) as e:
                if attempt >= max_attempts:
                    pbar.close()
                    raise RuntimeError(f"Giving up on page {page}: {e}")
                sleep_for = 2.0 ** attempt
                print(f"  [retry {attempt}/{max_attempts}] {e} "
                      f"-> sleeping {sleep_for:.1f}s")
                time.sleep(sleep_for)

        # --- collect records ---
        list_records = root.find("oai:ListRecords", NS)
        if list_records is None:
            print("  [no ListRecords element] -> done")
            break

        page_count = 0
        for rec in list_records.findall("oai:record", NS):
            parsed = _parse_record(rec)
            if parsed:
                records.append(parsed)
                page_count += 1
                pbar.update(1)
                if max_records and len(records) >= max_records:
                    pbar.close()
                    return records

        token_el = list_records.find("oai:resumptionToken", NS)
        if expected_total is None and token_el is not None:
            size_attr = token_el.get("completeListSize")
            if size_attr and size_attr.isdigit():
                expected_total = int(size_attr)
                print(f"  [server reports completeListSize={expected_total}]")

        print(f"  [page {page}] +{page_count} records, total={len(records)}"
              + (f"/{expected_total}" if expected_total else ""))

        if token_el is None or not (token_el.text and token_el.text.strip()):
            break

        params = {"verb": "ListRecords",
                  "resumptionToken": token_el.text.strip()}

        if delay_s:
            time.sleep(delay_s)

    pbar.close()
    return records


def _to_utc_date(s: str) -> date:
    """Accepts 'YYYY-MM-DD' or 'YYYY-MM-DDThh:mm:ssZ'."""
    s = s.strip()
    if "T" in s:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    return date.fromisoformat(s)


def _month_ranges(start: date, end: date):
    """Yield (from_iso, until_iso) timestamp pairs for each month in [start, end]."""
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt = cur + relativedelta(months=1)
        last = min(nxt - timedelta(days=1), end)
        first = max(cur, start)
        yield (
            f"{first.isoformat()}T00:00:00Z",
            f"{last.isoformat()}T23:59:59Z",
        )
        cur = nxt


def _split_slice(f_iso: str, u_iso: str) -> List[tuple]:
    """Halve a [f..u] timestamp slice. Returns [] if it cannot be split."""
    f_dt = datetime.fromisoformat(f_iso.replace("Z", "+00:00"))
    u_dt = datetime.fromisoformat(u_iso.replace("Z", "+00:00"))
    if (u_dt - f_dt) <= timedelta(days=1):
        return []
    mid = f_dt + (u_dt - f_dt) / 2
    mid = mid.replace(microsecond=0)
    left = (f_iso, mid.strftime("%Y-%m-%dT%H:%M:%SZ"))
    right_start = (mid + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    right = (right_start, u_iso)
    return [left, right]


def _harvest_slice_with_split(
        f: str, u: str, *, s, metadata_prefix, delay_s, timeout,
        max_attempts, depth: int = 0, max_depth: int = 5,
) -> tuple:
    """Returns (records, failed_subslices)."""
    indent = "  " * (depth + 1)
    try:
        recs = harvest_records(
            metadata_prefix=metadata_prefix,
            from_date=f, until_date=u,
            session=s, delay_s=delay_s, timeout=timeout,
            max_attempts=max_attempts,
        )
        return recs, []
    except RuntimeError as e:
        print(f"{indent}!! slice {f}..{u} failed: {e}")
        if depth >= max_depth:
            return [], [(f, u, str(e))]
        sub = _split_slice(f, u)
        if not sub:
            return [], [(f, u, str(e))]
        print(f"{indent}-> splitting into {len(sub)} sub-slices")
        all_recs, all_failed = [], []
        for sf, su in sub:
            r, fail = _harvest_slice_with_split(
                sf, su, s=s, metadata_prefix=metadata_prefix,
                delay_s=delay_s, timeout=timeout,
                max_attempts=max_attempts,
                depth=depth + 1, max_depth=max_depth,
            )
            all_recs.extend(r)
            all_failed.extend(fail)
        return all_recs, all_failed


def harvest_records_chunked(
        metadata_prefix: str = "oai_dc",
        start: str = "2010-01-01",
        end: Optional[str] = None,
        session: Optional = None,
        delay_s: float = 1.0,
        timeout: tuple = (10, 300),
        max_attempts: int = 6,
        chunk: str = "month",
) -> List[Dict[str, Any]]:
    """Harvest the repository in month/year slices, splitting failing slices."""
    s = session or _make_session()

    # 1) fetch server bounds and clamp the start
    info = identify(s)
    earliest = _to_utc_date(info["earliestDatestamp"]) if info["earliestDatestamp"] else date(2000, 1, 1)

    user_start = date.fromisoformat(start)
    user_end = date.fromisoformat(end) if end else date.today()

    effective_start = max(user_start, earliest)
    if effective_start != user_start:
        print(f"  [info] requested start {user_start} is before repo's "
              f"earliestDatestamp {earliest} -> using {effective_start}")

    if effective_start > user_end:
        print("  [info] nothing to harvest (start > end)")
        return []

    # 2) build slices
    if chunk == "year":
        slices = []
        for y in range(effective_start.year, user_end.year + 1):
            first = max(date(y, 1, 1), effective_start)
            last = min(date(y, 12, 31), user_end)
            slices.append((
                f"{first.isoformat()}T00:00:00Z",
                f"{last.isoformat()}T23:59:59Z",
            ))
    else:
        slices = list(_month_ranges(effective_start, user_end))

    print(f"  [info] harvesting {len(slices)} {chunk} slices "
          f"from {effective_start} to {user_end}")

    # 3) harvest slice by slice, collect errors
    all_records: List[Dict[str, Any]] = []
    seen_ids: set = set()
    failed_slices: List[tuple] = []

    for i, (f, u) in enumerate(slices, 1):
        print(f"\n=== Slice {i}/{len(slices)}: {f} .. {u} ===")
        recs, fails = _harvest_slice_with_split(
            f, u,
            s=s,
            metadata_prefix=metadata_prefix,
            delay_s=delay_s,
            timeout=timeout,
            max_attempts=max_attempts,
        )
        failed_slices.extend(fails)

        new = 0
        for r in recs:
            oid = r.get("oai_identifier")
            if oid and oid not in seen_ids:
                seen_ids.add(oid)
                all_records.append(r)
                new += 1
        print(f"  -> slice yielded {len(recs)} records ({new} new), "
              f"grand total={len(all_records)}")

    if failed_slices:
        print(f"\n!! {len(failed_slices)} slices failed:")
        for f, u, e in failed_slices:
            print(f"   {f}..{u}: {e}")

    return all_records


# ----------------------------------------------------------------------------
# Boolean Query Parser (AND / OR / NOT / parentheses / "phrases")
# ----------------------------------------------------------------------------

def _tokenize_query(query: str) -> List[str]:
    return re.findall(r'\(|\)|\bAND\b|\bOR\b|\bNOT\b|"[^"]+"|[^\s()]+', query)


def compile_query(query: str):
    """Compile a boolean query string into a matcher(text_lower) -> bool."""
    tokens = _tokenize_query(query)
    pos = [0]

    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else None

    def consume():
        t = tokens[pos[0]];
        pos[0] += 1;
        return t

    def parse_or():
        node = parse_and()
        nodes = [node]
        while peek() == "OR":
            consume()
            nodes.append(parse_and())
        return ("OR", nodes) if len(nodes) > 1 else node

    def parse_and():
        node = parse_not()
        nodes = [node]
        # implicit AND (two atoms without operator) as in many search engines
        while peek() not in (None, ")", "OR"):
            if peek() == "AND":
                consume()
            nodes.append(parse_not())
        return ("AND", nodes) if len(nodes) > 1 else node

    def parse_not():
        if peek() == "NOT":
            consume()
            return ("NOT", parse_atom())
        return parse_atom()

    def parse_atom():
        t = peek()
        if t == "(":
            consume()
            node = parse_or()
            if peek() == ")":
                consume()
            return node
        term = consume().strip('"').lower()
        return ("TERM", term)

    ast = parse_or()
    return lambda text_lower: _eval(ast, text_lower)


def _eval(node, text: str) -> bool:
    t = node[0]
    if t == "TERM":
        # word boundary – prevents "x" from matching inside "extension"
        return re.search(r'(?<!\w)' + re.escape(node[1]) + r'(?!\w)', text) is not None
    if t == "AND": return all(_eval(c, text) for c in node[1])
    if t == "OR":  return any(_eval(c, text) for c in node[1])
    if t == "NOT": return not _eval(node[1], text)
    return False


def _build_search_text(r: dict) -> str:
    if r.get("_search_text"):
        return r["_search_text"]
    return " ".join(
        str(r.get(k, "") or "")
        for k in ("title", "description")
    ).lower()


def filter_records(records: List[dict], query: str) -> List[dict]:
    matcher = compile_query(query)
    return [r for r in records if matcher(_build_search_text(r))]

# ----------------------------------------------------------------------------
# Multi-Query Pipeline
# ----------------------------------------------------------------------------

def search_all(
        queries: Iterable[str],
        *,
        records: Optional[List[dict]] = None,
        from_date: Optional[str] = None,
        until_date: Optional[str] = None,
        delay_seconds: float = 0.5,
        deduplicate_by: Optional[str] = "oai_identifier",
        metadata_prefix: str = "oai_dc",
        cache_path: Optional[str] = None,
) -> pd.DataFrame:
    """Harvest (or reuse) records, filter against all queries and deduplicate."""
    queries = list(queries)
    session = _make_session()

    # Optional: cache harvested records (the repo is large)
    if records is None and cache_path and os.path.exists(cache_path):
        print(f"=== Loading cached harvest from {cache_path} ===")
        cache_df = pd.read_pickle(cache_path)
        records = cache_df.to_dict("records")
        print(f"  -> {len(records)} records loaded from cache")

    if records is None:
        print("=== Harvesting full OAI-PMH repository ===")
        records = harvest_records(
            metadata_prefix=metadata_prefix,
            from_date=from_date,
            until_date=until_date,
            session=session,
            delay_s=delay_seconds,
        )
        print(f"  -> {len(records)} records harvested")
        if cache_path:
            pd.DataFrame(records).to_pickle(cache_path)
            print(f"  -> cached to {cache_path}")

    print(f"\n=== Filtering against {len(queries)} queries ===\n")

    frames: List[pd.DataFrame] = []
    for i, q in enumerate(tqdm(queries, desc="Queries", unit="query"), start=1):
        print(f"\n[{i}/{len(queries)}] Query: {q}")
        matches = filter_records(records, q)

        print(f"  -> {len(matches)} matches")
        running = sum(len(f) for f in frames) + len(matches)
        print(f"  [running total: {running}]")

        if matches:
            frames.append(pd.DataFrame(matches))

    if not frames:
        print("\nNo results at all!")
        return pd.DataFrame()

    df_all = pd.concat(frames, ignore_index=True)
    before = len(df_all)

    # Dedup primarily by oai_identifier (always present), DOI as fallback
    if "oai_identifier" in df_all.columns:
        df_all = df_all.drop_duplicates(subset="oai_identifier", keep="last")
    if deduplicate_by and deduplicate_by != "oai_identifier" and deduplicate_by in df_all.columns:
        mask = df_all[deduplicate_by].fillna("").ne("")
        df_dedup = df_all[mask].drop_duplicates(subset=deduplicate_by, keep="last")
        df_all = pd.concat([df_dedup, df_all[~mask]], ignore_index=True)
    print(f"\nDeduplication: {before} -> {len(df_all)} "
          f"({before - len(df_all)} duplicates removed)")

    if "_search_text" in df_all.columns:
        df_all = df_all.drop(columns=["_search_text"])

    if "created" in df_all.columns:
        df_all["created"] = pd.to_datetime(df_all["created"], errors="coerce", utc=True)
        df_all = df_all.sort_values(by="created")

    print(f"\n=== Done: {len(df_all)} unique datasets ===\n")
    return df_all.reset_index(drop=True)


def identify(session=None) -> Dict[str, str]:
    """Call the OAI Identify verb and return key repository metadata."""
    s = session or _make_session()
    resp = s.get(OAI_BASE, params={"verb": "Identify"}, timeout=60)
    root = ET.fromstring(resp.content)
    ident = root.find("oai:Identify", NS)
    info = {
        "granularity": ident.findtext("oai:granularity", "", NS),
        "earliestDatestamp": ident.findtext("oai:earliestDatestamp", "", NS),
        "repositoryName": ident.findtext("oai:repositoryName", "", NS),
        "protocolVersion": ident.findtext("oai:protocolVersion", "", NS),
    }
    print("Identify:", info)
    return info


def main() -> int:
    """Run the full Mendeley Data harvest and write ``mendeley_oai.csv``."""
    identify()

    # OAI-PMH filters by timestamp only, so the whole catalogue is harvested in
    # monthly slices and the search terms are applied client-side afterwards.
    records = harvest_records_chunked(
        start=MIN_CREATED_DATE,
        end=date.today().isoformat(),
        chunk="month",
        delay_s=1.0,
    )
    print(f"  -> {len(records)} unique records harvested")

    queries = build_queries(PLATFORMS, TEXT_TERMS, boolean=True)
    df = search_all(queries=queries, records=records, deduplicate_by="doi")
    df = standardise(df)
    # Dublin Core metadata carries no file sizes, so the threshold is skipped.
    df = finalise(df, deduplicate_by="id", apply_size_filter=False)
    write_output(df, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
