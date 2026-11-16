
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATASET_PATH = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv"
)


def analyse_dataset_quality() -> None:
    """Investigate repeated instructions and label consistency."""

    df = pd.read_csv(DATASET_PATH)

    print("\n=== DATASET QUALITY ANALYSIS ===\n")

    # 1. Find repeated customer instructions.
    repeated = (
        df.groupby("instruction")
        .agg(
            occurrences=("instruction", "size"),
            category_count=("category", "nunique"),
            intent_count=("intent", "nunique"),
            categories=(
                "category",
                lambda values: ", ".join(sorted(values.unique())),
            ),
            intents=(
                "intent",
                lambda values: ", ".join(sorted(values.unique())),
            ),
        )
        .query("occurrences > 1")
        .sort_values(
            ["occurrences", "category_count"],
            ascending=[False, False],
        )
    )

    print("1. REPEATED INSTRUCTION ANALYSIS")
    print(f"Repeated instruction groups: {len(repeated)}")
    print(
        "Repeated occurrences beyond first appearance:",
        int((repeated["occurrences"] - 1).sum()),
    )

    # 2. Find instructions with conflicting category labels.
    conflicts = repeated[
        repeated["category_count"] > 1
    ]

    print("\n2. CATEGORY LABEL CONFLICTS")
    print(f"Conflicting instruction groups: {len(conflicts)}")

    if not conflicts.empty:
        print(conflicts.head(20).to_string())

    # 3. Check whether each intent maps consistently to a category.
    intent_mapping = (
        df.groupby("intent")["category"]
        .nunique()
        .sort_values(ascending=False)
    )

    inconsistent_intents = intent_mapping[
        intent_mapping > 1
    ]

    print("\n3. INTENT-TO-CATEGORY CONSISTENCY")
    print(f"Unique intents: {len(intent_mapping)}")
    print(
        "Intents mapped to multiple categories:",
        len(inconsistent_intents),
    )

    if not inconsistent_intents.empty:
        print(inconsistent_intents.to_string())

    # 4. Examine the category-to-intent relationship.
    print("\n4. CATEGORY AND INTENT MAPPING")

    mapping = (
        df.groupby(["category", "intent"])
        .size()
        .unstack(fill_value=0)
    )

    print(mapping.to_string())

    # 5. Examine placeholder values in the instruction text.
    placeholders = df["instruction"].str.contains(
        r"\{\{[^}]+\}\}",
        regex=True,
        na=False,
    )

    print("\n5. PLACEHOLDER ANALYSIS")
    print(f"Records containing placeholders: {placeholders.sum()}")
    print(
        f"Percentage: {placeholders.mean() * 100:.2f}%"
    )

    # 6. Show the most frequently repeated instructions.
    print("\n6. TOP 15 REPEATED INSTRUCTIONS")
    print(
        repeated.head(15)[
            [
                "occurrences",
                "categories",
                "intents",
            ]
        ].to_string()
    )

    print("\n=== QUALITY ANALYSIS COMPLETED ===")


if __name__ == "__main__":
    analyse_dataset_quality()
