# Social Media Dataset Survey — Pipeline

Code accompanying *A Survey on Publicly Available Text-Based English Social Media Datasets From Online Data Sharing Repositories*.

The pipeline queries fourteen open data repositories — Kaggle, Zenodo, GitHub, Figshare, Hugging Face, CESSDA and others — for publicly available, English, text-based social media datasets, filters the results with an LLM and a manual review, and analyses the resulting catalogue. From 7,291 retrieved records it produced a catalogue of 1,997 hand-annotated datasets.

The catalogue itself is **not in this repository**. It is published as a citable Zenodo record and fetched by `download_data.py`; see [Data](#data) below.

## Quick start

```bash
git clone <repository-url>
cd <repository>

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python download_data.py            # fetch the catalogue from Zenodo into data/
Rscript feature_analysis.R         # reproduce the analysis figures
```

That reproduces the analysis. The FAIR assessment in step 4 needs a local F-UJI
container and takes several hours, so `download_data.py` also fetches its results
from the Zenodo record. Re-run it only if you want to verify them. Re-running the harvest itself needs API tokens and takes hours; see [Setup](#setup) and the note on reproducibility below.

## Data

| | |
| --- | --- |
| Catalogue (Zenodo record) | [10.5281/zenodo.21704651](https://doi.org/10.5281/zenodo.21704651) |
| Interactive dashboard | https://janina-lsd.shinyapps.io/sm-datasets-dashboard/ |
| Dashboard fallback (shinylive, in-browser, if the shinyapps.io hours run out) | https://sm-datasets-dashboard.netlify.app/ |

The record holds these files:

| File | Rows | Content |
| --- | --- | --- |
| `dataset_relevant.csv` | 1,997 | The catalogue: bibliographic metadata plus the hand-annotated features |
| `dataset_initial.csv` | 7,291 | The full harvest before filtering, with LLM votes and exclusion reasons |
| `fair_assessment.csv` | 1,997 | The F-UJI FAIR assessment of every catalogued dataset |
| `development_set.csv` | 100 | Human and LLM labels used to develop the classification prompt |
| `heldout_test_set.csv` | 100 | Untouched sample used for the final performance estimate |
| the reliability material | 234 + 20 | The annotation codebook, the reliability sample with both annotators' labels, the pilot set, the annotation forms and the agreement results |

`download_data.py` writes them into `data/`, which is git-ignored. The Zenodo
record's own README documents every column, category and caveat.

The data are licensed CC BY-NC 4.0; the code in this repository is MIT.

## Layout

```
├── README.md
├── LICENSE                 MIT
├── CITATION.cff            how to cite this repository
├── config.py               local paths and API tokens, read from .env
├── query_specs.py          shared search vocabulary and file-relevance rules
├── harvest_common.py       shared HTTP, language, schema and output helpers
├── download_data.py        fetch the published catalogue from Zenodo
├── run_harvest.py          (1) orchestrator for the harvesting scripts
├── harvesting/             (1) one script per repository
├── backfill/               (1) creation dates resolved a second time, per repository
├── merge_datasets.py       (2) merge harvests, deduplicate, derive platform
├── label_relevant.py       (3) LLM relevance classification and evaluation
├── fair/                   (4) FAIR assessment with F-UJI, plus its Dockerfile
├── feature_analysis.R      (5) exploratory analysis and figures
├── dashboard/              the interactive Shiny dashboard over the catalogue
├── sampling/               the three samples, and the blind annotation workbook
├── annotation/             agreement between the two annotators: Kappa and ICC
├── requirements.txt
├── .env.example            template for the local .env file
└── .gitignore
```

Every harvesting script exposes the same three functions, so switching repository means changing one import:

| Function | Purpose |
| --- | --- |
| `search_records(query, ...)` | Run one query, return a table in the common schema |
| `search_all(queries, ...)` | Run every query, concatenate the results |
| `main()` | Filter, deduplicate and write the CSV; returns an exit code |

The OAI-PMH harvesters add `harvest_records(...)`, which pulls the full metadata set before the queries are applied client-side.

## Pipeline

> **Reproducibility.** Steps 1–3 query live repositories. Repositories change, datasets are deleted and APIs are deprecated, so re-running the harvest will **not** reproduce the exact record set collected on 17 July 2026. The Zenodo record is the frozen snapshot the paper is based on. Step 4 runs on that snapshot and is fully reproducible.

### 1. Harvest the repositories

Each script in `harvesting/` queries one repository, keeps open, English-language datasets that contain at least one relevant data file created on or after `MIN_CREATED_DATE`, and writes a CSV into `DATA_OUTPUT_DIR`.

| Script | Repository | Protocol | Output |
| --- | --- | --- | --- |
| `kaggle_api.py` | Kaggle | API | `kaggle.csv` |
| `github_api.py` | GitHub | API | `github.csv` |
| `huggingface_api.py` | Hugging Face | API | `huggingface.csv` |
| `figshare_api.py` | Figshare | API | `figshare.csv` |
| `dryad_api.py` | Dryad | API | `dryad.csv` |
| `osf_api.py` | OSF (Trove index) | API | `osf.csv` |
| `harvard_dataverse_api.py` | Harvard Dataverse | API | `dataverse.csv` |
| `scienceDB_api.py` | Science Data Bank | API | `science_db.csv` |
| `zenodo_api.py` | Zenodo | API | `zenodo.csv` |
| `data_gov_oai.py` | data.gov (US) | API | `datagov.csv` |
| `eu_odp_oai.py` | EU Open Data Portal | API | `eu_opendata.csv` |
| `mendeley_oai.py` | Mendeley Data | OAI-PMH | `mendeley_oai.csv` |
| `cessda_oai.py` | CESSDA | OAI-PMH | `cessda.csv` |
| `rd_aus_oai.py` | Research Data Australia | OAI-PMH | `rda.csv` |

Run a single repository:

```bash
cd harvesting
python zenodo_api.py
```

Or run several in sequence, with a timestamped log:

```bash
python run_harvest.py                  # every repository used in the paper
python run_harvest.py zenodo figshare  # a subset
```

Harvesting the large repositories takes hours and is bounded by the platforms' rate limits. The scripts retry with exponential backoff (factor 1.5, up to five attempts, capped at 180 s) on HTTP 429/500/502/503/504, and pause 0.5 s between requests and 2 s between queries. Some scripts additionally write a checkpoint file so an interrupted harvest can be resumed cheaply.

Every script follows the same shape:

1. Build queries from `PLATFORMS` × `TEXT_TERMS` (or one query per platform where the API supports Boolean operators).
2. Page through the search endpoint, handling retries and rate limits.
3. Keep public datasets with at least one relevant data file (`is_relevant_file`) created on or after `MIN_CREATED_DATE` and totalling at least `MIN_SIZE_BYTES`.
4. Filter to English, via the repository's own language metadata where available and fastText (`lid.176.bin`) on the description otherwise.
5. Deduplicate, drop records without a title, description or identifier, and write the result to CSV.

#### Creation dates

The repository APIs disagree about what a date means. Kaggle reports only the last
update, and for the remaining repositories the creation date was either not offered
or lost in the merge, so the harvest carries a modification date in `updated`
throughout. `backfill/` resolves every record a second time and asks its repository
for the creation date.

```bash
python3 backfill/fetch_meta_kaggle.py                    # Datasets.csv, about 95 MB
python3 backfill/backfill_created.py --repos Kaggle --meta-kaggle-dir ~/Downloads/meta-kaggle
python3 backfill/backfill_created.py --repos all --retry-failed --datacite-fallback
python3 backfill/apply_created_column.py --report-only   # coverage, writes nothing
python3 backfill/apply_created_column.py                 # add the `created` column
```

Kaggle is the exception that needs the extra step: its API exposes no creation date
at all, so the dates come from an offline join against Meta Kaggle, the platform's
own metadata dump. Every other repository answers natively, with DataCite as the
fallback for records that do not.

`backfill_created.py` writes a lookup table and never touches a catalogue file, so
an interrupted run costs nothing and a rerun continues where it stopped.
`apply_created_column.py` then inserts `created` directly after `updated` and leaves
`updated` in place, which keeps every published figure valid until it is deliberately
recomputed. The catalogue in the Zenodo record already carries the column.

### 2. Merge and deduplicate

```bash
python merge_datasets.py
```

Concatenates the per-repository CSVs, removes cross-repository duplicates (records agreeing on at least two of `id`, `doi`, `title`), strips HTML from the descriptions, derives the source platform from title and description, and adds the empty columns that are filled during manual inspection. The result corresponds to `dataset_initial.csv` in the Zenodo record.

Duplicate removal is interactive: the script reports what it found and asks before deleting. Pass `interactive=False` to `merge_datasets()` for unattended runs.

### 3. Classify relevance

`label_relevant.py` sends each record's title and description to an OpenAI-compatible chat-completions endpoint three times and stores the individual runs plus their majority vote. Two prompts are provided: the initial, stricter `CLASSIFIER_SYSTEM_PROMPT_STRICT` (Listing 1 in the paper) and the final, looser `CLASSIFIER_SYSTEM_PROMPT` (Listing 2), which is the one used for the full catalogue.

```python
import os
from label_relevant import (
    CLASSIFIER_SYSTEM_PROMPT,
    label_relevance,
    confusion_and_metrics,
    annotator_agreement,
)

# Classify the full harvest
label_relevance(
    file="data/dataset_initial.csv",
    prompt_template=CLASSIFIER_SYSTEM_PROMPT,
    model="gpt-oss-120b",
    base_url=os.getenv("LOCAL_LLM_BASE_URL"),
    api_key=os.getenv("LOCAL_LLM_API_KEY"),
)

# Reproduce Tables 4 and 5 of the paper
annotator_agreement("data/development_set.csv")
confusion_and_metrics("data/development_set.csv", model="gpt-oss-120b", prompt="p2")
confusion_and_metrics("data/heldout_test_set.csv", model="gpt-oss-120b")
```

Classification is deliberately tuned for recall (reported as recall and F2 with β = 2): a false negative silently drops a relevant dataset, whereas a false positive only adds screening effort. The predictions stored in the data files were produced with GPT-OSS-120B, Llama-3.3-70B and Gemma 4 served locally, and one proprietary baseline queried through the official API.

Records surviving this step are then reviewed by hand, which both removes false positives and adds the annotated features.

### 4. Assess FAIR compliance

```bash
cd fair
docker build -f Dockerfile.fuji -t fuji-local .    # from a clone of the F-UJI source
docker run -d --name fuji -p 1071:1071 --shm-size=1g fuji-local
python fuji_assessment.py
```

`fuji_assessment.py` sends every catalogued dataset to a local [F-UJI](https://www.f-uji.net) 4.0.0 instance and collects the FAIRsFAIR metrics `metrics_v0.5`. It writes one row per dataset to `data/fair_fuji.csv`, plus the complete JSON response per dataset under `data/fuji_raw/`. The run is resumable, so `Ctrl-C` costs nothing.

**The project's own Docker image does not work.** F-UJI 4.0.0 starts a Playwright Chromium during application startup, but the upstream Dockerfile never runs `playwright install`, so the container exits immediately with code 3. The same Dockerfile also installs the Java runtime Apache Tika needs and removes it again in the same `RUN` instruction, and the published image exists for amd64 only. `Dockerfile.fuji` fixes all three and builds natively on arm64 as well.

Each row is assessed under the best identifier available: the DOI where one exists, the landing-page URL otherwise. 1,506 of the 1,997 datasets carry no DOI, so their low Findability scores are the finding rather than a measurement error. The column `fuji_identifier` records which identifier was used.

`metrics_v0.5` is deliberately not the newest metric set. The published literature reports v0.5 scores, so these results are comparable to existing work and not to scores produced under a different version.


### 5. Analyse

```bash
Rscript feature_analysis.R      # or open it in RStudio and run section by section
```

Reads `data/dataset_relevant.csv` and reproduces the univariate, bivariate and multivariate figures: bar, lollipop and histogram plots for the marginal distributions; heatmaps with marginal record sums for the cross-tabulations; per-month timelines; a bias-corrected Cramér's V association matrix with Holm-adjusted χ² tests; and adjusted standardised Pearson residual heatmaps.

The script is written for interactive use — each section defines a plotting function and then calls it for the variables shown in the paper. Figures are returned as ggplot objects rather than written to disk, so you can inspect them before saving.

The FAIR section of the script reads `data/fair_fuji.csv` and draws the pass rate per metric and the mean score per repository.

Two figures in the paper are schematics rather than results — the pipeline diagram and the workflow overview. They are drawn separately, carry no data beyond the counts printed on them, and are not part of this repository.

## Dashboard

`dashboard/` holds the interactive dashboard that accompanies the paper. It is a Shiny app compiled to WebAssembly with [shinylive](https://posit-dev.github.io/r-shinylive/), so it runs entirely in the browser with no server and no installation. It reproduces the analysis figures from the paper and lets you reconfigure the variables behind each view, filter the catalogue along every annotated feature, and read the 1,997 records as a searchable table.

The app reads a single flat CSV, which joins the FAIR results onto the catalogue. Build it before running the app:

```bash
cd dashboard
python ../download_data.py
python build_dashboard_data.py
```

Then open `app.R` in RStudio and click **Run App**, or export the static site with `shinylive::export(".", "site")`. `dashboard/DEPLOY.md` covers hosting.

## Sampling

`sampling/draw_samples.py` draws the three samples the paper reports, all from a single fixed seed:

| Subcommand | Sample |
| --- | --- |
| `feature-agreement` | The 234-record reliability sample for the feature annotation, in two strata |
| `pilot` | The 20-record pilot set used to refine the annotation codebook |
| `rejected` | The 300-record stratified verification sample from the rejected stratum, which bounds the recall of the pipeline |

Each subcommand refuses to overwrite an existing annotation workbook unless `--force` is given, and writes a timestamped backup when it does.

`sampling/finalize_annotation_workbook.py` prepares the drawn workbook for the
second annotator: it formats the `paper` column as text, since the codebook asks for
the identifier rather than a boolean and Excel would otherwise coerce it, and it
restores the full Kaggle descriptions, whose catalogue entries are capped at 80
characters. It refuses to run once any label cell is filled, so an annotation in
progress cannot be overwritten. The Kaggle descriptions come from a working file
that is not part of the data record. Without it the step is skipped and the
descriptions stay as the catalogue holds them.

## Reliability of the feature annotation

The features in the catalogue were annotated by one person. A second annotator relabelled the 234-record reliability sample independently, working from the annotation codebook and blind to the existing labels. `annotation/feature_agreement.py` quantifies how far the two agree: Cohen's Kappa with asymptotic 95 % confidence intervals for the binary and categorical features, ICC(2,1) on log10(`number_posts`), and a confirmation rate for `synthetic`, which occurs too rarely in the sample to support a Kappa.

```bash
python3 download_data.py                    # fetches the reliability files as well
python3 annotation/feature_agreement.py
python3 annotation/feature_agreement.py --json data/feature_agreement_results.json
```

It reads `data/dataset_feature_agreement_key.csv` for the first annotator and `data/dataset_feature_agreement_ta.xlsx` for the second, and prints the full table followed by every individual disagreement. Records the second annotator could not open are excluded pairwise by default. `--missing disagree` re-runs the analysis counting them as disagreements instead, which is the conservative bound reported alongside the main figures.

Kappa runs from 0.890 for `license` down to 0.561 for `task`, and ICC(2,1) is 0.925. The published output of this script is part of the Zenodo record as `feature_agreement_results.txt` and `feature_agreement_results.json`, and the record's README carries the full table.

## Setup

Python 3.10 or newer and R 4.3 or newer.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

For the analysis step only, `pandas` and `requests` are enough; the rest is needed for harvesting and classification.

For R:

```r
install.packages(c("tidyverse", "ggnewscale", "scales", "lubridate",
                   "RColorBrewer", "factoextra", "ggpubr", "patchwork"))
```

### Harvesting prerequisites

Download the fastText language-identification model [`lid.176.bin`](https://fasttext.cc/docs/en/language-identification.html) (about 130 MB) — every harvesting script needs it.

```bash
cp .env.example .env
```

Then fill in `.env`:

- `FASTTEXT_MODEL_PATH` — absolute path to `lid.176.bin`
- `DATA_OUTPUT_DIR` — where harvest CSVs are written (defaults to `./data`)
- the repository tokens you have. Tokens are optional for the anonymous  endpoints but raise the rate limits enough to matter; without them a full harvest is impractical. `KAGGLE_USERNAME`/`KAGGLE_KEY` follow the standard [Kaggle API](https://www.kaggle.com/docs/api) authentication.
- `LOCAL_LLM_BASE_URL` / `LOCAL_LLM_API_KEY` and `OPENAI_API_KEY` for step 3

`.env` holds credentials. It is listed in `.gitignore` and must never be committed.

## Shared modules

**`query_specs.py`** holds the search vocabulary shared by every harvesting script, so that "a relevant social media text dataset" means the same thing everywhere: the 23 `PLATFORMS` terms and 28 `TEXT_TERMS` of Table 1, the accepted `DATA_FORMATS`, `MIN_SIZE_BYTES` (10 KB), `MIN_CREATED_DATE` (2010-01-01) and the `is_relevant_file()` filter, which rejects boilerplate files such as READMEs and licences.

**`config.py`** reads local paths and tokens from environment variables, loaded from `.env` via python-dotenv. Shell and CI variables take precedence over the file. It exposes `FASTTEXT_MODEL_PATH`, `DATA_OUTPUT_DIR`, the `data_path()` helper (which creates the output directory on demand) and `get_token()`.

**`harvest_common.py`** holds everything the harvesting scripts do identically around their repository-specific query: `make_session()` (the shared retry and backoff policy), `detect_language()` and `is_english()` (the fastText fallback), `summarise_files()` (aggregating a file list into size, extensions and the relevance flag), `standardise()` (reducing a repository response to the common schema), `finalise()` (the filter chain) and `write_output()`.

All three live in the repository root. The scripts in `harvesting/` prepend the parent directory to `sys.path`, so they can be run directly from inside that folder.

### The common output schema

Every harvest CSV has the same nine columns, whichever repository produced it:

| Column | Role |
| --- | --- |
| `id`, `doi`, `url`, `updated`, `title`, `description` | the bibliographic metadata reported in the paper; these survive into the merged catalogue |
| `language`, `total_file_size`, `file_types` | the fields the filters act on, kept so a filtering decision stays checkable |

Everything else a repository happens to return — creator names, affiliations, download and view counts, stars, keywords, subtitles, internal revision numbers — is dropped. None of it is used in the analysis or reported in the paper.

`finalise()` then applies, in order: drop records without an identifier, title or description; drop records below `MIN_SIZE_BYTES`; drop records updated before `MIN_CREATED_DATE`; fill missing languages via fastText and keep English only; deduplicate; sort by update date. Records whose size is unknown are kept — a missing size is a gap in the repository's metadata, not evidence that a dataset is too small. Repositories that publish no sizes at all (data.gov, CESSDA, Mendeley) skip the size threshold explicitly.

## Citing

See [`CITATION.cff`](CITATION.cff). Cite the paper for the study, the Zenodo data record for the catalogue, and this repository's own Zenodo DOI for the code.

## Licence

MIT — see [`LICENSE`](LICENSE). The catalogue published on Zenodo is licensed separately under CC BY-NC 4.0.

The licences cover this work only. The catalogued datasets are third-party works under their own terms; nothing here grants rights to them, and nothing here is legal advice.
