# =============================================================================
# Exploratory data analysis of the social-media dataset catalogue
# =============================================================================
#
# Step 4 of the pipeline. Reads the annotated catalogue (data/dataset_relevant.csv)
# and reproduces the figures reported in the accompanying paper. The script is
# organised as a sequence of foldable sections; each section defines one plotting
# function and then calls it for the variables shown in the paper:
#
#   Colour scheme                 shared palette for all figures
#   Load Data                     read the catalogue, recode features
#   General overview              univariate bar / lollipop / histogram plots
#   Heat Map                      bivariate cross-tabulations with marginal sums
#   Year Barchart                 datasets per year, split by platform
#   Timeline of platforms         datasets per month, split by a grouping variable
#   Cramers V heatmap             pairwise association between all features
#   Category-level residuals      adjusted standardised Pearson residuals
#   FAIR assessment               pass rate per metric, score per repository
#
# Feature types follow the paper: categorical (platform, repository, macro_topic,
# task, license), ordinal (year, derived from the last-update date), binary
# (raw, synthetic, collection_described, timestamp, labeled, paper, code) and one
# numerical variable (number_posts).
#
# Requirements: R >= 4.3 and the packages loaded below. Install with
#   install.packages(c("tidyverse", "ggnewscale", "scales", "lubridate",
#                      "RColorBrewer", "factoextra", "ggpubr", "patchwork"))
#
# Run from the repository root. The catalogue is not stored in the repository;
# fetch it first with "python download_data.py", or adjust DATA_FILE below.
# =============================================================================

##### Library ----
library(tidyverse)
library(ggplot2)
library(ggnewscale)
library(scales)
library(lubridate)
library(RColorBrewer)
library(factoextra)
library(ggpubr)
library(patchwork)

##### Colour scheme ----
palette_navy <- "#182F50"   # primary colour, used for bars and fills
palette_gold <- "#C7A24C"   # accent colour, used for highlights and sums

# Anchor hues shared by all qualitative palettes below
palette_anchors <- c("#182F50", "#3E6DA0", "#7FB0D6", "#4F9D8E",
                     "#8FBF73", "#C7A24C", "#9C6B2E", "#7E4A57")

# Shared qualitative palette for plots with many categories
palette_qual <- function(n) {
  grDevices::colorRampPalette(palette_anchors)(n)
}

# Set a colour to a target CIE L* lightness while keeping its hue.
# Chroma is scaled by chroma_mul and, if necessary, shrunk further until the
# result is inside the sRGB gamut.
palette_set_lightness <- function(hex, L_target, chroma_mul = 1) {
  rgb_in <- t(grDevices::col2rgb(hex) / 255)
  lab    <- grDevices::convertColor(rgb_in, from = "sRGB", to = "Lab")
  ab     <- lab[1, 2:3] * chroma_mul
  k <- 1
  repeat {
    out <- grDevices::convertColor(matrix(c(L_target, ab * k), nrow = 1),
                                   from = "Lab", to = "sRGB")
    if (all(out >= -0.002 & out <= 1.002) || k < 0.05) break
    k <- k * 0.92
  }
  out <- pmin(pmax(out, 0), 1)
  grDevices::rgb(out[1, 1], out[1, 2], out[1, 3])
}

# Lightness bands (CIE L*, chroma multiplier) used by palette_qual_ordered().
#
# _fill: for areas seen directly against each other (stacked bars, tiles).
#        The light band is very light, which maximises the step between
#        neighbouring segments; on white it would be too faint for a thin line.
# _line: for lines and points on a white panel. The bands are compressed so
#        every colour keeps >= 2.4:1 against the background, at the cost of a
#        slightly smaller step between neighbours.
palette_bands_fill <- list(c(L = 32, C = 0.85),   # dark
                           c(L = 86, C = 0.50),   # light
                           c(L = 59, C = 0.90))   # mid
palette_bands_line <- list(c(L = 24, C = 1.00),
                           c(L = 68, C = 0.75),
                           c(L = 45, C = 0.95))

# Qualitative palette for adjacency-critical plots (stacked bars, many lines).
#
# The plain ramp above interpolates smoothly between the anchors, so with many
# categories neighbouring classes get near-identical colours - unreadable once
# printed. This variant keeps exactly the same hues but
#   (a) walks the anchors in steps of 3, so consecutive classes never come from
#       neighbouring anchors, and
#   (b) cycles three fixed lightness bands (dark -> light -> mid), so any two
#       adjacent classes differ clearly in luminance and stay separable in
#       greyscale and for colour-vision deficiencies.
# LIMIT: the 24 (anchor, band) combinations are unique, but they are NOT all
# perceptually distinct. Anchors 1-3 are all blues, and the step-of-3 walk puts
# them on positions 1, 4, 7 - which share the same band index, hence the same
# lightness. Measured minimum CIEDE2000 across all pairs:
#   n <= 8  : dE >= 19.6  (the even-spread branch below avoids adjacent blues)
#   n =  9  : dE  4.8
#   n = 15  : dE  4.8   (7 of 105 pairs below dE 10)
#   n = 20  : dE  3.5   (9 of 190 pairs below dE 10)
# dE below ~10 is not reliably separable in a chart. Anything above 8 categories
# therefore needs fewer categories, not a different palette - collapse the tail
# into an "Other" level instead (see create_platform_year_plot).
palette_qual_ordered <- function(n, bands = palette_bands_fill) {
  n_a <- length(palette_anchors)
  if (n > n_a) {
    warning(sprintf(paste0("palette_qual_ordered(): %d categories requested, but only %d are ",
                           "perceptually distinct. Colours will repeat hue families at equal ",
                           "lightness. Collapse the tail into 'Other' instead."), n, n_a),
            call. = FALSE)
  }
  # Up to n_a categories the anchors are spread evenly across the whole range,
  # so few categories still get maximally different hues (three of the anchors
  # are blues - taking them in order would give two near-identical dark blues).
  # Beyond that the anchors are cycled in steps of 3, which keeps consecutive
  # classes away from neighbouring anchors.
  idx <- if (n <= n_a) round(seq(1, n_a, length.out = n)) else ((seq_len(n) - 1) * 3) %% n_a + 1
  bnd <- (seq_len(n) - 1) %% length(bands) + 1
  vapply(seq_len(n), function(i) {
    b <- bands[[bnd[i]]]
    palette_set_lightness(palette_anchors[idx[i]], b[["L"]], b[["C"]])
  }, character(1))
}
#####

##### Load Data ----

assign_topic_number <- function(topic, order) {
  match(topic, order)
}

scale_minmax <- function(x) {
  out <- apply(as.matrix(x), 2, function(col) {
    (col - min(col)) / (max(col) - min(col))
  })
  return(tibble::as_tibble(out))
}

platform_order <- c("X/Twitter", "Bluesky", "Mastodon", "Gab", "Truth Social", "Facebook", "LinkedIn", "Reddit", "4chan", 
                    "Tumblr", "YouTube", "Tiktok", "Instagram", "Telegram", "WhatsApp", "Quora", "Discord", "Twitch",
                    "Multiple", "Not specified")

topic_order <- c("General", "Conflicts", "Politics", "Countries", "Economics", "Finance", "Science", "Culture", 
                 "Celebrities", "Harmful Content", "COVID-19", "Health", "Sports", "Disaster", "Environment", NA)

repository_order <- c("Dryad", "Figshare", "Harvard Dataverse", "ScienceDB", "Zenodo", "MendeleyData", "OSF", 
                      "Kaggle", "GitHub", "HuggingFace", 
                      "CESSDA", "RDA", "EU-ODP")

# Path to the annotated catalogue, relative to the repository root. The file is
# published separately on Zenodo and downloaded into data/ by download_data.py.
DATA_FILE <- file.path("data", "dataset_relevant.csv")

if (!file.exists(DATA_FILE)) {
  stop("Catalogue not found at ", DATA_FILE,
       ". Run 'python download_data.py' first, or set DATA_FILE manually.")
}

# ── Which date column drives the temporal views ─────────────────────────────
# `updated` is the last-modification timestamp the repository reports and was
# the only date available until September 2026. `created` is the creation or
# first-publication date, present for 1,961 of the 1,997 catalogue records.
# Set DATE_COL once, and every year and month view below follows it.
#
# Records without a value in the chosen column drop out of the temporal plots
# and are counted in a message, so the bar totals no longer necessarily sum to
# the full catalogue. Everything that is not a time series is unaffected.
DATE_COL <- "updated"          # "updated" or "created"

DATE_LABEL <- if (DATE_COL == "created") "Creation" else "Last Update"
YEAR_XLAB  <- paste0("Year (", DATE_LABEL, ")")
MONTH_XLAB <- paste0("Date of ", tolower(DATE_LABEL), " grouped per month")

df <- read_delim(DATA_FILE, delim = ";", locale = locale(encoding = "UTF-8"),
                 show_col_types = FALSE)

# Both date columns as UTC timestamps, whatever readr made of them.
# as.POSIXct() is not used here: its default formats do not cover the
# ISO 8601 notation the catalogue uses ("2021-06-02T13:49:17Z"), and it
# would fail on the "T" separator and the trailing "Z".
df <- df %>% mutate(across(any_of(c("updated", "created")),
                           ~ if (is.character(.x))
                               lubridate::parse_date_time(
                                 .x, orders = c("YmdHMS", "Ymd"),
                                 tz = "UTC", quiet = TRUE)
                             else .x))

bool_vars <- c("raw", "synthetic", "collection_described", "timestamp", "labeled", "code")

df <- df %>%
  select(-nb) %>%
  mutate(
    year = lubridate::year(.data[[DATE_COL]]),
    across(all_of(bool_vars),~ as.integer(str_trim(str_to_lower(as.character(.x))) == "true")),
    paper_binary = case_when(
      str_trim(str_to_lower(paper)) == "false" ~ 0L,
      !is.na(paper) ~ 1L,
      TRUE ~ NA_integer_
    ),
    across(where(is.logical), as.integer),  # safety net in case readr does return logical
    macro_topic_code = assign_topic_number(macro_topic, topic_order),
    platform = ifelse(!is.na(platform) & grepl(",", platform), "Multiple", platform),
    platform_code = assign_topic_number(platform, platform_order),
    repository_code = assign_topic_number(repository, repository_order)
  )

# Top 15 platforms by number of datasets (used for all platform-related plots).
# The remaining platforms have only a handful of datasets and are omitted there.
platform_top15 <- df %>%
  count(platform, sort = TRUE) %>%
  slice_head(n = 15) %>%
  pull(platform)

df_platform_top15 <- df %>% filter(platform %in% platform_top15)


num_vars <- df %>%
  mutate(across(where(is.character), ~ as.integer(factor(.x)))) %>%
  dplyr::select(platform_code, collection_described, timestamp, number_posts, year, macro_topic_code, 
                repository_code, labeled, raw, synthetic, labeled, paper_binary)

num_vars_scaled <- scale_minmax(num_vars) 
names(num_vars_scaled)[names(num_vars_scaled) == "year"] <- "year_code"

# Codes 
topic_codes <- tibble(original = topic_order[1:15], code = seq(from = 0, to = 1, length.out = length(topic_order)-1))
platform_codes <- tibble(original = platform_order, code = seq(from = 0, to = 1, length.out = length(platform_order)))
repository_codes<- tibble(original = repository_order, code = seq(from = 0, to = 1, length.out = length(repository_order)))
post_codes <- tibble(original = unique(df$number_posts), code = seq(from = 0, to = 1, length.out = length(unique(df$number_posts))))
year_codes <- tibble(original = sort(unique(num_vars$year)), code = seq(from = 0, to = 1, length.out = length(unique(num_vars$year))))
binary_codes <- tibble(original = c(0, 1), code = c(0,1))

##### 

##### General overview of numerical/categorical variables ----
plot_categorical_distribution <- function(data, var, title_label = var,
                                          top_n = NULL, fill = palette_navy) {
  
  # Drop the residual category as well as NAs. It is not a value of the
  # variable, and leaving it in costs a slot in the Top-N (it pushed a real
  # task out of the Task panel in an earlier render).
  plot_data <- data %>%
    filter(!is.na(.data[[var]]), !.data[[var]] %in% c("Unspecified", "None specific", "Not specified")) %>%
    count(category = .data[[var]], name = "n") %>%
    arrange(desc(n))
  
  # For variables with many levels (e.g. platform) show only the top N.
  # Only state the Top-N fact here: which residual category was dropped differs
  # per variable, so naming one of them in a shared subtitle is wrong for the
  # others. The figure caption names them.
  note <- NULL
  if (!is.null(top_n) && nrow(plot_data) > top_n) {
    hidden <- nrow(plot_data) - top_n
    plot_data <- plot_data %>% slice_head(n = top_n)
    note <- paste0("(Top ", top_n, " of ", top_n + hidden, ")")
  }
  
  ggplot(plot_data, aes(x = n, y = reorder(category, n))) +
    geom_col(fill = fill, width = 0.7) +
    geom_text(aes(label = n), hjust = -0.2, size = 4.5) +
    # Headroom scales with the width of the longest label; 0.15 clipped the
    # four-digit Kaggle count to "113" in the four-across overview layout.
    scale_x_continuous(expand = expansion(
      mult = c(0, 0.06 * max(nchar(format(plot_data$n, scientific = FALSE)))))) +
    labs(x = "Number of datasets", y = NULL,
         title = title_label, subtitle = note) +
    theme_minimal(base_size = 18) +
    theme(panel.grid.major.y = element_blank(),
          plot.title = element_text(face = "bold", size = 16),
          plot.subtitle = element_text(size = 12, color = "grey40"))
}

# --- Distribution of number_posts (strongly right-skewed -> log10 axis) 
plot_posts_distribution <- function(data, fill = palette_navy) {
  
  med <- median(data$number_posts, na.rm = TRUE)
  
  ggplot(data, aes(x = number_posts)) +
    geom_histogram(bins = 30, fill = fill, color = "white", linewidth = 0.2) +
    scale_x_log10(
      labels = scales::label_number(scale_cut = scales::cut_short_scale()),
      breaks = scales::trans_breaks("log10", function(x) 10^x)
    ) +
    annotation_logticks(sides = "b", linewidth = 0.3) +
    geom_vline(xintercept = med, linetype = "dashed", color = palette_gold) +
    annotate("text", x = med, y = Inf,
             label = paste0("Median = ", scales::label_number(scale_cut = scales::cut_short_scale())(med)),
             hjust = -0.05, vjust = 1.5, size = 4.5, color = palette_gold) +
    labs(x = "Number of posts (log scale)", y = "Number of datasets",
         title = "Dataset size") +
    theme_minimal(base_size = 18) +
    theme(plot.title = element_text(face = "bold", size = 16))
}

# --- Distribution of year (last update) 
plot_year_distribution <- function(data, fill = palette_navy) {
  
  data %>%
    filter(!is.na(year)) %>%
    count(year, name = "n") %>%
    ggplot(aes(x = factor(year), y = n)) +
    geom_col(fill = fill, width = 0.7) +
    geom_text(aes(label = n), vjust = -0.3, size = 4.5) +
    scale_x_discrete(labels = function(x) paste0("'", substr(x, 3, 4))) +
    scale_y_continuous(expand = expansion(mult = c(0, 0.15))) +
    labs(x = "Year (last update)", y = "Number of datasets",
         title = "Update year") +
    theme_minimal(base_size = 18) +
    theme(panel.grid.major.x = element_blank(),
          plot.title = element_text(face = "bold", size = 16))
}

plot_binary_distribution <- function(data, vars) {
  
  plot_data <- data %>%
    select(all_of(vars)) %>%
    pivot_longer(cols = everything(), names_to = "variable", values_to = "value") %>%
    group_by(variable) %>%
    summarise(percentage = mean(value == 1, na.rm = TRUE) * 100) %>%
    arrange(percentage)
  
  labels <- c(
    timestamp            = "timestamp",
    raw                  = "raw",
    labeled              = "labeled",
    synthetic            = "synthetic",
    paper_binary         = "paper",
    collection_described = "coll. descr.",
    code                 = "code avail."
  )
  plot_data$variable <- ifelse(plot_data$variable %in% names(labels),
                               labels[plot_data$variable], plot_data$variable)
  
  ggplot(plot_data, aes(x = percentage, y = reorder(variable, percentage))) +
    geom_segment(aes(x = 0, xend = percentage, y = variable, yend = variable),
                 color = palette_navy, linewidth = 1) +
    geom_point(color = palette_gold, size = 5) +
    geom_text(aes(label = paste0(round(percentage, 1), "%")), hjust = -0.5, size = 5) +
    scale_x_continuous(limits = c(0, 110), breaks = seq(0, 100, 20)) +
    labs(x = "Percent True", y = NULL) +
    theme_minimal(base_size = 18)
}

p_platform <- plot_categorical_distribution(df, "platform",   "Platform", top_n = 20)
p_repo     <- plot_categorical_distribution(df, "repository",  "Repository")
p_topic    <- plot_categorical_distribution(df, "macro_topic", "Macro topic")
p_year     <- plot_year_distribution(df)
p_posts    <- plot_posts_distribution(df)
p_task <- plot_categorical_distribution(
  df %>% filter(!task %in% c("None specific", "None")),
  "task", "Task", top_n = 15
)
p_license <- plot_categorical_distribution(
  df %>% filter(!license %in% c("None specific")),
  "license", "License", top_n = 15
)
p_binary <- plot_binary_distribution(
  df, c("timestamp", "raw", "labeled", "synthetic", "paper_binary", "code", "collection_described")
) +
  labs(title = "Binary properties") +
  theme(plot.title = element_text(face = "bold", size = 15))

overview_figure <-
  (p_platform | p_repo | p_topic | p_task) /
  (p_license | p_year | p_posts | p_binary) +
  plot_annotation(
    theme = theme(plot.title = element_text(face = "bold", size = 13))
  )
overview_figure

#####

##### Heat Map ---- 

create_summary_heatmap <- function(data, x_var, y_var, value_var, x_label = "X-Axis", y_label = "Y-Axis", 
                                   fill_main = palette_navy, fill_sum = palette_gold) {
  
  # Formatting helper, installed on demand if missing
  if (!requireNamespace("scales", quietly = TRUE)) install.packages("scales")
  
  df_temp <- data %>% 
    select(all_of(c(x_var, y_var, value_var))) %>% 
    mutate(across(all_of(c(x_var, y_var)), as.character))
  
  colnames(df_temp) <- c("x_axis", "y_axis", "val")
  
  main_matrix <- df_temp %>%
    group_by(x_axis, y_axis) %>%
    summarise(
      count_n = n(),
      sum_val = sum(val, na.rm = TRUE),
      .groups = "drop"
    )
  
  x_sums <- df_temp %>%
    group_by(x_axis) %>%
    summarise(sum_val = sum(val, na.rm = TRUE), .groups = "drop") %>%
    mutate(y_axis = "Sum Total", count_n = 0)
  
  y_sums <- df_temp %>%
    group_by(y_axis) %>%
    summarise(sum_val = sum(val, na.rm = TRUE), .groups = "drop") %>%
    mutate(x_axis = "Sum Total", count_n = 0)
  
  all_data <- bind_rows(main_matrix, x_sums, y_sums) %>%
    complete(x_axis, y_axis, fill = list(count_n = 0, sum_val = 0))
  
  # Place the grand total in the bottom-right corner
  total_sum <- sum(df_temp$val, na.rm = TRUE)
  all_data <- all_data %>%
    mutate(sum_val = ifelse(x_axis == "Sum Total" & y_axis == "Sum Total", total_sum, sum_val))
  
  # Marginal sums span several orders of magnitude, so they are displayed in
  # billions of records rather than raw counts.
  all_data <- all_data %>%
    mutate(sum_val_bn = sum_val / 1e9)
  
  # Build the cell labels: counts inside the matrix, billions in the margins
  all_data <- all_data %>%
    mutate(label_text = case_when(
      x_axis == "Sum Total" | y_axis == "Sum Total" ~ scales::comma(sum_val_bn, accuracy = 0.01),
      TRUE ~ as.character(count_n)
    ))
  
  x_order <- all_data %>%
    filter(x_axis != "Sum Total") %>%
    group_by(x_axis) %>%
    summarise(total_n = sum(count_n, na.rm = TRUE), .groups = "drop") %>%
    arrange(total_n) %>%
    pull(x_axis)
  
  all_data$x_axis <- factor(all_data$x_axis, levels = c(x_order, "Sum Total"))
  
  y_order <- all_data %>%
    filter(y_axis != "Sum Total") %>%
    group_by(y_axis) %>%
    summarise(total_n = sum(count_n, na.rm = TRUE), .groups = "drop") %>%
    arrange(total_n) %>%
    pull(y_axis)
  
  all_data$y_axis <- factor(all_data$y_axis, levels = c(y_order, "Sum Total"))
  
  # Split the matrix cells from the marginal cells: they use separate scales
  main_plot_data <- all_data %>% filter(x_axis != "Sum Total" & y_axis != "Sum Total")
  sum_plot_data <- all_data %>% filter(x_axis == "Sum Total" | y_axis == "Sum Total")
  
  # Assemble the plot
  ggplot() +
    geom_tile(data = main_plot_data, 
              aes(x = x_axis, y = y_axis, fill = count_n), color = "grey80") +
    scale_fill_gradient(low = "white", high = fill_main, name = "Number of Datasets") +
    ggnewscale::new_scale_fill() + # allows two independent fill scales in one plot
    geom_tile(data = sum_plot_data, 
              aes(x = x_axis, y = y_axis, fill = sum_val_bn), color = "grey80") +
    scale_fill_gradient(low = "white", high = fill_sum, name = "Sum of records (Billions)") +
    geom_text(data = all_data, 
              aes(x = x_axis, y = y_axis, label = label_text, size = 18), show.legend = FALSE) +
    labs(x = x_label, y = y_label) +
    theme_minimal(base_size = 18) +
    theme(axis.text.x = element_text(angle = 90, vjust = 0.5, hjust = 1))
}

create_summary_heatmap(data=df, x_var="platform", y_var="macro_topic", value_var="number_posts",
                      x_label="Platform", y_label="Macro Topic")

create_summary_heatmap(data=df, x_var="platform", y_var="repository", value_var="number_posts",
                       x_label="Platform", y_label="Repository")

create_summary_heatmap(data=df, x_var="platform", y_var="year", value_var="number_posts",
                       x_label="Platform", y_label="Last Update")

#####


##### Year Barchart ----

# top_n: how many levels are shown individually; everything else is pooled into
# "Other". Two reasons this is not optional:
#   (1) Totals. Pre-filtering the data to the top levels (as an earlier version
#       did) silently drops the remaining datasets, so the bar totals no longer
#       match the overall counts in the univariate figure.
#   (2) Colour. palette_qual_ordered() is only perceptually distinct up to
#       8 levels, so top_n must stay <= 7 to leave room for "Other".
create_platform_year_plot <- function(data, 
                                      year_var = "year", 
                                      platform_var = "platform", 
                                      x_label = YEAR_XLAB, 
                                      y_label = "Number of datasets",
                                      top_n = 7, other_label = "Other") {
  
  n_missing <- sum(is.na(data[[year_var]]))
  if (n_missing > 0) {
    message(sprintf("%d of %d records carry no %s date and are left out of this plot.",
                    n_missing, nrow(data), tolower(DATE_LABEL)))
    data <- data |> filter(!is.na(.data[[year_var]]))
  }

  years_range = min(data$year):max(data$year)
  
  stopifnot(top_n <= length(palette_anchors) - 1)
  
  keep <- data |>
    count(!!sym(platform_var), name = "n") |>
    arrange(desc(n)) |>
    slice_head(n = top_n) |>
    pull(!!sym(platform_var))
  
  data <- data |>
    mutate(!!sym(platform_var) := ifelse(!!sym(platform_var) %in% keep,
                                         as.character(!!sym(platform_var)), other_label))
  
  platform_order <- c(
    data |> filter(!!sym(platform_var) != other_label) |>
      count(!!sym(platform_var), name = "n") |> arrange(desc(n)) |> pull(!!sym(platform_var)),
    other_label
  )
  
  platform_totals <- data %>%
    count(!!sym(platform_var), name = "n") %>%
    complete(!!sym(platform_var) := platform_order, fill = list(n = 0)) %>%
    mutate(!!sym(platform_var) := factor(!!sym(platform_var), levels = platform_order)) %>%
    arrange(!!sym(platform_var))
  
  label_map <- setNames(
    paste0(platform_totals[[platform_var]], " (", platform_totals$n, ")"),
    platform_totals[[platform_var]]
  )
  
  counts <- data |>
    count(!!sym(year_var), !!sym(platform_var), name = "freq") |>
    complete(!!sym(year_var) := years_range, !!sym(platform_var) := platform_order, fill = list(freq = 0)) |>
    mutate(
      !!sym(platform_var) := factor(!!sym(platform_var), levels = platform_order),
      !!sym(year_var) := factor(!!sym(year_var), levels = years_range)
    )
  
  n_platforms <- length(platform_order)
  cols <- palette_qual_ordered(n_platforms)
  names(cols) <- platform_order
  
  p <- ggplot(counts, aes(x = !!sym(year_var), y = freq, fill = !!sym(platform_var))) +
    geom_col(color = "grey20", linewidth = 0.2) +
    scale_fill_manual(
      values = cols, 
      breaks = platform_order, 
      labels = label_map[platform_order], 
      drop = FALSE, 
      name = "Platform"
    ) +
    labs(x = x_label, y = y_label) +
    theme_minimal(base_size = 20) +
    theme(axis.text.x = element_text(angle = 60, hjust = 1), legend.position = "right")
  
  totals <- counts |>
    group_by(!!sym(year_var)) |>
    summarise(total = sum(freq), .groups = "drop")
  
  p +
    geom_text(
      data = totals,
      mapping = aes(x = !!sym(year_var), y = total, label = total),
      inherit.aes = FALSE,
      vjust = -0.2
    ) +
    expand_limits(y = max(totals$total) * 1.07)
}

# Full df, not df_platform_top15: the function now pools the tail into "Other"
# itself, so every dataset is counted and the bar totals match Figure 2.
create_platform_year_plot(df, year_var = "year", platform_var = "platform",
                          x_label = YEAR_XLAB, y_label = "Number of datasets")

create_platform_year_plot(df, year_var = "year", platform_var = "repository", 
                          x_label = YEAR_XLAB, y_label = "Count")

create_platform_year_plot(df, year_var = "year", platform_var = "macro_topic", 
                          x_label = YEAR_XLAB, y_label = "Count")

#####


##### Platform shares per year ----

# Companion to create_platform_year_plot(): the same levels, but each year
# normalised to itself, so the figure shows how the mix shifts rather than how
# the catalogue grows. Every year sums to 100%.
#
#   drop_labels    records that name no platform are removed BEFORE the shares
#                  are formed. They are not a platform, and leaving them in
#                  would dilute every share by an amount that varies per year.
#                  Their number is reported for the caption.
#   pooled_labels  "Multiple" is never a level of its own here. It is not a
#                  platform but a mixture, so it joins "Other".
#   top_n          <= 7, for the reason given at create_platform_year_plot():
#                  the palette carries eight distinct levels, one of which is
#                  "Other". Six, not seven: on the creation dates 4chan,
#                  Instagram and Telegram all hold 16 records, so rank seven is
#                  a three-way tie and any of them would be shown for no reason
#                  other than sort order. A tie on the last rank is still broken
#                  alphabetically so the figure is reproducible, and reported.
#   min_year_n     a share over three datasets is noise drawn as a data point.
#                  Years below this many records drop out, and the n of every
#                  year that stays is printed under its tick.
create_platform_share_plot <- function(data,
                                       year_var      = "year",
                                       platform_var  = "platform",
                                       x_label       = YEAR_XLAB,
                                       y_label       = "Share of datasets per year",
                                       legend_label  = "Platform",
                                       top_n         = 6,
                                       other_label   = "Other",
                                       pooled_labels = c("Multiple"),
                                       drop_labels   = c("Not specified", "Unspecified"),
                                       min_year_n    = 10) {

  stopifnot(top_n <= length(palette_anchors) - 1)

  n_start <- nrow(data)

  n_missing <- sum(is.na(data[[year_var]]))
  if (n_missing > 0) {
    message(sprintf("%d of %d records carry no %s date and are left out of this plot.",
                    n_missing, n_start, tolower(DATE_LABEL)))
    data <- data |> filter(!is.na(.data[[year_var]]))
  }

  n_dropped <- sum(is.na(data[[platform_var]]) | data[[platform_var]] %in% drop_labels)
  if (n_dropped > 0) {
    message(sprintf("%d of %d records name no platform and are left out before the shares are formed.",
                    n_dropped, n_start))
    data <- data |>
      filter(!is.na(.data[[platform_var]]), !.data[[platform_var]] %in% drop_labels)
  }

  year_n <- data |> count(.data[[year_var]], name = "total")
  thin   <- year_n |> filter(total < min_year_n)
  if (nrow(thin) > 0) {
    message(sprintf("Years below %d records are left out: %s.", min_year_n,
                    paste0(thin[[year_var]], " (n=", thin$total, ")", collapse = ", ")))
    data   <- data |> filter(.data[[year_var]] %in% year_n[[year_var]][year_n$total >= min_year_n])
    year_n <- year_n |> filter(total >= min_year_n)
  }

  # Ranked over the whole catalogue, not per year: the levels have to be the
  # same in every year, otherwise the lines are not comparable.
  ranked <- data |>
    filter(!.data[[platform_var]] %in% pooled_labels) |>
    count(.data[[platform_var]], name = "n") |>
    arrange(desc(n), .data[[platform_var]])

  keep <- ranked[[platform_var]][seq_len(min(top_n, nrow(ranked)))]

  if (nrow(ranked) > top_n && ranked$n[top_n] == ranked$n[top_n + 1]) {
    tied <- ranked[[platform_var]][ranked$n == ranked$n[top_n]]
    message(sprintf("Rank %d is a tie at n=%d between %s; %s is shown, the rest joins '%s'.",
                    top_n, ranked$n[top_n], paste(tied, collapse = ", "),
                    ranked[[platform_var]][top_n], other_label))
  }

  data <- data |>
    mutate(!!sym(platform_var) := ifelse(!!sym(platform_var) %in% keep,
                                         as.character(!!sym(platform_var)), other_label))

  platform_levels <- c(keep, other_label)

  platform_totals <- data |>
    count(.data[[platform_var]], name = "n") |>
    complete(!!sym(platform_var) := platform_levels, fill = list(n = 0))
  label_map <- setNames(paste0(platform_totals[[platform_var]], " (", platform_totals$n, ")"),
                        platform_totals[[platform_var]])

  years_range <- sort(unique(data[[year_var]]))

  shares <- data |>
    count(.data[[year_var]], .data[[platform_var]], name = "freq") |>
    complete(!!sym(year_var) := years_range, !!sym(platform_var) := platform_levels,
             fill = list(freq = 0)) |>
    group_by(.data[[year_var]]) |>
    mutate(share = freq / sum(freq)) |>
    ungroup() |>
    mutate(!!sym(platform_var) := factor(!!sym(platform_var), levels = platform_levels),
           !!sym(year_var)     := factor(!!sym(year_var), levels = years_range))

  year_labels <- setNames(paste0(year_n[[year_var]], " (n=", year_n$total, ")"),
                          as.character(year_n[[year_var]]))

  cols <- palette_qual_ordered(length(platform_levels), bands = palette_bands_line)
  names(cols) <- platform_levels

  ggplot(shares, aes(x = !!sym(year_var), y = share,
                     colour = !!sym(platform_var), group = !!sym(platform_var))) +
    geom_line(linewidth = 0.7) +
    geom_point(size = 2) +
    scale_colour_manual(values = cols, breaks = platform_levels,
                        labels = label_map[platform_levels], drop = FALSE,
                        name = legend_label) +
    scale_x_discrete(labels = year_labels[as.character(years_range)]) +
    scale_y_continuous(labels = scales::percent_format(accuracy = 1),
                       expand = expansion(mult = c(0.02, 0.05))) +
    labs(x = x_label, y = y_label) +
    theme_minimal(base_size = 20) +
    theme(axis.text.x = element_text(angle = 60, hjust = 1), legend.position = "right")
}

# Full df: the function pools the tail itself and reports what it drops.
create_platform_share_plot(df, year_var = "year", platform_var = "platform")

#####

##### Timeline of platforms ---- 
# All Datasets 
df_timeline <- df %>%
  filter(!is.na(.data[[DATE_COL]])) %>%
  mutate(month = floor_date(.data[[DATE_COL]], "month")) %>%
  group_by(month) %>%
  summarise(n = n(), .groups = "drop") 

# date_min / date_max fix the reporting window for every timeline, so panels
# for different grouping variables share one x axis and stay comparable.
# Observations outside the window are dropped; months without data are filled
# with zeros.
create_timeline <- function(data, group_var, group_var_order, filter = 5, 
                            timeframe_unit="month", timeline_breaks="6 month", timeline_label="%m/%y",
                            date_min = as.Date("2016-01-01"), date_max = as.Date("2026-07-14"),
                            x_label = MONTH_XLAB, legend_label = "Caption") {
  
  top_5_categories <- data %>%
    count(.data[[group_var]]) %>%
    slice_max(n, n = 5, with_ties = FALSE) %>%
    pull(1)
  
  df_aggregated <- data %>% 
    filter(!is.na(.data[[DATE_COL]])) %>% 
    filter(.data[[group_var]] %in% top_5_categories) %>% 
    group_by(.data[[group_var]]) %>%
    filter(n() > filter) %>% 
    ungroup() %>% 
    mutate(
      timeframe = as.Date(floor_date(.data[[DATE_COL]], timeframe_unit)), 
      group_var_factor = factor(.data[[group_var]], levels = group_var_order[group_var_order %in% top_5_categories]) 
    ) %>%
    group_by(timeframe, group_var_factor) %>% 
    summarise(n = n(), .groups = "drop") %>% 
    mutate(group_var_factor = droplevels(group_var_factor))
  
  df_aggregated <- df_aggregated %>%
    filter(timeframe >= date_min, timeframe <= date_max)
  
  if(nrow(df_aggregated) == 0) stop("No data left after filtering.")
  
  all_dates <- seq.Date(from = date_min, to = date_max, by = timeframe_unit)
  
  df_timeline <- df_aggregated %>%
    complete(timeframe = all_dates, group_var_factor, fill = list(n = 0)) 
  
  n_categories <- length(unique(df_timeline$group_var_factor))

  plot_colors <- palette_qual_ordered(n_categories, bands = palette_bands_line)

  ggplot(df_timeline, aes(x = timeframe, y = n, color = group_var_factor, group = group_var_factor)) +
    geom_line(linewidth = 0.5) +           
    geom_point(size = 1) +              
    scale_x_date(breaks = seq.Date(date_min, date_max, by = timeline_breaks),
                 date_labels = timeline_label,
                 limits = c(date_min, date_max),
                 expand = expansion(mult = 0.01)) + 
    labs(x = x_label, y = "Number Datasets", color = legend_label) +
    scale_color_manual(values = plot_colors) + 
    theme_minimal(base_size=18) +     
    theme(axis.text.x = element_text(angle = 45, hjust = 1))
}

tl_topic = create_timeline(data = df, group_var = "macro_topic", group_var_order = unique(df$macro_topic), filter=50,
                           legend_label = "Macro Topics")

tl_platform = create_timeline(data = df_platform_top15, group_var = "platform", group_var_order = platform_order, filter=0,
                              legend_label = "Platform")

tl_repo = create_timeline(data = df, group_var = "repository", group_var_order = unique(df$repository), filter=0,
                          legend_label = "Repository")

tl_cd = create_timeline(data = df, group_var = "collection_described", 
                        group_var_order = sort(unique(df$collection_described)), filter=0, 
                        legend_label = "Collection described")

tl_ts = create_timeline(data = df, group_var = "timestamp", group_var_order = sort(unique(df$timestamp)), filter=0,
                        legend_label = "Timestamped")

tl_labeled = create_timeline(data = df, group_var = "labeled", group_var_order = sort(unique(df$labeled)), filter=0,
                             legend_label = "Labeled")

tl_raw = create_timeline(data = df, group_var = "raw", group_var_order = sort(unique(df$raw)), filter=0,
                         legend_label = "Raw Data")

tl_syn = create_timeline(data = df, group_var = "synthetic", group_var_order = sort(unique(df$synthetic)), filter=0,
                         legend_label = "Synthetic Data")

tl_category <- tl_platform / tl_repo / tl_topic
tl_category + plot_layout(guides = "collect") + plot_layout(guides = "keep") 

##### Comparison of the two date columns ----
# One figure, two panels, one shared x axis.
#
# The upper panel gives the annual counts and answers whether the catalogue
# still grows. The lower panel gives the monthly counts and separates the two
# peaks: December 2022 rises on both series and is therefore new material,
# whereas May 2023 rises only on the last-update series and is a re-deposit of
# archives created years earlier.
#
# The window is the same one every other timeline uses, so the figure can be
# read against the timeline figure without rescaling. The incomplete final
# year is dropped from the annual panel, because half a year plotted next to
# full ones reads as a collapse. The monthly panel keeps it.
create_date_comparison <- function(data,
                                   date_min = as.Date("2016-01-01"),
                                   date_max = as.Date("2026-07-14"),
                                   timeline_breaks = "1 year",
                                   timeline_label = "%Y",
                                   drop_partial_year = TRUE) {

  stopifnot(all(c("created", "updated") %in% names(data)))

  series_levels <- c("Creation", "Last update")
  # Two named anchors instead of palette_qual_ordered(2). At n = 2 the ramp
  # returns the two ends of the anchor list and forces them onto the
  # outermost lightness bands, and the light one lands at a contrast of
  # 2.4:1 against a white page. That is too pale for a 0.5 pt line the
  # reader has to follow across the whole window. Anchors 1 and 7 keep
  # the house hues, separate by dE 31 in normal vision and dE 27 under
  # protanopia, and both clear 3:1 against the surface.
  plot_colors <- palette_anchors[c(1, 7)]
  names(plot_colors) <- series_levels

  long <- data %>%
    select(created, updated) %>%
    pivot_longer(everything(), names_to = "series", values_to = "date") %>%
    mutate(
      series = factor(ifelse(series == "created", series_levels[1], series_levels[2]),
                      levels = series_levels),
      date = as.Date(date)
    )

  n_missing <- sum(is.na(long$date))
  n_before <- sum(!is.na(long$date) & long$date < date_min)
  message(sprintf(
    "Date comparison: %d of %d values carry no date and %d fall before %s. Both are left out.",
    n_missing, nrow(long), n_before, format(date_min, "%B %Y")))

  long <- long %>% filter(!is.na(date), date >= date_min, date <= date_max)

  # ── monthly ───────────────────────────────────────────────────────────────
  months_all <- seq.Date(floor_date(date_min, "month"),
                         floor_date(date_max, "month"), by = "month")
  monthly <- long %>%
    mutate(t = floor_date(date, "month")) %>%
    count(series, t) %>%
    complete(series, t = months_all, fill = list(n = 0))

  # ── annual ────────────────────────────────────────────────────────────────
  years_all <- seq.Date(floor_date(date_min, "year"),
                        floor_date(date_max, "year"), by = "year")
  annual <- long %>%
    mutate(t = floor_date(date, "year")) %>%
    count(series, t) %>%
    complete(series, t = years_all, fill = list(n = 0))

  if (drop_partial_year && date_max < ceiling_date(floor_date(date_max, "year"), "year") - 1) {
    annual <- annual %>% filter(t < floor_date(date_max, "year"))
  }

  gemeinsame_x <- function() {
    scale_x_date(breaks = seq.Date(date_min, date_max, by = timeline_breaks),
                 date_labels = timeline_label,
                 limits = c(date_min, date_max),
                 expand = expansion(mult = 0.01))
  }

  p_year <- ggplot(annual, aes(x = t, y = n, color = series, group = series)) +
    geom_line(linewidth = 0.7) +
    geom_point(size = 2) +
    gemeinsame_x() +
    scale_color_manual(values = plot_colors) +
    labs(x = NULL, y = "Datasets per year", color = "Date recorded") +
    theme_minimal(base_size = 18) +
    theme(axis.text.x = element_blank())

  p_month <- ggplot(monthly, aes(x = t, y = n, color = series, group = series)) +
    geom_line(linewidth = 0.5) +
    geom_point(size = 1) +
    gemeinsame_x() +
    scale_color_manual(values = plot_colors) +
    labs(x = "Month", y = "Datasets per month", color = "Date recorded") +
    theme_minimal(base_size = 18) +
    theme(axis.text.x = element_text(angle = 45, hjust = 1))

  (p_year / p_month) + plot_layout(guides = "collect")
}

date_comparison <- create_date_comparison(df)
date_comparison

tl_binary <- tl_cd / tl_ts / tl_labeled / tl_raw / tl_syn
tl_binary + plot_layout(guides = "collect") + plot_layout(guides = "keep") 

##### 

##### Cramers V heatmap -----

cramers_v <- function(a, b) {
  tab <- table(a, b)
  if (nrow(tab) < 2 || ncol(tab) < 2) return(NA_real_)
  chi2 <- suppressWarnings(chisq.test(tab, correct = FALSE)$statistic)
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
   suppressWarnings(chisq.test(tab)$p.value)
 }
 
 # Collapse high-cardinality categoricals to the top-N levels + "Other"
 # so Cramér's V is not inflated by dozens of singleton categories.
 lump_levels <- function(x, n = 8, other = "Other", na_label = "Not specified") {
   x <- as.character(x); x[is.na(x) | x == ""] <- na_label
   keep <- names(sort(table(x), decreasing = TRUE))[seq_len(min(n, length(unique(x))))]
   factor(ifelse(x %in% keep, x, other))
 }
 
 yesno <- function(x) factor(ifelse(x == 1, "Yes", "No"), levels = c("No", "Yes"))
 
 # Feature set (uses the already-cleaned df: bool_vars are 0/1 integers here)
 cramer_feat <- tibble(
   collection_described = yesno(df$collection_described),
   timestamp            = yesno(df$timestamp),
   labeled              = yesno(df$labeled),
   raw                  = yesno(df$raw),
   synthetic            = yesno(df$synthetic),
   paper                = yesno(df$paper_binary),
   code                 = yesno(df$code),
   macro_topic          = lump_levels(df$macro_topic, n = 15),
   platform             = lump_levels(df$platform,   n = 8),
   repository           = lump_levels(df$repository, n = 8),
   license              = lump_levels(df$license,    n = 6)
 )
 
 cramers_v_heatmap <- function(feat) {
   vars <- colnames(feat); m <- length(vars)
   V <- matrix(NA_real_, m, m, dimnames = list(vars, vars))
   P <- matrix(NA_real_, m, m, dimnames = list(vars, vars))
   for (i in seq_len(m)) for (j in seq_len(m)) {
     # Diagonal stays NA so the tile is drawn blank; a self-association of 1
     # would otherwise render as the darkest cell in the matrix and read as a
     # result. The figure caption states that the diagonal is omitted.
     if (i == j) { V[i, j] <- NA_real_; P[i, j] <- NA_real_; next }
     V[i, j] <- cramers_v(feat[[i]], feat[[j]])
     P[i, j] <- chisq_p(feat[[i]], feat[[j]])
   }
   # Holm correction across the unique off-diagonal pairs, mirrored back
   ut <- upper.tri(P)
   Padj <- matrix(NA_real_, m, m, dimnames = list(vars, vars))
   Padj[ut] <- p.adjust(P[ut], method = "holm")
   Padj[lower.tri(Padj)] <- t(Padj)[lower.tri(Padj)]
   
   long <- expand.grid(Var1 = vars, Var2 = vars, stringsAsFactors = FALSE) %>%
     mutate(
       V     = mapply(function(a, b) V[a, b], Var1, Var2),
       padj  = mapply(function(a, b) Padj[a, b], Var1, Var2),
       stars = case_when(is.na(padj) ~ "", padj < .001 ~ "***",
                         padj < .01 ~ "**", padj < .05 ~ "*", TRUE ~ ""),
       label = ifelse(Var1 == Var2, "", paste0(sprintf("%.2f", V), stars)),
       Var1  = factor(Var1, levels = vars),
       Var2  = factor(Var2, levels = rev(vars))
     )
   
   ggplot(long, aes(Var1, Var2, fill = V)) +
     geom_tile(color = "white", linewidth = 0.4) +
     geom_text(aes(label = label, color = V > 0.5), size = 3.5, show.legend = FALSE) +
     scale_color_manual(values = c(`TRUE` = "white", `FALSE` = palette_navy)) +
     scale_fill_gradient(low = "white", high = palette_navy, limits = c(0, 1),
                         na.value = "grey96", name = "Cramér's V") +
     coord_fixed() +
     labs(x = NULL, y = NULL) + # ,
          # title = "Feature association (bias-corrected Cramér's V)",
          # subtitle = "Stars = Holm-adjusted chi-square: *p<.05  **p<.01  ***p<.001") +
     theme_minimal(base_size = 15) +
     theme(axis.text.x   = element_text(angle = 45, hjust = 1),
           panel.grid    = element_blank(),
           plot.title    = element_text(face = "bold", size = 15),
           plot.subtitle = element_text(size = 11, color = "grey40"))
 }
 
 p_cramer <- cramers_v_heatmap(cramer_feat)
 p_cramer
#####

 
 ##### Category-level residual heatmaps -----
 
 residual_heatmap <- function(data, cat_var, flags, cat_order = NULL,
                              min_n = 10, flag_labels = NULL,
                              low = palette_navy, high = palette_gold,
                              title = NULL) {
   
   d <- data %>% filter(!is.na(.data[[cat_var]]))
   d[[cat_var]] <- as.character(d[[cat_var]])
   
   # Drop categories with too few datasets
   keep <- d %>% count(.data[[cat_var]], name = "n") %>%
     filter(n >= min_n) %>% pull(1)
   d <- d %>% filter(.data[[cat_var]] %in% keep)
   
   N    <- nrow(d)
   cats <- if (is.null(cat_order)) sort(unique(d[[cat_var]])) else cat_order[cat_order %in% keep]
   
   # Adjusted standardised residual (Agresti) for the "flag = 1" cell
   res <- lapply(flags, function(fl) {
     x <- d[[fl]]; col_yes <- sum(x == 1)
     lapply(cats, function(c) {
       m  <- d[[cat_var]] == c; ni <- sum(m); o <- sum(x[m] == 1)
       e  <- ni * col_yes / N
       dstd <- if (e > 0) (o - e) / sqrt(e * (1 - ni / N) * (1 - col_yes / N)) else NA_real_
       tibble(category = c, flag = fl, pct = 100 * o / ni, resid = dstd, n = ni)
     }) %>% bind_rows()
   }) %>% bind_rows()
   
   flag_levels <- flags
   if (!is.null(flag_labels)) {
     res$flag    <- flag_labels[res$flag]
     flag_levels <- unname(flag_labels[flags])
   }
   
   lim <- max(abs(res$resid), na.rm = TRUE)
   res <- res %>%
     mutate(
       category = factor(category, levels = rev(cats)),
       flag     = factor(flag, levels = flag_levels),
       label    = paste0(round(pct), "%", ifelse(abs(resid) > 1.96, "*", ""))
     )
   
   ggplot(res, aes(flag, category, fill = resid)) +
     geom_tile(color = "white", linewidth = 0.4) +
     geom_text(aes(label = label, color = resid < -0.4 * lim),
               size = 3.4, show.legend = FALSE) +
     scale_color_manual(values = c(`TRUE` = "white", `FALSE` = "grey15")) +
     scale_fill_gradient2(low = low, mid = "white", high = high, midpoint = 0,
                          limits = c(-lim, lim), name = "Adjusted residual") +
     labs(x = NULL, y = NULL, title = title) +
     theme_minimal(base_size = 18) +
     theme(axis.text.x = element_text(angle = 45, hjust = 1),
           panel.grid  = element_blank())
 }
 
 flag_lab <- c(timestamp = "timestamp", labeled = "labeled", raw = "raw",
               paper_binary = "paper", code = "code",
               collection_described = "collection\ndescribed")
 
 # Content: which topics differ in the data-quality flags
 p_resid_topic <- residual_heatmap(
   df, cat_var = "macro_topic", flags = c("timestamp", "labeled", "raw"),
   cat_order = topic_order[!is.na(topic_order)], min_n = 10, flag_labels = flag_lab
 )
 
 # Provenance: which repositories differ in the provenance flags
 p_resid_repo <- residual_heatmap(
   df, cat_var = "repository",
   flags = c("paper_binary", "code", "collection_described", "raw"),
   cat_order = repository_order, min_n = 10, flag_labels = flag_lab
 )
 
 ggarrange(p_resid_topic, p_resid_repo, common.legend = TRUE)
 
 #####

##### FAIR assessment ----
#
# Figures for the FAIR assessment. Reads the output of fuji_assessment.py and
# joins it to the catalogue by record id. Two figures:
#
#   plot_fair_metrics()        pass rate per FsF metric, grouped by principle
#   plot_fair_by_repository()  mean score per repository and FAIR category
#
# The metric-level figure carries the argument of the section: the catalogue
# does comparatively well on licence and embedded metadata and fails on
# persistent identifiers. A single overall score averages that contrast away,
# which is why neither figure shows one.
#
# Colour: the four FAIR principles are nominal and never touch each other in
# either figure, so the plain ramp palette_qual() applies rather than the
# _ordered variant. Four categories are far below the eight-anchor limit.
# In the metric figure the facet strips name the principle, so identity does
# not rest on colour alone and the legend is redundant.

FAIR_FILE <- file.path("data", "fair_fuji.csv")
FAIR_FIG_DIR <- "."

# Short names of the FsF metrics, metric version 0.5.
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

# The FAIR principle of a metric is encoded in its identifier (FsF-R1.1-01M -> R).
fair_metric_category <- function(metric_id) {
  sub("^FsF-([FAIR]).*$", "\\1", metric_id)
}

# Figure 1: how many records pass each individual metric.
plot_fair_metrics <- function(fair_data, metric_labels = fair_metric_labels) {

  plot_data <- fair_data %>%
    select(ends_with("_status"), -any_of("run_status")) %>%
    pivot_longer(everything(), names_to = "metric", values_to = "status") %>%
    mutate(metric = sub("_status$", "", sub("^m_", "", metric))) %>%
    filter(!is.na(status)) %>%
    group_by(metric) %>%
    summarise(percentage = mean(status == "pass", na.rm = TRUE) * 100,
              n_assessed = n(), .groups = "drop") %>%
    mutate(
      category = factor(fair_metric_category(metric), levels = fair_categories),
      label    = ifelse(metric %in% names(metric_labels),
                        metric_labels[metric], metric),
      label    = fct_reorder(label, percentage)
    )

  cols <- setNames(palette_qual(length(fair_categories)), fair_categories)

  ggplot(plot_data, aes(x = percentage, y = label, colour = category)) +
    geom_segment(aes(x = 0, xend = percentage, yend = label), linewidth = 1) +
    geom_point(size = 5) +
    geom_text(aes(label = paste0(round(percentage, 1), "%")),
              hjust = -0.3, size = 4, colour = "grey20") +
    scale_colour_manual(values = cols, guide = "none") +
    scale_x_continuous(limits = c(0, 118), breaks = seq(0, 100, 20)) +
    facet_grid(category ~ ., scales = "free_y", space = "free_y") +
    labs(x = "Records passing the metric (percent)", y = NULL) +
    theme_minimal(base_size = 18) +
    theme(panel.grid.major.y = element_blank(),
          strip.text.y       = element_text(face = "bold"))
}

# Figure 2: mean score per repository and FAIR category.
#
# min_n matters here. Dryad and EU-ODP hold one record each, RDA two, CESSDA and
# ScienceDB four. A mean over one record is not a mean, but it fills a tile just
# as strongly as Kaggle with more than a thousand. Repositories below min_n are
# therefore pooled, and every row label carries its n.
plot_fair_by_repository <- function(fair_data, min_n = 10) {

  other_label <- sprintf("Other (n < %d each)", min_n)

  keep <- fair_data %>%
    count(repository) %>%
    filter(n >= min_n) %>%
    pull(repository)

  pooled <- fair_data %>%
    mutate(repo = ifelse(repository %in% keep, repository, other_label))

  repo_n <- pooled %>% count(repo, name = "n_repo")

  plot_data <- pooled %>%
    select(repo, pct_F, pct_A, pct_I, pct_R) %>%
    pivot_longer(starts_with("pct_"), names_to = "category", values_to = "score") %>%
    mutate(category = factor(sub("^pct_", "", category), levels = fair_categories)) %>%
    group_by(repo, category) %>%
    summarise(mean_score = mean(score, na.rm = TRUE), .groups = "drop")

  overall <- plot_data %>%
    group_by(repo) %>%
    summarise(overall_mean = mean(mean_score, na.rm = TRUE), .groups = "drop")

  plot_data <- plot_data %>%
    left_join(repo_n,  by = "repo") %>%
    left_join(overall, by = "repo") %>%
    mutate(repo_label = fct_reorder(sprintf("%s (n = %d)", repo, n_repo),
                                    overall_mean))

  ggplot(plot_data, aes(x = category, y = repo_label, fill = mean_score)) +
    geom_tile(colour = "white", linewidth = 1) +
    geom_text(aes(label = sprintf("%.0f", mean_score),
                  colour = mean_score > 55),
              size = 5, show.legend = FALSE) +
    scale_fill_gradient(low = "white", high = palette_navy,
                        limits = c(0, 100),
                        name = "Mean score\n(percent)") +
    scale_colour_manual(values = c(`TRUE` = "white", `FALSE` = "grey20")) +
    labs(x = "FAIR category", y = NULL) +
    theme_minimal(base_size = 18) +
    theme(panel.grid = element_blank())
}

# The assessment is a separate run and may not have finished yet, so the figures
# are skipped rather than allowed to break the script.
if (!file.exists(FAIR_FILE)) {
  message("FAIR results not found at ", FAIR_FILE,
          " - skipping the FAIR figures. Run fuji_assessment.py first.")
} else {
  fair_raw <- read_delim(FAIR_FILE, delim = ";",
                         locale = locale(encoding = "UTF-8"),
                         show_col_types = FALSE)

  # Only completed assessments enter the figures. The records that failed are
  # reported separately in the text, they are a finding in their own right.
  # id is a mix of hashes and numeric repository ids, so both sides are forced to
  # character before the join rather than relying on the guessed column type.
  fair <- fair_raw %>%
    filter(run_status == "ok") %>%
    select(-any_of("repository")) %>%
    mutate(id = as.character(id)) %>%
    inner_join(df %>% select(id, repository) %>% mutate(id = as.character(id)),
               by = "id")

  message(sprintf("FAIR figures based on %d of %d assessed records.",
                  nrow(fair), nrow(fair_raw)))

  p_fair_metrics <- plot_fair_metrics(fair)
  p_fair_repo    <- plot_fair_by_repository(fair)

  print(p_fair_metrics)
  print(p_fair_repo)

  # File names are fixed: main.tex includes exactly these two.
  ggsave(file.path(FAIR_FIG_DIR, "fair_metrics.png"), p_fair_metrics,
         width = 10, height = 8, dpi = 300, bg = "white")
  ggsave(file.path(FAIR_FIG_DIR, "fair_by_repository.png"), p_fair_repo,
         width = 9, height = 6, dpi = 300, bg = "white")
}

#####
