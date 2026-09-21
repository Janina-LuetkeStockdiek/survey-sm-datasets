"""Reliability of the feature annotation: Cohen's Kappa (binary and categorical)
and ICC(2,1) on log10(number_posts).

Annotator 1 = the catalogue (``dataset_feature_agreement_key.csv``)
Annotator 2 = the second annotation (``dataset_feature_agreement_ta.xlsx``)

The key file also carries the second annotator's labels in its ``_annotator_2``
columns, so that the record is readable on its own. They are a copy of the
workbook and are ignored here -- the workbook stays the single source, so that
the numbers reported in the paper are reproduced from the material the second
annotator actually handed over.

Stratum ``main`` (n = 200, SRS from the 1,997 catalogued datasets) carries every
Kappa and the ICC. Stratum ``synthetic`` (n = 34) is NOT pooled with it. The
feature ``synthetic`` is instead reported as a confirmation rate over all 39
records labelled positive by annotator 1.

``platform`` is excluded (term matching, not a coding decision) and so is
``topic`` (590 values, effectively free text) -- ``macro_topic`` stands in for it.

Both input files are looked up relative to this script, so the working directory
does not matter and the same file runs in the project tree and in the published
repository.

Usage::

    python3 feature_agreement.py [--missing pairwise|disagree] [--json PATH]
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

KEY_FILE = "dataset_feature_agreement_key.csv"
TA_FILE = "dataset_feature_agreement_ta.xlsx"
BINARY = ["raw", "timestamp", "labeled", "collection_described", "code"]
CATEGORICAL = ["macro_topic", "task", "license"]
LABELS = {
    "raw": "Raw text",
    "timestamp": "Timestamp",
    "labeled": "Labeled",
    "collection_described": "Collection described",
    "code": "Code",
    "paper": "Paper",
    "synthetic": "Synthetic",
    "macro_topic": "Macro topic",
    "task": "Task",
    "license": "License",
}


def data_dir():
    """Locate the folder holding both input files.

    Walks up from this script and accepts either layout: ``data/`` next to the
    scripts, as in the published repository, or ``data/feature_agreement/`` in
    the project tree.

    Returns:
        The directory containing the key file and the workbook.

    Raises:
        SystemExit: If neither layout is found.
    """
    here = Path(__file__).resolve()
    for base in [here.parent, *here.parents]:
        for cand in (base / "data" / "feature_agreement", base / "data"):
            if (cand / KEY_FILE).exists():
                return cand
    sys.exit(
        f"{KEY_FILE} not found. Fetch the record with `python3 download_data.py` "
        "first, or run this script from inside the project tree."
    )


# ----------------------------------------------------------------------
# Loading and harmonising
# ----------------------------------------------------------------------
def norm_url(s):
    return s.astype(str).str.strip().str.lower().str.rstrip("/")


def to_bool(s):
    """True/False -> 1/0, everything else (empty, UNREACHABLE) -> NaN."""
    s = s.astype(str).str.strip().str.lower()
    return s.map({"true": 1.0, "false": 0.0}).where(s.isin(["true", "false"]))


def paper_to_bool(s):
    """The catalogue holds an identifier, the second annotator sometimes an
    identifier and sometimes False. Binarised to 'a publication exists'."""
    s = s.astype(str).str.strip()
    low = s.str.lower()
    out = np.where(low.isin(["", "nan", "none", "false"]), 0.0, 1.0)
    out = np.where(low.isin(["", "nan", "none"]), np.nan, out)
    return pd.Series(out, index=s.index, dtype="float")


def clean_cat(s):
    s = s.astype(str).str.strip()
    return s.where(~s.str.lower().isin(["", "nan", "none", "unreachable"]))


def load():
    path = data_dir()
    a1 = pd.read_csv(path / KEY_FILE, sep=";")
    a1 = a1[[c for c in a1.columns if not c.endswith("_annotator_2")]]
    a1.columns = [c.replace("_annotator_1", "") for c in a1.columns]
    a2 = pd.read_excel(path / TA_FILE)
    a2.columns = [c.strip() for c in a2.columns]

    for d in (a1, a2):
        d["url"] = norm_url(d["url"])
        for c in BINARY + ["synthetic"]:
            d[c] = to_bool(d[c])
        d["paper"] = paper_to_bool(d["paper"])
        for c in CATEGORICAL:
            d[c] = clean_cat(d[c])
        d["number_posts"] = pd.to_numeric(d["number_posts"], errors="coerce")

    m = a1.merge(a2, on="url", suffixes=("_a1", "_a2"), how="inner")
    assert len(m) == len(a1) == len(a2), f"Merge loses rows: {len(m)}"
    return m


# ----------------------------------------------------------------------
# Cohen's Kappa with asymptotic CI (Fleiss, Cohen & Everitt 1969)
# ----------------------------------------------------------------------
def kappa_ci(a, b, alpha=0.05):
    a, b = np.asarray(a, dtype=object), np.asarray(b, dtype=object)
    n = len(a)
    cats = sorted(set(a) | set(b), key=str)
    idx = {c: i for i, c in enumerate(cats)}
    k = len(cats)

    p = np.zeros((k, k))
    for x, y in zip(a, b):
        p[idx[x], idx[y]] += 1
    p /= n

    row, col = p.sum(axis=1), p.sum(axis=0)
    po = np.trace(p)
    pe = float(row @ col)
    if pe >= 1.0:
        return po, np.nan, np.nan, np.nan, np.nan
    kp = (po - pe) / (1 - pe)

    t1 = sum(p[i, i] * (1 - (row[i] + col[i]) * (1 - kp)) ** 2 for i in range(k))
    t2 = (1 - kp) ** 2 * sum(
        p[i, j] * (col[i] + row[j]) ** 2
        for i in range(k)
        for j in range(k)
        if i != j
    )
    t3 = (kp - pe * (1 - kp)) ** 2
    var = (t1 + t2 - t3) / (n * (1 - pe) ** 2)
    se = float(np.sqrt(max(var, 0.0)))
    z = stats.norm.ppf(1 - alpha / 2)
    return po, kp, se, kp - z * se, kp + z * se


def cells(a, b):
    a, b = np.asarray(a, dtype=int), np.asarray(b, dtype=int)
    return (
        int(((a == 1) & (b == 1)).sum()),
        int(((a == 1) & (b == 0)).sum()),
        int(((a == 0) & (b == 1)).sum()),
        int(((a == 0) & (b == 0)).sum()),
    )


# ----------------------------------------------------------------------
# ICC(2,1), absolute agreement, single measure (Shrout & Fleiss 1979)
# ----------------------------------------------------------------------
def icc21(x, y, alpha=0.05):
    m = np.column_stack([np.asarray(x, float), np.asarray(y, float)])
    n, k = m.shape
    grand = m.mean()
    rows, colm = m.mean(axis=1), m.mean(axis=0)

    sst = ((m - grand) ** 2).sum()
    ssr = k * ((rows - grand) ** 2).sum()
    ssc = n * ((colm - grand) ** 2).sum()
    sse = sst - ssr - ssc

    msr = ssr / (n - 1)
    msc = ssc / (k - 1)
    mse = sse / ((n - 1) * (k - 1))

    icc = (msr - mse) / (msr + (k - 1) * mse + k * (msc - mse) / n)

    a = k * icc / (n * (1 - icc))
    b = 1 + k * icc * (n - 1) / (n * (1 - icc))
    v = (a * msc + b * mse) ** 2 / (
        (a * msc) ** 2 / (k - 1) + (b * mse) ** 2 / ((n - 1) * (k - 1))
    )
    f_lo = stats.f.ppf(1 - alpha / 2, n - 1, v)
    f_up = stats.f.ppf(1 - alpha / 2, v, n - 1)
    lo = n * (msr - f_lo * mse) / (f_lo * (k * msc + (k * n - k - n) * mse) + n * msr)
    up = n * (f_up * msr - mse) / (k * msc + (k * n - k - n) * mse + n * f_up * msr)
    return icc, lo, up


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--missing", choices=["pairwise", "disagree"], default="pairwise")
    ap.add_argument("--json", metavar="PATH")
    args = ap.parse_args()

    m = load()
    main_s = m[m.stratum == "main"].copy()
    print(f"{len(m)} pairs in total -- stratum main {len(main_s)}, "
          f"synthetic {int((m.stratum == 'synthetic').sum())}")

    unreach = m[m.note.astype(str).str.upper().str.contains("UNREACH", na=False)]
    print(f"UNREACHABLE for annotator 2: {len(unreach)} records "
          f"({int((unreach.stratum == 'main').sum())} of them in stratum main)")
    if args.missing == "disagree":
        print("Mode: missing second-annotator values count as a disagreement "
              "(A2 := the opposite of A1)")
    else:
        print("Mode: pairwise deletion, n reported per feature")
    print()

    out = {}

    print("BINARY (stratum main)")
    hdr = (f"{'feature':<22}{'n':>4}{'p_o':>7}{'kappa':>8}{'95 % CI':>18}"
           f"{'PABAK':>8}   {'TT':>4}{'T.F':>5}{'F.T':>5}{'FF':>5}")
    print(hdr)
    print("-" * len(hdr))
    for c in BINARY + ["paper"]:
        s = main_s[[f"{c}_a1", f"{c}_a2"]].copy()
        if args.missing == "disagree":
            s[f"{c}_a2"] = s[f"{c}_a2"].fillna(1 - s[f"{c}_a1"])
        s = s.dropna()
        a, b = s[f"{c}_a1"].astype(int), s[f"{c}_a2"].astype(int)
        po, kp, se, lo, hi = kappa_ci(a, b)
        tt, tf, ft, ff = cells(a, b)
        print(f"{LABELS[c]:<22}{len(s):>4}{po:>7.3f}{kp:>8.3f}"
              f"{f'[{lo:.3f}, {hi:.3f}]':>18}{2 * po - 1:>8.3f}   "
              f"{tt:>4}{tf:>5}{ft:>5}{ff:>5}")
        out[c] = dict(n=len(s), po=po, kappa=kp, se=se, lo=lo, hi=hi,
                      pabak=2 * po - 1, cells=[tt, tf, ft, ff])

    syn = m[m.synthetic_a1 == 1]
    syn_ok = syn.dropna(subset=["synthetic_a2"])
    conf = int((syn_ok.synthetic_a2 == 1).sum())
    print(f"\nSYNTHETIC (all {len(syn)} records labelled positive by annotator 1)")
    print(f"  confirmed by A2        {conf} of {len(syn_ok)} "
          f"= {100 * conf / len(syn_ok):.1f} %")
    print(f"  not judged by A2       {len(syn) - len(syn_ok)} (UNREACHABLE)")
    fp = m[(m.synthetic_a1 == 0) & (m.synthetic_a2 == 1)]
    print(f"  A2 positive, A1 negative {len(fp)}")
    out["synthetic"] = dict(n_positives=len(syn), n_judged=len(syn_ok),
                            confirmed=conf, rate=conf / len(syn_ok),
                            a2_only=len(fp))

    print("\nCATEGORICAL (stratum main)")
    hdr = f"{'feature':<22}{'n':>4}{'cat.':>6}{'p_o':>8}{'kappa':>8}{'95 % CI':>18}"
    print(hdr)
    print("-" * len(hdr))
    for c in CATEGORICAL:
        s = main_s[[f"{c}_a1", f"{c}_a2"]].dropna()
        a, b = s[f"{c}_a1"], s[f"{c}_a2"]
        po, kp, se, lo, hi = kappa_ci(a, b)
        ncat = len(set(a) | set(b))
        print(f"{LABELS[c]:<22}{len(s):>4}{ncat:>6}{po:>8.3f}{kp:>8.3f}"
              f"{f'[{lo:.3f}, {hi:.3f}]':>18}")
        out[c] = dict(n=len(s), n_cat=ncat, po=po, kappa=kp, se=se, lo=lo, hi=hi)

    np_df = main_s[["number_posts_a1", "number_posts_a2"]].dropna()
    np_df = np_df[(np_df.number_posts_a1 > 0) & (np_df.number_posts_a2 > 0)]
    l1, l2 = np.log10(np_df.number_posts_a1), np.log10(np_df.number_posts_a2)
    icc, lo, hi = icc21(l1, l2)
    d = (l1 - l2).abs()
    r = float(np.corrcoef(l1, l2)[0, 1])
    rho = float(stats.spearmanr(l1, l2).statistic)
    exact = int((np_df.number_posts_a1 == np_df.number_posts_a2).sum())
    print("\nNUMBER OF POSTS, log10 (stratum main)")
    print(f"  n with both values     {len(np_df)} of {len(main_s)}")
    print(f"  ICC(2,1)               {icc:.3f} [{lo:.3f}, {hi:.3f}]")
    print(f"  Pearson r              {r:.3f}")
    print(f"  Spearman rho           {rho:.3f}")
    print(f"  identical              {exact}")
    print(f"  median |diff| in dex   {d.median():.3f}")
    print(f"  |diff| > 0.1 dex       {int((d > 0.1).sum())}")
    print(f"  |diff| > 1.0 dex       {int((d > 1.0).sum())}")
    out["number_posts"] = dict(n=len(np_df), icc=icc, lo=lo, hi=hi, r=r,
                               rho=rho, exact=exact,
                               median_dex=float(d.median()),
                               gt_1dex=int((d > 1.0).sum()))

    print("\nDISAGREEMENTS IN DETAIL (stratum main)")
    for c in BINARY + ["paper"] + CATEGORICAL:
        s = main_s.dropna(subset=[f"{c}_a1", f"{c}_a2"])
        dis = s[s[f"{c}_a1"] != s[f"{c}_a2"]]
        if dis.empty:
            continue
        print(f"\n  {c}  ({len(dis)}/{len(s)})")
        for _, r_ in dis.iterrows():
            print(f"    A1={str(r_[f'{c}_a1']):<22} A2={str(r_[f'{c}_a2']):<22} "
                  f"{r_.url[:64]}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        print(f"\nMetrics written to {args.json}.", file=sys.stderr)


if __name__ == "__main__":
    main()
