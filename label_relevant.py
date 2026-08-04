"""LLM-based relevance classification and evaluation of the classifiers.

This is step 3 of the pipeline. Every harvested record is classified as relevant
(1) or irrelevant (0) by a large language model on the basis of its title and
description. Because the manual inspection that follows can only remove false
positives, the classification is tuned for recall.

The module provides two entry points:

- :func:`label_relevance` sends every record of a file to a chat-completions
  endpoint three times and stores the individual runs plus their majority vote.
- :func:`confusion_and_metrics` compares the stored predictions against the
  adjudicated human labels and reports run-to-run consistency, a confusion
  matrix, recall and the F2 score.

Any OpenAI-compatible endpoint can be used, which is how the open-weight models
(GPT-OSS, Llama, Gemma) served locally and the proprietary baseline are queried
through the same code path. Credentials and endpoints are read from the
environment; see ``.env.example``.

Typical use::

    label_relevance(
        file="data/dataset_initial.csv",
        prompt_template=CLASSIFIER_SYSTEM_PROMPT,
        model="gpt-oss-120b",
        base_url=os.getenv("LOCAL_LLM_BASE_URL"),
        api_key=os.getenv("LOCAL_LLM_API_KEY"),
    )
"""

import pandas as pd
from openai import OpenAI
from sklearn.metrics import confusion_matrix, fbeta_score, recall_score
from tqdm import tqdm

# The classification is repeated three times per record. These suffixes are
# appended to the model name to form the prediction column names; "p2" marks the
# final (second) prompt, "p1" the initial, stricter one.
RUN_SUFFIXES = ["_p2", "_2_p2", "_3_p2"]

# Value written when the API call fails, so failed calls stay distinguishable
# from a genuine 0/1 decision.
API_ERROR_VALUE = -1


CLASSIFIER_SYSTEM_PROMPT_STRICT = """
You are a strict classifier for dataset descriptions. Return only 1 or 0. No other text, spaces, or punctuation.

Return 1 if:
1. The dataset contains user-authored social media text (e.g., posts, comments, tweets, messages).
2. The dataset texts are in English (next to English, other languages present are possible).
3. The texts are mostly unmodified (no heavy preprocessing, cleaning, filtering, paraphrasing, or tokenization).

Return 0 if:
1. All user texts are non-English or target non-English-speaking communities/countries.
2. The repository provides mostly code, models, or data-collection tools (e.g., scrapers), not an actual dataset.
3. It presents only a method/model applied to an existing dataset, not a completely new dataset.
4. It contains only metadata, labels, IDs, embeddings, paraphrases, summaries, or short illustrative snippets, without the raw posts.
"""

CLASSIFIER_SYSTEM_PROMPT = """
You are a strict binary classifier for English-language social-media datasets. Output **only** `1` or `0`-no other characters, spaces, or punctuation.

**Output 1** if **all** of the following are true:
- The repository contains raw, social-media text (posts, comments, tweets, messages, etc.).
- At least one part of the dataset is in English or originates from English-speaking communities.

**Output 0** if **any** of the following applies:
- Every single text is non-English or come from non-English-speaking users/communities.
- Only identifiers, metadata, labels, or short illustrative snippets are provided, without the raw posts.
"""


def label_relevance(
    file: str,
    prompt_template: str,
    model: str,
    base_url: str | None,
    api_key: str | None,
) -> pd.DataFrame:
    """Classify every record of ``file`` as relevant (1) or irrelevant (0).

    Each record is sent to the model three times. The individual runs are stored
    in separate columns so that run-to-run consistency can be measured, and their
    majority vote is stored in ``<model>_majority_p2``.

    Two rule-based shortcuts are applied before the model is queried, to save
    calls on cases the prompt reliably gets wrong:

    - Descriptions mentioning "Hydrator" contain tweet IDs only, never posts.
    - GitHub repositories that merely point at a dataset hosted on Kaggle or
      Zenodo are mirrors, not new datasets.

    Args:
        file: Path to a ``;``-separated CSV or an Excel file with ``title``,
            ``description`` and ``url`` columns.
        prompt_template: System prompt defining the classification rules.
        model: Model identifier passed to the endpoint; also the column prefix.
        base_url: Base URL of an OpenAI-compatible endpoint, or None for the
            official OpenAI API.
        api_key: API key for that endpoint.

    Returns:
        The table with the three prediction columns and the majority vote added.
        The result is also written back to ``file``.
    """
    if file.endswith(".csv"):
        df = pd.read_csv(file, sep=";", encoding="utf-8-sig")
    else:
        df = pd.read_excel(file)

    if base_url is None:
        client = OpenAI(api_key=api_key)
    else:
        client = OpenAI(api_key=api_key, base_url=base_url)

    prompt = [
        {"role": "system", "content": prompt_template},
        {"role": "user", "content": ""},
    ]

    for suffix in RUN_SUFFIXES:
        column = model + suffix
        df[column] = pd.Series(dtype="object")

        for i in tqdm(range(len(df)), desc=f"{model}{suffix}"):
            text = f"{df.loc[i, 'title']}. {df.loc[i, 'description']}"

            # Rehydration-only datasets: IDs are provided, the posts are not.
            if " Hydrator" in text:
                df.loc[i, column] = 0
                continue

            # GitHub repositories that only mirror a dataset hosted elsewhere.
            if df.loc[i, "url"].lower() == "github" and (
                "kaggle" in text.lower() or "zenodo" in text.lower()
            ):
                df.loc[i, column] = 0
                continue

            prompt[1]["content"] = text

            try:
                resp = client.chat.completions.create(model=model, messages=prompt)
                df.loc[i, column] = resp.choices[0].message.content.strip()
            except Exception:
                df.loc[i, column] = API_ERROR_VALUE

    run_columns = [model + suffix for suffix in RUN_SUFFIXES]
    df[model + "_majority_p2"] = df[run_columns].mode(axis=1)[0]

    if file.endswith(".csv"):
        df.to_csv(file, sep=";", index=False, encoding="utf-8-sig")
    else:
        df.to_excel(file, index=False)

    return df


def confusion_and_metrics(
    file: str,
    model: str,
    truth_col: str = "ground_truth",
    prompt: str = "p1",
) -> dict:
    """Evaluate the stored predictions of one model against the human labels.

    All prediction columns starting with ``model`` and containing ``prompt`` are
    evaluated. Consistency is computed across the individual runs only (the
    majority column, which is assumed to be last, is excluded).

    Recall and F2 are reported rather than accuracy or F1: a false negative
    silently drops a relevant dataset from the study, whereas a false positive
    only adds manual screening effort.

    Args:
        file: Path to the annotated development or held-out test set.
        model: Column prefix identifying the model, e.g. ``"gpt-oss-120b"``.
        truth_col: Column holding the adjudicated human labels.
        prompt: Substring selecting the prompt variant, ``"p1"`` or ``"p2"``.
            Ignored for files that store only one prompt variant.

    Returns:
        A dictionary mapping each prediction column to its confusion matrix,
        recall and F2 score, plus a ``"consistency"`` entry with the share of
        records on which all runs agree.
    """
    if file.endswith(".csv"):
        df = pd.read_csv(file, sep=";", encoding="utf-8-sig")
    else:
        df = pd.read_excel(file)

    cols = [c for c in df.columns if c.startswith(model)]
    if any(prompt in c for c in cols):
        cols = [c for c in cols if prompt in c]
    if not cols:
        raise ValueError(f"No prediction columns found for model '{model}' in {file}.")

    run_columns = cols[:-1]  # the last column is the majority vote
    consistency_rate = (df[run_columns].nunique(axis=1) == 1).mean()
    print(f"{model} was consistent in {consistency_rate * 100:.1f} % of cases.")

    y_true = df[truth_col].values
    results = {"consistency": round(float(consistency_rate), 4)}

    for col in cols:
        y_pred = df[col].values.astype(int)
        cm = confusion_matrix(y_true, y_pred, labels=[0, 1])
        cm_df = pd.DataFrame(
            cm,
            index=pd.Index([f"Actual_{i}" for i in [0, 1]], name="Actual"),
            columns=pd.Index([f"Pred_{i}" for i in [0, 1]], name="Predicted"),
        )
        rec = recall_score(
            y_true, y_pred, pos_label=1, zero_division=0, average="binary"
        )
        f2 = fbeta_score(
            y_true, y_pred, beta=2, pos_label=1, zero_division=0, average="binary"
        )

        results[col] = {
            "confusion": cm_df,
            "recall": round(rec, 2),
            "f2": round(f2, 2),
        }

    return results


def annotator_agreement(file: str) -> dict:
    """Return Cohen's Kappa and raw agreement between the two human annotators.

    Args:
        file: Path to the development or held-out test set, containing the
            columns ``annotator_1`` and ``annotator_2``.

    Returns:
        A dictionary with ``"kappa"`` and ``"agreement"``.
    """
    from sklearn.metrics import cohen_kappa_score

    if file.endswith(".csv"):
        df = pd.read_csv(file, sep=";", encoding="utf-8-sig")
    else:
        df = pd.read_excel(file)

    kappa = cohen_kappa_score(df["annotator_1"], df["annotator_2"])
    agreement = (df["annotator_1"] == df["annotator_2"]).mean()

    print(f"Annotator Kappa: {kappa:.3f}")
    print(f"Annotator agreement: {agreement:.3f}")

    return {"kappa": float(kappa), "agreement": float(agreement)}
