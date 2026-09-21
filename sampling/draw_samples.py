"""All sampling for the survey, in one place.

Three samples are drawn over the course of the project. They used to live in
three separate scripts with copied constants and relative paths that only
resolved when the script happened to be started from ``data/``. They are
consolidated here: one seed, one feature list, one path resolution, three
subcommands.

    python3 sampling/draw_samples.py feature-agreement
    python3 sampling/draw_samples.py pilot
    python3 sampling/draw_samples.py rejected

The catalogue and the two LLM-test workbooks are not in this repository. Fetch
the catalogue with `python download_data.py` first, which writes it into `data/`.

Every subcommand accepts ``--out-dir`` to write somewhere else than the project
folder, which is how a redraw is checked against the current files before it
replaces them. Paths are resolved from this file's location, so the working
directory no longer matters.

WARNING, the blind workbooks. ``dataset_feature_agreement.xlsx`` and
``dataset_pilot.xlsx`` are the files the second annotator works in. Rerunning a
subcommand overwrites them and destroys any labels already entered. Both
subcommands therefore refuse to overwrite an existing workbook unless
``--force`` is given, and both write a timestamped backup first.

THE THREE SAMPLES
-----------------

feature-agreement
    Reliability sample for the feature annotation of the 1,997 included
    datasets. The features were originally annotated by a single person. To
    quantify their reliability, a second annotator relabels a sample
    independently and blind to the existing labels.

    Two strata, annotated together but analysed separately:

    main       simple random sample of n = 200 from the 1,997 records. All
               agreement statistics are computed on this stratum alone, so that
               the marginal distributions match the full data.

    synthetic  every record labelled synthetic = True that the main sample did
               not already contain. With only 39 positives in 1,997, kappa for
               this feature cannot be estimated from a random sample; the
               positives are therefore verified exhaustively and reported
               separately. They must NOT be pooled into the main stratum:
               synthetic datasets differ systematically from the rest (raw
               38.5 % vs 78.1 %, timestamp 30.8 % vs 62.3 %, labeled 46.2 % vs
               26.5 %), so adding them would shift the prevalences the
               agreement statistics depend on.

    The key is a separate file on purpose: the annotator must not be able to see
    which stratum a record belongs to, or what was labelled the first time.

pilot
    Pilot set for refining the codebook before the reliability annotation
    starts. NOT a random sample and NOT part of any statistic. Its purpose is to
    surface the cases where the codebook is silent or ambiguous, so it is
    selected to cover the decision space rather than to represent the catalogue:
    both values of every binary feature, a spread of repositories (their pages
    differ a great deal in what they expose), several macro topics, tasks and
    licenses, and both extremes of the size distribution. Selection is a greedy
    set cover over these targets, ties broken by the fixed seed, so the choice
    is reproducible and not hand-picked.

    Every record of the reliability sample (both strata, 234 records) is
    excluded, so that the pilot cannot contaminate the reported figures.

    Both annotators code the pilot independently. For the first annotator this
    is a re-coding of records they labelled months earlier, which also gives an
    intra-rater check: where the two passes disagree, the codebook is
    underspecified even for the person who wrote it.

rejected
    Stratified verification sample for the false negative rate of the LLM
    filter. Every record classified as relevant by GPT-OSS was manually
    screened, so the number of false positives is known for the entire
    population. Only the false negative rate within the rejected stratum is
    unknown, and it alone bounds the recall of the complete pipeline.

    Population : 4,276 records with gpt-oss-120b_majority == 0 in
                 dataset_initial_dedup.csv
    Already
    annotated  : 126 of these were drawn into the development or held-out test
                 set and therefore already carry a human label
    To annotate: simple random sample of n = 300 without replacement from the
                 remaining 4,150 unannotated records

    The 126 records that already carry a label are NOT pooled with the new
    sample. They were drawn from the corpus as it stood on 26 June 2026, i.e.
    before the later collection rounds added 881 records to the deduplicated
    data (most notably for OSF, where 73 % of the current records postdate that
    frame). They therefore do not constitute a random subset of the present
    stratum and are retained in the workbook for reference only. The estimate is
    computed from the 300 newly drawn records alone.

    Residual note on the draw: excluding the 126 leaves 4,150 eligible records
    whose share of post-June records is 14.2 % against 13.8 % in the full
    stratum, so the exclusion does not materially shift the composition of the
    frame.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------
# Shared constants and paths
# --------------------------------------------------------------------------

SEED = 20260827

N_MAIN = 200        # feature-agreement, main stratum
N_PILOT = 20        # pilot
N_REJECTED = 300    # rejected stratum

N_CATALOGUE = 1997      # rows expected in dataset_relevant.csv
N_REJECTED_POP = 4276   # rows with gpt-oss-120b_majority == 0

# platform is excluded: it is derived automatically from title and description
# by a term-matching rule rather than coded by hand, so a second annotator
# would be reproducing that rule instead of forming an independent judgement.
FEATURES = ["raw", "synthetic", "collection_described", "timestamp", "labeled",
            "paper", "code", "topic", "macro_topic", "task", "license",
            "number_posts"]
CONTEXT = ["url", "repository", "title", "description"]

# <repo>/sampling/draw_samples.py -> repository root
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

CATALOGUE = DATA / "dataset_relevant.csv"
DEDUP = DATA / "dataset_initial_dedup.csv"
AGREEMENT_DIR = DATA / "feature_agreement"
LLM_TEST_DIR = DATA / "LLM_test"


def read_semicolon(path: Path) -> pd.DataFrame:
    """Every CSV in this project is UTF-8 with BOM and semicolon-separated."""
    return pd.read_csv(path, sep=";", encoding="utf-8-sig", low_memory=False)


def guard(path: Path, force: bool) -> None:
    """Refuse to clobber a workbook an annotator may already have worked in."""
    if not path.exists():
        return
    if not force:
        sys.exit(
            f"{path.name} already exists.\n"
            f"Rerunning replaces it and destroys any labels entered in it.\n"
            f"Pass --force to overwrite (a timestamped backup is written), or\n"
            f"--out-dir <dir> to write the redraw somewhere else and compare first."
        )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.stem}_backup_{stamp}{path.suffix}")
    shutil.copy2(path, backup)
    print(f"backup written: {backup.name}")


def blank_sheet(frame: pd.DataFrame) -> pd.DataFrame:
    """Context columns plus one empty column per feature, for blind coding."""
    sheet = frame[CONTEXT].copy()
    for f in FEATURES:
        sheet[f] = ""
    sheet["note"] = ""
    return sheet


# --------------------------------------------------------------------------
# feature-agreement
# --------------------------------------------------------------------------

def cmd_feature_agreement(out_dir: Path, force: bool) -> None:
    d = read_semicolon(CATALOGUE)
    assert len(d) == N_CATALOGUE, len(d)

    main = d.sample(n=N_MAIN, random_state=SEED)
    syn_extra = d[(d["synthetic"] == True) & (~d.index.isin(main.index))]

    pool = pd.concat([main.assign(stratum="main"),
                      syn_extra.assign(stratum="synthetic")])
    pool = pool.sample(frac=1.0, random_state=SEED)      # shuffle for blinding

    sheet = blank_sheet(pool)
    key = pool[["url", "stratum"] + FEATURES].rename(
        columns={f: f"{f}_annotator_1" for f in FEATURES})

    workbook = out_dir / "dataset_feature_agreement.xlsx"
    keyfile = out_dir / "dataset_feature_agreement_key.csv"

    guard(workbook, force)
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet.to_excel(workbook, sheet_name="to_annotate", index=False)
    key.to_csv(keyfile, sep=";", index=False)

    print("main stratum      :", int((pool["stratum"] == "main").sum()))
    print("synthetic extra   :", int((pool["stratum"] == "synthetic").sum()))
    print("synthetic already in main:", int(main["synthetic"].sum()))
    print("total to annotate :", len(pool))
    print("features          :", len(FEATURES), FEATURES)
    print("written           :", workbook.name, "|", keyfile.name)


# --------------------------------------------------------------------------
# pilot
# --------------------------------------------------------------------------

def cmd_pilot(out_dir: Path, force: bool) -> None:
    d = read_semicolon(CATALOGUE)
    used = set(read_semicolon(AGREEMENT_DIR / "dataset_feature_agreement_key.csv")["url"])
    pool = d[~d["url"].isin(used)].copy()
    pool["has_paper"] = pool["paper"].astype(str).str.strip() != "False"
    q10, q90 = pool["number_posts"].quantile([0.10, 0.90])

    binary = ["raw", "timestamp", "labeled", "collection_described", "code"]

    def targets_of(r):
        t = {f"{c}={r[c]}" for c in binary}
        t.add(f"paper={r['has_paper']}")
        if r["synthetic"]:
            t.add("synthetic=True")
        t.add(f"repo={r['repository']}")
        t.add(f"topic={r['macro_topic']}")
        t.add(f"task={r['task']}")
        t.add(f"lic={r['license']}")
        if r["number_posts"] <= q10:
            t.add("size=small")
        if r["number_posts"] >= q90:
            t.add("size=large")
        return t

    # targets worth covering: everything above, but repositories/tasks/licenses
    # only where they are frequent enough that the annotator will meet them again
    freq_repo = set(pool["repository"].value_counts().head(8).index)
    freq_task = set(pool["task"].value_counts().head(8).index)
    freq_lic = set(pool["license"].value_counts().head(6).index)
    wanted = set()
    for _, r in pool.iterrows():
        for t in targets_of(r):
            k, _, v = t.partition("=")
            if k == "repo" and v not in freq_repo:
                continue
            if k == "task" and v not in freq_task:
                continue
            if k == "lic" and v not in freq_lic:
                continue
            wanted.add(t)

    order = pool.sample(frac=1.0, random_state=SEED)     # fixed tie-break order

    # All 39 synthetic records went into the reliability sample, so the pilot
    # cannot contain a positive case of the feature whose rule is the most
    # ambiguous one. We therefore seed the pilot with two "near misses":
    # datasets whose text points at bots or language models but which were coded
    # synthetic = False. These are exactly the cases the rule has to separate.
    txt = (order["title"].astype(str) + " " + order["description"].astype(str)).str.lower()
    near = order[(~order["synthetic"]) &
                 txt.str.contains(r"\bbots?\b|generated by|synthetic|chatgpt|\bllm\b|\bgpt-",
                                  regex=True)]
    chosen = list(near.index[:2])
    covered = set()
    for i in chosen:
        covered |= targets_of(order.loc[i]) & wanted

    while len(chosen) < N_PILOT:
        best, best_gain = None, -1
        for i, r in order.iterrows():
            if i in chosen:
                continue
            gain = len((targets_of(r) & wanted) - covered)
            if gain > best_gain:
                best, best_gain = i, gain
        if best is None:
            break
        chosen.append(best)
        covered |= targets_of(order.loc[best]) & wanted

    pilot = pool.loc[chosen].sample(frac=1.0, random_state=SEED)    # shuffle output

    sheet = blank_sheet(pilot)
    key = pilot[["url"] + FEATURES].rename(columns={f: f"{f}_original" for f in FEATURES})

    workbook = out_dir / "dataset_pilot.xlsx"
    keyfile = out_dir / "dataset_pilot_key.csv"

    guard(workbook, force)
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet.to_excel(workbook, sheet_name="pilot", index=False)
    key.to_csv(keyfile, sep=";", index=False)

    print(f"pilot n = {len(pilot)}")
    print(f"coverage: {len(covered)}/{len(wanted)} targets")
    print("missed  :", sorted(wanted - covered)[:12])
    print()
    print("repositories:", pilot["repository"].value_counts().to_dict())
    print("synthetic   :", int(pilot["synthetic"].sum()))
    for c in binary:
        print(f"  {c:22s} True {int(pilot[c].sum()):2d} / False {int((~pilot[c]).sum()):2d}")
    print("  paper present         ", int(pilot["has_paper"].sum()))
    print("posts: min", int(pilot["number_posts"].min()),
          "max", int(pilot["number_posts"].max()))
    print("written           :", workbook.name, "|", keyfile.name)


# --------------------------------------------------------------------------
# rejected
# --------------------------------------------------------------------------

def cmd_rejected(out_dir: Path, force: bool) -> None:
    d = read_semicolon(DEDUP)
    rejected = d[d["gpt-oss-120b_majority"] == 0].copy()
    assert len(rejected) == N_REJECTED_POP, len(rejected)

    dev = pd.read_excel(LLM_TEST_DIR / "dataset_sample.xlsx")[["url", "ground_truth"]] \
        .assign(prior_set="dev")
    test = pd.read_excel(LLM_TEST_DIR / "dataset_sample_2.xlsx")[["url", "ground_truth"]] \
        .assign(prior_set="test")
    prior = pd.concat([dev, test]).drop_duplicates("url").set_index("url")

    is_known = rejected["url"].isin(prior.index)
    known = rejected[is_known].copy()
    unknown = rejected[~is_known].copy()

    sample = unknown.sample(n=N_REJECTED, random_state=SEED).sort_index()

    cols = ["url", "repository", "id", "doi", "updated", "title", "description"]
    out = sample[cols].copy()
    out["annotator_1"] = ""
    out["annotator_2"] = ""
    out["annotator_3"] = ""   # adjudication, filled only where 1 and 2 disagree
    out["ground_truth"] = ""  # majority over the annotators who labelled the record
    out["note"] = ""

    known_out = known[cols].copy()
    known_out["prior_set"] = known_out["url"].map(prior["prior_set"])
    known_out["ground_truth"] = known_out["url"].map(prior["ground_truth"])

    info = pd.DataFrame({
        "key": ["base_file", "stratum", "population_N", "already_annotated",
                "eligible_for_draw", "sample_n", "method", "seed", "drawn_on",
                "n_for_analysis", "relevant_among_prior_labels_NOT_pooled"],
        "value": [DEDUP.name, "gpt-oss-120b_majority == 0", len(rejected), len(known),
                  len(unknown), N_REJECTED,
                  "simple random sample without replacement (pandas.DataFrame.sample)",
                  SEED, "2026-08-27", N_REJECTED,
                  int((known_out["ground_truth"] == 1).sum())],
    })

    workbook = out_dir / "dataset_sample_rejected.xlsx"
    guard(workbook, force)
    out_dir.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(workbook, engine="openpyxl") as w:
        out.to_excel(w, sheet_name="to_annotate", index=False)
        known_out.to_excel(w, sheet_name="prior_labels_reference", index=False)
        info.to_excel(w, sheet_name="sampling_info", index=False)

    assert not out["url"].isin(prior.index).any(), "unlabelled draw is contaminated"
    assert out["url"].nunique() == N_REJECTED
    print("to_annotate:", len(out), "| already_labelled:", len(known_out),
          "| combined:", len(out) + len(known_out))
    print("relevant among already_labelled:", int((known_out["ground_truth"] == 1).sum()))
    print("written           :", workbook.name)


COMMANDS = {
    "feature-agreement": (cmd_feature_agreement, AGREEMENT_DIR),
    "pilot": (cmd_pilot, AGREEMENT_DIR),
    "rejected": (cmd_rejected, LLM_TEST_DIR),
}


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sample", choices=sorted(COMMANDS))
    p.add_argument("--out-dir", type=Path, default=None,
                   help="write the output somewhere else than the project folder")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing workbook (a backup is written first)")
    a = p.parse_args()

    fn, default_dir = COMMANDS[a.sample]
    fn(a.out_dir or default_dir, a.force)


if __name__ == "__main__":
    main()
