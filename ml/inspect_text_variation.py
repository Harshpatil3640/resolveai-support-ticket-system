
from collections import Counter
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATASET_PATH = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv"
)


def inspect_text_variation() -> None:
    """Inspect text variations, flags and placeholder patterns."""

    df = pd.read_csv(DATASET_PATH)

    print("\n=== TEXT VARIATION ANALYSIS ===\n")

    # 1. Inspect duplicate instructions with different intents.
    repeated = (
        df.groupby("instruction")
        .agg(
            occurrences=("instruction", "size"),
            category_count=("category", "nunique"),
            intent_count=("intent", "nunique"),
            category=("category", "first"),
            intent=("intent", "first"),
        )
        .query("occurrences > 1")
    )

    intent_conflicts = repeated[
        repeated["intent_count"] > 1
    ]

    print("1. REPEATED INSTRUCTIONS WITH DIFFERENT INTENTS")
    print(f"Conflicting groups: {len(intent_conflicts)}")

    if not intent_conflicts.empty:
        print(intent_conflicts.head(10).to_string())

    # 2. Inspect dataset flags.
    print("\n2. FLAGS DISTRIBUTION")
    print(df["flags"].value_counts(dropna=False).to_string())

    # 3. Inspect placeholders.
    print("\n3. PLACEHOLDER TYPES")

    placeholder_counter = Counter(
        placeholder
        for placeholders in df["instruction"].str.findall(
            r"\{\{([^{}]+)\}\}"
        )
        for placeholder in placeholders
    )

    for placeholder, count in placeholder_counter.most_common(20):
        print(f"{placeholder}: {count}")

    print(
        "\nTotal distinct placeholder types:",
        len(placeholder_counter),
    )

    # 4. Inspect repeated text after normalisation.
    normalized = (
        df["instruction"]
        .str.lower()
        .str.replace(r"[^\w\s{}]", "", regex=True)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )

    normalized_groups = (
        pd.DataFrame({
            "normalized": normalized,
            "category": df["category"],
        })
        .groupby("normalized")
        .agg(
            occurrences=("normalized", "size"),
            category_count=("category", "nunique"),
            category=("category", "first"),
        )
        .query("occurrences > 1")
        .sort_values("occurrences", ascending=False)
    )

    print("\n4. REPEATED INSTRUCTIONS AFTER NORMALISATION")
    print(f"Repeated groups: {len(normalized_groups)}")

    print("\nTop 15 normalised instructions:")

    print(
        normalized_groups.head(15).to_string()
    )

    # 5. Check normalised text with conflicting categories.
    normalized_conflicts = normalized_groups[
        normalized_groups["category_count"] > 1
    ]

    print("\n5. NORMALISED TEXT WITH CONFLICTING CATEGORIES")
    print(f"Conflicting groups: {len(normalized_conflicts)}")

    if not normalized_conflicts.empty:
        print(normalized_conflicts.head(10).to_string())

    print("\n=== TEXT VARIATION ANALYSIS COMPLETED ===")


if __name__ == "__main__":
    inspect_text_variation()
