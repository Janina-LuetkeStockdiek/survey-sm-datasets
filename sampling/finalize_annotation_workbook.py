"""Make `dataset_feature_agreement.xlsx` ready to hand to the second annotator.

Run this after every `draw_samples.py feature-agreement`, which regenerates the
workbook from the catalogue and therefore undoes both steps below.

1. Kaggle descriptions. The catalog's `description` holds Kaggle's subtitle,
   capped at 80 characters. The full texts sit in `kaggle_descriptions.csv` and
   are put in their place, so the annotator sees the same text the repository
   page shows. This does not touch the blinding -- the description is public
   information, not a label.

2. The `paper` column. §5.6 asks for the identifier, not a boolean, but Excel
   turns a typed "True" into a boolean and the pilot annotator filled the
   column with True and False. The column is formatted as text and carries an
   input prompt.

Refuses to run if any label cell already holds a value, so an annotation in
progress can never be overwritten.

`kaggle_descriptions.csv` is a working file and not part of the published data
record. Without it step 1 is skipped and the workbook keeps the descriptions the
catalogue holds.

    python3 finalize_annotation_workbook.py
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

# The reliability material sits under data/feature_agreement/ in the project
# tree and under data/ in the published repository. Resolved by walking up from
# this file, so that one and the same file works in both layouts and from any
# working directory.
def _feature_agreement_dir() -> Path:
    here = Path(__file__).resolve()
    for base in [here.parent, *here.parents]:
        for candidate in (base / "data" / "feature_agreement", base / "data"):
            # Anchored on the key file rather than on the workbook: the
            # workbook is what this script writes and may legitimately be
            # missing, the key file is always there.
            if (candidate / "dataset_feature_agreement_key.csv").exists():
                return candidate
    return here.parents[2] / "data" / "feature_agreement"


FA = _feature_agreement_dir()
BOOK = FA / "dataset_feature_agreement.xlsx"
DESCRIPTIONS = FA / "kaggle_descriptions.csv"
SHEET = "to_annotate"

LABELS = ["raw", "synthetic", "collection_described", "timestamp", "labeled",
          "paper", "code", "topic", "macro_topic", "task", "license",
          "number_posts", "note"]


def main() -> int:
    if not BOOK.exists():
        print(f"{BOOK} not found")
        return 1

    df = pd.read_excel(BOOK, sheet_name=SHEET)
    filled = {c: int(df[c].notna().sum()) for c in LABELS if c in df.columns}
    if sum(filled.values()):
        print("ABORTED: the workbook already holds label values.")
        for c, n in filled.items():
            if n:
                print(f"  {c}: {n}")
        print("The second annotation is evidently under way. Nothing changed.")
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(BOOK, BOOK.with_name(f"{BOOK.stem}_backup_{stamp}{BOOK.suffix}"))

    wb = load_workbook(BOOK)
    ws = wb[SHEET]
    header = [c.value for c in ws[1]]
    col = {name: i + 1 for i, name in enumerate(header)}

    # --- 1. full Kaggle descriptions
    replaced = 0
    if DESCRIPTIONS.exists():
        k = pd.read_csv(DESCRIPTIONS)[["url", "description"]].dropna(subset=["description"])
        lookup = dict(zip(k["url"].astype(str), k["description"].astype(str)))
        for r in range(2, ws.max_row + 1):
            url = str(ws.cell(row=r, column=col["url"]).value)
            full = lookup.get(url)
            if full and len(full) > len(str(ws.cell(row=r, column=col["description"]).value or "")):
                ws.cell(row=r, column=col["description"]).value = full[:8000]
                replaced += 1
        ws.column_dimensions[get_column_letter(col["description"])].width = 80
        for r in range(2, ws.max_row + 1):
            ws.cell(row=r, column=col["description"]).alignment = Alignment(
                vertical="top", wrap_text=True)
    else:
        print(f"NOTE: {DESCRIPTIONS.name} not found, descriptions are left as they are")

    # --- 2. paper as a text column, with an input prompt
    letter = get_column_letter(col["paper"])
    for r in range(2, ws.max_row + 1):
        ws.cell(row=r, column=col["paper"]).number_format = "@"
    dv = DataValidation(type="textLength", operator="greaterThan", formula1="0",
                        allow_blank=True, showInputMessage=True, showErrorMessage=False)
    # The prompt is the German wording the second annotator actually saw. It is
    # part of the instrument and is kept verbatim, so that rerunning this script
    # reproduces the workbook published with the data record.
    dv.promptTitle = "DOI oder URL eintragen"
    dv.prompt = ("Den Identifier des Papers eintragen, nicht True. "
                 "Wenn es keines gibt: False.")
    ws.add_data_validation(dv)
    dv.add(f"{letter}2:{letter}{ws.max_row}")
    ws.cell(row=1, column=col["paper"]).font = Font(bold=True)
    ws.cell(row=1, column=col["paper"]).comment = None

    wb.save(BOOK)
    print(f"descriptions replaced: {replaced}")
    print(f"column `paper` formatted as text, prompt attached ({letter}2:{letter}{ws.max_row})")
    print(f"backup: {BOOK.stem}_backup_{stamp}{BOOK.suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
