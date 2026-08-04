"""Harvest social-media text datasets from Research Data Australia (OAI-PMH).

RDA exposes its registry as RIF-CS over OAI-PMH. OAI-PMH filters by timestamp
only, so the "collection" set — the datasets — is harvested in full and cached to
a pickle, and the search terms are applied to the cache afterwards. A checkpoint
is written every few thousand records, so an interrupted harvest can resume
without refetching everything.

RIF-CS carries no language element, so English filtering relies entirely on
fastText. Results are written to ``rda.csv``.
"""


# Make the shared modules in the parent directory (config.py, query_specs.py)
# importable when this script is executed directly from within ``harvesting/``.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Optional, Iterable, List
import re, time, os
import xml.etree.ElementTree as ET
import pandas as pd
import requests
from tqdm import tqdm
import pickle
from harvest_common import (
    build_queries,
    finalise,
    make_session,
    standardise,
    write_output,
)
from query_specs import MIN_CREATED_DATE, PLATFORMS, TEXT_TERMS
from config import DATA_OUTPUT_DIR


OAI_BASE = "https://researchdata.edu.au/api/registry/oai"
OUTPUT_FILE = "rda.csv"
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "rif": "http://ands.org.au/standards/rif-cs/registryObjects",
}


# ---------------------------------------------------------------------------
# Session & Helpers
# ---------------------------------------------------------------------------

def _txt(el):
    return (el.text or "").strip() if el is not None else ""


def _extract_created(coll, fallback: str = "") -> str:
    """
    Try to find the creation / publication date.
    Falls back to dateAccessioned or the given fallback (e.g. datestamp).
    """
    # 1) collection attributes (RIF-CS)
    for attr in ("dateAccessioned",):
        v = coll.get(attr, "")
        if v:
            return v

    # 2) citationMetadata date (publicationDate / created)
    for date_el in coll.findall(".//rif:citationInfo//rif:date", NS):
        dtype = (date_el.get("type") or "").lower()
        if dtype in ("publicationdate", "created", "issued", "available"):
            v = _txt(date_el)
            if v:
                return v

    # 3) <dates> block (type e.g. dc.created / created)
    for dates in coll.findall(".//rif:dates", NS):
        dtype = (dates.get("type") or "").lower()
        if "creat" in dtype or "publ" in dtype:
            for d in dates.findall("rif:date", NS):
                v = _txt(d)
                if v:
                    return v

    # 4) fallback (e.g. the OAI-PMH datestamp)
    return fallback


def _extract_size_mb(coll) -> str:
    """Try to find the file size (rarely present in RIF-CS)."""
    for loc in coll.findall(".//rif:electronic", NS):
        bs = loc.get("byteSize", "")
        if bs:
            try:
                return str(round(int(bs) / (1024 * 1024), 3))
            except ValueError:
                return bs
    for d in coll.findall(".//rif:description", NS):
        if d.get("type") in ("size", "extent") and _txt(d):
            return _txt(d)
    return ""


# ---------------------------------------------------------------------------
# OAI-PMH harvest of the collection objects
# ---------------------------------------------------------------------------
def harvest_records(
    metadata_prefix: str = "rif",
    set_spec: Optional[str] = None,
    from_date: Optional[str] = None,
    until_date: Optional[str] = None,
    timeout: tuple = (5, 300),
    delay_s: float = 0.5,
    max_records: Optional[int] = None,
    session: Optional[requests.Session] = None,
    checkpoint_file: Optional[str] = None,
    checkpoint_every: int = 5000,
) -> List[dict]:
    s = session or make_session()

    out: List[dict] = []
    token: Optional[str] = None
    if checkpoint_file and os.path.exists(checkpoint_file):
        try:
            with open(checkpoint_file, "rb") as f:
                cp = pickle.load(f)
            out = cp.get("records", [])
            token = cp.get("token") or None
            print(f"  Resume from checkpoint: {len(out)} records, token={'yes' if token else 'no'}")
        except Exception as e:
            print(f"  Checkpoint could not be loaded ({e}) – starting over")

    params = {"verb": "ListRecords", "metadataPrefix": metadata_prefix}
    if set_spec:
        params["set"] = set_spec
    if from_date:
        params["from"] = from_date
    if until_date:
        params["until"] = until_date

    pbar = tqdm(desc="  harvesting", unit="rec", initial=len(out))
    last_checkpoint = len(out)

    while True:
        if token:
            params = {"verb": "ListRecords", "resumptionToken": token}

        try:
            r = s.get(OAI_BASE, params=params, timeout=timeout)
        except requests.exceptions.RequestException as e:
            print(f"\n  [network error] {e}")
            print("  Saving intermediate state and aborting.")
            if checkpoint_file:
                with open(checkpoint_file, "wb") as f:
                    pickle.dump({"records": out, "token": token}, f)
            break

        if r.status_code >= 400:
            print(f"\n  [HTTP {r.status_code}] {r.text[:300]}")
            break

        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            print(f"\n  [XML error] {e} – aborting")
            break

        err = root.find("oai:error", NS)
        if err is not None:
            code = err.get("code")
            if code == "noRecordsMatch":
                print("  [OAI-PMH] no records in the time window")
            else:
                print(f"  [OAI-PMH error] {code}: {_txt(err)}")
            break

        for rec in root.findall(".//oai:record", NS):
            header = rec.find("oai:header", NS)
            if header is not None and header.get("status") == "deleted":
                continue
            rec_id = _txt(header.find("oai:identifier", NS)) if header is not None else ""
            datestamp = _txt(header.find("oai:datestamp", NS)) if header is not None else ""

            ro = rec.find(".//rif:registryObject", NS)
            if ro is None:
                continue
            coll = ro.find("rif:collection", NS)
            if coll is None:
                continue

            ro_key = _txt(ro.find("rif:key", NS))
            updated = coll.get("dateModified", "") or datestamp
            created = _extract_created(coll, fallback=datestamp)

            # title
            title = ""
            for nm in coll.findall(".//rif:name", NS):
                parts = [_txt(p) for p in nm.findall("rif:namePart", NS)]
                cand = " ".join(x for x in parts if x).strip()
                if cand:
                    title = cand
                    break

            # description
            desc = ""
            for d in coll.findall(".//rif:description", NS):
                if d.get("type") in ("full", "brief", None) and _txt(d):
                    desc = _txt(d)
                    break

            # DOI
            doi = ""
            for ident in coll.findall(".//rif:identifier", NS):
                v = _txt(ident)
                if ident.get("type") == "doi" or "doi.org" in v.lower():
                    doi = v
                    break
            if not doi:
                doi = rec_id

            # URL
            url = ""
            for loc in coll.findall(".//rif:electronic", NS):
                v = _txt(loc.find("rif:value", NS))
                if v:
                    url = v
                    break

                    size_mb = _extract_size_mb(coll)

            try:
                total_file_size = int(float(size_mb) * 1024 * 1024) if size_mb else ""
            except (TypeError, ValueError):
                total_file_size = ""

            out.append({
                "id": ro_key or rec_id,
                "doi": doi,
                "url": url,
                "updated": updated or created,
                "title": title,
                "description": desc,
                "language": "",  # RIF-CS carries none; filled in by fastText
                "total_file_size": total_file_size,
                "file_types": "",
            })

            if max_records and len(out) >= max_records:
                pbar.update(len(out) - pbar.n)
                pbar.close()
                if checkpoint_file and os.path.exists(checkpoint_file):
                    os.remove(checkpoint_file)
                return out

        pbar.update(len(out) - pbar.n)

        rt = root.find(".//oai:resumptionToken", NS)
        token = _txt(rt) if rt is not None else ""

        if checkpoint_file and (len(out) - last_checkpoint) >= checkpoint_every:
            with open(checkpoint_file, "wb") as f:
                pickle.dump({"records": out, "token": token}, f)
            last_checkpoint = len(out)

        if not token:
            if checkpoint_file and os.path.exists(checkpoint_file):
                os.remove(checkpoint_file)
            break
        if delay_s:
            time.sleep(delay_s)

    pbar.close()
    return out


# ---------------------------------------------------------------------------
# Query matching & search
# ---------------------------------------------------------------------------
def _build_matcher(query: str):
    q = query.replace("(", " ").replace(")", " ")
    and_groups = re.split(r"\bAND\b", q)
    groups = []
    for g in and_groups:
        terms = [t.strip().lower() for t in re.split(r"\bOR\b", g) if t.strip()]
        terms = [t for t in terms if t and t not in ("and", "or")]
        if terms:
            pat = re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.I)
            groups.append(pat)
    if not groups:
        raise ValueError(f"Query parsed to 0 groups: {query!r}")

    def match(text: str) -> bool:
        return all(p.search(text) for p in groups)
    return match


def search_all(
    queries: Iterable[str],
    *,
    cache: Optional[List[dict]] = None,
    deduplicate_by: str = "doi",
    max_records: Optional[int] = None,
    min_created_date: Optional[str] = None,
) -> pd.DataFrame:
    session = make_session()
    records = cache if cache is not None else harvest_records(
        session=session, max_records=max_records
    )
    print(f"  total harvested: {len(records)}")

    frames = []
    for q in queries:
        m = _build_matcher(q)
        hits = [r for r in records if m((r["title"] or "") + " " + (r["description"] or ""))]
        print(f"  query '{q[:40]}...' -> {len(hits)}")
        df = pd.DataFrame(hits)
        if df.empty:
            continue
        if "x " in q.lower():
            exclude = '|'.join(map(re.escape, ['xray', "X-ray", "Xray", "x_", "X-Men"]))
            df = df[~df["description"].fillna("").str.contains(exclude, case=False, regex=True)]
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    df_all = pd.concat(frames, ignore_index=True)
    before = len(df_all)
    df_all = df_all.drop_duplicates(subset=deduplicate_by, keep="last")
    print(f"  dedup: {before} -> {len(df_all)}")

    df_all["updated"] = pd.to_datetime(df_all["updated"], errors="coerce", utc=True)

    # --- Filter: only datasets created after MIN_CREATED_DATE ---
    if min_created_date is not None and "created" in df_all.columns:
        df_all["created"] = pd.to_datetime(df_all["created"], errors="coerce", utc=True)
        cutoff = pd.to_datetime(min_created_date, utc=True)
        before_c = len(df_all)
        # records without a recognizable creation date are dropped (NaT)
        df_all = df_all[df_all["created"].notna() & (df_all["created"] >= cutoff)]
        print(f"  created filter (>= {min_created_date}): {before_c} -> {len(df_all)}")

    df_all = df_all.sort_values("updated").reset_index(drop=True)
    return df_all


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    """Run the full Research Data Australia harvest and write ``rda.csv``."""
    base = os.path.join(DATA_OUTPUT_DIR, "")
    cache_file = base + "rda_oai_cache.pkl"
    checkpoint_file = base + "rda_oai_cache.partial.pkl"

    # OAI-PMH filters by timestamp only, so the whole collection set is harvested
    # once and cached; the search terms are applied client-side afterwards.
    if os.path.exists(cache_file):
        with open(cache_file, "rb") as handle:
            records = pickle.load(handle)
        print(f"  loaded cache: {len(records)} records")
    else:
        records = harvest_records(
            metadata_prefix="rif",
            set_spec="class:collection",
            from_date=MIN_CREATED_DATE,
            delay_s=0.3,
            checkpoint_file=checkpoint_file,
            checkpoint_every=5000,
        )
        if os.path.exists(checkpoint_file):
            print(f"  Harvest incomplete ({len(records)} records). "
                  f"Restart the script; the cache was NOT written.")
            return 1
        with open(cache_file, "wb") as handle:
            pickle.dump(records, handle)
        print(f"  harvest complete, cache saved: {len(records)} records")

    queries = build_queries(PLATFORMS, TEXT_TERMS, boolean=True)
    df = search_all(queries, cache=records, deduplicate_by="doi",
                    min_created_date=MIN_CREATED_DATE)
    df = standardise(df)
    df = finalise(df, deduplicate_by="id")
    write_output(df, OUTPUT_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
