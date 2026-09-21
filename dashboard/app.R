# =====================================================================
#  Social media datasets - Dashboard (app.R)
#  Framework: Shiny + bslib + plotly + DT + ggplot2
#
#  Bidirectional filtering: the sidebar filters are the single source of
#  truth; fdata() feeds every chart/table; clicking a bar sets a filter.
#
#  Page order (single scrollable page):
#    title -> key figures -> large data table -> univariate variable
#    overview -> overview bar charts -> binary distribution + timeline ->
#    share per year -> creation against last update -> heatmap -> FAIR assessment -> Cramer's V association matrix ->
#    residual analysis.
#
#  Time is switchable: the sidebar radio "Date basis" decides whether every
#  year-based view reads the creation date (default) or the last-update date.
#  Views never touch `created`/`updated` directly, only `date_dt` and `year`.
#
#  The data file carries the FAIR columns (fair_score, fair_F/A/I/R and one
#  fm_<metric> flag per FsF metric). They are joined onto the catalogue by
#  build_dashboard_data.py so that the app keeps a single data source and
#  every FAIR view reacts to the sidebar filters like all other views.
#
#  Cramer's V and the residual heatmaps are ported 1:1 from feature_analysis.R
#  (bias-corrected Cramer's V + Holm stars; Agresti adjusted residuals).
#«
#  NOTE: self-contained - runs via "Run App", shiny::runApp() or source("app.R").
# =====================================================================

# ======================= Libraries ===================================
library(shiny)
library(bslib)
library(dplyr)
library(lubridate)
library(ggplot2)
library(plotly)
library(DT)

# --- Shinylive: force-bundle transitive dependencies that the static scan
# --- misses. webR's scales (1.3.0) still needs {munsell}, but newer local
# --- scales no longer lists it, so shinylive skips it -> ggplot2 fails to load.
# --- This block never runs; it only tells shinylive to include these packages.
if (FALSE) {
  library(munsell)
  library(farver)
  library(labeling)
}

# ======================= Colour scheme ===============================
pal_navy       <- "#182F50"   # primary
pal_navy_light <- "#3E6DA0"
pal_gold       <- "#C7A24C"   # accent

# Shared qualitative palette for plots with many categories
pal_qual <- function(n) {
  anchors <- c("#182F50", "#3E6DA0", "#7FB0D6", "#4F9D8E",
               "#8FBF73", "#C7A24C", "#9C6B2E", "#7E4A57")
  grDevices::colorRampPalette(anchors)(max(n, 1))
}
# aliases used across the app
brand_navy       <- pal_navy
brand_navy_light <- pal_navy_light
brand_gold       <- pal_gold

# ======================= Orderings / codes ===========================
platform_order <- c("X/Twitter", "Bluesky", "Mastodon", "Gab", "Truth Social", "Facebook",
                    "LinkedIn", "Reddit", "4chan", "Tumblr", "YouTube", "Tiktok", "Instagram",
                    "Telegram", "WhatsApp", "Quora", "Discord", "Twitch", "Multiple",
                    "Not specified")

topic_order <- c("General", "Conflicts", "Politics", "Countries", "Economics", "Finance",
                 "Science", "Culture", "Celebrities", "Harmful Content", "COVID-19", "Health",
                 "Sports", "Disaster", "Environment")

repository_order <- c("Dryad", "Figshare", "Harvard Dataverse", "ScienceDB", "Zenodo",
                      "MendeleyData", "OSF", "Kaggle", "GitHub", "HuggingFace",
                      "CESSDA", "RDA", "EU-ODP")

# Binary attributes (code is now included, matching feature_analysis.R)
binary_vars <- c("collection_described", "timestamp", "labeled", "raw", "synthetic",
                 "paper_binary", "code")
binary_labels <- c(collection_described = "Collection described", timestamp = "Timestamped",
                   labeled = "Labeled", raw = "Raw data", synthetic = "Synthetic",
                   paper_binary = "Paper", code = "Code available")

# Selectable dimensions for every multivariate plot (categoricals + binaries).
# Keeps all those plots maximally flexible from a single shared choice list.
dim_choices <- c("Macro topic" = "macro_topic", "Repository" = "repository",
                 "Platform" = "platform", "Year" = "year", "Task" = "task",
                 "License" = "license",
                 "Collection described" = "collection_described", "Timestamped" = "timestamp",
                 "Labeled" = "labeled", "Raw" = "raw", "Synthetic" = "synthetic",
                 "Paper" = "paper_binary", "Code available" = "code")
dim_label <- function(v) { i <- match(v, dim_choices); ifelse(is.na(i), v, names(dim_choices)[i]) }

# ======================= Helper functions ============================
to_int_bool <- function(x) {
  x <- trimws(tolower(as.character(x)))
  dplyr::case_when(x == "true" ~ 1L, x == "false" ~ 0L, TRUE ~ NA_integer_)
}

# ---- Cramer's V toolkit (ported from feature_analysis.R) ------------
cramers_v <- function(a, b) {                       # bias-corrected (Bergsma)
  tab <- table(a, b)
  if (nrow(tab) < 2 || ncol(tab) < 2) return(NA_real_)
  chi2 <- suppressWarnings(stats::chisq.test(tab, correct = FALSE)$statistic)
  n <- sum(tab); r <- nrow(tab); k <- ncol(tab)
  phi2     <- as.numeric(chi2) / n
  phi2corr <- max(0, phi2 - (r - 1) * (k - 1) / (n - 1))
  rcorr    <- r - (r - 1)^2 / (n - 1)
  kcorr    <- k - (k - 1)^2 / (n - 1)
  denom    <- min(kcorr - 1, rcorr - 1)
  if (denom <= 0) return(NA_real_)
  sqrt(phi2corr / denom)
}

chisq_p <- function(a, b) {
  tab <- table(a, b)
  if (nrow(tab) < 2 || ncol(tab) < 2) return(NA_real_)
  suppressWarnings(stats::chisq.test(tab)$p.value)
}

# Collapse high-cardinality categoricals to top-N levels + "Other"
lump_levels <- function(x, n = 8, other = "Other", na_label = "Not specified") {
  x <- as.character(x); x[is.na(x) | x == ""] <- na_label
  keep <- names(sort(table(x), decreasing = TRUE))[seq_len(min(n, length(unique(x))))]
  factor(ifelse(x %in% keep, x, other))
}

yesno <- function(x) factor(ifelse(x == 1, "Yes", "No"), levels = c("No", "Yes"))

# Turn any selected variable into a factor: binaries -> Yes/No, high-cardinality
# categoricals lumped to their top levels (+ "Other") so Cramer's V is not inflated.
lump_for <- function(d, v) {
  if (v %in% binary_vars) return(yesno(d[[v]]))
  n <- switch(v, macro_topic = 15, task = 15, license = 6, platform = 8,
              repository = 8, year = 100, 10)
  lump_levels(d[[v]], n = n)
}

build_cramer_feat <- function(d, vars) {
  out <- lapply(vars, function(v) lump_for(d, v))
  names(out) <- vapply(vars, dim_label, character(1))
  as.data.frame(out, check.names = FALSE)
}

cramers_v_heatmap <- function(feat) {
  vars <- colnames(feat); m <- length(vars)
  V <- matrix(NA_real_, m, m, dimnames = list(vars, vars))
  P <- matrix(NA_real_, m, m, dimnames = list(vars, vars))
  for (i in seq_len(m)) for (j in seq_len(m)) {
    if (i == j) { V[i, j] <- 1; P[i, j] <- 0; next }
    V[i, j] <- cramers_v(feat[[i]], feat[[j]])
    P[i, j] <- chisq_p(feat[[i]], feat[[j]])
  }
  ut   <- upper.tri(P)
  Padj <- matrix(NA_real_, m, m, dimnames = list(vars, vars))
  Padj[ut] <- p.adjust(P[ut], method = "holm")
  Padj[lower.tri(Padj)] <- t(Padj)[lower.tri(Padj)]

  long <- expand.grid(Var1 = vars, Var2 = vars, stringsAsFactors = FALSE) %>%
    mutate(
      V     = mapply(function(a, b) V[a, b], Var1, Var2),
      padj  = mapply(function(a, b) Padj[a, b], Var1, Var2),
      stars = dplyr::case_when(is.na(padj) ~ "", padj < .001 ~ "***",
                               padj < .01 ~ "**", padj < .05 ~ "*", TRUE ~ ""),
      label = ifelse(Var1 == Var2, "", paste0(sprintf("%.2f", V), stars)),
      Var1  = factor(Var1, levels = vars),
      Var2  = factor(Var2, levels = rev(vars))
    )

  ggplot(long, aes(Var1, Var2, fill = V)) +
    geom_tile(color = "white", linewidth = 0.4) +
    geom_text(aes(label = label, color = V > 0.5), size = 3.5, show.legend = FALSE) +
    scale_color_manual(values = c(`TRUE` = "white", `FALSE` = pal_navy)) +
    scale_fill_gradient(low = "white", high = pal_navy, limits = c(0, 1),
                        name = "Cramer's V", na.value = "grey90") +
    coord_fixed() +
    labs(x = NULL, y = NULL,
         subtitle = "Stars = Holm-adjusted chi-square: *p<.05  **p<.01  ***p<.001") +
    theme_minimal(base_size = 14) +
    theme(axis.text.x = element_text(angle = 45, hjust = 1),
          panel.grid  = element_blank(),
          plot.subtitle = element_text(size = 10, color = "grey40"))
}

# ---- Category-level residual heatmap (ported) -----------------------
residual_heatmap <- function(data, cat_var, flags, cat_order = NULL,
                             min_n = 10, flag_labels = NULL,
                             low = pal_navy, high = pal_gold, title = NULL) {

  d <- data %>% filter(!is.na(.data[[cat_var]]))
  d[[cat_var]] <- as.character(d[[cat_var]])

  keep <- d %>% count(.data[[cat_var]], name = "n") %>% filter(n >= min_n) %>% pull(1)
  d <- d %>% filter(.data[[cat_var]] %in% keep)
  if (nrow(d) == 0) return(NULL)

  N    <- nrow(d)
  cats <- if (is.null(cat_order)) sort(unique(d[[cat_var]])) else cat_order[cat_order %in% keep]

  res <- lapply(flags, function(fl) {
    x <- d[[fl]]; col_yes <- sum(x == 1, na.rm = TRUE)
    lapply(cats, function(c) {
      m  <- d[[cat_var]] == c; ni <- sum(m); o <- sum(x[m] == 1, na.rm = TRUE)
      e  <- ni * col_yes / N
      dstd <- if (e > 0) (o - e) / sqrt(e * (1 - ni / N) * (1 - col_yes / N)) else NA_real_
      data.frame(category = c, flag = fl, pct = 100 * o / ni, resid = dstd, n = ni,
                 stringsAsFactors = FALSE)
    }) %>% bind_rows()
  }) %>% bind_rows()

  flag_levels <- flags
  if (!is.null(flag_labels)) {
    res$flag    <- flag_labels[res$flag]
    flag_levels <- unname(flag_labels[flags])
  }

  lim <- max(abs(res$resid), na.rm = TRUE); if (!is.finite(lim) || lim == 0) lim <- 1
  res <- res %>%
    mutate(category = factor(category, levels = rev(cats)),
           flag     = factor(flag, levels = flag_levels),
           label    = paste0(round(pct), "%", ifelse(abs(resid) > 1.96, "*", "")))

  ggplot(res, aes(flag, category, fill = resid)) +
    geom_tile(color = "white", linewidth = 0.4) +
    geom_text(aes(label = label, color = resid < -0.4 * lim), size = 5.4, show.legend = FALSE) +
    scale_color_manual(values = c(`TRUE` = "white", `FALSE` = "grey15")) +
    scale_fill_gradient2(low = low, mid = "white", high = high, midpoint = 0,
                         limits = c(-lim, lim), name = "Adjusted residual") +
    labs(x = NULL, y = NULL, title = title) +
    theme_minimal(base_size = 17) +
    theme(axis.text.x = element_text(angle = 45, hjust = 1), panel.grid = element_blank())
}

flag_lab <- c(timestamp = "timestamp", labeled = "labeled", raw = "raw",
              paper_binary = "paper", code = "code",
              collection_described = "collection\ndescribed", synthetic = "synthetic")

# ======================= Load & prepare data =========================
data_path <- "dataset_relevant.csv"

# Base-R reader (avoids the readr/vroom dependency for a lighter Shinylive build).
# All columns read as character; numeric/date columns are converted explicitly below.
df <- utils::read.delim(data_path, sep = ";", quote = "\"", header = TRUE,
                        colClasses = "character", check.names = FALSE,
                        na.strings = c("", "NA"), comment.char = "", fill = TRUE,
                        fileEncoding = "UTF-8")

if ("nb" %in% names(df)) df <- dplyr::select(df, -nb)

# Two date bases. `updated` is the last-modification date and exists for every
# record; `created` is the creation / first-publication date and is missing for
# a handful of records. Both are parsed here, but no view reads either column
# directly: every time-related view reads `date_dt` and `year`, which bdata()
# fills from the basis chosen in the sidebar. One switch moves them all.
parse_dt <- function(x) lubridate::parse_date_time(
  x, orders = c("ymd HMS", "ymd HM", "ymd", "dmy HMS", "dmy HM", "dmy"),
  tz = "UTC", quiet = TRUE)

has_created <- "created" %in% names(df)

df <- df %>%
  mutate(
    updated_dt = parse_dt(updated),
    across(all_of(c("raw", "synthetic", "collection_described", "timestamp", "labeled", "code")),
           to_int_bool),
    paper_binary = dplyr::case_when(
      trimws(tolower(paper)) == "false" ~ 0L,
      !is.na(paper) & trimws(paper) != "" ~ 1L,
      TRUE ~ NA_integer_),
    number_posts = suppressWarnings(as.numeric(number_posts)),
    platform = ifelse(!is.na(platform) & grepl(",", platform), "Multiple", platform)
  ) %>%
  mutate(.rid = dplyr::row_number())

df$created_dt   <- if (has_created) parse_dt(df$created) else as.POSIXct(NA, tz = "UTC")
df$year_updated <- lubridate::year(df$updated_dt)
df$year_created <- lubridate::year(df$created_dt)

# Selectable date bases. Creation is the default: it says when a dataset
# entered the record, while the update date also moves for purely editorial
# changes. "Last update" stays one click away. An older data file without a
# `created` column simply loses the switch and behaves as before.
date_bases <- c("Creation date" = "created", "Last update" = "updated")
if (!has_created || all(is.na(df$year_created)))
  date_bases <- date_bases[date_bases != "created"]
default_basis <- unname(date_bases[1])
basis_word <- function(b) if (identical(b, "created")) "creation" else "last update"

# The two bases do not span the same years, so the slider is re-fitted
# whenever the basis changes instead of keeping one shared range.
year_bounds <- function(v) {
  r <- suppressWarnings(range(v, na.rm = TRUE))
  if (any(!is.finite(r))) c(2005L, as.integer(format(Sys.Date(), "%Y"))) else as.integer(r)
}
yr_bounds <- list(updated = year_bounds(df$year_updated),
                  created = year_bounds(df$year_created))
n_missing_date <- c(updated = sum(is.na(df$year_updated)),
                    created = sum(is.na(df$year_created)))
year_min <- yr_bounds[[default_basis]][1]
year_max <- yr_bounds[[default_basis]][2]
platform_choices   <- df %>% count(platform, sort = TRUE) %>% pull(platform)
repository_choices <- df %>% count(repository, sort = TRUE) %>% pull(repository)
topic_choices      <- df %>% count(macro_topic, sort = TRUE) %>% pull(macro_topic)
task_choices       <- df %>% filter(!is.na(task)) %>% count(task, sort = TRUE) %>% pull(task)
license_choices    <- df %>% filter(!is.na(license)) %>% count(license, sort = TRUE) %>% pull(license)

explorer_numeric   <- c("number_posts", "year")
explorer_categoric <- c("platform", "repository", "macro_topic", "topic", "task", "license",
                        "collection_described", "timestamp", "labeled", "raw",
                        "synthetic", "paper_binary", "code")

# ---- FAIR assessment ------------------------------------------------
# Short names of the FsF metrics (metric version 0.5). The FAIR principle is
# encoded in the identifier itself (FsF-R1.1-01M -> R), so it is derived
# rather than stored twice.
fair_metric_labels <- c(
  "FsF-F1-01D"   = "Unique identifier",
  "FsF-F1-02D"   = "Persistent identifier",
  "FsF-F2-01M"   = "Descriptive core metadata",
  "FsF-F3-01M"   = "Data identifier in metadata",
  "FsF-F4-01M"   = "Searchable metadata",
  "FsF-A1-01M"   = "Data access information",
  "FsF-A1-02M"   = "Standard protocol (metadata)",
  "FsF-A1-03D"   = "Standard protocol (data)",
  "FsF-I1-01M"   = "Formal representation of metadata",
  "FsF-I2-01M"   = "Metadata with semantic resources",
  "FsF-I3-01M"   = "Links to related entities",
  "FsF-R1-01MD"  = "Metadata of data content",
  "FsF-R1.1-01M" = "Data usage license",
  "FsF-R1.2-01M" = "Data provenance",
  "FsF-R1.3-01M" = "Community-endorsed standard",
  "FsF-R1.3-02D" = "Data file format"
)
fair_categories <- c("F", "A", "I", "R")

# Only the metrics the data file actually carries (guards an older CSV).
fair_metric_ids <- names(fair_metric_labels)[
  paste0("fm_", names(fair_metric_labels)) %in% names(df)]
has_fair <- "fair_score" %in% names(df) && length(fair_metric_ids) > 0
if (has_fair) {
  df$fair_score <- suppressWarnings(as.numeric(df$fair_score))
  for (cc in fair_categories) {
    df[[paste0("fair_", cc)]] <- suppressWarnings(as.numeric(df[[paste0("fair_", cc)]]))
  }
  for (m in fair_metric_ids) {
    df[[paste0("fm_", m)]] <- suppressWarnings(as.integer(df[[paste0("fm_", m)]]))
  }
}

# ------------------------------- UI ----------------------------------
sidebar_ui <- sidebar(
  title = "Filters", width = 300, open = TRUE,
  actionButton("reset", "Reset filters", icon = icon("rotate-left"),
               class = "btn-outline-secondary btn-sm"),
  textOutput("n_selected"),
  hr(),
  selectizeInput("f_platform", "Platform", choices = platform_choices,
                 multiple = TRUE, options = list(placeholder = "all")),
  selectizeInput("f_repository", "Repository", choices = repository_choices,
                 multiple = TRUE, options = list(placeholder = "all")),
  selectizeInput("f_topic", "Macro topic", choices = topic_choices,
                 multiple = TRUE, options = list(placeholder = "all")),
  selectizeInput("f_task", "Task", choices = task_choices,
                 multiple = TRUE, options = list(placeholder = "all")),
  selectizeInput("f_license", "License", choices = license_choices,
                 multiple = TRUE, options = list(placeholder = "all")),
  if (length(date_bases) > 1)
    radioButtons("f_datebasis", "Date basis", choices = date_bases,
                 selected = default_basis, inline = TRUE),
  div(class = "text-muted small mb-2", style = "margin-top:-.5rem;",
      textOutput("basis_note", inline = TRUE)),
  sliderInput("f_year", sprintf("Year (%s)", basis_word(default_basis)),
              min = year_min, max = year_max,
              value = c(year_min, year_max), step = 1, sep = ""),
  sliderInput("f_fair", "FAIR score (percent)",
              min = 0, max = 100, value = c(0, 100), step = 1, sep = ""),
  accordion(
    open = FALSE,
    accordion_panel(
      "Binary attributes",
      lapply(binary_vars, function(v) {
        selectInput(paste0("f_", v), binary_labels[[v]],
                    choices = c("All" = "all", "Yes (1)" = "1", "No (0)" = "0"),
                    selected = "all")
      })
    )
  )
)

brand_theme <- bs_theme(
  version = 5, primary = pal_navy, secondary = pal_gold,
  info = pal_navy_light, success = pal_gold, "navbar-bg" = pal_navy
)

# Plain text title (logo removed to keep the app anonymous for review)
brand_title <- tags$span("Social media datasets", style = "font-weight:600;")

ui <- page_sidebar(
  title = brand_title,
  theme = brand_theme,
  sidebar = sidebar_ui,
  fillable = FALSE,

  tags$head(tags$style(HTML(sprintf("
    .navbar { border-bottom: 3px solid %s; }
    .navbar .navbar-brand, .navbar-brand span { color: #fff !important; width:100%%; }
    .card-header { color: %s; font-weight: 600; border-bottom: 2px solid %s; }
    .bslib-sidebar-layout > .sidebar { border-top: 3px solid %s; }
    a, .btn-link { color: %s; }
  ", pal_gold, pal_navy, pal_gold, pal_gold, pal_navy)))),

  # ---- Key figures ------------------------------------------------
  layout_columns(
    fill = FALSE, height = "92px",
    value_box("Datasets", textOutput("vb_n"), showcase = icon("database"), theme = "primary"),
    value_box("Total posts", textOutput("vb_posts"), showcase = icon("hashtag"), theme = "secondary"),
    value_box("Platforms", textOutput("vb_platforms"), showcase = icon("share-nodes"), theme = "info"),
    value_box("Macro topics", textOutput("vb_topics"), showcase = icon("tags"), theme = "success"),
    value_box("Mean FAIR", textOutput("vb_fair"), showcase = icon("certificate"), theme = "primary")
  ),

  # ---- 1) Large data table ----------------------------------------
  card(
    card_header("Data table (filtered)"),
    DTOutput("table")
  ),

  div(class = "text-muted small mt-2 mb-1",
      "Tip: clicking a bar below filters the table and all charts. ",
      "'Reset filters' (sidebar) clears everything again."),

  # ---- 2) Univariate variable overview ----------------------------
  card(
    card_header("Univariate overview - single variable distribution"),
    layout_columns(
      fill = FALSE,
      selectInput("ex_var", "Variable", width = 260,
                  choices = c(explorer_categoric, explorer_numeric), selected = "number_posts"),
      radioButtons("ex_scope", "Data basis", inline = TRUE,
                   choices = c("Filtered" = "filtered", "Full" = "all"), selected = "filtered"),
      checkboxInput("ex_log", "Log scale (numeric only)", value = TRUE)
    ),
    layout_columns(
      col_widths = c(8, 4),
      plotlyOutput("ex_plot", height = 420),
      div(h6("Summary statistics"), tableOutput("ex_summary"))
    )
  ),

  # ---- 3) Single-variable bar charts ------------------------------
  card(
    card_header("Single-variable distributions (click any bar to filter)"),
    layout_columns(
      col_widths = c(4, 4, 4),
      plotlyOutput("ov_platform", height = 300),
      plotlyOutput("ov_repository", height = 300),
      plotlyOutput("ov_topic", height = 300)
    ),
    layout_columns(
      col_widths = c(4, 4, 4),
      plotlyOutput("ov_year", height = 300),
      plotlyOutput("ov_license", height = 300),
      plotlyOutput("ov_task", height = 300)
    )
  ),
  card(card_header("Share of binary attributes"), plotlyOutput("ov_binary", height = 320)),

  # ===== visual divider: FAIR assessment ==========================
  div(class = "mt-4 mb-2",
      style = sprintf("border-top: 3px solid %s;", pal_gold),
      h4("FAIR assessment",
         style = sprintf("color:%s; font-weight:600; margin-top:.6rem; margin-bottom:0;", pal_navy)),
      tags$span(class = "text-muted small",
                "Automated assessment against the FAIRsFAIR metrics (F-UJI, metric version 0.5). ",
                "Both views react to the filters.")),

  card(
    card_header("Metrics - share of records passing each test"),
    div(class = "text-muted small mb-2",
        "Colour gives the FAIR principle the metric belongs to. ",
        "The principle is also named in each label, so the bars stay readable without the colours."),
    plotlyOutput("fair_metrics", height = 480)
  ),

  card(
    card_header("Mean score per repository and FAIR principle"),
    div(class = "text-muted small mb-2",
        "Rows carry the number of records they summarise. Repositories below the minimum ",
        "count are pooled, because a mean over one or two records fills a tile just as ",
        "strongly as a mean over a thousand."),
    layout_columns(
      fill = FALSE,
      numericInput("fair_min_n", "Minimum records per repository", value = 10,
                   min = 1, max = 200, step = 1, width = 260)
    ),
    plotOutput("fair_repo", height = 520)
  ),

  # ===== visual divider: simple bars above, multivariate below =====
  div(class = "mt-4 mb-2",
      style = sprintf("border-top: 3px solid %s;", pal_gold),
      h4("Multivariate views",
         style = sprintf("color:%s; font-weight:600; margin-top:.6rem; margin-bottom:0;", pal_navy)),
      tags$span(class = "text-muted small", "Plots relating two or more variables.")),

  # ---- 4) Timeline + stacked year bar chart -----------------------
  layout_columns(
    col_widths = c(6, 6),
    card(
      card_header("Timeline"),
      selectInput("tl_group", "Grouping", choices = c("Total" = "none", dim_choices),
                  selected = "none", width = 220),
      plotlyOutput("ov_timeline", height = 360)
    ),
    card(
      card_header("Datasets per year (stacked)"),
      selectInput("sy_group", "Stack by", choices = dim_choices[dim_choices != "year"],
                  selected = "platform", width = 220),
      plotlyOutput("ov_stackyear", height = 360)
    )
  ),

  # ---- 4b) Share per year -----------------------------------------
  card(
    card_header("Share per year - composition over time"),
    div(class = "text-muted small mb-2",
        "Shares within each year, so a year holding few datasets weighs as much as a large one. ",
        "The levels are ranked over the whole selection rather than per year, otherwise the lines ",
        "would not be comparable. Records naming no value are left out, 'Multiple' joins 'Other', ",
        "and years below the minimum count are dropped because a share over six records is noise."),
    layout_columns(
      fill = FALSE,
      selectInput("sh_var", "Variable", choices = dim_choices[dim_choices != "year"],
                  selected = "platform", width = 220),
      numericInput("sh_top", "Levels shown", value = 6, min = 2, max = 12, step = 1, width = 170),
      numericInput("sh_minn", "Minimum records per year", value = 10, min = 1, max = 500,
                   step = 1, width = 250)
    ),
    plotlyOutput("ov_share", height = 420)
  ),

  # ---- 4c) Creation vs. last update -------------------------------
  card(
    card_header("Creation against last update"),
    div(class = "text-muted small mb-2",
        "Both date series at once, which tells a wave of new deposits from a bulk edit of older ",
        "material. This card deliberately ignores the date-basis switch, because it shows both ",
        "bases. Every other filter applies as usual."),
    layout_columns(
      fill = FALSE,
      numericInput("dc_from", "From year", value = 2016, min = 2005, max = 2026,
                   step = 1, width = 170),
      checkboxInput("dc_droplast", "Omit the last, incomplete year (annual panel)", value = TRUE)
    ),
    plotlyOutput("dc_year", height = 260),
    plotlyOutput("dc_month", height = 320)
  ),

  # ---- 5) Heatmap -------------------------------------------------
  card(
    card_header(textOutput("hm_title")),
    layout_columns(
      fill = FALSE,
      selectInput("hm_x", "X axis", choices = dim_choices, selected = "platform", width = 220),
      selectInput("hm_y", "Y axis", choices = dim_choices, selected = "macro_topic", width = 220),
      selectInput("hm_val", "Cell value", width = 220,
                  choices = c("Number of datasets" = "count", "Sum of posts" = "posts"))
    ),
    plotlyOutput("heatmap", height = 620)
  ),

  # ---- 6) Cramer's V association matrix (bottom) ------------------
  card(
    card_header("Cramer's V - association between categorical variables"),
    div(class = "text-muted small mb-2",
        "Bias-corrected Cramer's V (0 = no association, 1 = perfect). ",
        "High-cardinality variables are lumped to their top levels. Reacts to filters."),
    selectizeInput("cr_vars", "Variables in the matrix", choices = dim_choices,
                   selected = unname(dim_choices), multiple = TRUE, width = "100%"),
    plotOutput("cramer", height = 640)
  ),

  # ---- 7) Residual analysis (bottom) ------------------------------
  card(
    card_header("Residual analysis - adjusted standardized residuals"),
    div(class = "text-muted small mb-2",
        "Share of 'Yes' per category with Agresti adjusted residual colouring ",
        "(gold = above expected, navy = below). * marks |residual| > 1.96. Reacts to filters."),
    layout_columns(
      fill = FALSE,
      selectInput("res_cat", "Category (rows)", width = 220,
                  choices = dim_choices, selected = "macro_topic"),
      checkboxGroupInput("res_flags", "Binary flags (columns)", inline = TRUE,
                         choices = c("Timestamp" = "timestamp", "Labeled" = "labeled",
                                     "Raw" = "raw", "Paper" = "paper_binary", "Code" = "code",
                                     "Collection descr." = "collection_described",
                                     "Synthetic" = "synthetic"),
                         selected = c("timestamp", "labeled", "raw"))
    ),
    plotOutput("resid", height = 580)
  )
)

# ----------------------------- SERVER --------------------------------
server <- function(input, output, session) {

  # ---- 0) Date basis ----------------------------------------------
  # Single source of truth for "which date does this app mean by year?".
  date_basis <- reactive({
    b <- input$f_datebasis
    if (is.null(b) || !b %in% date_bases) default_basis else b
  })
  basis_lab  <- reactive(basis_word(date_basis()))
  year_lab   <- reactive(sprintf("Year (%s)", basis_lab()))
  cur_bounds <- reactive(yr_bounds[[date_basis()]])

  # Base table for every view: date_dt and year carry the selected basis.
  bdata <- reactive({
    d <- df
    if (date_basis() == "created") {
      d$date_dt <- d$created_dt
      d$year    <- d$year_created
    } else {
      d$date_dt <- d$updated_dt
      d$year    <- d$year_updated
    }
    d
  })

  observeEvent(date_basis(), {
    r <- cur_bounds()
    updateSliderInput(session, "f_year", label = year_lab(),
                      min = r[1], max = r[2], value = c(r[1], r[2]))
  }, ignoreInit = TRUE)

  output$basis_note <- renderText({
    n <- unname(n_missing_date[date_basis()])
    if (is.na(n) || n == 0) {
      sprintf("Every record carries a %s date.", basis_lab())
    } else {
      sprintf("%s of %s records carry no %s date; the year filter keeps them.",
              format(n, big.mark = ","), format(nrow(df), big.mark = ","), basis_lab())
    }
  })

  # ---- 1) Central filtered data -----------------------------------
  fdata <- reactive({
    d <- bdata()
    if (length(input$f_platform))   d <- dplyr::filter(d, platform %in% input$f_platform)
    if (length(input$f_repository)) d <- dplyr::filter(d, repository %in% input$f_repository)
    if (length(input$f_topic))      d <- dplyr::filter(d, macro_topic %in% input$f_topic)
    if (length(input$f_task))       d <- dplyr::filter(d, task %in% input$f_task)
    if (length(input$f_license))    d <- dplyr::filter(d, license %in% input$f_license)
    d <- dplyr::filter(d, is.na(year) | (year >= input$f_year[1] & year <= input$f_year[2]))
    if (has_fair && !is.null(input$f_fair)) {
      d <- dplyr::filter(d, is.na(fair_score) |
                           (fair_score >= input$f_fair[1] & fair_score <= input$f_fair[2]))
    }
    for (v in binary_vars) {
      sel <- input[[paste0("f_", v)]]
      if (!is.null(sel) && sel != "all") d <- d[which(d[[v]] == as.integer(sel)), ]
    }
    d
  })

  output$n_selected <- renderText({
    sprintf("%s of %s rows selected", format(nrow(fdata()), big.mark = ","),
            format(nrow(df), big.mark = ","))
  })

  # ---- 2) Reset ---------------------------------------------------
  observeEvent(input$reset, {
    updateSelectizeInput(session, "f_platform", selected = character(0))
    updateSelectizeInput(session, "f_repository", selected = character(0))
    updateSelectizeInput(session, "f_topic", selected = character(0))
    updateSelectizeInput(session, "f_task", selected = character(0))
    updateSelectizeInput(session, "f_license", selected = character(0))
    r <- cur_bounds()
    updateSliderInput(session, "f_year", value = c(r[1], r[2]))
    updateSliderInput(session, "f_fair", value = c(0, 100))
    for (v in binary_vars) updateSelectInput(session, paste0("f_", v), selected = "all")
  })

  # ---- 3) Value boxes ---------------------------------------------
  output$vb_n <- renderText(format(nrow(fdata()), big.mark = ","))
  output$vb_posts <- renderText({
    s <- sum(fdata()$number_posts, na.rm = TRUE)
    if (s >= 1e9) sprintf("%.2fB", s/1e9) else if (s >= 1e6) sprintf("%.1fM", s/1e6)
    else format(round(s), big.mark = ",")
  })
  output$vb_platforms <- renderText(dplyr::n_distinct(fdata()$platform))
  output$vb_topics <- renderText(dplyr::n_distinct(fdata()$macro_topic))
  output$vb_fair <- renderText({
    if (!has_fair) return("n/a")
    v <- fdata()$fair_score
    if (!length(v) || all(is.na(v))) "n/a" else sprintf("%.1f%%", mean(v, na.rm = TRUE))
  })

  # ---- 4) Overview bar charts (clickable) -------------------------
  bar_count <- function(d, col, topn = 15) {
    d %>% filter(!is.na(.data[[col]])) %>% count(cat = .data[[col]], name = "n") %>%
      arrange(desc(n)) %>% head(topn)
  }
  make_barplot <- function(d, col, color, src = NULL, clickable = TRUE) {
    dd <- bar_count(d, col)
    if (!nrow(dd)) return(plotly_empty(type = "bar"))
    dd$cat <- factor(dd$cat, levels = rev(dd$cat))
    if (clickable) {
      p <- plot_ly(dd, x = ~n, y = ~cat, type = "bar", orientation = "h", source = src,
                   marker = list(color = color), hovertemplate = "%{y}: %{x}<extra></extra>",
                   key = ~as.character(cat)) %>% event_register("plotly_click")
    } else {
      p <- plot_ly(dd, x = ~n, y = ~cat, type = "bar", orientation = "h",
                   marker = list(color = color), hovertemplate = "%{y}: %{x}<extra></extra>")
    }
    p %>% layout(xaxis = list(title = "Count"), yaxis = list(title = ""), margin = list(l = 10)) %>%
      config(displayModeBar = FALSE)
  }

  output$ov_platform  <- renderPlotly(make_barplot(fdata(), "platform",  pal_navy, "src_platform"))
  output$ov_repository <- renderPlotly(make_barplot(fdata(), "repository", pal_navy, "src_repository"))
  output$ov_topic     <- renderPlotly(make_barplot(fdata(), "macro_topic",pal_navy, "src_topic"))
  output$ov_license   <- renderPlotly(make_barplot(fdata(), "license", pal_navy, "src_license"))
  output$ov_task      <- renderPlotly(make_barplot(fdata(), "task",    pal_navy, "src_task"))

  output$ov_year <- renderPlotly({
    dd <- fdata() %>% filter(!is.na(year)) %>% count(year, name = "n")
    if (!nrow(dd)) return(plotly_empty(type = "bar"))
    dd$year <- factor(dd$year, levels = sort(unique(dd$year)))   # ascending -> oldest at bottom
    plot_ly(dd, x = ~n, y = ~year, type = "bar", orientation = "h", source = "src_year",
            marker = list(color = pal_navy), key = ~as.character(year),
            hovertemplate = "%{y}: %{x}<extra></extra>") %>%
      layout(xaxis = list(title = "Count"), yaxis = list(title = year_lab())) %>%
      event_register("plotly_click") %>% config(displayModeBar = FALSE)
  })

  observeEvent(event_data("plotly_click", source = "src_platform"), {
    k <- event_data("plotly_click", source = "src_platform")$key
    if (!is.null(k)) updateSelectizeInput(session, "f_platform", selected = union(input$f_platform, k))
  })
  observeEvent(event_data("plotly_click", source = "src_repository"), {
    k <- event_data("plotly_click", source = "src_repository")$key
    if (!is.null(k)) updateSelectizeInput(session, "f_repository", selected = union(input$f_repository, k))
  })
  observeEvent(event_data("plotly_click", source = "src_topic"), {
    k <- event_data("plotly_click", source = "src_topic")$key
    if (!is.null(k)) updateSelectizeInput(session, "f_topic", selected = union(input$f_topic, k))
  })
  observeEvent(event_data("plotly_click", source = "src_license"), {
    k <- event_data("plotly_click", source = "src_license")$key
    if (!is.null(k)) updateSelectizeInput(session, "f_license", selected = union(input$f_license, k))
  })
  observeEvent(event_data("plotly_click", source = "src_task"), {
    k <- event_data("plotly_click", source = "src_task")$key
    if (!is.null(k)) updateSelectizeInput(session, "f_task", selected = union(input$f_task, k))
  })
  observeEvent(event_data("plotly_click", source = "src_year"), {
    k <- event_data("plotly_click", source = "src_year")$key
    if (!is.null(k)) { y <- as.integer(k); updateSliderInput(session, "f_year", value = c(y, y)) }
  })

  # ---- 5) Binary distribution (lollipop) --------------------------
  output$ov_binary <- renderPlotly({
    d <- fdata()
    if (!nrow(d)) return(plotly_empty(type = "scatter"))
    pd <- data.frame(var = binary_vars,
                 pct = sapply(binary_vars, function(v) mean(d[[v]] == 1, na.rm = TRUE) * 100),
                 stringsAsFactors = FALSE) %>%
      mutate(label = binary_labels[var]) %>% arrange(pct)
    pd$label <- factor(pd$label, levels = pd$label)
    plot_ly(pd) %>%
      add_segments(x = 0, xend = ~pct, y = ~label, yend = ~label,
                   line = list(color = pal_navy), showlegend = FALSE, hoverinfo = "none") %>%
      add_markers(x = ~pct, y = ~label, marker = list(color = pal_gold, size = 11),
                  hovertemplate = "%{y}: %{x:.1f}%<extra></extra>", showlegend = FALSE) %>%
      layout(xaxis = list(title = "Percent True (%)", range = c(0, 100)), yaxis = list(title = "")) %>%
      config(displayModeBar = FALSE)
  })

  # ---- 6) Timeline ------------------------------------------------
  output$ov_timeline <- renderPlotly({
    # guard against implausible/epoch dates so the axis can't jump to 1970
    d <- fdata() %>% filter(!is.na(date_dt),
                            date_dt >= as.POSIXct("2005-01-01", tz = "UTC"))
    if (!nrow(d)) return(plotly_empty(type = "scatter"))
    d <- d %>% mutate(month = lubridate::floor_date(date_dt, "month"))
    # pin the date axis to the data range (prevents plotly autorange artefacts)
    rng <- range(d$month, na.rm = TRUE)
    xax <- list(title = sprintf("Month (%s)", basis_lab()), type = "date",
                range = c(as.character(rng[1]), as.character(rng[2])))
    grp <- input$tl_group
    if (grp == "none") {
      ts <- d %>% count(month, name = "n")
      plot_ly(ts, x = ~month, y = ~n, type = "scatter", mode = "lines+markers",
              line = list(color = pal_navy)) %>%
        layout(xaxis = xax, yaxis = list(title = "Count")) %>%
        config(displayModeBar = FALSE)
    } else {
      keep <- d %>% count(.data[[grp]], sort = TRUE) %>% slice_head(n = 5) %>% pull(1)
      ts <- d %>% filter(.data[[grp]] %in% keep) %>% count(month, g = .data[[grp]], name = "n")
      plot_ly(ts, x = ~month, y = ~n, color = ~g, type = "scatter", mode = "lines+markers",
              colors = pal_qual(n_distinct(ts$g))) %>%
        layout(xaxis = xax, yaxis = list(title = "Count"),
               legend = list(font = list(size = 9))) %>%
        config(displayModeBar = FALSE)
    }
  })

  # ---- 6b) Stacked datasets-per-year bar chart --------------------
  output$ov_stackyear <- renderPlotly({
    gv <- input$sy_group
    d <- fdata() %>% filter(!is.na(year), !is.na(.data[[gv]]))
    if (!nrow(d)) return(plotly_empty(type = "bar"))
    top <- d %>% count(.data[[gv]], sort = TRUE) %>% slice_head(n = 12) %>% pull(1)
    dd <- d %>%
      mutate(grp = ifelse(.data[[gv]] %in% top, as.character(.data[[gv]]), "Other")) %>%
      count(year, grp, name = "n")
    ord <- dd %>% group_by(grp) %>% summarise(t = sum(n), .groups = "drop") %>%
      arrange(desc(t)) %>% pull(grp)
    dd$grp <- factor(dd$grp, levels = ord)
    plot_ly(dd, x = ~year, y = ~n, color = ~grp, type = "bar",
            colors = pal_qual(nlevels(dd$grp)),
            hovertemplate = "%{x} - %{fullData.name}: %{y}<extra></extra>") %>%
      layout(barmode = "stack", xaxis = list(title = year_lab(), dtick = 1),
             yaxis = list(title = "Number of datasets"),
             legend = list(font = list(size = 9))) %>%
      config(displayModeBar = FALSE)
  })

  # ---- 6c) Share per year -----------------------------------------
  # Ported from create_platform_share_plot() in feature_analysis.R, with the
  # variable made selectable. The grid is completed with base R because tidyr
  # is not a dependency of this app.
  output$ov_share <- renderPlotly({
    gv <- input$sh_var
    if (is.null(gv) || !gv %in% names(df)) return(plotly_empty(type = "scatter"))
    top_n <- input$sh_top;  if (is.null(top_n) || is.na(top_n) || top_n < 2) top_n <- 6
    min_n <- input$sh_minn; if (is.null(min_n) || is.na(min_n) || min_n < 1) min_n <- 1

    d <- fdata()
    d <- d[!is.na(d$year), , drop = FALSE]
    if (!nrow(d)) return(plotly_empty(type = "scatter"))
    v <- if (gv %in% binary_vars) as.character(yesno(d[[gv]])) else as.character(d[[gv]])
    ok <- !is.na(v) & !(v %in% c("", "Not specified", "Unspecified", "NA"))
    d <- d[ok, , drop = FALSE]; v <- v[ok]
    if (!nrow(d)) return(plotly_empty(type = "scatter"))

    # A share over a handful of records swings wildly, so thin years go out.
    yr_n  <- table(d$year)
    good  <- as.integer(names(yr_n)[yr_n >= min_n])
    sel   <- d$year %in% good
    d <- d[sel, , drop = FALSE]; v <- v[sel]
    if (!nrow(d)) return(plotly_empty(type = "scatter"))

    # Ranked over the whole selection, not per year: every year needs the same
    # levels, otherwise the lines are not comparable. "Multiple" never earns a
    # line of its own and joins "Other", as in the printed figure.
    ranked <- sort(table(v[v != "Multiple"]), decreasing = TRUE)
    keep   <- names(ranked)[seq_len(min(top_n, length(ranked)))]
    grp    <- ifelse(v %in% keep, v, "Other")
    lvls   <- c(keep, if (any(grp == "Other")) "Other")

    years <- sort(unique(d$year))
    cnt   <- table(factor(d$year, levels = years), factor(grp, levels = lvls))
    shr   <- prop.table(cnt, margin = 1)
    tot   <- as.integer(colSums(cnt))
    lab   <- sprintf("%s (%d)", lvls, tot)

    dd <- data.frame(year  = rep(years, times = length(lvls)),
                     grp   = rep(lab, each = length(years)),
                     share = as.numeric(shr),
                     n     = as.numeric(cnt),
                     stringsAsFactors = FALSE)
    dd$grp  <- factor(dd$grp, levels = lab)
    dd$year <- factor(dd$year, levels = years)

    plot_ly(dd, x = ~year, y = ~share, color = ~grp, colors = pal_qual(length(lvls)),
            type = "scatter", mode = "lines+markers", text = ~n,
            hovertemplate = "%{fullData.name}, %{x}: %{y:.1%} (%{text})<extra></extra>") %>%
      layout(xaxis = list(title = year_lab(), type = "category",
                          tickvals = as.character(years),
                          ticktext = sprintf("%d (n=%d)", years,
                                             as.integer(table(factor(d$year, levels = years))))),
             yaxis = list(title = "Share of datasets per year", tickformat = ".0%",
                          rangemode = "tozero"),
             legend = list(font = list(size = 9))) %>%
      config(displayModeBar = FALSE)
  })

  # ---- 6d) Creation against last update ---------------------------
  # Ported from create_date_comparison(). Two panels instead of a patchwork
  # stack, because patchwork is not a dependency of this app. Both series are
  # drawn side by side, so this view does NOT follow the date-basis switch.
  dc_cols <- c("Creation" = pal_navy, "Last update" = "#9C6B2E")

  dc_data <- reactive({
    d <- fdata()
    if (!nrow(d)) return(NULL)
    long <- data.frame(
      series = factor(rep(c("Creation", "Last update"), each = nrow(d)),
                      levels = c("Creation", "Last update")),
      date   = c(as.Date(d$created_dt), as.Date(d$updated_dt)))
    y0 <- input$dc_from
    if (is.null(y0) || is.na(y0)) y0 <- 2016
    long <- long[!is.na(long$date), , drop = FALSE]
    long$y <- as.integer(format(long$date, "%Y"))
    long <- long[long$y >= y0, , drop = FALSE]
    if (!nrow(long)) NULL else long
  })

  output$dc_year <- renderPlotly({
    long <- dc_data()
    if (is.null(long)) return(plotly_empty(type = "scatter"))
    years <- seq(min(long$y), max(long$y))
    # The catalogue stops mid-year, so the final year is short by construction
    # and would read as a collapse in both series.
    if (isTRUE(input$dc_droplast) && length(years) > 1) years <- years[-length(years)]
    cnt <- table(factor(long$y, levels = years), long$series)
    dd <- data.frame(year   = rep(years, times = ncol(cnt)),
                     series = rep(colnames(cnt), each = length(years)),
                     n      = as.numeric(cnt), stringsAsFactors = FALSE)
    plot_ly(dd, x = ~year, y = ~n, color = ~series, colors = dc_cols,
            type = "scatter", mode = "lines+markers",
            hovertemplate = "%{fullData.name} %{x}: %{y}<extra></extra>") %>%
      layout(xaxis = list(title = "", dtick = 1),
             yaxis = list(title = "Datasets per year", rangemode = "tozero"),
             legend = list(orientation = "h", x = 0, y = 1.18, font = list(size = 10))) %>%
      config(displayModeBar = FALSE)
  })

  output$dc_month <- renderPlotly({
    long <- dc_data()
    if (is.null(long)) return(plotly_empty(type = "scatter"))
    long$m <- lubridate::floor_date(long$date, "month")
    months <- seq(min(long$m), max(long$m), by = "month")
    key <- format(months, "%Y-%m")
    cnt <- table(factor(format(long$m, "%Y-%m"), levels = key), long$series)
    dd <- data.frame(month  = rep(months, times = ncol(cnt)),
                     series = rep(colnames(cnt), each = length(months)),
                     n      = as.numeric(cnt), stringsAsFactors = FALSE)
    plot_ly(dd, x = ~month, y = ~n, color = ~series, colors = dc_cols,
            type = "scatter", mode = "lines+markers", marker = list(size = 4),
            line = list(width = 1),
            hovertemplate = "%{fullData.name} %{x|%b %Y}: %{y}<extra></extra>") %>%
      layout(xaxis = list(title = "Month", type = "date"),
             yaxis = list(title = "Datasets per month", rangemode = "tozero"),
             showlegend = FALSE) %>%
      config(displayModeBar = FALSE)
  })

  # ---- 7) Heatmap -------------------------------------------------
  output$hm_title <- renderText({
    vl <- c(count = "Number of datasets", posts = "Sum of posts")[input$hm_val]
    ttl <- sprintf("%s x %s  -  %s", dim_label(input$hm_x), dim_label(input$hm_y), vl)
    if ("year" %in% c(input$hm_x, input$hm_y))
      ttl <- sprintf("%s  (year = %s date)", ttl, basis_lab())
    ttl
  })
  output$heatmap <- renderPlotly({
    d <- fdata() %>% filter(!is.na(.data[[input$hm_x]]), !is.na(.data[[input$hm_y]]))
    if (!nrow(d)) return(plotly_empty())
    agg <- d %>% group_by(x = as.character(.data[[input$hm_x]]),
                          y = as.character(.data[[input$hm_y]])) %>%
      summarise(count = n(), posts = sum(number_posts, na.rm = TRUE), .groups = "drop")
    agg$val <- if (input$hm_val == "count") agg$count else agg$posts
    xord <- agg %>% group_by(x) %>% summarise(t = sum(count)) %>% arrange(t) %>% pull(x)
    yord <- agg %>% group_by(y) %>% summarise(t = sum(count)) %>% arrange(t) %>% pull(y)
    # complete the x/y grid with base R (avoids the tidyr dependency)
    grid <- expand.grid(x = xord, y = yord, KEEP.OUT.ATTRS = FALSE, stringsAsFactors = FALSE)
    m <- dplyr::left_join(grid, agg[, c("x", "y", "val")], by = c("x", "y"))
    m$val[is.na(m$val)] <- 0
    m <- m %>% mutate(x = factor(x, xord), y = factor(y, yord))
    plot_ly(m, x = ~x, y = ~y, z = ~val, type = "heatmap", colors = c("white", pal_navy),
            hovertemplate = paste0(dim_label(input$hm_x), ": %{x}<br>",
                                   dim_label(input$hm_y), ": %{y}<br>Value: %{z}<extra></extra>")) %>%
      layout(xaxis = list(title = dim_label(input$hm_x), tickangle = -90),
             yaxis = list(title = dim_label(input$hm_y)))
  })

  # ---- 8) Univariate overview (single variable) -------------------
  ex_data <- reactive(if (input$ex_scope == "filtered") fdata() else bdata())

  output$ex_plot <- renderPlotly({
    d <- ex_data(); v <- input$ex_var; vals <- d[[v]]
    if (v %in% explorer_numeric) {
      x <- suppressWarnings(as.numeric(vals)); x <- x[!is.na(x)]
      if (!length(x)) return(plotly_empty())
      use_log <- input$ex_log && v == "number_posts"
      xx <- if (use_log) log10(x[x > 0]) else x
      med <- if (use_log) log10(stats::median(x[x > 0])) else stats::median(x)
      xlab <- if (use_log) "log10(number_posts)" else v
      plot_ly(x = ~xx, type = "histogram", marker = list(color = pal_navy),
              hovertemplate = "%{x}: %{y}<extra></extra>") %>%
        layout(xaxis = list(title = xlab), yaxis = list(title = "Count"), bargap = 0.05,
               shapes = list(list(type = "line", x0 = med, x1 = med, y0 = 0, y1 = 1,
                                  yref = "paper", line = list(color = pal_gold, dash = "dash"))),
               annotations = list(list(x = med, y = 1, yref = "paper", text = "median",
                                       showarrow = FALSE, xanchor = "left",
                                       font = list(color = pal_gold, size = 10)))) %>%
        config(displayModeBar = FALSE)
    } else {
      dd <- data.frame(cat = as.character(vals), stringsAsFactors = FALSE) %>%
        mutate(cat = ifelse(is.na(cat), "NA", cat)) %>%
        count(cat, sort = TRUE) %>% head(25)
      dd$cat <- factor(dd$cat, levels = rev(dd$cat))
      plot_ly(dd, x = ~n, y = ~cat, type = "bar", orientation = "h",
              marker = list(color = pal_gold), hovertemplate = "%{y}: %{x}<extra></extra>") %>%
        layout(xaxis = list(title = "Count"), yaxis = list(title = "")) %>%
        config(displayModeBar = FALSE)
    }
  })

  output$ex_summary <- renderTable({
    d <- ex_data(); v <- input$ex_var; vals <- d[[v]]
    if (v %in% explorer_numeric) {
      x <- suppressWarnings(as.numeric(vals)); x <- x[!is.na(x)]
      data.frame(Statistic = c("n", "Missing", "Min", "Median", "Mean", "Max"),
             Value = c(length(x), sum(is.na(suppressWarnings(as.numeric(vals)))),
                      round(min(x), 1), round(stats::median(x), 1),
                      round(mean(x), 1), round(max(x), 1)), stringsAsFactors = FALSE)
    } else {
      data.frame(Statistic = c("n", "Missing", "Categories", "Most frequent"),
             Value = c(length(vals), sum(is.na(vals) | vals == ""),
                      as.character(dplyr::n_distinct(vals)),
                      names(sort(table(vals), decreasing = TRUE))[1]),
             stringsAsFactors = FALSE)
    }
  })

  # ---- 9) Cramer's V heatmap --------------------------------------
  output$cramer <- renderPlot({
    d <- fdata()
    validate(need(nrow(d) >= 20, "Not enough rows in the current selection for Cramer's V."))
    validate(need(length(input$cr_vars) >= 2, "Select at least two variables for the matrix."))
    cramers_v_heatmap(build_cramer_feat(d, input$cr_vars))
  })

  # ---- 10) Residual heatmap (free choice of category + flags) -----
  output$resid <- renderPlot({
    d <- fdata()
    validate(need(nrow(d) >= 20, "Not enough rows in the current selection for the residual analysis."))
    validate(need(length(input$res_flags) >= 1, "Select at least one binary flag (columns)."))
    cat_var <- input$res_cat
    ord <- switch(cat_var,
                  macro_topic = topic_order,
                  repository  = repository_order,
                  platform    = platform_order,
                  NULL)
    p <- residual_heatmap(d, cat_var = cat_var, flags = input$res_flags,
                          cat_order = ord, min_n = 10, flag_labels = flag_lab)
    validate(need(!is.null(p), "No category reaches the minimum count (min_n = 10) in this selection."))
    p
  })

  # ---- 10b) FAIR assessment ---------------------------------------
  # Both views recompute on the filtered selection. The metric view mirrors the
  # lollipop of the binary attributes above, so the two read the same way.
  output$fair_metrics <- renderPlotly({
    validate(need(has_fair, "The data file carries no FAIR columns. Run build_dashboard_data.py."))
    d <- fdata()
    validate(need(nrow(d) >= 1, "No records in the current selection."))

    pct <- vapply(fair_metric_ids, function(m) {
      v <- d[[paste0("fm_", m)]]
      if (all(is.na(v))) NA_real_ else mean(v == 1, na.rm = TRUE) * 100
    }, numeric(1))

    pd <- data.frame(metric = fair_metric_ids, pct = as.numeric(pct),
                     stringsAsFactors = FALSE)
    pd <- pd[!is.na(pd$pct), , drop = FALSE]
    validate(need(nrow(pd) > 0, "No FAIR results in the current selection."))

    pd$cat       <- sub("^FsF-([FAIR]).*$", "\\1", pd$metric)
    pd$principle <- sub("^FsF-([A-Z0-9.]+)-.*$", "\\1", pd$metric)
    pd$label     <- sprintf("%s (%s)", fair_metric_labels[pd$metric], pd$principle)
    pd <- pd[order(pd$pct), , drop = FALSE]
    pd$label <- factor(pd$label, levels = pd$label)

    cols <- setNames(pal_qual(length(fair_categories)), fair_categories)
    p <- plot_ly()
    for (cc in fair_categories) {
      dd <- pd[pd$cat == cc, , drop = FALSE]
      if (!nrow(dd)) next
      p <- p %>%
        add_segments(data = dd, x = 0, xend = ~pct, y = ~label, yend = ~label,
                     line = list(color = cols[[cc]]), showlegend = FALSE,
                     hoverinfo = "none") %>%
        add_markers(data = dd, x = ~pct, y = ~label, name = cc,
                    marker = list(color = cols[[cc]], size = 11),
                    hovertemplate = "%{y}: %{x:.1f}%<extra></extra>")
    }
    p %>%
      layout(xaxis = list(title = "Records passing the metric (%)", range = c(0, 100)),
             yaxis = list(title = ""),
             legend = list(orientation = "h", y = -0.14, title = list(text = ""))) %>%
      config(displayModeBar = FALSE)
  })

  output$fair_repo <- renderPlot({
    validate(need(has_fair, "The data file carries no FAIR columns. Run build_dashboard_data.py."))
    d <- fdata()
    d <- d[!is.na(d$fair_score), , drop = FALSE]
    validate(need(nrow(d) >= 1, "No FAIR results in the current selection."))

    min_n <- input$fair_min_n
    if (is.null(min_n) || is.na(min_n) || min_n < 1) min_n <- 1
    cnt <- as.data.frame(table(d$repository), stringsAsFactors = FALSE)
    names(cnt) <- c("repository", "n")
    keep <- cnt$repository[cnt$n >= min_n]
    other_label <- sprintf("Other (n < %d each)", min_n)
    d$repo <- ifelse(d$repository %in% keep, d$repository, other_label)

    # Built with base R: tidyr is deliberately not a dependency of this app.
    long <- do.call(rbind, lapply(fair_categories, function(cc) {
      data.frame(repo = d$repo, category = cc,
                 score = d[[paste0("fair_", cc)]], stringsAsFactors = FALSE)
    }))
    agg <- aggregate(score ~ repo + category, data = long, FUN = mean)
    validate(need(nrow(agg) > 0, "No FAIR results in the current selection."))

    nrep <- as.data.frame(table(d$repo), stringsAsFactors = FALSE)
    names(nrep) <- c("repo", "n")
    ov <- aggregate(score ~ repo, data = agg, FUN = mean)
    names(ov) <- c("repo", "overall")
    agg <- merge(merge(agg, nrep, by = "repo"), ov, by = "repo")

    agg$repo_label <- sprintf("%s (n = %d)", agg$repo, agg$n)
    lvl <- unique(agg$repo_label[order(agg$overall)])
    agg$repo_label <- factor(agg$repo_label, levels = lvl)
    agg$category   <- factor(agg$category, levels = fair_categories)

    ggplot(agg, aes(x = category, y = repo_label, fill = score)) +
      geom_tile(colour = "white", linewidth = 1) +
      geom_text(aes(label = sprintf("%.0f", score), colour = score > 55),
                size = 5, show.legend = FALSE) +
      scale_fill_gradient(low = "white", high = pal_navy, limits = c(0, 100),
                          name = "Mean score\n(percent)") +
      scale_colour_manual(values = c(`TRUE` = "white", `FALSE` = "grey20")) +
      labs(x = "FAIR category", y = NULL) +
      theme_minimal(base_size = 15) +
      theme(panel.grid = element_blank())
  })

  # ---- 11) Data table (taller: more rows visible) -----------------
  table_cols <- c("repository", "platform", "macro_topic", "topic", "date_day", "year",
                  "number_posts", "collection_described", "timestamp", "labeled",
                  "raw", "synthetic", "paper_binary", "code", "title", "url")
  # truncate long cell text to one line; full text via hover tooltip
  truncate_js <- DT::JS(
    "function(data, type, row, meta){",
    "  if(type==='display' && data != null){",
    "    var s = String(data);",
    "    if(s.length > 45){",
    "      var esc = s.replace(/\"/g,'&quot;');",
    "      return '<span title=\"'+esc+'\">'+s.substr(0,45)+'\\u2026</span>';",
    "    }",
    "  }",
    "  return data;",
    "}")
  output$table <- renderDT({
    # ISO string, not a Date: DT would give a Date column a range-slider filter,
    # and text sorts chronologically in this format anyway.
    d <- fdata() %>% mutate(date_day = format(date_dt, "%Y-%m-%d")) %>%
      dplyr::select(any_of(table_cols)) %>%
      mutate(number_posts = round(number_posts))
    # Both date columns are labelled with the basis in force, so the table never
    # shows a bare "year" whose meaning depends on a sidebar setting.
    names(d)[names(d) == "date_day"] <- sprintf("date (%s)", basis_lab())
    names(d)[names(d) == "year"]     <- sprintf("year (%s)", basis_lab())
    datatable(d, filter = "top", rownames = FALSE, extensions = "Buttons",
              class = "compact nowrap stripe hover",
              # pagination instead of an inner scroller: DataTables renders all
              # pageLength rows at full height and the page itself scrolls, so the
              # number of visible rows no longer depends on the card height.
              options = list(pageLength = 25, scrollX = TRUE, dom = "lftip",
                             autoWidth = FALSE,
                             lengthMenu = list(c(25, 50, 100, -1), c("25", "50", "100", "All")),
                             columnDefs = list(list(targets = "_all", render = truncate_js))))
  })
}

shinyApp(ui, server)
