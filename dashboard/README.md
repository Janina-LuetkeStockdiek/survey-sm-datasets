# Social media datasets - Shiny dashboard

Interactive dashboard over the collected social media datasets. Built on the
analysis from `feature_analysis.R`, extended with a full data table and a
variable explorer.

## Files

- `app.R` - self-contained app: libraries, data preparation, UI and server.
- `build_dashboard_data.py` - builds the data file. Run it from this folder.
- `dataset_relevant.csv` - the data file (must sit in this folder, and is not in
  the repository). It is the catalogue with the FAIR assessment joined on. Do not
  edit it by hand and do not replace it with the plain catalogue, or the FAIR
  views fall back to a placeholder message and the FAIR score filter has nothing
  to act on. The file carries both date columns, `updated` and `created`, and
  `created` is empty for a handful of records.

Build it like this:

```bash
python ../download_data.py        # fetch the catalogue into ../data
python ../fair/fuji_assessment.py # produce ../data/fair_fuji.csv
python build_dashboard_data.py    # join the two into dataset_relevant.csv
```

The title bar shows no logo. The navy `#182F50` and gold `#C7A24C` colour scheme is
defined via `pal_*` constants near the top of `app.R`. Adjust them there to retheme.

## Run

Open `app.R` in RStudio and click **Run App**, or in the R console:

```r
setwd("path/to/social_media_dashboard")
shiny::runApp()
```

## Required packages

Dependencies were deliberately kept lean (base R is used for reading, string and
table work) so the app also loads quickly under Shinylive:

```r
install.packages(c("shiny", "bslib", "dplyr", "lubridate", "ggplot2", "plotly", "DT"))
```

`bslib` should be >= 0.6 (for `value_box(theme = ...)` and `accordion`).

## Deployment

### Classic Shiny server

Publish the folder as an app on **shinyapps.io** or **Posit Connect**:

```r
rsconnect::deployApp("social_media_dashboard")
```

### Shinylive (runs fully in the browser, no server)

The app is Shinylive-compatible: no server-only features, and every dependency is
available as a webR/WebAssembly binary. `dataset_relevant.csv` is read via a relative
path and is bundled automatically.

`DEPLOY.md` covers hosting, including the anonymous route for a blinded submission.

```r
install.packages("shinylive")
shinylive::export("social_media_dashboard", "site")   # writes a static site to ./site
httpuv::runStaticServer("site")                        # preview locally
```

The `site/` folder is a static bundle you can host anywhere (GitHub Pages, Netlify,
S3, ...). Note: the first load downloads webR plus the seven R packages (mainly
plotly, DT and ggplot2), which can take a few seconds to ~1 minute depending on the
connection; afterwards everything runs client-side.

## Concept / usage

**Date basis.** The sidebar radio *Date basis* decides which date the whole
dashboard means by "year": the creation date (default) or the date of the last
update. It moves everything at once - the year slider and its bounds, the year
bar chart, the timeline, the stacked per-year chart, the year axis of the
heatmap, Cramer's V and the residual view, and the two date columns in the
table, which are labelled with the basis in force. Records without a date on
the selected basis stay in the selection; the note under the switch says how
many that is. Internally no view reads `updated` or `created` directly, they
all read `date_dt` and `year`, which `bdata()` fills from the switch - keep it
that way when adding a view. An older data file without a `created` column
hides the switch and behaves as the app did before.

**Bidirectional filtering.** The sidebar filters (platform, repository, macro
topic, year, binary attributes) are the single source of truth. All charts and
the table read the same filtered data, so any filter change updates everything at
once. Conversely, **clicking a bar** (platform / repository / topic / year) sets
exactly that filter, so the table and all other charts filter along. "Reset
filters" clears everything again.

**Layout (a single scrollable page, filters in the sidebar)**

Top to bottom:

1. **Key figures** - datasets, total posts, platforms, macro topics (react to filters).
2. **Data table** - large, searchable table (70vh tall, 50 rows/page). Long cell text is truncated to one line; hover a truncated cell to read the full value.
3. **Univariate overview** - pick any variable and inspect its distribution + summary statistics; on filtered or full data (numeric variables get a log option and a median line).
4. **Single-variable bar charts** - platform, repository, macro topic, year, license, task. Clicking platform / repository / macro topic / year filters the whole dashboard.
5. **Binary distribution** (lollipop, incl. `code`).

Below a visual divider ("Multivariate views"). **Every multivariate plot lets you
freely choose its variable(s)** from a shared pool: macro topic, repository,
platform, year, task, license and all binary attributes.

6. **Timeline** (group by any variable, top-5 categories) next to a **stacked datasets-per-year bar chart** (stack by any variable), followed by a **share-per-year line chart** (composition within each year, with the variable, the number of levels and the minimum records per year selectable) and the **creation-against-last-update card** (an annual and a monthly panel showing both date series at once - the one view that deliberately ignores the date-basis switch).
7. **Heatmap** - free choice of both axes (any variable x any variable), number of datasets or sum of posts.
8. **Cramer's V** - bias-corrected association matrix over a freely chosen set of variables, with Holm-adjusted chi-square significance stars; high-cardinality variables lumped to top levels.
9. **Residual analysis** - Agresti adjusted standardized residuals: pick the row variable and any set of binary flags (columns). Gold = above expected, navy = below; * marks |residual| > 1.96.

The PCA section was removed. Cramer's V and the residual heatmaps are ported 1:1
from `feature_analysis.R` and, like every other panel, react to the current filters.

## Notes / differences from the original script

- **More robust date parsing:** `updated` contains mixed formats
  (`2026-06-02 13:49:17+00:00` *and* `23.03.21 20:58`). Parsing covers `ymd` and
  `dmy` variants so almost all rows get a year. `created` is parsed the same way
  and, unlike `updated`, is missing for a few records - those keep `NA` and pass
  the year filter rather than dropping out of the selection.
- **Two views are ported from `feature_analysis.R`:** `create_platform_share_plot()` became the
  share-per-year chart, with its fixed `platform` opened up to any variable, and
  `create_date_comparison()` became the creation-against-last-update card. Both are rebuilt
  without `tidyr` and `patchwork`, which are not dependencies here - the grids are completed with
  `table()` and `prop.table()`, and the two panels are two plots instead of a patchwork stack. On
  the same data both reproduce the printed figures value for value, which was checked against the
  originals rather than assumed. When one of them changes in the script, change it here too.
- **`code`** is treated as a binary attribute (like raw/labeled/...): it appears in
  the sidebar filters, the binary-distribution lollipop, Cramer's V and the
  repository residual heatmap.
- Colour scheme uses navy `#182F50` + gold `#C7A24C` and the `pal_qual()`
  qualitative palette (defined near the top of `app.R`).
