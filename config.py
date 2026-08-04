"""Shared configuration for the dataset-harvesting scripts.

All environment-specific settings (local file paths and API tokens) are read
from environment variables. These are loaded from a local ``.env`` file via
``python-dotenv`` when this module is imported, so every script only needs to
``import config`` (or ``from config import ...``) to pick them up.

Getting started:
    1. Copy ``.env.example`` to ``.env``.
    2. Fill in your API tokens and local paths.
    3. Download the fastText language-identification model ``lid.176.bin``
       (https://fasttext.cc/docs/en/language-identification.html) and point
       ``FASTTEXT_MODEL_PATH`` at it.

Nothing in this module has side effects beyond loading the ``.env`` file and
(when :func:`data_path` is used) creating the output directory.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load variables from a local .env file into the process environment. Existing
# environment variables always take precedence, so the values can also be set
# by the shell / CI without editing .env.
load_dotenv()

# --------------------------------------------------------------------------- #
# Local file paths
# --------------------------------------------------------------------------- #

# Absolute path to the fastText language-identification model (lid.176.bin),
# used by every script for the English-language filter.
FASTTEXT_MODEL_PATH = os.getenv("FASTTEXT_MODEL_PATH", "")

# Directory into which the resulting CSV files are written. Defaults to a local
# "data" folder next to the scripts so the code runs out of the box.
DATA_OUTPUT_DIR = os.getenv("DATA_OUTPUT_DIR", "data")


def data_path(filename: str) -> str:
    """Return the full path for an output file inside ``DATA_OUTPUT_DIR``.

    The output directory is created if it does not yet exist.
    """
    Path(DATA_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    return str(Path(DATA_OUTPUT_DIR) / filename)


# --------------------------------------------------------------------------- #
# API tokens
# --------------------------------------------------------------------------- #

def get_token(name: str):
    """Return the API token stored under the environment variable ``name``.

    Returns ``None`` when the variable is unset. Kept as a thin helper so token
    access is consistent across scripts.
    """
    return os.getenv(name)
