"""Merge the per-repository harvest results into a single annotation table.

This is step 2 of the pipeline. It takes the CSV files produced by the scripts
in ``harvesting/`` (one file per repository), concatenates them, removes
cross-repository duplicates, cleans the free-text description field, derives the
source platform from title/description, and adds the empty columns that are
filled during manual inspection.

Typical use::

    from merge_datasets import merge_datasets

    merged = merge_datasets("data/")
    merged.to_csv("dataset_initial.csv", sep=";", index=False,
                  encoding="utf-8-sig")

Duplicate handling is interactive: the script reports the duplicate groups it
found and asks for confirmation before dropping anything.
"""

import re
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

# Make the shared modules importable regardless of the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Columns that are added empty here and filled in during manual inspection.
MANUAL_ANNOTATION_COLUMNS = [
    "raw",
    "synthetic",
    "collection_described",
    "timestamp",
    "labeled",
    "paper",
    "code",
    "number_posts",
    "topic",
    "macro_topic",
    "task",
    "license",
    "reason",
]

# Final column order of the merged table.
OUTPUT_COLUMNS = [
    "nb",
    "repository",
    "id",
    "doi",
    "url",
    "updated",
    "title",
    "description",
    "platform",
] + MANUAL_ANNOTATION_COLUMNS

# Regular expressions used to infer the source platform from the metadata. The
# lookarounds prevent substring matches (e.g. "gab" inside "gabble"); the
# "X/Twitter" pattern additionally matches a standalone capital "X".
PLATFORM_PATTERNS = {
    "4chan": r"(?i)(?<!\w)(?:4chan)(?!\w)",
    "Facebook": r"(?i)(?<!\w)(?:facebook|fb)(?!\w)",
    "X/Twitter": r"(?:(?i:twitter|x\.com|tweet)|(?<!\S)X(?!\S))",
    "LinkedIn": r"(?i)(?<!\w)(?:linkedin|lnkd\.in)(?!\w)",
    "YouTube": r"(?:(?i:youtube|youtu\.be)|(?<!\S)YT(?!\S))",
    "Instagram": r"(?i)(?<!\w)(?:instagram)(?!\w)",
    "Tiktok": r"(?i)(?<!\w)(?:tiktok)(?!\w)",
    "Tumblr": r"(?i)(?<!\w)(?:tumblr)(?!\w)",
    "Bluesky": r"(?i)(?<!\w)(?:bluesky|bsky|skeet)(?!\w)",
    "Mastodon": r"(?i)(?<!\w)(?:mastodon|toot)(?!\w)",
    "Reddit": r"(?:(?i:reddit)|(?<!\w)r/)",
    "WhatsApp": r"(?i)(?<!\w)(?:whatsapp|wa\.me)(?!\w)",
    "Telegram": r"(?i)(?<!\w)(?:telegram)(?!\w)",
    "Quora": r"(?i)(?<!\w)(?:quora)(?!\w)",
    "Discord": r"(?i)(?<!\w)(?:discord)(?!\w)",
    "Gab": r"(?i)(?<!\w)(?:gab)(?!\w)",
    "Twitch": r"(?i)(?<!\w)(?:twitch)(?!\w)",
    "Truth Social": r"(?i)(?<!\w)(?:truth social)(?!\w)",
}

NOT_SPECIFIED = "Not specified"


def clean_html_column(dataset: pd.DataFrame, column: str) -> pd.DataFrame:
    """Strip HTML markup and collapse whitespace in a text column.

    Several repositories return descriptions as HTML fragments. BeautifulSoup is
    used to extract the plain text; escaped newlines/tabs and runs of whitespace
    are collapsed into single spaces.

    Args:
        dataset: The table to clean. It is not modified in place.
        column: Name of the column holding the HTML text.

    Returns:
        A copy of ``dataset`` with ``column`` cleaned.
    """

    def clean_text(text):
        if pd.isna(text):
            return text

        text = str(text)
        # Remove escaped newlines and tabs that survived the API response.
        text = text.replace("\\n", " ").replace("\\t", " ")

        # Parse the HTML and keep the text nodes only.
        soup = BeautifulSoup(text, "html.parser")
        clean = soup.get_text(separator=" ")

        # Collapse repeated whitespace.
        clean = re.sub(r"\s+", " ", clean)

        return clean.strip()

    dataset = dataset.copy()
    dataset[column] = dataset[column].apply(clean_text)
    return dataset


def determine_platform(dataset: pd.DataFrame) -> pd.DataFrame:
    """Infer the source social-media platform from title and description.

    The title is searched first; only if it yields no match is the description
    consulted, because titles are far less noisy. If several platforms match,
    all of them are recorded as a comma-separated string. If none matches, the
    platform is set to ``"Not specified"``.

    The result is verified and corrected during manual inspection.

    Args:
        dataset: Table with ``title`` and ``description`` columns.

    Returns:
        A copy of ``dataset`` with a filled ``platform`` column.
    """
    compiled = {name: re.compile(pat) for name, pat in PLATFORM_PATTERNS.items()}

    dataset = dataset.copy()
    for i in dataset.index:
        title = dataset.at[i, "title"]
        matches = [
            name
            for name, rx in compiled.items()
            if isinstance(title, str) and rx.search(title)
        ]

        if not matches:
            description = dataset.at[i, "description"]
            matches = [
                name
                for name, rx in compiled.items()
                if isinstance(description, str) and rx.search(description)
            ]

        dataset.at[i, "platform"] = ", ".join(matches) if matches else NOT_SPECIFIED

    return dataset


def find_duplicates(dataset: pd.DataFrame, interactive: bool = True):
    """Identify and optionally remove records that appear in several repositories.

    Two records are treated as the same dataset when they agree on at least two
    of the three identifying columns ``id``, ``doi`` and ``title``. Requiring two
    matches avoids false positives from generic titles. Matching records are
    grouped with a union-find structure so that transitive matches end up in the
    same group; within a group the last record is kept as the original and the
    remaining ones are marked as duplicates.

    Args:
        dataset: The merged table.
        interactive: If True, ask on the command line before dropping rows. Set
            to False for unattended runs, in which case nothing is removed.

    Returns:
        A tuple ``(dataset, duplicates)``. ``dataset`` has the duplicates removed
        if they were confirmed for deletion; ``duplicates`` lists all records
        belonging to a duplicate group, including the retained originals.
    """
    cols = [c for c in ["id", "doi", "title"] if c in dataset.columns]
    if len(cols) < 2:
        print("At least two of the columns id/doi/title are required.")
        return dataset, pd.DataFrame()

    parent = {i: i for i in dataset.index}

    def find(a_item):
        while parent[a_item] != a_item:
            parent[a_item] = parent[parent[a_item]]
            a_item = parent[a_item]
        return a_item

    def union(a_item, b_item):
        ra, rb = find(a_item), find(b_item)
        if ra != rb:
            parent[ra] = rb

    # Count, for every pair of records, on how many identifying columns they agree.
    match_count = {}
    for col in cols:
        sub = dataset[dataset[col].notna()]
        for _, grp in sub.groupby(col):
            ix = grp.index.tolist()
            for a, b in combinations(ix, 2):
                key = (a, b) if a < b else (b, a)
                match_count[key] = match_count.get(key, 0) + 1

    for (a, b), cnt in match_count.items():
        if cnt >= 2:
            union(a, b)

    groups = {}
    for i in dataset.index:
        r = find(i)
        groups.setdefault(r, []).append(i)
    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}

    blocks = []
    for gid, members in enumerate(dup_groups.values()):
        block = dataset.loc[members].copy()
        block["group"] = gid
        # The last record of a group is kept, the preceding ones are duplicates.
        block["status"] = ["Duplicate"] * (len(block) - 1) + ["Original"]
        blocks.append(block)

    duplicates = pd.concat(blocks) if blocks else pd.DataFrame()
    # Sort so that each original is listed next to its duplicates.
    if not duplicates.empty:
        duplicates = duplicates.sort_values(by=["group"]).reset_index()

    print(f"Duplicate records found (originals included): {len(duplicates)}")

    if not duplicates.empty and interactive:
        answer = input("Delete duplicates? (y/n): ").strip().lower()
        if answer == "y":
            to_drop = duplicates.loc[duplicates["status"] == "Duplicate", "index"]
            dataset = dataset.drop(index=to_drop).reset_index(drop=True)
            print(f"Deleted: {len(to_drop)} rows")

    return dataset, duplicates


def merge_datasets(folder_path: str | Path, interactive: bool = True) -> pd.DataFrame:
    """Read every harvest CSV in a folder and merge them into one table.

    The file name (without extension) is used as the ``repository`` label, so the
    files written by the harvesting scripts should keep their default names.

    Args:
        folder_path: Directory containing the per-repository CSV files.
        interactive: Passed through to :func:`find_duplicates`.

    Returns:
        The merged table with the manual-annotation columns added and empty.

    Raises:
        NotADirectoryError: If ``folder_path`` is not a directory.
    """
    folder = Path(folder_path)

    if not folder.is_dir():
        raise NotADirectoryError(f"'{folder}' is not a valid directory.")

    dataframes = []

    for file in sorted(folder.glob("*.csv")):
        dataset = pd.read_csv(file, sep=";")
        dataset["repository"] = file.stem
        dataframes.append(dataset)

    if not dataframes:
        raise FileNotFoundError(f"No CSV files found in '{folder}'.")

    merged = pd.concat(dataframes, ignore_index=True, sort=False)
    merged, _duplicates = find_duplicates(merged, interactive=interactive)

    merged["nb"] = list(range(1, len(merged) + 1))
    merged = clean_html_column(merged, "description")
    merged = determine_platform(merged)

    for column in MANUAL_ANNOTATION_COLUMNS:
        merged[column] = np.nan

    return merged[OUTPUT_COLUMNS]


if __name__ == "__main__":
    from config import DATA_OUTPUT_DIR, data_path

    result = merge_datasets(DATA_OUTPUT_DIR)
    out = data_path("dataset_initial.csv")
    result.to_csv(out, sep=";", index=False, encoding="utf-8-sig")
    print(f"Wrote {len(result)} records to {out}")
