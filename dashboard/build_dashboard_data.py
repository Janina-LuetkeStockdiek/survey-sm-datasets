#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_dashboard_data.py
=======================
Builds the data file the dashboard reads: `dashboard/dataset_relevant.csv`.

The dashboard runs entirely in the browser through Shinylive and reads exactly
ONE flat CSV. The FAIR results therefore have to be joined onto the catalogue
beforehand. Were they to sit in a second file, the sidebar filters would not act
on the FAIR views, and acting on every view at once is the point of the dashboard.

Because the browser downloads the whole file, this does not carry over all 130
columns of fair_fuji.csv, only the ones the dashboard draws:

    fair_score              overall score in percent
    fair_F, fair_A,
    fair_I, fair_R          score per FAIR category, in percent
    fm_<metric id>          1 = passed, 0 = failed, empty = not assessed

Run it from the `dashboard` folder, after the FAIR assessment has produced
`data/fair_fuji.csv`:

    python ../fair/fuji_assessment.py
    python build_dashboard_data.py

Options:
    --catalog   input catalogue  (default: ../data/dataset_relevant.csv)
    --fair      FAIR results     (default: ../data/fair_fuji.csv)
    --out       output file      (default: ./dataset_relevant.csv)
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

SEP = ";"
CATEGORIES = ["F", "A", "I", "R"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    here = Path(__file__).resolve().parent
    parser.add_argument("--catalog", default=here.parent / "data" / "dataset_relevant.csv")
    parser.add_argument("--fair", default=here.parent / "data" / "fair_fuji.csv")
    parser.add_argument("--out", default=here / "dataset_relevant.csv")
    cfg = parser.parse_args()

    catalog_path, fair_path, out_path = Path(cfg.catalog), Path(cfg.fair), Path(cfg.out)
    for p in (catalog_path, fair_path):
        if not p.is_file():
            print(f"ERROR: not found: {p}", file=sys.stderr)
            return 1

    catalog = pd.read_csv(catalog_path, sep=SEP, dtype={"id": str}, low_memory=False)
    fair = pd.read_csv(fair_path, sep=SEP, dtype={"id": str}, low_memory=False)
    print(f"catalogue: {len(catalog)} rows")
    print(f"FAIR     : {len(fair)} rows, of which ok: {(fair.run_status == 'ok').sum()}")

    fair = fair[fair.run_status == "ok"].copy()

    slim = pd.DataFrame({"id": fair["id"].astype(str)})
    slim["fair_score"] = fair["fair_score_percent"].round(1)
    for c in CATEGORIES:
        slim[f"fair_{c}"] = fair[f"pct_{c}"].round(1)

    # Metric status as 0/1. Against "pass"/"fail" that saves roughly three
    # quarters of the bytes, and the dashboard only computes shares from it.
    status_cols = [c for c in fair.columns
                   if c.startswith("m_") and c.endswith("_status")]
    for c in sorted(status_cols):
        metric = c[len("m_"):-len("_status")]
        slim[f"fm_{metric}"] = fair[c].map({"pass": 1, "fail": 0}).astype("Int64")
    print(f"metrics  : {len(status_cols)}")

    merged = catalog.merge(slim, on="id", how="left")
    if len(merged) != len(catalog):
        print("ERROR: the join changed the row count -- duplicate ids?", file=sys.stderr)
        return 1

    missing = merged["fair_score"].isna().sum()
    print(f"without FAIR values: {missing} rows")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(out_path, sep=SEP, index=False)
    size_before = catalog_path.stat().st_size / 1024
    size_after = out_path.stat().st_size / 1024
    print(f"written: {out_path}")
    print(f"size: {size_before:.0f} KB -> {size_after:.0f} KB "
          f"(+{size_after - size_before:.0f} KB, +{(size_after / size_before - 1) * 100:.0f} %)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
