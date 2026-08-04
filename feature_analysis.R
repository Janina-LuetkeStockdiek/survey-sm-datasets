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

# Shared qualitative palette for plots with many categories
palette_qual <- function(n) {
  anchors <- c("#182F50", "#3E6DA0", "#7FB0D6", "#4F9D8E",
               "#8FBF73", "#C7A24C", "#9C6B2E", "#7E4A57")
  grDevices::colorRampPalette(anchors)(n)
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

df <- read_delim(DATA_FILE, delim = ";", locale = locale(encoding = "UTF-8"),
                 show_col_types = FALSE)

bool_vars <- c("raw", "synthetic", "collection_described", "timestamp", "labeled", "code")

df <- df %>%
  select(-nb) %>%
  mutate(
    year = lubridate::year(df$updated),
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
  
  plot_data <- data %>%
    filter(!is.na(.data[[var]])) %>%
    count(category = .data[[var]], name = "n") %>%
    arrange(desc(n))
  
  # For variables with many levels (e.g. platform) show only the top N
  note <- NULL
  if (!is.null(top_n) && nrow(plot_data) > top_n) {
    hidden <- nrow(plot_data) - top_n
    plot_data <- plot_data %>% slice_head(n = top_n)
    note <- paste0("(Top ", top_n, " of ", top_n + hidden, ", 'Not specified' omitted)")
  }
  
  ggplot(plot_data, aes(x = n, y = reorder(category, n))) +
    geom_col(fill = fill, width = 0.7) +
    geom_text(aes(label = n), hjust = -0.2, size = 4.5) +
    scale_x_continuous(expand = expansion(mult = c(0, 0.15))) +
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
    collection_described = "collection descr.",
    code                 = "code available"
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

create_platform_year_plot <- function(data, 
                                      year_var = "year", 
                                      platform_var = "platform", 
                                      x_label = "Year (Last Update)", 
                                      y_label = "Number of datasets") {
  
  years_range = min(data$year):max(data$year)
  
  platform_order <- data |>
    count(!!sym(platform_var), name = "n") |>
    arrange(desc(n)) |>
    pull(!!sym(platform_var))
  
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
  cols <- palette_qual(n_platforms)
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

create_platform_year_plot(df_platform_top15, year_var = "year", platform_var = "platform",
                          x_label = "Year (Last Update)", y_label = "Number of datasets")

create_platform_year_plot(df, year_var = "year", platform_var = "repository", 
                          x_label = "Year (Last Update)", y_label = "Count")

create_platform_year_plot(df, year_var = "year", platform_var = "macro_topic", 
                          x_label = "Year (Last Update)", y_label = "Count")

#####


##### Timeline of platforms ---- 
# All Datasets 
df_timeline <- df %>%
  mutate(month = floor_date(updated, "month")) %>%
  group_by(month) %>%
  summarise(n = n(), .groups = "drop") 

create_timeline <- function(data, group_var, group_var_order, filter = 5, 
                            timeframe_unit="month", timeline_breaks="6 month", timeline_label="%m/%y",
                            x_label = "Date of last update grouped per month", legend_label = "Caption") {
  
  top_5_categories <- data %>%
    count(.data[[group_var]]) %>%
    slice_max(n, n = 5, with_ties = FALSE) %>%
    pull(1)
  
  df_aggregated <- data %>% 
    filter(.data[[group_var]] %in% top_5_categories) %>% 
    group_by(.data[[group_var]]) %>%
    filter(n() > filter) %>% 
    ungroup() %>% 
    mutate(
      timeframe = as.Date(floor_date(updated, timeframe_unit)), 
      group_var_factor = factor(.data[[group_var]], levels = group_var_order[group_var_order %in% top_5_categories]) 
    ) %>%
    group_by(timeframe, group_var_factor) %>% 
    summarise(n = n(), .groups = "drop") %>% 
    mutate(group_var_factor = droplevels(group_var_factor))
  
  if(nrow(df_aggregated) == 0) stop("No data left after filtering.")
  
  all_dates <- seq.Date(
    from = min(df_aggregated$timeframe, na.rm = TRUE), 
    to = max(df_aggregated$timeframe, na.rm = TRUE), 
    by = timeframe_unit)
  
  df_timeline <- df_aggregated %>%
    complete(timeframe = all_dates, group_var_factor, fill = list(n = 0)) 
  
  n_categories <- length(unique(df_timeline$group_var_factor))

  plot_colors <- palette_qual(n_categories)

  ggplot(df_timeline, aes(x = timeframe, y = n, color = group_var_factor, group = group_var_factor)) +
    geom_line(linewidth = 0.5) +           
    geom_point(size = 1) +              
    scale_x_date(date_labels = timeline_label, date_breaks = timeline_breaks) + 
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
     if (i == j) { V[i, j] <- 1; P[i, j] <- 0; next }
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
                         name = "Cramér's V") +
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