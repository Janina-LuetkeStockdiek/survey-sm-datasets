# Harvesting scripts

One script per repository. Each queries a single repository for publicly
available, English, text-based social media datasets and writes its results to a
CSV in `DATA_OUTPUT_DIR`. The scripts are independent and can be run in any
order, individually or via `../run_harvest.py`.

See [`../README.md`](../README.md) for setup, the full repository table and the
shared pipeline description.

## Running

```bash
cd harvesting
python zenodo_api.py
```

The scripts prepend the parent directory to `sys.path`, so `config.py` and
`query_specs.py` are found when a script is executed from within this folder.

## API scripts

| Script | Repository | Endpoint | Queries |
| --- | --- | --- | --- |
| `kaggle_api.py` | Kaggle | `/dataset_list` | 644 |
| `github_api.py` | GitHub | `/search/repositories` | 644 |
| `huggingface_api.py` | Hugging Face | `/datasets`, `tree/main` | 23 |
| `figshare_api.py` | Figshare | `/articles/search`, `/articles/{id}` | 644 |
| `dryad_api.py` | Dryad | `/search`, `/versions` | 644 |
| `osf_api.py` | OSF | `/trove/index-card-search` | 23 |
| `harvard_dataverse_api.py` | Harvard Dataverse | `/search`, `/dataset` | 23 |
| `scienceDB_api.py` | Science Data Bank | `/api/sdb-query-service/query` | 23 |
| `zenodo_api.py` | Zenodo | `/records` | 23 |
| `data_gov_oai.py` | data.gov (US) | `/catalog.data.gov/search` | 23 |
| `eu_odp_oai.py` | EU Open Data Portal | `/search` | 23 |

The query count depends on Boolean support: repositories that accept `AND`/`OR`
receive one query per platform (23); the rest receive the Cartesian product
`PLATFORMS × TEXT_TERMS` (23 × 28 = 644). Cartesian-product queries return more
results per request and are therefore more likely to hit server-side search
limits, which lowers recall.

## OAI-PMH scripts

| Script | Repository | Endpoint | Metadata format |
| --- | --- | --- | --- |
| `mendeley_oai.py` | Mendeley Data | `data.mendeley.com/oai` | Dublin Core |
| `cessda_oai.py` | CESSDA | `datacatalogue.cessda.eu/oai-pmh/v0/oai` | Dublin Core |
| `rd_aus_oai.py` | Research Data Australia | `researchdata.edu.au/api/registry/oai` | RIF-CS |

OAI-PMH supports only a small set of operations (mainly `ListRecords`) and
filters essentially by timestamp, so these scripts harvest all available metadata
and apply the search constraints on the client side. The harvest is cached, which
makes repeated runs cheap.

## Known issues encountered during the collection reported in the paper

- **Harvard Dataverse, OSF** — frequent HTTP 403 rate-limit responses; the call
  delays were increased. Harvard hit server-side search limits twice, on the
  largest queries (`social network`, `social media`).
- **Science Data Bank** — supports OR-style matching only, so text filtering runs
  client-side. Nine of the 23 platform terms aborted with Elasticsearch query
  failures after more than 90 % of the data had been collected; the runs could
  not be resumed, always stopping at the same point.
