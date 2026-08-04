"""Shared building blocks for the repository harvesting scripts.

Every script in ``harvesting/`` queries a different API, but they all do the same
four things around that query: build an HTTP session that retries politely, keep
only English records, reduce a repository-specific response to one common schema,
and write a CSV. Those four things live here, so that the per-repository scripts
contain only what is genuinely repository-specific.

The common schema is :data:`OUTPUT_COLUMNS`. It holds exactly two kinds of field:

- the bibliographic metadata reported in the paper (``id``, ``doi``, ``url``,
  ``updated``, ``title``, ``description``), which is what survives into the
  merged catalogue, and
- the three fields the filters are based on (``language``, ``total_file_size``,
  ``file_types``), kept so that a filtering decision can be checked after the
  fact.

Everything else a repository happens to return — creator names, affiliations,
download and view counts, stars, keywords, subtitles, internal revision numbers —
is dropped at the end of each script. It is not used in the analysis and is not
reported in the paper.
"""

import os
import re
from functools import lru_cache
from typing import Iterable, Optional, Sequence

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import FASTTEXT_MODEL_PATH, data_path
from query_specs import DATA_FORMATS, MIN_CREATED_DATE, MIN_SIZE_BYTES, is_relevant_file

# --------------------------------------------------------------------------- #
# Common output schema
# --------------------------------------------------------------------------- #

# Bibliographic metadata recorded for every dataset, as reported in the paper.
METADATA_COLUMNS = ["id", "doi", "url", "updated", "title", "description"]

# Fields the filters operate on. Retained so that the reason a record was kept or
# dropped remains inspectable; not carried into the merged catalogue.
FILTER_COLUMNS = ["language", "total_file_size", "file_types"]

OUTPUT_COLUMNS = METADATA_COLUMNS + FILTER_COLUMNS

# Language codes treated as English. Repositories are inconsistent about which
# standard they use, and fastText returns two-letter codes.
ENGLISH_CODES = {"en", "eng", "en-us", "en-gb", "english"}

# Retry policy shared by every script: five attempts with exponential backoff on
# throttling and transient server errors, as described in the paper.
RETRY_TOTAL = 5
RETRY_BACKOFF_FACTOR = 1.5
RETRY_STATUS = (429, 500, 502, 503, 504)

# Politeness delays, also as described in the paper.
REQUEST_DELAY_S = 0.5
QUERY_DELAY_S = 2.0

USER_AGENT = "social-media-dataset-survey/1.0"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def make_session(
    token: Optional[str] = None,
    token_env: Optional[str] = None,
    headers: Optional[dict] = None,
) -> requests.Session:
    """Build a requests session with the shared retry and backoff policy.

    Args:
        token: Bearer token to send. If None and ``token_env`` is given, the
            token is read from that environment variable.
        token_env: Name of the environment variable holding the token.
        headers: Additional headers to merge in, e.g. an API-specific ``Accept``.

    Returns:
        A configured session. Retries are mounted for HTTPS only.
    """
    retry = Retry(
        total=RETRY_TOTAL,
        connect=3,
        read=3,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUS,
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10))
    session.headers.update({"User-Agent": USER_AGENT})

    if headers:
        session.headers.update(headers)

    if token is None and token_env:
        token = os.getenv(token_env)
    if token:
        session.headers["Authorization"] = f"Bearer {token}"

    return session


# --------------------------------------------------------------------------- #
# Language identification
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def load_language_model():
    """Load the fastText language-identification model once per process.

    Returns:
        The loaded ``lid.176.bin`` model.

    Raises:
        RuntimeError: If ``FASTTEXT_MODEL_PATH`` is unset or does not exist.
    """
    import fasttext

    if not FASTTEXT_MODEL_PATH or not os.path.exists(FASTTEXT_MODEL_PATH):
        raise RuntimeError(
            "fastText model not found. Download lid.176.bin from "
            "https://fasttext.cc/docs/en/language-identification.html and set "
            "FASTTEXT_MODEL_PATH in your .env file."
        )
    return fasttext.load_model(FASTTEXT_MODEL_PATH)


def detect_language(texts: Sequence[str]) -> list[str]:
    """Predict the language of each text with fastText.

    Args:
        texts: Texts to classify. Newlines are stripped, as fastText rejects them.

    Returns:
        A list of two-letter language codes, one per input text.
    """
    if len(texts) == 0:
        return []

    model = load_language_model()
    cleaned = [str(t).replace("\n", " ") if pd.notna(t) else "" for t in texts]
    labels, _probs = model.predict(cleaned, k=1)
    return [label[0].replace("__label__", "") for label in labels]


def fill_missing_language(df: pd.DataFrame) -> pd.DataFrame:
    """Fill blank ``language`` values by running fastText on the description.

    The repository's own language metadata is trusted where present; fastText is
    only used as a fallback, because it is unreliable on very short texts. The
    description is preferred over the title for the same reason.

    Args:
        df: Table with ``language`` and at least one of ``description``/``title``.

    Returns:
        A copy of ``df`` with ``language`` filled in where it was blank.
    """
    if df.empty:
        return df

    df = df.copy()
    if "language" not in df.columns:
        df["language"] = ""

    df["language"] = df["language"].fillna("").astype(str).str.strip()
    missing = df["language"].eq("") | df["language"].str.lower().eq("nan")
    if not missing.any():
        return df

    source = df.loc[missing, "description"]
    if "title" in df.columns:
        source = source.fillna(df.loc[missing, "title"])

    df.loc[missing, "language"] = detect_language(source.fillna("").tolist())
    return df


def is_english(series: pd.Series) -> pd.Series:
    """Return a boolean mask of the rows whose language counts as English.

    Args:
        series: The ``language`` column.

    Returns:
        A boolean Series, True where the value is an English language code.
    """
    return series.fillna("").astype(str).str.strip().str.lower().isin(ENGLISH_CODES)


# --------------------------------------------------------------------------- #
# File-level filtering
# --------------------------------------------------------------------------- #

def summarise_files(filenames: Iterable[str], sizes: Iterable[int]) -> dict:
    """Aggregate a dataset's file list into the shared filter fields.

    Args:
        filenames: File names belonging to one dataset.
        sizes: Byte sizes in the same order.

    Returns:
        A dict with ``total_file_size`` (int), ``file_types`` (space-separated
        extensions) and ``has_relevant_file`` (bool).
    """
    filenames = list(filenames)
    sizes = list(sizes)

    extensions = []
    has_relevant = False
    for name in filenames:
        name = str(name)
        if "." in name:
            extensions.append(name.rsplit(".", 1)[-1].lower())
        if is_relevant_file(name):
            has_relevant = True

    total = 0
    for size in sizes:
        try:
            total += int(size)
        except (TypeError, ValueError):
            continue

    return {
        "total_file_size": total,
        "file_types": " ".join(sorted(set(extensions))),
        "has_relevant_file": has_relevant,
    }


def file_types_are_relevant(file_types: str) -> bool:
    """Return True if a space-separated extension list contains a data format.

    Used by the OAI-PMH harvesters, which get extensions from the metadata rather
    than from a real file listing.

    Args:
        file_types: Space- or comma-separated file extensions.

    Returns:
        True if at least one extension is in ``DATA_FORMATS``.
    """
    allowed = {fmt.lower().lstrip(".") for fmt in DATA_FORMATS}
    tokens = re.split(r"[,\s]+", str(file_types).lower())
    return any(token.lstrip(".") in allowed for token in tokens if token)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def standardise(df: pd.DataFrame, rename: Optional[dict] = None) -> pd.DataFrame:
    """Reduce a repository-specific table to the common output schema.

    Columns are renamed, restricted to :data:`OUTPUT_COLUMNS` and put in that
    order. Columns the repository does not provide are created empty, so every
    harvest CSV has an identical header regardless of source.

    Args:
        df: The repository-specific table.
        rename: Mapping from the repository's column names to the common ones.

    Returns:
        A table with exactly the columns of :data:`OUTPUT_COLUMNS`.
    """
    if df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    df = df.rename(columns=rename or {})
    df = df.loc[:, ~df.columns.duplicated()]

    for column in OUTPUT_COLUMNS:
        if column not in df.columns:
            df[column] = ""

    return df[OUTPUT_COLUMNS].reset_index(drop=True)


def finalise(
    df: pd.DataFrame,
    *,
    deduplicate_by: str = "id",
    require_description: bool = True,
    apply_size_filter: bool = True,
    apply_language_filter: bool = True,
) -> pd.DataFrame:
    """Apply the filters every repository shares and sort the result.

    In order: drop records without an identifier, without a title or without a
    description; drop records below ``MIN_SIZE_BYTES``; drop records updated
    before ``MIN_CREATED_DATE``; fill missing language values with fastText and
    keep only English; deduplicate; sort by update date.

    Records whose size is unknown are kept, since an absent size is a gap in the
    repository's metadata rather than evidence that the dataset is too small.

    Args:
        df: Table in the common output schema.
        deduplicate_by: Column to deduplicate on.
        require_description: Drop records with an empty description.
        apply_size_filter: Apply the ``MIN_SIZE_BYTES`` threshold.
        apply_language_filter: Run the fastText fallback and keep English only.

    Returns:
        The filtered, deduplicated and sorted table.
    """
    if df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    df = df.copy()

    # Records without a title, description or identifier cannot be screened.
    df = df[df["id"].astype(str).str.strip().ne("")]
    df = df[df["title"].fillna("").astype(str).str.strip().ne("")]
    if require_description:
        df = df[df["description"].fillna("").astype(str).str.strip().ne("")]

    if apply_size_filter:
        sizes = pd.to_numeric(df["total_file_size"], errors="coerce")
        df = df[(sizes >= MIN_SIZE_BYTES) | sizes.isna()]

    updated = pd.to_datetime(df["updated"], errors="coerce", utc=True, format="mixed")
    min_date = pd.to_datetime(MIN_CREATED_DATE, utc=True)
    df = df[updated.isna() | (updated >= min_date)]

    if apply_language_filter:
        df = fill_missing_language(df)
        df = df[is_english(df["language"])]

    if deduplicate_by and deduplicate_by in df.columns:
        df = df.drop_duplicates(subset=deduplicate_by, keep="last")

    df = df.assign(_sort=pd.to_datetime(df["updated"], errors="coerce", utc=True, format="mixed"))
    df = df.sort_values("_sort", na_position="first").drop(columns="_sort")

    return df.reset_index(drop=True)


def write_output(df: pd.DataFrame, filename: str) -> str:
    """Write a harvest result to CSV in the format the merge step expects.

    Semicolon-separated and UTF-8 with BOM, matching the published data files.

    Args:
        df: Table in the common output schema.
        filename: File name inside ``DATA_OUTPUT_DIR``, e.g. ``"zenodo.csv"``.

    Returns:
        The full path the file was written to.
    """
    path = data_path(filename)
    df.to_csv(path, index=False, sep=";", encoding="utf-8-sig")
    print(f"Wrote {len(df)} records to {path}")
    return path


def build_queries(platforms: Sequence[str], text_terms: Sequence[str], boolean: bool = True) -> list[str]:
    """Build the search queries for a repository.

    Args:
        platforms: Platform search terms.
        text_terms: Content search terms.
        boolean: True if the API supports Boolean operators, in which case one
            query per platform is produced. False produces the Cartesian product.

    Returns:
        The list of query strings — 23 with Boolean support, 644 without.
    """
    if boolean:
        joined = f"({' OR '.join(text_terms)})"
        return [f"{platform} AND {joined}" for platform in platforms]
    return [f"{platform} AND {term}" for platform in platforms for term in text_terms]
