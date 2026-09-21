"""Join the backfilled creation dates onto the catalog files.

Reads the lookup table produced by ``backfill_created.py`` and inserts a
``created`` column directly after ``updated`` in every catalog file, in the
working copies under ``data/`` as well as in the mirrors under
``publication/zenodo_record/`` and ``programming/dashboard/`` wherever those
exist -- the published code repository holds neither, and there the script
simply patches the catalog files under ``data/``.  ``updated``
itself is never touched, so every figure and every number in the paper stays
valid until they are deliberately recomputed against the new column.

Everything is backed up first, into ``data/backup/13_created_column_<date>/``,
following the convention of the earlier cleaning passes.  The BOM rules of the
three locations are respected: no BOM under ``data/`` and in the dashboard
copy, BOM in the Zenodo record.

Usage
-----
    python apply_created_column.py --dry-run     # report only, writes nothing
    python apply_created_column.py               # back up, then write
    python apply_created_column.py --report-only # coverage and delta statistics
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

# config.py sits above the harvesting scripts in the project tree and at the
# root of the published repository. Resolved by walking up, so that one and the
# same file works in both layouts.
for _parent in Path(__file__).resolve().parents:
    if (_parent / "config.py").exists():
        sys.path.insert(0, str(_parent))
        break
from config import data_path

log = logging.getLogger("apply_created")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                                      datefmt="%H:%M:%S"))
    log.addHandler(_h)
log.setLevel(logging.INFO)

BOM = "﻿"

#: (path relative to the project root, has BOM, is xlsx).  Working copies only.
TARGETS: List[Tuple[str, bool, bool]] = [
    ("data/dataset_initial.csv", False, False),
    ("data/dataset_initial_dedup.csv", False, False),
    ("data/dataset_llm.csv", False, False),
    ("data/dataset_relevant.csv", False, False),
    ("data/dataset_relevant_fair.csv", False, False),
    ("data/dataset_initial.xlsx", False, True),
    ("data/dataset_relevant.xlsx", False, True),
]

#: (source under data/, mirror path, mirror needs a BOM).  These three files
#: are pure copies and are never maintained separately, so they are rebuilt
#: from data/ rather than patched on their own.  The Zenodo record is UTF-8
#: with BOM as its CODEBOOK documents; the dashboard copy must not have one,
#: because app.R would otherwise read the BOM into the first column name.
#: CAVEAT for the dashboard copy: it is not only a copy but a build artefact of
#: build_dashboard_data.py, which joins the FAIR columns onto the catalogue.
#: Rebuilding it here throws those columns away, so the run below ends with a
#: warning to rebuild it.  Do not silently drop that step.
MIRRORS: List[Tuple[str, str, bool]] = [
    ("dataset_initial.csv", "publication/zenodo_record/dataset_initial.csv", True),
    ("dataset_relevant.csv", "publication/zenodo_record/dataset_relevant.csv", True),
    ("dataset_relevant.csv", "programming/dashboard/dataset_relevant.csv", False),
]


def _norm_url(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.rstrip("/").str.lower()


def load_lookup(path: Path) -> pd.DataFrame:
    """Read the lookup table and reduce it to one row per URL."""
    df = pd.read_csv(path, sep=";", dtype=str).fillna("")
    df = df[df["created"].str.strip() != ""]
    df["key"] = _norm_url(df["url"])
    # A rerun appends, so the newest entry per URL wins.
    df = df.drop_duplicates(subset="key", keep="last")
    log.info("Lookup table: %d URLs with a creation date.", len(df))
    return df[["key", "created", "created_source"]]


def insert_after(df: pd.DataFrame, column: str, after: str) -> pd.DataFrame:
    """Move ``column`` so it sits directly behind ``after``."""
    cols = [c for c in df.columns if c != column]
    if after in cols:
        cols.insert(cols.index(after) + 1, column)
    else:
        cols.append(column)
    return df[cols]


def apply_to_frame(df: pd.DataFrame, lookup: pd.DataFrame) -> Tuple[pd.DataFrame, dict]:
    """Add the ``created`` column and report how far it reaches."""
    key = _norm_url(df["url"])
    mapping = dict(zip(lookup["key"], lookup["created"]))
    df = df.copy()
    df["created"] = key.map(mapping).fillna("")
    df = insert_after(df, "created", "updated")

    stats = {"rows": len(df), "filled": int((df["created"].str.strip() != "").sum())}
    if "repository" in df.columns:
        per_repo = (df.assign(has=df["created"].str.strip() != "")
                      .groupby("repository")["has"].agg(["sum", "size"]))
        stats["per_repo"] = per_repo
    return df, stats


def delta_report(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Compare ``created`` against ``updated`` -- the number that decides how
    much the temporal analysis actually changes."""
    if "created" not in df.columns or "updated" not in df.columns:
        return None
    both = df[(df["created"].astype(str).str.strip() != "")
              & (df["updated"].astype(str).str.strip() != "")].copy()
    if both.empty:
        return None
    c = pd.to_datetime(both["created"], errors="coerce", utc=True)
    u = pd.to_datetime(both["updated"], errors="coerce", utc=True)
    both["delta_days"] = (u - c).dt.total_seconds() / 86400
    both["same_year"] = c.dt.year.eq(u.dt.year)
    out = both.groupby("repository").agg(
        n=("delta_days", "size"),
        median_days=("delta_days", "median"),
        p90_days=("delta_days", lambda s: s.quantile(0.9)),
        same_year_share=("same_year", "mean"),
        negative=("delta_days", lambda s: int((s < -1).sum())),
    )
    return out.round(2)


def write_csv(df: pd.DataFrame, path: Path, bom: bool):
    encoding = "utf-8-sig" if bom else "utf-8"
    df.to_csv(path, sep=";", index=False, encoding=encoding)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lookup", default="created_backfill.csv",
                        help="lookup table from backfill_created.py (inside the data directory)")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument("--report-only", action="store_true",
                        help="print coverage and the created/updated delta, write nothing")
    parser.add_argument("--targets", default="",
                        help="comma-separated subset of the target files (default: all)")
    parser.add_argument("--no-mirror", action="store_true",
                        help="do not refresh the Zenodo-record and dashboard copies")
    args = parser.parse_args(argv)

    data_dir = Path(data_path("."))
    root = data_dir.parent
    lookup_path = Path(data_path(args.lookup))
    if not lookup_path.exists():
        log.error("Lookup table not found: %s -- run backfill_created.py first.", lookup_path)
        return 2
    lookup = load_lookup(lookup_path)

    targets = TARGETS
    if args.targets:
        wanted = {t.strip() for t in args.targets.split(",")}
        targets = [t for t in TARGETS if t[0] in wanted or Path(t[0]).name in wanted]

    dry = args.dry_run or args.report_only
    backup_dir = data_dir / "backup" / f"13_created_column_{pd.Timestamp.now():%Y%m%d}"
    if not dry:
        backup_dir.mkdir(parents=True, exist_ok=True)
        log.info("Backup: %s", backup_dir)

    for rel, bom, is_xlsx in targets:
        path = root / rel
        if not path.exists():
            log.warning("skipped, not found: %s", rel)
            continue

        sheet_name = None
        if is_xlsx:
            with pd.ExcelFile(path) as xl:
                sheet_name = xl.sheet_names[0]
                df = xl.parse(sheet_name, dtype=str).fillna("")
        else:
            df = pd.read_csv(path, sep=";", dtype=str).fillna("")
            df.columns = [c.lstrip(BOM) for c in df.columns]

        if "url" not in df.columns:
            log.warning("skipped, no url column: %s", rel)
            continue

        new_df, stats = apply_to_frame(df, lookup)
        log.info("%-52s %5d rows, %5d with a creation date (%.1f%%)",
                 rel, stats["rows"], stats["filled"],
                 100 * stats["filled"] / max(stats["rows"], 1))

        if args.report_only and "per_repo" in stats:
            for repo, r in stats["per_repo"].iterrows():
                log.info("    %-20s %4d / %4d", repo, int(r["sum"]), int(r["size"]))
            rep = delta_report(new_df)
            if rep is not None:
                log.info("    created vs. updated (positive = updated is later):\n%s",
                         rep.to_string())

        if dry:
            continue

        shutil.copy2(path, backup_dir / f"{rel.replace('/', '__')}")
        if is_xlsx:
            # keep the original sheet name, dataset_relevant.xlsx uses
            # 'dataset_relevant_clean' rather than the pandas default
            new_df.to_excel(path, index=False, sheet_name=sheet_name or "Sheet1")
        else:
            write_csv(new_df, path, bom)

    if not dry and not args.no_mirror and not args.targets:
        for src_name, mirror_rel, mirror_bom in MIRRORS:
            src = data_dir / src_name
            dst = root / mirror_rel
            if not src.exists():
                log.warning("mirror skipped, source missing: %s", src_name)
                continue
            if not dst.parent.exists():
                # The published repository holds none of these mirrors. Their
                # absence is normal there and must not create a stray folder.
                log.info("mirror skipped, not part of this tree: %s", mirror_rel)
                continue
            if dst.exists():
                shutil.copy2(dst, backup_dir / mirror_rel.replace("/", "__"))
            frame = pd.read_csv(src, sep=";", dtype=str).fillna("")
            frame.columns = [c.lstrip(BOM) for c in frame.columns]
            write_csv(frame, dst, mirror_bom)
            log.info("%-52s rebuilt from data/%s%s", mirror_rel, src_name,
                     " (with BOM)" if mirror_bom else "")
            if mirror_rel.startswith("programming/dashboard/"):
                log.warning("the dashboard copy is now the PLAIN catalogue, without the "
                            "FAIR columns -- run 'python programming/dashboard/build_dashboard_data.py' "
                            "to restore them, otherwise the FAIR views go blank")

    if dry:
        log.info("Nothing written (dry run).")
    else:
        log.info("Done. Backups in %s", backup_dir)
        log.info("Reminder: the figures and the year-based numbers in the paper still "
                 "rest on 'updated' -- rerun them deliberately, not by accident.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
