"""Download the published catalogue from Zenodo into ``data/``.

The CSV files are not kept in this repository — they are published as a citable
Zenodo record instead, and ``data/`` is git-ignored. This script fetches them so
that the analysis and evaluation steps can run against the same frozen snapshot
the paper is based on.

Usage::

    python download_data.py                 # fetch every file of the record
    python download_data.py --record 123456 # fetch a specific record
    python download_data.py --force         # re-download files that already exist

The record ID is read from ``ZENODO_RECORD_ID`` below, or from the environment
variable of the same name, or from ``--record``.
"""

import argparse
import os
import sys
from pathlib import Path

import requests

# Zenodo record holding the published catalogue. Replace the placeholder once the
# record has been created; you can reserve the DOI before publishing.
ZENODO_RECORD_ID = os.getenv("ZENODO_RECORD_ID", "")

ZENODO_API = "https://zenodo.org/api/records/{record_id}"
DATA_DIR = Path(__file__).resolve().parent / "data"

# Files the pipeline expects. Anything else in the record is downloaded too, but
# a missing entry from this list is reported as a warning.
EXPECTED_FILES = [
    "dataset_relevant.csv",
    "dataset_initial.csv",
    "development_set.csv",
    "heldout_test_set.csv",
]


def fetch_record(record_id: str, timeout: int = 30) -> dict:
    """Fetch a Zenodo record's metadata.

    Args:
        record_id: The numeric Zenodo record ID.
        timeout: Request timeout in seconds.

    Returns:
        The record as returned by the Zenodo API.

    Raises:
        SystemExit: If the record cannot be retrieved.
    """
    try:
        response = requests.get(ZENODO_API.format(record_id=record_id), timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        sys.exit(f"Could not fetch Zenodo record {record_id}: {exc}")


def download_file(url: str, target: Path, timeout: int = 120) -> None:
    """Stream one file to disk.

    Args:
        url: Direct download URL.
        target: Where to write the file.
        timeout: Request timeout in seconds.
    """
    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 16):
                handle.write(chunk)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", default=ZENODO_RECORD_ID,
                        help="Zenodo record ID holding the catalogue")
    parser.add_argument("--force", action="store_true",
                        help="Re-download files that are already present")
    args = parser.parse_args()

    if not args.record:
        sys.exit(
            "No Zenodo record ID configured. Set ZENODO_RECORD_ID in your "
            "environment, edit download_data.py, or pass --record."
        )

    record = fetch_record(args.record)
    files = record.get("files", [])
    if not files:
        sys.exit(f"Zenodo record {args.record} contains no files.")

    print(f"Record {args.record}: {record.get('metadata', {}).get('title', '')}")

    downloaded = []
    for entry in files:
        name = entry.get("key") or entry.get("filename", "")
        url = (entry.get("links") or {}).get("self", "")
        if not name or not url:
            continue

        target = DATA_DIR / name
        if target.exists() and not args.force:
            print(f"  skip     {name} (already present)")
            downloaded.append(name)
            continue

        print(f"  fetching {name} ...")
        download_file(url, target)
        downloaded.append(name)

    missing = [f for f in EXPECTED_FILES if f not in downloaded]
    if missing:
        print(f"\nWarning: expected but not found in the record: {', '.join(missing)}")

    print(f"\nData in {DATA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
