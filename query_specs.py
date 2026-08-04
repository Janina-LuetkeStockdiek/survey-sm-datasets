"""Shared search vocabulary and file-relevance rules.

Every harvesting script imports its query terms and filters from here so the
definition of "a relevant social-media text dataset" stays consistent across
all platforms:

- ``PLATFORMS``        social-media platforms / networks to search for
- ``TEXT_TERMS``       content words describing textual data (posts, tweets, ...)
- ``DATA_FORMATS``     file extensions considered to be actual data files
- ``MIN_SIZE_BYTES``   minimum total file size for a dataset to be kept
- ``MIN_CREATED_DATE`` earliest creation date to consider
- ``is_relevant_file`` decides whether a single filename is a usable data file
"""

# Search terms for social-media platforms, corresponding to the "Platforms" row
# of Table 1 in the accompanying paper. The list was compiled from a ranking of
# the most popular social platforms by monthly active users; platforms used
# predominantly by non-English-speaking communities (e.g. VK, Sina Weibo) were
# excluded, while Bluesky and Mastodon were added for their open data access and
# Truth Social for its political relevance.
#
# Note on the two X/Twitter entries: both the historical and the current brand
# name are searched. "X" alone is far too ambiguous (X-ray, X-axis, ...), so the
# addendum "platform" is appended. The two generic terms "social media" and
# "social network" catch datasets that are not tied to a named platform.
PLATFORMS = [
    "Twitter", "X platform",
    "Facebook", "Instagram",
    "Reddit", "4chan",
    "YouTube", "TikTok", "Twitch",
    "Telegram", "WhatsApp", "Discord", "Snapchat",
    "Bluesky", "Mastodon",
    "LinkedIn", "Tumblr", "Pinterest", "Quora",
    "Gettr", "Truth Social",
    "social media", "social network",
]
# 23 terms, matching Table 1 of the paper. Repositories that support Boolean
# operators receive one query per platform (23 queries); repositories that do
# not receive the Cartesian product PLATFORMS x TEXT_TERMS (23 x 28 = 644).
assert len(PLATFORMS) == 23


TEXT_TERMS = [
    "text", "texts",
    "post", "posts",
    "tweet", "tweets",
    "message", "messages",
    "comment", "comments",
    "chat", "chats",
    "conversation", "conversations",
    "submission", "submissions",
    "thread", "threads",
    "update", "updates",
    "skeet", "skeets",
    "toot", "toots",
    "story", "stories",
    "reblog", "reblogs",
]

MIN_SIZE_BYTES = 10 * 1024  # 10 KB

DATA_FORMATS = ["csv", "xlsx", "xls", "parquet", "rdata", "rds", "txt", "zip", "gz", "tsv", "xls", "ods",
                "sqlite", "db", "feather", "accdb", "arrow", "rda", "sav", "dta", "json", "jsonl",
                "ndjson", "xml", "rar", "tar", "tar.gz", "tab"]

MIN_CREATED_DATE = "2010-01-01"  # Format: YYYY-MM-DD


def is_relevant_file(filename):
    """Return True if ``filename`` looks like a usable data file.

    A file is relevant when its extension is one of ``DATA_FORMATS`` and it is
    not a boilerplate file (readme, license, changelog, ...). Extension and name
    are compared case-insensitively.
    """
    IGNORE_PATTERNS = ("readme", "license", "licence", "copying", "changelog")
    IGNORE_FILENAMES = {
        "readme.txt", "license.txt", "licence.txt", "changelog.txt",
        "notes.txt", "citation.txt", "authors.txt", "manifest.txt",
    }
    name_lower = filename.lower()

    if "." not in name_lower:
        return False
    ext = name_lower.rsplit(".", 1)[1]
    if ext not in DATA_FORMATS:
        return False

    if name_lower in IGNORE_FILENAMES:
        return False
    stem = name_lower.rsplit(".", 1)[0]
    if any(p in stem for p in IGNORE_PATTERNS):
        return False
    return True
