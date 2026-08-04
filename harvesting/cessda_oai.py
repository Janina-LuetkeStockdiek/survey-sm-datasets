"""Harvest social-media text datasets from CESSDA (OAI-PMH).

Harvests the full CESSDA Data Catalogue via OAI-PMH (Dublin Core, English set)
once, drops restricted records, keeps those published on or after
``MIN_CREATED_DATE``, then filters the harvested records against boolean
"platform AND (content terms)" queries. Results are language-filtered to English
via fastText and written to ``cessda.csv``.
"""


# Make the shared modules in the parent directory (config.py, query_specs.py)
# importable when this script is executed directly from within ``harvesting/``.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Optional, Iterable, List, Dict
from collections import Counter
import re
import time
import xml.etree.ElementTree as ET
import pandas as pd
import requests
from tqdm import tqdm
from harvest_common import (
    build_queries,
    finalise,
    make_session,
    standardise,
    write_output,
)
from query_specs import MIN_CREATED_DATE, PLATFORMS, TEXT_TERMS

OAI_BASE = "https://datacatalogue.cessda.eu/oai-pmh/v0/oai"
OUTPUT_FILE = "cessda.csv"

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}



def _txt(elem, path) -> str:
    """Return the stripped text of the first child matching ``path`` (or '')."""
    found = elem.find(path, NS)
    return (found.text or "").strip() if found is not None and found.text else ""


def _all_txt(elem, path) -> List[str]:
    """Return the stripped text of all children matching ``path``."""
    return [(e.text or "").strip() for e in elem.findall(path, NS) if e.text]


def _publish_date(rec: Dict) -> Optional[pd.Timestamp]:
    """
    Parse a record's publishDate as a UTC Timestamp.
    Returns None when there is no / no valid date.
    """
    raw = (rec.get("publishDate", "") or "").strip()
    if not raw:
        return None
    dt = pd.to_datetime(raw, errors="coerce", utc=True)
    if pd.isna(dt):
        return None
    return dt

def harvest_records(
        session: Optional[requests.Session] = None,
        metadata_prefix: str = "oai_dc",
        set_spec: Optional[str] = "language:en",
        from_date: Optional[str] = None,  # e.g. "2010-01-01"
        until_date: Optional[str] = None,
        delay_s: float = 0.3,
        timeout: tuple = (10, 300),
        show_publisher_stats: bool = True,
) -> List[Dict]:
    """
    Harvest the entire CESSDA OAI-PMH endpoint.
    No publisher filter – all records are kept.
    set_spec=None harvests ALL languages (otherwise e.g. 'language:en').
    """
    s = session or make_session()
    records: List[Dict] = []

    params = {"verb": "ListRecords", "metadataPrefix": metadata_prefix}
    if set_spec:
        params["set"] = set_spec
    if from_date:
        params["from"] = from_date  # OAI-PMH datestamp filter
    if until_date:
        params["until"] = until_date

    pbar = tqdm(desc=f"  harvesting OAI-PMH set={set_spec}", unit="rec")
    page = 0
    n_seen = 0
    n_kept = 0
    publisher_counter: Counter = Counter()

    while True:
        page += 1
        try:
            resp = s.get(OAI_BASE, params=params, timeout=timeout)
            resp.raise_for_status()
        except requests.HTTPError as e:
            print(f"  [error] {e.response.status_code} on page {page}: {e}")
            print(f"  [body] {resp.text[:400]}")
            break

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            print(f"  [parse error] {e}")
            print(f"  [body] {resp.text[:400]}")
            break

        err = root.find("oai:error", NS)
        if err is not None:
            code = err.get("code", "?")
            print(f"  [oai error] {code}: {err.text}")
            break

        list_recs = root.find("oai:ListRecords", NS)
        if list_recs is None:
            break

        page_records = list_recs.findall("oai:record", NS)
        n_seen += len(page_records)

        for rec in page_records:
            header = rec.find("oai:header", NS)
            if header is None or header.get("status") == "deleted":
                continue
            identifier = _txt(header, "oai:identifier")
            datestamp = _txt(header, "oai:datestamp")

            md = rec.find("oai:metadata", NS)
            if md is None:
                continue

            # Bugfix: do not use "or md" (ElementTree truthiness!)
            dc = md.find("oai_dc:dc", NS)
            if dc is None:
                dc = md

            publishers = _all_txt(dc, "dc:publisher")
            for p in publishers:
                publisher_counter[p] += 1

            title = " ".join(_all_txt(dc, "dc:title"))
            descs = _all_txt(dc, "dc:description")
            description = " ".join(descs)
            creators = _all_txt(dc, "dc:creator")
            rights = _all_txt(dc, "dc:rights")
            subjects = _all_txt(dc, "dc:subject")
            languages = _all_txt(dc, "dc:language")
            ids = _all_txt(dc, "dc:identifier")
            formats = _all_txt(dc, "dc:format")
            dates = _all_txt(dc, "dc:date")

            url = ""
            doi = ""
            for ident in ids:
                if ident.startswith("http"):
                    if not url:
                        url = ident
                    if "doi.org" in ident and not doi:
                        doi = ident
                elif ident.lower().startswith("doi:"):
                    doi = ident
            if not doi and ids:
                doi = ids[0]

            records.append({
                "id": identifier,
                "doi": doi,
                "url": url,
                "updated": datestamp,
                "title": title,
                "description": description,
                "language": languages[0] if languages else "",
                "file_types": " ".join(formats),
                # The two fields below are working data, not output: "rights"
                # feeds _is_restricted() and "subjects" is part of the text the
                # query filter searches. standardise() drops both.
                "rights": " | ".join(rights),
                "subjects": "; ".join(subjects),
            })
            n_kept += 1

        pbar.update(len(page_records))
        pbar.set_postfix(seen=n_seen, kept=n_kept)

        token_el = list_recs.find("oai:resumptionToken", NS)
        token = token_el.text.strip() if token_el is not None and token_el.text else None
        if not token:
            break

        params = {"verb": "ListRecords", "resumptionToken": token}
        if delay_s:
            time.sleep(delay_s)

    pbar.close()
    print(f"  -> seen {n_seen} records, kept {n_kept} (whole CESSDA)")

    if show_publisher_stats:
        print("\n  Top 30 publisher values:")
        for val, cnt in publisher_counter.most_common(30):
            print(f"    {cnt:6d}  {val!r}")

    return records


def _is_restricted(rec: Dict) -> bool:
    """True if the record's rights text indicates restricted access."""
    txt = (rec.get("rights", "") or "").lower()
    if not txt:
        return False
    restricted_terms = (
        "special licence", "special license", "safeguarded",
        "controlled", "secure access", "secure use",
        "not available", "restricted", "no access",
    )
    return any(t in txt for t in restricted_terms)


def _matches_query(text: str, query: str) -> bool:
    """
    Evaluate a query of the form
        'Foo AND (a OR b OR c) AND bar'
    case-insensitively as a whole-word match against ``text``.
    """
    text_l = " " + text.lower() + " "

    # split top-level on ' AND ', but NOT inside parentheses
    parts = []
    buf = ""
    depth = 0
    i = 0
    q = query
    while i < len(q):
        if q[i] == "(":
            depth += 1
            buf += q[i]
        elif q[i] == ")":
            depth -= 1
            buf += q[i]
        elif depth == 0 and q[i:i + 5].upper() == " AND ":
            parts.append(buf.strip())
            buf = ""
            i += 4
        else:
            buf += q[i]
        i += 1
    if buf.strip():
        parts.append(buf.strip())

    def term_in_text(term: str) -> bool:
        term = term.strip().strip("()").strip()
        if not term:
            return True
        if " OR " in term.upper():
            subs = re.split(r"\s+OR\s+", term, flags=re.I)
            return any(term_in_text(s) for s in subs)
        pattern = r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])"
        return re.search(pattern, text_l) is not None

    return all(term_in_text(p) for p in parts)


def filter_records_by_query(records: List[Dict], query: str) -> List[Dict]:
    """Return the records whose title/description/subjects match ``query``."""
    out = []
    for r in records:
        haystack = " ".join([
            r.get("title", "") or "",
            r.get("description", "") or "",
            r.get("subjects", "") or "",
        ])
        if _matches_query(haystack, query):
            out.append(r)
    return out


def search_all(
        queries: Iterable[str],
        *,
        set_spec: Optional[str] = "language:en",
        min_created_date: Optional[str] = None,  # e.g. "2010-01-01"
        deduplicate_by: Optional[str] = "id",
        drop_restricted: bool = True,
) -> pd.DataFrame:
    """Harvest once, then filter the records against all queries and dedupe."""
    queries = list(queries)
    session = make_session()

    print("\n=== Harvesting ALL records via CESSDA OAI-PMH (one-time) ===\n")
    all_records = harvest_records(session=session, set_spec=set_spec)

    if drop_restricted:
        before = len(all_records)
        all_records = [r for r in all_records if not _is_restricted(r)]
        print(f"  -> dropped {before - len(all_records)} restricted records")

    if min_created_date:
        min_dt = pd.to_datetime(min_created_date, utc=True)
        before = len(all_records)
        all_records = [
            r for r in all_records
            if (_publish_date(r) is not None and _publish_date(r) >= min_dt)
        ]
        print(f"  -> created-date filter >= {min_created_date}: "
              f"{before} -> {len(all_records)}")

    frames: List[pd.DataFrame] = []
    print(f"\n=== Filtering against {len(queries)} queries ===\n")

    for i, q in enumerate(queries, start=1):
        print(f"[{i}/{len(queries)}] Query: {q}")
        matched = filter_records_by_query(all_records, q)
        print(f"  -> {len(matched)} hits")
        if matched:
            frames.append(pd.DataFrame(matched))

    if not frames:
        print("\n No results at all!")
        return pd.DataFrame()

    df_all = pd.concat(frames, ignore_index=True)
    before = len(df_all)

    if deduplicate_by and deduplicate_by in df_all.columns:
        df_all = df_all.drop_duplicates(subset=deduplicate_by, keep="last")
        print(f"\n Deduplication: {before} -> {len(df_all)} "
              f"({before - len(df_all)} duplicates removed)")

    if "updated" in df_all.columns:
        df_all["updated"] = pd.to_datetime(df_all["updated"], errors="coerce", utc=True)
        df_all = df_all.sort_values(by="updated")

    print(f"\n=== Done: {len(df_all)} unique CESSDA datasets ===\n")
    return standardise(df_all)


def main() -> int:
    """Run the full CESSDA harvest and write ``cessda.csv``."""
    queries = build_queries(PLATFORMS, TEXT_TERMS, boolean=True)
    df = search_all(
        queries=queries,
        set_spec="language:en",
        min_created_date=MIN_CREATED_DATE,
        deduplicate_by="id",
        drop_restricted=True,
    )
    # CESSDA metadata carries no file sizes, so the size threshold is skipped.
    df = finalise(df, deduplicate_by="id", apply_size_filter=False)
    write_output(df, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
