#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fuji_assessment.py
==================
Assesses the datasets in `data/dataset_relevant.csv` against the FAIRsFAIR
metrics by sending each one to an F-UJI instance.

F-UJI (Huber & Devaraju, PANGAEA) is the reference service for automated FAIR
assessment: <https://www.f-uji.net> / <https://github.com/pangaea-data-publisher/fuji>.
The public instance is rate-limited and not meant for close to 2,000 objects, so
this script talks to a LOCAL instance by default.

--------------------------------------------------------------------------------
ONE-TIME SETUP (local F-UJI instance via Docker)
--------------------------------------------------------------------------------
Do NOT use the project's own image `ghcr.io/pangaea-data-publisher/fuji`. It exits
immediately with code 3, because F-UJI 4.0.0 starts a Playwright Chromium in
`app.py` but the project Dockerfile never runs `playwright install`. Build from
`Dockerfile.fuji` in this repository instead, which fixes that, keeps the Java
runtime Apache Tika needs, and builds natively on arm64 as well as amd64:

    git clone --depth 1 https://github.com/pangaea-data-publisher/fuji.git
    cd fuji
    docker build -f /path/to/this/repo/Dockerfile.fuji -t fuji-local .
    docker run -d --name fuji -p 1071:1071 --shm-size=1g fuji-local

    # is it up?
    open http://localhost:1071/fuji/api/v1/ui/

The credentials are the image defaults (fuji_server/config/users.py): user
`marvel`, password `wonderwoman`. Stop the container with `docker stop fuji` and
restart it with `docker start fuji`.

--------------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------------
    pip install requests pandas

    # check the connection and a single object before starting the long run
    python fuji_assessment.py --check

    # trial run over the first 20 open rows
    python fuji_assessment.py --limit 20

    # the full run, interruptible with Ctrl-C at any point and resumable
    python fuji_assessment.py

    # one repository only
    python fuji_assessment.py --repository Zenodo

    # retry the rows that failed
    python fuji_assessment.py --retry-errors

The script is RESUMABLE. Rows already assessed are skipped, keyed on the column
`id`, intermediate results are written every --checkpoint rows, and Ctrl-C is
caught so that no completed work is lost.

--------------------------------------------------------------------------------
A NOTE ON --use-github
--------------------------------------------------------------------------------
The flag exists but has no effect under `metrics_v0.5`, which is the metric
version used for the paper. F-UJI evaluates GitHub API data in three places
only, and all three first test for a metric prefix `FRSM` — the software metrics
introduced in v0.7. `metrics_v0.5` contains none of them, so the GitHub rows
score the same with a token as without one.

Leave the flag off. F-UJI calls the GitHub API for EVERY assessed object,
including Kaggle and Zenodo URLs. Unauthenticated that is 60 requests per hour,
after which the server backs off for up to an hour and the run looks as though it
has hung.

--------------------------------------------------------------------------------
OUTPUT
--------------------------------------------------------------------------------
* data/fair_fuji.csv    -- one row per dataset, separator ";", holding
                           - identification: id, repository, url, doi, fuji_identifier
                           - run state:      run_status, run_error, assessed_at, duration_s
                           - overall scores: fair_score_percent, score_earned/total,
                             tests_passed/total, maturity_FAIR
                           - per FAIR category and principle: pct_*, mat_*,
                             passed_*, total_* (e.g. pct_F, pct_R1.1, mat_A)
                           - per metric: m_FsF-R1.1-01M_status / _score / _maturity
* data/fuji_raw/<id>.json -- the complete F-UJI response per dataset, so that
                           later analyses do not have to run the assessment again

With --merge the script additionally writes `data/dataset_relevant_fair.csv`: the
input table plus the main score columns. The original `dataset_relevant.csv` is
never modified.

--------------------------------------------------------------------------------
IDENTIFIER LOGIC
--------------------------------------------------------------------------------
F-UJI receives the best available identifier per row:
    1. the column `doi` where it is filled -> as https://doi.org/<doi>
    2. the column `url` otherwise
This matters because roughly three quarters of the rows, above all Kaggle, GitHub
and Hugging Face, carry no DOI. Their low scores are not a measurement error but
the finding itself: without a persistent identifier, F1 and F2 cannot pass. The
column `fuji_identifier` records which identifier was used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from requests.auth import HTTPBasicAuth

# --------------------------------------------------------------------------- #
# Voreinstellungen
# --------------------------------------------------------------------------- #

DEFAULT_API_BASE = "http://localhost:1071/fuji/api/v1"
DEFAULT_USER = "marvel"
DEFAULT_PASSWORD = "wonderwoman"
DEFAULT_METRIC_VERSION = "metrics_v0.5"

SEP = ";"
KEY_COL = "id"          # unique key in dataset_relevant.csv
CORE_COLS = [
    "id", "repository", "url", "doi", "fuji_identifier",
    "run_status", "run_error", "assessed_at", "duration_s",
    "fuji_version", "metric_version", "resolved_url",
    "fair_score_percent", "score_earned", "score_total",
    "tests_passed", "tests_total", "maturity_FAIR",
]

# columns --merge writes into the copy of the input table
MERGE_COLS = [
    "fuji_identifier", "run_status", "fair_score_percent",
    "pct_F", "pct_A", "pct_I", "pct_R",
    "maturity_FAIR", "tests_passed", "tests_total",
]

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Pfade
# --------------------------------------------------------------------------- #

def find_project_root(start: Path) -> Path:
    """Walk upwards to the folder holding `data/dataset_relevant.csv`."""
    for candidate in [start, *start.parents]:
        if (candidate / "data" / "dataset_relevant.csv").is_file():
            return candidate
    return start


# --------------------------------------------------------------------------- #
# Identifier
# --------------------------------------------------------------------------- #

def build_identifier(row: pd.Series) -> str | None:
    """Prefer the DOI, fall back to the URL. Returns None where both are missing."""
    doi = row.get("doi")
    if isinstance(doi, str) and doi.strip() and doi.strip().lower() != "nan":
        doi = doi.strip()
        doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi, flags=re.I)
        return f"https://doi.org/{doi}"

    url = row.get("url")
    if isinstance(url, str) and url.strip() and url.strip().lower() != "nan":
        return url.strip()

    return None


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value))[:120]


# --------------------------------------------------------------------------- #
# F-UJI-Aufruf
# --------------------------------------------------------------------------- #

def evaluate(identifier: str, cfg: argparse.Namespace, session: requests.Session) -> dict:
    """Assess one object. Raises if the call fails."""
    payload = {
        "object_identifier": identifier,
        "test_debug": bool(cfg.debug_output),
        "use_datacite": True,
        "use_github": bool(cfg.use_github),
        "use_headless": bool(cfg.headless),
        "metric_version": cfg.metric_version,
    }
    last_error: Exception | None = None

    for attempt in range(1, cfg.retries + 1):
        try:
            response = session.post(
                f"{cfg.api_base.rstrip('/')}/evaluate",
                json=payload,
                auth=HTTPBasicAuth(cfg.user, cfg.password),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=cfg.timeout,
            )
            if response.status_code == 401:
                raise RuntimeError(
                    "401 -- wrong credentials (--user / --password)."
                )
            if response.status_code == 429:
                wait = min(60, 5 * attempt)
                log(f"    429 (Rate-Limit), warte {wait}s")
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, re-raised
            last_error = exc
            if attempt < cfg.retries:
                wait = min(30, 3 * attempt)
                log(f"    Versuch {attempt}/{cfg.retries} fehlgeschlagen ({exc}), warte {wait}s")
                time.sleep(wait)

    raise RuntimeError(str(last_error))


# --------------------------------------------------------------------------- #
# response -> flat row
# --------------------------------------------------------------------------- #

def flatten(result: dict) -> dict:
    """Turn the F-UJI response into one flat result row."""
    out: dict = {}
    summary = result.get("summary") or {}

    out["fuji_version"] = result.get("software_version")
    out["metric_version"] = result.get("metric_version")
    out["resolved_url"] = result.get("resolved_url")

    score_percent = summary.get("score_percent") or {}
    score_earned = summary.get("score_earned") or {}
    score_total = summary.get("score_total") or {}
    maturity = summary.get("maturity") or {}
    passed = summary.get("status_passed") or {}
    total = summary.get("status_total") or {}

    out["fair_score_percent"] = score_percent.get("FAIR")
    out["score_earned"] = score_earned.get("FAIR")
    out["score_total"] = score_total.get("FAIR")
    out["maturity_FAIR"] = maturity.get("FAIR")
    out["tests_passed"] = passed.get("FAIR")
    out["tests_total"] = total.get("FAIR")

    # per FAIR category (F, A, I, R) and per principle (F1, F2, R1.1 ...)
    for key, value in score_percent.items():
        if key != "FAIR":
            out[f"pct_{key}"] = value
    for key, value in maturity.items():
        if key != "FAIR":
            out[f"mat_{key}"] = value
    for key, value in passed.items():
        if key != "FAIR":
            out[f"passed_{key}"] = value
    for key, value in total.items():
        if key != "FAIR":
            out[f"total_{key}"] = value

    # je Einzelmetrik
    for entry in result.get("results") or []:
        metric = entry.get("metric_identifier")
        if not metric:
            continue
        out[f"m_{metric}_status"] = entry.get("test_status")
        out[f"m_{metric}_maturity"] = entry.get("maturity")
        score = entry.get("score") or {}
        out[f"m_{metric}_score"] = score.get("earned")

    return out


# --------------------------------------------------------------------------- #
# Ergebnistabelle
# --------------------------------------------------------------------------- #

def load_existing(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    frame = pd.read_csv(path, sep=SEP, dtype={KEY_COL: str})
    frame = frame.where(pd.notna(frame), None)
    return {str(row[KEY_COL]): dict(row) for _, row in frame.iterrows()}


def write_results(rows: dict[str, dict], path: Path) -> None:
    frame = pd.DataFrame(list(rows.values()))
    ordered = [c for c in CORE_COLS if c in frame.columns]
    rest = sorted(c for c in frame.columns if c not in ordered)
    frame = frame[ordered + rest]
    tmp = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(tmp, sep=SEP, index=False)
    tmp.replace(path)


def write_merged(source: Path, results: Path, target: Path) -> None:
    base = pd.read_csv(source, sep=SEP, dtype={KEY_COL: str}, low_memory=False)
    scores = pd.read_csv(results, sep=SEP, dtype={KEY_COL: str}, low_memory=False)
    keep = [KEY_COL] + [c for c in MERGE_COLS if c in scores.columns]
    merged = base.merge(scores[keep], on=KEY_COL, how="left", suffixes=("", "_fuji"))
    merged.to_csv(target, sep=SEP, index=False)
    log(f"Zusammengefuehrt -> {target}")


# --------------------------------------------------------------------------- #
# Hauptlauf
# --------------------------------------------------------------------------- #

def run(cfg: argparse.Namespace) -> int:
    root = Path(cfg.root).resolve() if cfg.root else find_project_root(Path(__file__).resolve().parent)
    source = Path(cfg.input) if cfg.input else root / "data" / "dataset_relevant.csv"
    results_path = Path(cfg.output) if cfg.output else root / "data" / "fair_fuji.csv"
    raw_dir = Path(cfg.raw_dir) if cfg.raw_dir else root / "data" / "fuji_raw"

    if not source.is_file():
        log(f"ERROR: input file not found: {source}")
        return 1
    raw_dir.mkdir(parents=True, exist_ok=True)
    results_path.parent.mkdir(parents=True, exist_ok=True)

    data = pd.read_csv(source, sep=SEP, dtype={KEY_COL: str}, low_memory=False)
    log(f"input: {source}  ({len(data)} rows)")

    rows = load_existing(results_path)
    if rows:
        log(f"already present: {len(rows)} assessed rows in {results_path.name}")

    done = {
        key for key, value in rows.items()
        if value.get("run_status") == "ok" or (value.get("run_status") == "error" and not cfg.retry_errors)
    }

    todo = data
    if cfg.repository:
        todo = todo[todo["repository"].astype(str).str.lower() == cfg.repository.lower()]
    todo = todo[~todo[KEY_COL].astype(str).isin(done)]
    if cfg.limit:
        todo = todo.head(cfg.limit)

    log(f"to assess: {len(todo)} rows  (skipped: {len(done)})")
    if todo.empty:
        if cfg.merge:
            write_merged(source, results_path, root / "data" / "dataset_relevant_fair.csv")
        return 0

    session = requests.Session()
    counter = {"done": 0, "ok": 0, "error": 0}
    stop = threading.Event()

    def work(row: pd.Series) -> dict:
        key = str(row[KEY_COL])
        record = {
            "id": key,
            "repository": row.get("repository"),
            "url": row.get("url"),
            "doi": row.get("doi"),
            "assessed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        identifier = build_identifier(row)
        if not identifier:
            record.update(run_status="skipped", run_error="weder doi noch url", duration_s=0)
            return record

        record["fuji_identifier"] = identifier
        started = time.time()
        try:
            result = evaluate(identifier, cfg, session)
        except Exception as exc:  # noqa: BLE001
            record.update(
                run_status="error",
                run_error=str(exc)[:400],
                duration_s=round(time.time() - started, 1),
            )
            return record

        record["duration_s"] = round(time.time() - started, 1)
        (raw_dir / f"{safe_filename(key)}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        try:
            record.update(flatten(result))
            record["run_status"] = "ok"
            record["run_error"] = None
        except Exception as exc:  # noqa: BLE001
            record.update(run_status="parse_error", run_error=str(exc)[:400])
        return record

    def absorb(record: dict) -> None:
        rows[record["id"]] = record
        counter["done"] += 1
        counter["ok" if record.get("run_status") == "ok" else "error"] += 1
        score = record.get("fair_score_percent")
        label = f"{score}%" if score is not None else record.get("run_status")
        log(
            f"[{counter['done']}/{len(todo)}] {record.get('repository')} "
            f"{str(record.get('fuji_identifier'))[:70]} -> {label}"
        )
        if counter["done"] % cfg.checkpoint == 0:
            write_results(rows, results_path)
            log(f"    intermediate result saved ({len(rows)} rows)")

    pool: ThreadPoolExecutor | None = None
    try:
        if cfg.workers <= 1:
            for _, row in todo.iterrows():
                if stop.is_set():
                    break
                absorb(work(row))
                if cfg.sleep:
                    time.sleep(cfg.sleep)
        else:
            pool = ThreadPoolExecutor(max_workers=cfg.workers)
            futures = {pool.submit(work, row): idx for idx, row in todo.iterrows()}
            for future in as_completed(futures):
                absorb(future.result())
            pool.shutdown(wait=True)
    except KeyboardInterrupt:
        stop.set()
        log("Abbruch durch Nutzerin -- speichere Zwischenstand ...")
        write_results(rows, results_path)
        log(f"saved: {len(rows)} rows -> {results_path}")
        if pool is not None:
            # Threads can be stuck in a hanging request and cannot be joined
            # cleanly. The intermediate state is already on disk.
            pool.shutdown(wait=False, cancel_futures=True)
            os._exit(1)
        return 1

    write_results(rows, results_path)
    log(f"done. ok={counter['ok']}  errors={counter['error']}  -> {results_path}")

    if cfg.merge:
        write_merged(source, results_path, root / "data" / "dataset_relevant_fair.csv")
    return 0


# --------------------------------------------------------------------------- #
# Vorabpruefung
# --------------------------------------------------------------------------- #

def check(cfg: argparse.Namespace) -> int:
    base = cfg.api_base.rstrip("/")
    log(f"Pruefe {base} ...")
    session = requests.Session()
    try:
        version = cfg.metric_version.replace("metrics_v", "")
        response = session.get(
            f"{base}/metrics/{version}",
            auth=HTTPBasicAuth(cfg.user, cfg.password),
            timeout=30,
        )
        response.raise_for_status()
        metrics = response.json()
        log(f"  Instanz erreichbar, {metrics.get('total', '?')} Metriken in {cfg.metric_version}")
    except Exception as exc:  # noqa: BLE001
        log(f"  ERROR: {exc}")
        log("  Is the container running? docker run -d --name fuji -p 1071:1071 "
            "ghcr.io/pangaea-data-publisher/fuji")
        return 1

    probe = cfg.probe or "https://doi.org/10.1594/PANGAEA.908011"
    log(f"  Testbewertung: {probe}")
    started = time.time()
    try:
        result = evaluate(probe, cfg, session)
    except Exception as exc:  # noqa: BLE001
        log(f"  ERROR during the test assessment: {exc}")
        return 1
    flat = flatten(result)
    log(
        f"  OK in {time.time() - started:.1f}s -- FAIR {flat.get('fair_score_percent')}% "
        f"(F {flat.get('pct_F')} / A {flat.get('pct_A')} / I {flat.get('pct_I')} / R {flat.get('pct_R')}), "
        f"F-UJI {flat.get('fuji_version')}"
    )
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FAIR assessment of the catalogued datasets with F-UJI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--check", action="store_true",
                        help="check the connection and a single object only, write nothing")
    parser.add_argument("--probe", help="identifier to use for the test assessment with --check")

    parser.add_argument("--root", help="Projektwurzel (Standard: automatisch gesucht)")
    parser.add_argument("--input", "-i", help="Eingabe-CSV (Standard: data/dataset_relevant.csv)")
    parser.add_argument("--output", "-o", help="Ergebnis-CSV (Standard: data/fair_fuji.csv)")
    parser.add_argument("--raw-dir", help="folder for the raw JSON responses (default: data/fuji_raw)")

    parser.add_argument("--api-base", default=os.environ.get("FUJI_API", DEFAULT_API_BASE),
                        help="base URL of the F-UJI API")
    parser.add_argument("--user", default=os.environ.get("FUJI_USER", DEFAULT_USER))
    parser.add_argument("--password", default=os.environ.get("FUJI_PASSWORD", DEFAULT_PASSWORD))
    parser.add_argument("--metric-version", default=DEFAULT_METRIC_VERSION,
                        help="z.B. metrics_v0.5 oder metrics_v0.8")

    parser.add_argument("--limit", type=int, help="process the first N open rows only")
    parser.add_argument("--repository", help="process one repository only, e.g. Zenodo")
    parser.add_argument("--retry-errors", action="store_true",
                        help="retry rows that failed earlier")
    parser.add_argument("--merge", action="store_true",
                        help="zusaetzlich data/dataset_relevant_fair.csv schreiben")

    parser.add_argument("--workers", type=int, default=2,
                        help="parallele Anfragen (lokal 2-4 sinnvoll, oeffentliche Instanz: 1)")
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="pause between requests in seconds (only with --workers 1)")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout je Anfrage in Sekunden")
    parser.add_argument("--retries", type=int, default=2, help="Versuche je Objekt")
    parser.add_argument("--checkpoint", type=int, default=5,
                        help="write an intermediate result every N rows")

    # NOTE: this flag has no effect under metrics_v0.5, see the module docstring.
    # F-UJI also calls the GitHub API for EVERY object, Kaggle and Zenodo URLs
    # included, which is 60 requests per hour unauthenticated. Leave it off.
    parser.add_argument("--use-github", action="store_true", default=False,
                        help="use the GitHub API for metadata harvesting (no effect under metrics_v0.5)")
    parser.add_argument("--no-use-github", dest="use_github", action="store_false")
    parser.add_argument("--headless", action="store_true",
                        help="use a headless browser (for JS-rendered pages, markedly slower)")
    parser.add_argument("--debug-output", action="store_true",
                        help="include the verbose test log in the JSON response")

    return parser.parse_args(argv)


def main() -> int:
    cfg = parse_args()
    if cfg.check:
        return check(cfg)
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
