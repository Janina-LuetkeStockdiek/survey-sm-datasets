"""Run the repository harvesting scripts one after another.

Step 1 of the pipeline. Each script in ``harvesting/`` queries one repository and
writes its results to a CSV in ``DATA_OUTPUT_DIR``. They are independent, so they
can also be run individually; this orchestrator simply runs a selection of them
in sequence and tees the combined output to a timestamped log file.

Usage::

    python run_harvest.py                 # run every script in DEFAULT_SCRIPTS
    python run_harvest.py zenodo figshare # run only the named scripts

Harvesting the large repositories takes hours and is subject to the platforms'
rate limits. Running the scripts individually is often the more practical choice.
"""

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

from config import DATA_OUTPUT_DIR

HARVEST_DIR = Path(__file__).resolve().parent / "harvesting"

# Scripts in the order used for the data collection reported in the paper.
# Note that ICPSR is absent: its OAI-PMH endpoint did not complete a harvest
# within three days on repeated attempts, so the repository was excluded.
DEFAULT_SCRIPTS = [
    "kaggle_api.py",
    "github_api.py",
    "huggingface_api.py",
    "figshare_api.py",
    "dryad_api.py",
    "osf_api.py",
    "harvard_dataverse_api.py",
    "scienceDB_api.py",
    "zenodo_api.py",
    "mendeley_oai.py",
    "data_gov_oai.py",
    "eu_odp_oai.py",
    "rd_aus_oai.py",
    "cessda_oai.py",
]


def resolve(name: str) -> Path:
    """Return the path of a harvesting script, accepting a short name.

    Args:
        name: Either a file name (``zenodo_api.py``) or a stem fragment
            (``zenodo``).

    Returns:
        The resolved path.

    Raises:
        SystemExit: If no script or more than one script matches.
    """
    candidate = HARVEST_DIR / name
    if candidate.is_file():
        return candidate

    matches = sorted(HARVEST_DIR.glob(f"*{name}*.py"))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"No harvesting script matches '{name}'.")
    sys.exit(f"'{name}' is ambiguous: {[m.name for m in matches]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scripts",
        nargs="*",
        help="Scripts to run. Defaults to all repositories used in the paper.",
    )
    args = parser.parse_args()

    scripts = [resolve(s) for s in args.scripts] if args.scripts else [
        HARVEST_DIR / s for s in DEFAULT_SCRIPTS
    ]

    log_dir = Path(DATA_OUTPUT_DIR) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = log_dir / f"harvest_{stamp}.log"

    failed = []
    with log_file.open("w", encoding="utf-8") as log:
        for script in scripts:
            header = f"\n{'=' * 70}\n{script.name}  ({dt.datetime.now():%H:%M:%S})\n{'=' * 70}\n"
            print(header, end="")
            log.write(header)
            log.flush()

            proc = subprocess.run(
                [sys.executable, script.name],
                cwd=HARVEST_DIR,
                capture_output=True,
                text=True,
            )
            output = proc.stdout + proc.stderr
            print(output, end="")
            log.write(output)
            log.flush()

            if proc.returncode != 0:
                failed.append(script.name)
                print(f"--> {script.name} exited with code {proc.returncode}")

    print(f"\nLog written to {log_file}")
    if failed:
        print(f"Failed scripts: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
