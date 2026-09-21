"""Download the Meta Kaggle files needed by ``backfill_created.py``.

Deliberately does *not* use ``KaggleApi.dataset_download_file``: version 1.7.x
of the client raises ``KaggleHttpClient.call() got an unexpected keyword
argument 'headers'`` as soon as a download resumes, and it drops the file
under a name of its own choosing.  This script talks to the same REST endpoint
directly with ``requests`` and HTTP basic auth, so the output path is known and
the client version does not matter.

Files fetched from ``kaggle/meta-kaggle``:

    Datasets.csv          one row per public dataset with its CreationDate.
                          About 95 MB, this is the one that matters -- it
                          resolves roughly 2,360 of the 2,390 Kaggle records.
    DatasetVersions.csv   about 1.3 GB, only needed for the last ~30 records
                          that have no numeric Kaggle id left in the raw
                          harvests and have to be matched by owner and slug.
    Users.csv             goes with DatasetVersions.csv, to tell apart
                          datasets that share a slug.

Usage
-----
    python fetch_meta_kaggle.py                    # Datasets.csv only
    python fetch_meta_kaggle.py --all              # all three, ~1.5 GB
    python fetch_meta_kaggle.py --only Users.csv
    python fetch_meta_kaggle.py --dest /some/where --force

Credentials
-----------
Read from ``~/.kaggle/kaggle.json`` (Kaggle profile -> Settings -> API ->
Create New Token) or from ``KAGGLE_USERNAME`` and ``KAGGLE_KEY`` in the
environment.  The project's ``.env`` holds only ``KAGGLE_KEY``, so on its own
it is not enough.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import requests

DATASET_OWNER = "kaggle"
DATASET_SLUG = "meta-kaggle"
ENDPOINT = f"https://www.kaggle.com/api/v1/datasets/download/{DATASET_OWNER}/{DATASET_SLUG}"

#: file name -> rough download size, for the progress line
FILES = {
    "Datasets.csv": "~95 MB",
    "DatasetVersions.csv": "~1.3 GB",
    "Users.csv": "~100 MB",
}
DEFAULT_FILES = ["Datasets.csv"]

ZIP_MAGIC = b"PK\x03\x04"


def credentials() -> tuple:
    """Return ``(username, key)`` from kaggle.json or the environment."""
    user = os.getenv("KAGGLE_USERNAME")
    key = os.getenv("KAGGLE_KEY")
    if user and key:
        return user, key

    path = Path(os.getenv("KAGGLE_CONFIG_DIR", "~/.kaggle")).expanduser() / "kaggle.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            sys.exit(f"{path} is not readable as JSON: {exc}")
        user = user or data.get("username")
        key = key or data.get("key")
        if user and key:
            return user, key

    sys.exit(
        "No Kaggle credentials found.\n"
        f"Expected {path}, or both KAGGLE_USERNAME and KAGGLE_KEY in the "
        "environment.\nThe project's .env holds only KAGGLE_KEY, which is not "
        "enough on its own -- add KAGGLE_USERNAME=<your kaggle user name> there, "
        "or create the token file under Kaggle -> Settings -> API."
    )


def human(n: int) -> str:
    return f"{n / 1024 / 1024:,.1f} MB"


def download_one(session: requests.Session, file_name: str, dest: Path,
                 force: bool) -> bool:
    """Stream one file into ``dest`` and unzip it when it arrives zipped."""
    target = dest / file_name
    if target.exists() and not force:
        print(f"  {file_name:<22} already there ({human(target.stat().st_size)}), skipped")
        return True

    tmp = dest / f".{file_name}.part"
    print(f"  {file_name:<22} downloading, {FILES.get(file_name, '')} ...")
    try:
        with session.get(ENDPOINT, params={"file_name": file_name},
                         stream=True, timeout=(10, 300)) as r:
            if r.status_code == 401:
                sys.exit("  Kaggle rejected the credentials (401). Check kaggle.json.")
            if r.status_code == 404:
                print(f"  {file_name:<22} not offered by the dataset (404)")
                return False
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = 100 * done / total
                        print(f"\r  {file_name:<22} {human(done)} / {human(total)} "
                              f"({pct:5.1f} %)", end="", flush=True)
                    else:
                        print(f"\r  {file_name:<22} {human(done)}", end="", flush=True)
            print()
    except KeyboardInterrupt:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        print(f"  {file_name:<22} failed: {type(exc).__name__}: {exc}")
        return False

    # Kaggle serves a single file either raw or wrapped in a zip archive.
    with open(tmp, "rb") as fh:
        is_zip = fh.read(4) == ZIP_MAGIC

    if is_zip:
        with zipfile.ZipFile(tmp) as zf:
            names = zf.namelist()
            member = file_name if file_name in names else (names[0] if names else None)
            if member is None:
                print(f"  {file_name:<22} the archive is empty")
                tmp.unlink(missing_ok=True)
                return False
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        tmp.unlink(missing_ok=True)
    else:
        tmp.replace(target)

    print(f"  {file_name:<22} ready ({human(target.stat().st_size)})")
    return True


def cleanup(dest: Path):
    """Remove the leftovers an aborted client download may have left behind."""
    junk = [p for p in dest.iterdir()
            if p.is_file() and (p.suffix == ".zip"
                                or p.name.endswith(".part")
                                or p.name in ("DownloadDataset", "archive"))]
    for p in junk:
        print(f"  removing leftover {p.name} ({human(p.stat().st_size)})")
        p.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", default="~/Downloads/meta-kaggle",
                        help="target directory (default: ~/Downloads/meta-kaggle)")
    parser.add_argument("--only", default="",
                        help="comma-separated subset, e.g. 'DatasetVersions.csv,Users.csv'")
    parser.add_argument("--all", action="store_true",
                        help="fetch all three files (about 1.5 GB) instead of Datasets.csv only")
    parser.add_argument("--force", action="store_true",
                        help="download again even if the file is already there")
    parser.add_argument("--clean", action="store_true",
                        help="remove zip and partial-download leftovers first")
    args = parser.parse_args(argv)

    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    if args.only:
        wanted = [f.strip() for f in args.only.split(",") if f.strip()]
    elif args.all:
        wanted = list(FILES)
    else:
        wanted = list(DEFAULT_FILES)
    unknown = [f for f in wanted if f not in FILES]
    if unknown:
        sys.exit(f"Unknown file(s): {', '.join(unknown)}. Known: {', '.join(FILES)}")

    print(f"Meta Kaggle -> {dest}")
    if args.clean:
        cleanup(dest)

    user, key = credentials()
    session = requests.Session()
    session.auth = (user, key)
    session.headers.update({"User-Agent": "sota-dataset-backfill/1.0"})

    ok = all([download_one(session, f, dest, args.force) for f in wanted])

    print("\nNext step:")
    print(f"  python3 backfill_created.py --repos Kaggle --meta-kaggle-dir {dest}")
    if "DatasetVersions.csv" not in wanted and not (dest / "DatasetVersions.csv").exists():
        print("\n  Without DatasetVersions.csv about 30 of the 2,390 Kaggle records stay")
        print("  unresolved. Fetch it with --only DatasetVersions.csv,Users.csv if you")
        print("  want those too.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
