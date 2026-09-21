"""Backfill a creation date for every record in the catalog.

The harvesting scripts in this folder all recorded a *last modification*
timestamp in the ``updated`` column -- for Kaggle because its API offers
nothing else, for the remaining repositories because the creation date was
either not requested or dropped when the per-repository CSVs were merged.
This script resolves each record a second time, asks its repository for the
creation / first-publication date and writes the result to a lookup table
(``created_backfill.csv``).  ``apply_created_column.py`` then joins that table
onto the catalog files as a new ``created`` column.

The script never touches the catalog files themselves and is safe to
interrupt: every resolved record is appended to the output file immediately,
and a rerun skips everything that already has a date.

Usage
-----
    # smoke test: five records per repository, nothing is kept
    python backfill_created.py --repos all --limit 5 --dry-run

    # one repository at a time (recommended for the first full run)
    python backfill_created.py --repos Zenodo
    python backfill_created.py --repos GitHub,HuggingFace,OSF

    # Kaggle: offline join against Meta Kaggle, see --help of --meta-kaggle-dir
    python backfill_created.py --repos Kaggle --meta-kaggle-dir ~/Downloads/meta-kaggle

    # everything DataCite knows, for records the native API could not answer
    python backfill_created.py --repos all --retry-failed --datacite-fallback

Requirements
------------
``requests``, ``pandas``, ``python-dotenv`` -- the same set the harvesting
scripts use.  Tokens are read from ``.env`` via ``config.py``; all endpoints
used here work anonymously, tokens only raise the rate limits.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# config.py sits above the harvesting scripts in the project tree and at the
# root of the published repository. Resolved by walking up, so that one and the
# same file works in both layouts.
for _parent in Path(__file__).resolve().parents:
    if (_parent / "config.py").exists():
        sys.path.insert(0, str(_parent))
        break
from config import data_path, get_token

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

log = logging.getLogger("backfill_created")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                                      datefmt="%H:%M:%S"))
    log.addHandler(_h)
log.setLevel(logging.INFO)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Source catalog the work list is taken from.  ``dataset_initial.csv`` is the
#: superset of every other catalog file, so resolving it once covers
#: ``dataset_initial_dedup.csv``, ``dataset_llm.csv`` and
#: ``dataset_relevant.csv`` as well.
DEFAULT_SOURCE = "dataset_initial.csv"

#: Lookup table written by this script and read by ``apply_created_column.py``.
DEFAULT_OUTPUT = "created_backfill.csv"

#: Raw per-repository harvests that still carry the numeric Kaggle dataset id.
#: The final catalog replaced it with a hash, so the id has to be recovered
#: from these files.  Paths are relative to the data directory.
KAGGLE_ID_SOURCES = [
    "backup/raw_data/Kaggle.csv",
    "backup/0_raw_data_after_collection/Kaggle.csv",
    "backup/1_raw_data_after_filtered_terms/Kaggle.csv",
    "backup/2_raw_data_after_platform_extension/kaggle.csv",
    "backup/3_raw_data_after_update_6-12/kaggle.csv",
]

OUT_COLUMNS = ["repository", "url", "id", "doi", "created", "created_source",
               "status", "note", "fetched_at"]

#: Per-repository pause between two requests, in seconds.  Deliberately
#: conservative -- the whole catalog is 7,291 records and none of these APIs
#: is worth getting blocked from.
DELAYS = {
    "Zenodo": 0.4,
    "Figshare": 0.3,
    "GitHub": 0.8,
    "HuggingFace": 0.3,
    "OSF": 0.6,
    "Harvard Dataverse": 0.5,
    "Dryad": 0.5,
    "MendeleyData": 0.5,
    "ScienceDB": 0.5,
    "CESSDA": 0.5,
    "RDA": 0.5,
    "EU-ODP": 0.5,
    "DataGov": 0.5,
    "Kaggle": 0.0,
}

TIMEOUT = (5, 60)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

#: A browser-shaped user agent. Several hosts -- data.mendeley.com and
#: search.gesis.org among them -- answer 403 to anything that announces itself
#: as a script, which would look like access control rather than bot blocking.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def make_session(user_agent: str = BROWSER_UA,
                 headers: Optional[dict] = None) -> requests.Session:
    """Build a requests session with retry/backoff, mirroring the harvest scripts."""
    retry = Retry(
        total=5,
        connect=3,
        read=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    if headers:
        s.headers.update(headers)
    return s


def get_json(session: requests.Session, url: str, *, params: Optional[dict] = None,
             timeout: tuple = TIMEOUT) -> Tuple[Optional[dict], str]:
    """GET a JSON document.  Returns ``(payload, status)``.

    ``status`` is ``ok``, ``not_found``, ``http_<code>`` or ``error:<reason>``
    and is written to the output file so a rerun can tell a missing record
    from a transient failure.
    """
    try:
        r = session.get(url, params=params, timeout=timeout)
    except Exception as exc:  # network level: DNS, TLS, timeout, ...
        return None, f"error:{type(exc).__name__}"
    if r.status_code == 404:
        return None, "not_found"
    if r.status_code == 410:
        return None, "gone"
    if r.status_code >= 400:
        return None, f"http_{r.status_code}"
    try:
        return r.json(), "ok"
    except ValueError:
        return None, "error:not_json"


def norm_date(value) -> Optional[str]:
    """Normalise a timestamp to ``%Y-%m-%dT%H:%M:%SZ`` in UTC.

    This is the notation the catalog already uses for ``updated``, so both
    columns stay comparable.  Date-only values become midnight UTC.
    """
    if value is None:
        return None
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_kaggle_date(value) -> Optional[str]:
    """Normalise a Meta Kaggle timestamp.

    Meta Kaggle writes US notation (``MM/DD/YYYY HH:MM:SS``), which is
    ambiguous for the first twelve days of a month if it is guessed rather
    than stated, so the known formats are tried explicitly first.
    """
    if value is None or str(value).strip() in ("", "nan"):
        return None
    raw = str(value).strip()
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        ts = pd.to_datetime(raw, format=fmt, errors="coerce", utc=True)
        if not pd.isna(ts):
            return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return norm_date(raw)


def _first(*values):
    """Return the first value that is neither None nor an empty string."""
    for v in values:
        if v is not None and str(v).strip() != "":
            return v
    return None


def clean_doi(doi) -> Optional[str]:
    """Strip the ``doi:`` prefix and any resolver URL from a DOI."""
    if doi is None or (isinstance(doi, float) and pd.isna(doi)):
        return None
    d = str(doi).strip()
    if not d or d.lower() == "nan":
        return None
    d = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", d, flags=re.I)
    return d.strip().strip("/") or None


# --------------------------------------------------------------------------- #
# Generic fallback: DataCite
# --------------------------------------------------------------------------- #

#: order in which DataCite's date types are trusted
_DATACITE_ORDER = ("created", "submitted", "issued", "available", "accepted")


def _precision(raw) -> str:
    """Return ``year``, ``month`` or ``full`` for a DataCite date string.

    DataCite happily stores ``"2021"`` as an Issued date.  Parsed naively that
    becomes 1 January, which is indistinguishable from a real timestamp once
    it sits in the catalog, so the precision has to be known before the value
    is used.
    """
    s = str(raw).strip()
    if re.fullmatch(r"\d{4}", s):
        return "year"
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return "month"
    return "full"


def resolve_datacite(session: requests.Session, doi: Optional[str]) -> Tuple[Optional[str], str, str]:
    """Resolve a creation date through the DataCite REST API.

    Works for every DOI minted by DataCite (Zenodo, Figshare, Dryad,
    Dataverse, Mendeley, ScienceDB, CESSDA, RDA) and is the fallback when a
    repository's own API does not answer.

    Order of preference:

    1. a full date from ``dates``, ``Created`` before ``Submitted`` before
       ``Issued``,
    2. the DOI registration timestamp -- a real moment in time and an upper
       bound for the creation date, which beats a bare year,
    3. a year- or month-only value, returned with the status ``year_only`` so
       the caller can decide whether to keep it.
    """
    if not doi:
        return None, "", "no_doi"
    payload, status = get_json(session, f"https://api.datacite.org/dois/{quote(doi, safe='')}")
    if payload is None:
        return None, "", status
    attrs = (payload.get("data") or {}).get("attributes") or {}

    by_type = {}
    for entry in attrs.get("dates") or []:
        dtype = (entry.get("dateType") or "").lower()
        if dtype and entry.get("date"):
            by_type.setdefault(dtype, entry["date"])

    # 1. a date that actually names a day
    for dtype in _DATACITE_ORDER:
        raw = by_type.get(dtype)
        if raw and _precision(raw) == "full":
            value = norm_date(raw)
            if value:
                return value, f"datacite.dates.{dtype}", "ok"

    # 2. the registration timestamp
    value = norm_date(attrs.get("created") or attrs.get("registered"))
    if value:
        return value, "datacite.registered", "ok"

    # 3. only a year or a month is on record
    for dtype in _DATACITE_ORDER:
        raw = by_type.get(dtype)
        if raw:
            value = norm_date(raw)
            if value:
                return value, f"datacite.dates.{dtype}", f"{_precision(raw)}_only"

    year = attrs.get("publicationYear")
    if year:
        return norm_date(f"{year}-01-01"), "datacite.publicationYear", "year_only"
    return None, "", "no_date_in_payload"


# --------------------------------------------------------------------------- #
# Per-repository resolvers
# --------------------------------------------------------------------------- #
# Every resolver takes the session and one catalog row and returns
# ``(created, created_source, status)``.  ``created`` is None whenever the
# record could not be resolved; ``status`` explains why.

def resolve_zenodo(session, row) -> Tuple[Optional[str], str, str]:
    """Zenodo REST API.  ``created`` is the deposition date of this version."""
    rec_id = str(row.get("id") or "").strip()
    if not rec_id.isdigit():
        doi = clean_doi(row.get("doi"))
        m = re.search(r"zenodo\.(\d+)$", doi or "", flags=re.I)
        rec_id = m.group(1) if m else ""
    if not rec_id:
        return None, "", "no_id"
    payload, status = get_json(session, f"https://zenodo.org/api/records/{rec_id}")
    if payload is None:
        return None, "", status
    value = norm_date(payload.get("created"))
    if value:
        return value, "zenodo.created", "ok"
    value = norm_date((payload.get("metadata") or {}).get("publication_date"))
    if value:
        return value, "zenodo.metadata.publication_date", "ok"
    return None, "", "no_date_in_payload"


def resolve_figshare(session, row) -> Tuple[Optional[str], str, str]:
    """Figshare v2 API.  Covers institutional portals too, they share the API."""
    art_id = str(row.get("id") or "").strip()
    if not art_id.isdigit():
        m = re.search(r"figshare\.(\d+)", clean_doi(row.get("doi")) or "", flags=re.I)
        art_id = m.group(1) if m else ""
    if not art_id:
        return None, "", "no_id"
    payload, status = get_json(session, f"https://api.figshare.com/v2/articles/{art_id}")
    if payload is None:
        return None, "", status
    value = norm_date(payload.get("created_date"))
    if value:
        return value, "figshare.created_date", "ok"
    value = norm_date(_first(payload.get("published_date"), payload.get("timeline", {}).get("posted")))
    if value:
        return value, "figshare.published_date", "ok"
    return None, "", "no_date_in_payload"


def resolve_github(session, row) -> Tuple[Optional[str], str, str]:
    """GitHub REST API.  Resolves by numeric repository id where available,
    which survives renames, and falls back to ``owner/repo`` from the URL."""
    repo_id = str(row.get("id") or "").strip()
    endpoint = None
    if repo_id.isdigit():
        endpoint = f"https://api.github.com/repositories/{repo_id}"
    else:
        path = urlparse(str(row.get("url") or "")).path.strip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            endpoint = f"https://api.github.com/repos/{parts[0]}/{parts[1]}"
    if not endpoint:
        return None, "", "no_id"
    payload, status = get_json(session, endpoint)
    if payload is None:
        return None, "", status
    value = norm_date(payload.get("created_at"))
    return (value, "github.created_at", "ok") if value else (None, "", "no_date_in_payload")


def resolve_huggingface(session, row) -> Tuple[Optional[str], str, str]:
    """Hugging Face Hub API.  ``createdAt`` is the repository creation."""
    ds_id = str(row.get("id") or "").strip()
    if "/" not in ds_id:
        path = urlparse(str(row.get("url") or "")).path.strip("/")
        ds_id = re.sub(r"^datasets/", "", path)
    if not ds_id:
        return None, "", "no_id"
    payload, status = get_json(session, f"https://huggingface.co/api/datasets/{ds_id}")
    if payload is None:
        return None, "", status
    value = norm_date(_first(payload.get("createdAt"), payload.get("created_at")))
    return (value, "huggingface.createdAt", "ok") if value else (None, "", "no_date_in_payload")


def resolve_osf(session, row) -> Tuple[Optional[str], str, str]:
    """OSF API v2.

    A five-character GUID can be a node, a registration, a preprint, a file or
    a component, and guessing the collection fails for the less common ones.
    The ``/v2/guids/`` endpoint resolves any GUID to its own type and often
    carries the dates directly, so it is asked first and the explicit
    collections only serve as a fallback.
    """
    guid = str(row.get("id") or "").strip()
    if not guid:
        path = urlparse(str(row.get("url") or "")).path.strip("/")
        guid = path.split("/")[0] if path else ""
    if not guid:
        return None, "", "no_id"

    # Do NOT pass resolve=false here. That returns a bare stub without any
    # attributes, which is what made this resolver report not_found for the
    # five records whose GUID belongs to a file rather than a node.
    payload, status = get_json(session, f"https://api.osf.io/v2/guids/{guid}/")
    if payload is not None:
        data = payload.get("data") or {}
        attrs = data.get("attributes") or {}
        kind = data.get("type") or "resource"
        value = norm_date(_first(attrs.get("date_created"), attrs.get("date_registered"),
                                 attrs.get("date_published"), attrs.get("date_modified")))
        if value:
            # the type is carried in the source, because for a file GUID this
            # is the upload date of that file and not the creation of the
            # project it sits in
            return value, f"osf.guids.{kind}.date_created", "ok"
        # the stub carries no dates, but it names the right collection
        link = ((data.get("links") or {}).get("html")
                or (data.get("relationships") or {}).get("referent", {})
                .get("links", {}).get("related", {}).get("href"))
        if isinstance(link, str) and link.startswith("https://api.osf.io/"):
            sub, sub_status = get_json(session, link)
            if sub is not None:
                sattrs = (sub.get("data") or {}).get("attributes") or {}
                value = norm_date(_first(sattrs.get("date_created"),
                                         sattrs.get("date_registered"),
                                         sattrs.get("date_published")))
                if value:
                    return value, "osf.guids.referent.date_created", "ok"

    last = status if payload is None else "no_date_in_payload"
    for collection in ("nodes", "registrations", "preprints"):
        payload, status = get_json(session, f"https://api.osf.io/v2/{collection}/{guid}/")
        if payload is None:
            last = status
            continue
        attrs = (payload.get("data") or {}).get("attributes") or {}
        value = norm_date(_first(attrs.get("date_created"), attrs.get("date_registered"),
                                 attrs.get("date_published")))
        if value:
            return value, f"osf.{collection}.date_created", "ok"
        last = "no_date_in_payload"
    return None, "", last


def resolve_dataverse(session, row) -> Tuple[Optional[str], str, str]:
    """Harvard Dataverse.  ``createTime`` is the draft creation, so the first
    publication date is preferred and the create time is only a fallback."""
    doi = clean_doi(row.get("doi"))
    if not doi:
        ident = str(row.get("id") or "").strip()
        doi = f"10.7910/DVN/{ident}" if ident else None
    if not doi:
        return None, "", "no_id"
    payload, status = get_json(
        session,
        "https://dataverse.harvard.edu/api/datasets/:persistentId/",
        params={"persistentId": f"doi:{doi}"},
    )
    if payload is None:
        return None, "", status
    data = payload.get("data") or {}
    value = norm_date(data.get("publicationDate"))
    if value:
        return value, "dataverse.publicationDate", "ok"
    value = norm_date(data.get("createTime"))
    return (value, "dataverse.createTime", "ok") if value else (None, "", "no_date_in_payload")


def resolve_dryad(session, row) -> Tuple[Optional[str], str, str]:
    """Dryad API v2.  ``publicationDate`` is the date of first publication."""
    doi = clean_doi(row.get("doi"))
    if not doi:
        ident = str(row.get("id") or "").strip()
        doi = f"10.5061/dryad.{ident}" if ident else None
    if not doi:
        return None, "", "no_id"
    payload, status = get_json(
        session, f"https://datadryad.org/api/v2/datasets/{quote('doi:' + doi, safe='')}")
    if payload is None:
        return None, "", status
    value = norm_date(payload.get("publicationDate"))
    if value:
        return value, "dryad.publicationDate", "ok"
    versions = payload.get("versions") or []
    if versions:
        value = norm_date(versions[0].get("lastModificationDate"))
        if value:
            return value, "dryad.versions[0].lastModificationDate", "first_version"
    return None, "", "no_date_in_payload"


def resolve_mendeley(session, row) -> Tuple[Optional[str], str, str]:
    """Mendeley Data.  The public API answers per dataset id; the catalog's
    DOI carries a version suffix, so the base id is used."""
    ident = str(row.get("id") or "").strip()
    base = ident.split(".")[0] if ident else ""
    if not base:
        m = re.search(r"/datasets/([^/]+)", str(row.get("url") or ""))
        base = m.group(1) if m else ""
    if not base:
        return None, "", "no_id"
    payload, status = get_json(
        session, f"https://data.mendeley.com/public-api/datasets/{base}/versions")
    if payload is not None:
        versions = payload if isinstance(payload, list) else payload.get("results") or []
        dates = [norm_date(_first(v.get("publish_date"), v.get("publishDate"),
                                  v.get("created"), v.get("available")))
                 for v in versions if isinstance(v, dict)]
        dates = [d for d in dates if d]
        if dates:
            return min(dates), "mendeley.versions.min(publish_date)", "ok"

    # Mendeley DOIs in the catalog carry a version suffix (10.17632/<id>.2),
    # and DataCite does not resolve that form -- only the base DOI is
    # registered. Ask for the base first, and for the suffixed one only if
    # that fails.
    doi = clean_doi(row.get("doi"))
    base_doi = re.sub(r"\.\d+$", "", doi) if doi else None
    if base_doi and base_doi != doi:
        result = resolve_datacite(session, base_doi)
        if result[0]:
            return result[0], result[1] + " (base DOI)", result[2]
    return resolve_datacite(session, doi)


def resolve_scienceDB(session, row) -> Tuple[Optional[str], str, str]:
    """Science Data Bank.  Its own API is unstable, DataCite is authoritative
    enough for 33 records."""
    return resolve_datacite(session, clean_doi(row.get("doi")))


def resolve_cessda(session, row) -> Tuple[Optional[str], str, str]:
    """CESSDA.  Weak on both ends and best treated as a manual case.

    The GESIS DOIs (10.7802/...) carry nothing but a publication year in
    DataCite, so they come back as ``year_only`` and are not written.  Two
    DOIs in the initial catalog also sit on the wrong row -- a collection
    artefact -- which would hand two records the same date.  Neither of those
    two is in the final catalog, and CESSDA has four records there in total,
    so filling them by hand from their landing pages is both faster and more
    trustworthy than anything this resolver can do.
    """
    return resolve_datacite(session, clean_doi(row.get("doi")))


def resolve_rda(session, row) -> Tuple[Optional[str], str, str]:
    """Research Data Australia.  The records carry institutional DOIs."""
    return resolve_datacite(session, clean_doi(row.get("doi")))


def resolve_euodp(session, row) -> Tuple[Optional[str], str, str]:
    """EU Open Data Portal.  No DOI, so the portal's own hub API is used."""
    ident = str(row.get("id") or "").strip()
    if not ident:
        m = re.search(r"/datasets/([^/?#]+)", str(row.get("url") or ""))
        ident = m.group(1) if m else ""
    if not ident:
        return None, "", "no_id"
    for base in ("https://data.europa.eu/api/hub/search/datasets/",
                 "https://data.europa.eu/api/hub/repo/datasets/"):
        payload, status = get_json(session, f"{base}{ident}")
        if payload is None:
            continue
        result = payload.get("result") if isinstance(payload, dict) else None
        result = result if isinstance(result, dict) else payload
        value = norm_date(_first(result.get("issued"), result.get("created"),
                                 result.get("dct:issued")))
        if value:
            return value, "euodp.issued", "ok"
    return None, "", "not_found"


def resolve_datagov(session, row) -> Tuple[Optional[str], str, str]:
    """data.gov runs CKAN; ``metadata_created`` is the catalog entry date."""
    ident = str(row.get("id") or "").strip()
    if not ident:
        m = re.search(r"/dataset/([^/?#]+)", str(row.get("url") or ""))
        ident = m.group(1) if m else ""
    if not ident:
        return None, "", "no_id"
    payload, status = get_json(session, "https://catalog.data.gov/api/3/action/package_show",
                               params={"id": ident})
    if payload is None:
        return None, "", status
    result = payload.get("result") or {}
    value = norm_date(_first(result.get("metadata_created"), result.get("issued")))
    return (value, "ckan.metadata_created", "ok") if value else (None, "", "no_date_in_payload")


RESOLVERS: Dict[str, Callable] = {
    "Zenodo": resolve_zenodo,
    "Figshare": resolve_figshare,
    "GitHub": resolve_github,
    "HuggingFace": resolve_huggingface,
    "OSF": resolve_osf,
    "Harvard Dataverse": resolve_dataverse,
    "Dryad": resolve_dryad,
    "MendeleyData": resolve_mendeley,
    "ScienceDB": resolve_scienceDB,
    "CESSDA": resolve_cessda,
    "RDA": resolve_rda,
    "EU-ODP": resolve_euodp,
    "DataGov": resolve_datagov,
}


# --------------------------------------------------------------------------- #
# Kaggle: offline join against Meta Kaggle
# --------------------------------------------------------------------------- #
# The Kaggle API exposes no creation date at all -- neither ``datasets list``
# nor ``datasets view`` carries one, only ``lastUpdated``.  Meta Kaggle,
# Kaggle's own daily dump of its platform metadata, does: ``Datasets.csv``
# holds one row per public dataset with its ``CreationDate``.  Download it
# once (about 30 MB for the file, the archive is much larger):
#
#     kaggle datasets download -d kaggle/meta-kaggle -f Datasets.csv
#     kaggle datasets download -d kaggle/meta-kaggle -f DatasetVersions.csv   # only for the slug fallback
#     kaggle datasets download -d kaggle/meta-kaggle -f Users.csv             # only for the slug fallback
#
# and unzip them into one directory, then pass it as --meta-kaggle-dir.
#
# The join runs on the numeric Kaggle dataset id.  The final catalog replaced
# it with a hash, so it is recovered from the raw harvest CSVs in data/backup
# (2,360 of the 2,390 Kaggle records).  For the remaining records the owner
# and slug are read from the URL and matched against DatasetVersions.csv.

def _load_kaggle_id_map(data_dir: Path) -> Dict[str, str]:
    """Map ``url -> numeric Kaggle id`` from the raw harvest files."""
    mapping: Dict[str, str] = {}
    for rel in KAGGLE_ID_SOURCES:
        path = data_dir / rel
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path, sep=";", dtype=str)
        except Exception as exc:
            log.warning("Kaggle id source %s not readable: %s", rel, exc)
            continue
        if "id" not in df.columns or "url" not in df.columns:
            continue
        for url, ident in zip(df["url"].astype(str), df["id"].astype(str)):
            key = url.strip().rstrip("/").lower()
            if key and ident and ident.lower() != "nan":
                mapping.setdefault(key, ident)
    log.info("Kaggle: %d url -> id pairs recovered from the raw harvests.", len(mapping))
    return mapping


def resolve_kaggle_page(session: requests.Session, row) -> Tuple[Optional[str], str, str]:
    """Read a creation date out of a Kaggle dataset page.

    Meta Kaggle turns out not to list every public dataset -- 41 of the 2,390
    records in this catalog are missing from ``Datasets.csv`` even though
    their pages answer with HTTP 200.  Kaggle embeds its metadata as
    schema.org JSON-LD in the delivered HTML rather than inserting it in the
    browser, which is the same property the FAIR assessment relies on, so the
    date can be read straight out of the page.

    ``datePublished`` is preferred over ``dateCreated``: on Kaggle the former
    is the first publication of the dataset, the latter is occasionally
    written per version.
    """
    url = str(row.get("url") or "").strip()
    if not url:
        return None, "", "no_id"
    try:
        r = session.get(url, timeout=(10, 45), headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        })
    except Exception as exc:
        return None, "", f"error:{type(exc).__name__}"
    if r.status_code == 404:
        return None, "", "not_found"
    if r.status_code >= 400:
        return None, "", f"http_{r.status_code}"

    html = r.text
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, flags=re.S | re.I)
    for raw in blocks:
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            for field in ("datePublished", "dateCreated", "dateModified"):
                value = norm_date(node.get(field))
                if value:
                    status = "ok" if field != "dateModified" else "modified_only"
                    return value, f"kaggle.jsonld.{field}", status
    return None, "", "no_jsonld_date"


def _owner_slug(url: str) -> Tuple[str, str]:
    """Split a Kaggle dataset URL into ``(owner, slug)``, lowercased."""
    path = urlparse(str(url)).path.strip("/")
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] == "datasets":
        parts = parts[1:]
    if len(parts) >= 2:
        return parts[0].lower(), parts[1].lower()
    return "", ""


def resolve_kaggle_batch(rows: pd.DataFrame, meta_dir: Path,
                         data_dir: Path) -> List[dict]:
    """Resolve every Kaggle row at once against the Meta Kaggle dump."""
    datasets_csv = meta_dir / "Datasets.csv"
    if not datasets_csv.exists():
        raise SystemExit(
            f"Datasets.csv not found in {meta_dir}. Download it with\n"
            f"    kaggle datasets download -d kaggle/meta-kaggle -f Datasets.csv\n"
            f"and unzip it into that directory."
        )

    log.info("Kaggle: reading %s ...", datasets_csv)
    meta = pd.read_csv(datasets_csv, usecols=lambda c: c in
                       {"Id", "OwnerUserId", "CreationDate", "CurrentDatasetVersionId"},
                       dtype=str)
    by_id = dict(zip(meta["Id"].astype(str), meta["CreationDate"].astype(str)))
    log.info("Kaggle: %d datasets in Meta Kaggle.", len(by_id))

    url_to_id = _load_kaggle_id_map(data_dir)

    results: List[dict] = []
    unresolved: List[int] = []
    for idx, row in rows.iterrows():
        url = str(row.get("url") or "").strip()
        key = url.rstrip("/").lower()
        kid = url_to_id.get(key)
        created = by_id.get(str(kid)) if kid else None
        if created:
            results.append(_record(row, norm_kaggle_date(created), "metakaggle.Datasets.CreationDate",
                                   "ok", f"kaggle_id={kid}"))
        else:
            unresolved.append(idx)

    log.info("Kaggle: %d resolved by numeric id, %d left for the slug fallback.",
             len(results), len(unresolved))

    if unresolved:
        results.extend(_kaggle_slug_fallback(rows.loc[unresolved], meta, meta_dir, by_id))
    return results


def _kaggle_slug_fallback(rows: pd.DataFrame, meta: pd.DataFrame, meta_dir: Path,
                          by_id: Dict[str, str]) -> List[dict]:
    """Match the remaining records by owner and slug via DatasetVersions.csv.

    ``DatasetVersions.csv`` is around 750 MB, so it is streamed in chunks and
    only the wanted slugs are kept.  Slugs are not unique across owners, so
    the owner is verified through ``Users.csv`` whenever that file is present.
    """
    versions_csv = meta_dir / "DatasetVersions.csv"
    if not versions_csv.exists():
        log.warning("DatasetVersions.csv not in %s -- %d records stay unresolved. "
                    "Download it with: kaggle datasets download -d kaggle/meta-kaggle "
                    "-f DatasetVersions.csv", meta_dir, len(rows))
        return [_record(r, None, "", "no_meta_kaggle_match", "no numeric id, DatasetVersions.csv missing")
                for _, r in rows.iterrows()]

    wanted = {}
    for idx, row in rows.iterrows():
        owner, slug = _owner_slug(row.get("url"))
        if slug:
            wanted.setdefault(slug, []).append((idx, owner))

    users_path = meta_dir / "Users.csv"
    user_names = {}
    if users_path.exists():
        users = pd.read_csv(users_path, usecols=lambda c: c in {"Id", "UserName"}, dtype=str)
        user_names = dict(zip(users["Id"].astype(str), users["UserName"].astype(str).str.lower()))
    else:
        log.warning("Users.csv not in %s -- owner verification is skipped, "
                    "slug collisions are possible.", meta_dir)

    owner_of_dataset = dict(zip(meta["Id"].astype(str), meta.get("OwnerUserId", pd.Series(dtype=str)).astype(str)))

    slug_hits: Dict[str, List[str]] = {}
    log.info("Kaggle: streaming %s for %d slugs ...", versions_csv, len(wanted))
    for chunk in pd.read_csv(versions_csv, usecols=lambda c: c in {"DatasetId", "Slug"},
                             dtype=str, chunksize=500_000):
        chunk = chunk[chunk["Slug"].astype(str).str.lower().isin(wanted)]
        for slug, dsid in zip(chunk["Slug"].astype(str).str.lower(), chunk["DatasetId"].astype(str)):
            slug_hits.setdefault(slug, [])
            if dsid not in slug_hits[slug]:
                slug_hits[slug].append(dsid)

    out = []
    for slug, entries in wanted.items():
        candidates = slug_hits.get(slug, [])
        for idx, owner in entries:
            row = rows.loc[idx]
            match = None
            if len(candidates) == 1:
                match = candidates[0]
            elif candidates and user_names:
                for dsid in candidates:
                    if user_names.get(owner_of_dataset.get(dsid, ""), "") == owner:
                        match = dsid
                        break
            if match and by_id.get(match):
                note = f"kaggle_id={match} via slug"
                status = "ok" if len(candidates) == 1 or user_names else "ambiguous_slug"
                out.append(_record(row, norm_kaggle_date(by_id[match]),
                                   "metakaggle.Datasets.CreationDate", status, note))
            else:
                out.append(_record(row, None, "", "no_meta_kaggle_match",
                                   f"slug={slug}, candidates={len(candidates)}"))
    return out


# --------------------------------------------------------------------------- #
# Output handling
# --------------------------------------------------------------------------- #

def _record(row, created, source, status, note="") -> dict:
    return {
        "repository": row.get("repository"),
        "url": row.get("url"),
        "id": row.get("id"),
        "doi": row.get("doi"),
        "created": created or "",
        "created_source": source,
        "status": status,
        "note": note,
        "fetched_at": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


class Writer:
    """Append-only CSV writer so an interrupted run loses nothing."""

    def __init__(self, path: Path, dry_run: bool = False):
        self.path = path
        self.dry_run = dry_run
        self.fh = None
        self.writer = None
        if dry_run:
            return
        is_new = not path.exists() or path.stat().st_size == 0
        self.fh = open(path, "a", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.fh, fieldnames=OUT_COLUMNS, delimiter=";")
        if is_new:
            self.writer.writeheader()

    def write(self, rec: dict):
        if self.dry_run:
            log.info("[dry-run] %-18s %-8s %-24s %s", rec["repository"], rec["status"],
                     rec["created"] or "-", rec["url"])
            return
        self.writer.writerow(rec)
        self.fh.flush()

    def close(self):
        if self.fh:
            self.fh.close()


def load_done(path: Path, retry_failed: bool) -> set:
    """Return the set of URLs that must not be fetched again."""
    if not path.exists():
        return set()
    df = pd.read_csv(path, sep=";", dtype=str).fillna("")
    if retry_failed:
        df = df[df["created"].str.strip() != ""]
    return set(df["url"].str.strip())


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill a creation date for every catalog record.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage")[-1],
    )
    parser.add_argument("--repos", default="all",
                        help="comma-separated repository names as they appear in the "
                             "catalog, or 'all' (default)")
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help=f"catalog file to take the work list from (default: {DEFAULT_SOURCE})")
    parser.add_argument("--out", default=DEFAULT_OUTPUT,
                        help=f"lookup table to append to (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after N records per repository (0 = no limit); "
                             "use it for a smoke test before the full run")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve but write nothing")
    parser.add_argument("--retry-failed", action="store_true",
                        help="also retry records that are already in the output file "
                             "but have no date")
    parser.add_argument("--datacite-fallback", action="store_true",
                        help="when a repository's own API returns no date, ask DataCite")
    parser.add_argument("--accept-year-only", action="store_true",
                        help="also write dates that are nothing but a publication year "
                             "(they become 1 January and would look like real dates in a "
                             "monthly series); by default such records stay empty and "
                             "carry the status 'year_only'")
    parser.add_argument("--kaggle-page-fallback", action="store_true",
                        help="for Kaggle records that Meta Kaggle does not list, read "
                             "the schema.org JSON-LD out of the dataset page")
    parser.add_argument("--meta-kaggle-dir", default="",
                        help="directory holding the unzipped Meta Kaggle CSVs "
                             "(required for --repos Kaggle)")
    parser.add_argument("--delay", type=float, default=None,
                        help="override the per-request pause in seconds")
    args = parser.parse_args(argv)

    src_path = Path(data_path(args.source))
    if not src_path.exists():
        log.error("Source catalog not found: %s", src_path)
        return 2
    catalog = pd.read_csv(src_path, sep=";", dtype=str).fillna("")
    data_dir = src_path.parent
    out_path = Path(data_path(args.out))

    known = set(RESOLVERS) | {"Kaggle"}
    if args.repos.strip().lower() == "all":
        repos = [r for r in catalog["repository"].dropna().unique() if r in known]
        missing = sorted(set(catalog["repository"].dropna().unique()) - known)
        if missing:
            log.warning("No resolver for: %s -- these records are skipped.", ", ".join(missing))
    else:
        repos = [r.strip() for r in args.repos.split(",") if r.strip()]
        unknown = [r for r in repos if r not in known]
        if unknown:
            log.error("Unknown repository: %s. Known: %s", ", ".join(unknown),
                      ", ".join(sorted(known)))
            return 2

    done = load_done(out_path, args.retry_failed)
    if done:
        log.info("%d records already resolved, they are skipped.", len(done))

    session = make_session()
    gh_token = get_token("GITHUB_TOKEN")
    if gh_token:
        session_github = make_session(headers={"Authorization": f"Bearer {gh_token}"})
    else:
        session_github = session
        log.warning("No GITHUB_TOKEN in .env -- GitHub allows only 60 requests per hour "
                    "anonymously, which is not enough for 360 records.")

    writer = Writer(out_path, dry_run=args.dry_run)
    totals = {}
    try:
        for repo in repos:
            rows = catalog[catalog["repository"] == repo]
            rows = rows[~rows["url"].str.strip().isin(done)]
            if args.limit:
                rows = rows.head(args.limit)
            if rows.empty:
                log.info("%-18s nothing to do.", repo)
                continue

            log.info("%-18s %d records ...", repo, len(rows))

            if repo == "Kaggle":
                if not args.meta_kaggle_dir:
                    log.error("Kaggle needs --meta-kaggle-dir (see the comment in this file).")
                    continue
                recs = resolve_kaggle_batch(rows, Path(args.meta_kaggle_dir).expanduser(),
                                            data_dir)
                if args.kaggle_page_fallback:
                    open_recs = [r for r in recs if not r["created"]]
                    if open_recs:
                        log.info("Kaggle: reading %d dataset pages for the records "
                                 "Meta Kaggle does not list ...", len(open_recs))
                    by_url = {r["url"]: r for r in open_recs}
                    for n, (_, row) in enumerate(rows.iterrows(), start=1):
                        rec = by_url.get(row.get("url"))
                        if rec is None:
                            continue
                        created, source, status = resolve_kaggle_page(session, row)
                        if created:
                            rec.update(created=created, created_source=source,
                                       status=status, note=rec["note"] + " | page")
                        else:
                            rec["status"] = f"{rec['status']}->{status}"
                        time.sleep(args.delay if args.delay is not None else 0.8)
                for rec in recs:
                    writer.write(rec)
                    totals.setdefault(repo, {}).setdefault(rec["status"], 0)
                    totals[repo][rec["status"]] += 1
                continue

            resolver = RESOLVERS[repo]
            sess = session_github if repo == "GitHub" else session
            delay = args.delay if args.delay is not None else DELAYS.get(repo, 0.5)

            for n, (_, row) in enumerate(rows.iterrows(), start=1):
                created, source, status = resolver(sess, row)
                if created is None and args.datacite_fallback:
                    dc_created, dc_source, dc_status = resolve_datacite(sess, clean_doi(row.get("doi")))
                    if dc_created:
                        created, source = dc_created, dc_source
                        status = f"{status}->{dc_status}"
                if (created is not None and not args.accept_year_only
                        and ("year_only" in status or "month_only" in status)):
                    # DataCite's publicationYear turns into 1 January, which is
                    # indistinguishable from a real date once it sits in the
                    # column and would put phantom spikes into every monthly
                    # series.  The finding is kept in the status, the value is
                    # not written unless it is asked for explicitly.
                    source = f"{source} (dropped, year only)"
                    created = None
                writer.write(_record(row, created, source, status))
                totals.setdefault(repo, {}).setdefault(status, 0)
                totals[repo][status] += 1
                if n % 100 == 0:
                    log.info("%-18s %d/%d", repo, n, len(rows))
                if delay:
                    time.sleep(delay)
    except KeyboardInterrupt:
        log.warning("Interrupted -- everything resolved so far is in %s.", out_path)
    finally:
        writer.close()

    log.info("--- summary ---")
    for repo, counts in totals.items():
        ok = sum(v for k, v in counts.items() if k.startswith("ok") or "->datacite" in k)
        log.info("%-18s %4d resolved of %4d   %s", repo, ok, sum(counts.values()),
                 ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if not args.dry_run:
        log.info("Lookup table: %s", out_path)
        log.info("Next step: python apply_created_column.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
