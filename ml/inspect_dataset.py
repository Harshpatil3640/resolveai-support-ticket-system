
from pathlib import Path

import pandas as pd


# Locate the project root and training dataset.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATASET_PATH = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv"
)


def inspect_dataset() -> None:
    """Load and inspect the customer support dataset."""

    if not DATASET_PATH.exists():
        raise FileNotFoundError(
            f"Dataset not found: {DATASET_PATH}"
        )

    print("\n=== RESOLVEAI DATASET EXPLORATION ===\n")

    # Load the CSV dataset.
    df = pd.read_csv(DATASET_PATH)

    # 1. Dataset dimensions.
    print("1. DATASET DIMENSIONS")
    print(f"Total records: {df.shape[0]}")
    print(f"Total columns: {df.shape[1]}")

    # 2. Column names.
    print("\n2. COLUMN NAMES")
    for column in df.columns:
        print(f"- {column}")

    # 3. Dataset information.
    print("\n3. DATASET INFORMATION")
    df.info()

    # 4. Missing values.
    print("\n4. MISSING VALUES")
    print(df.isnull().sum())

    # 5. Duplicate records.
    print("\n5. DUPLICATE RECORDS")
    print(f"Duplicate rows: {df.duplicated().sum()}")

    if "instruction" in df.columns:
        duplicate_requests = df.duplicated(
            subset=["instruction"]
        ).sum()

        print(
            f"Duplicate customer requests: "
            f"{duplicate_requests}"
        )

    # 6. Category distribution.
    if "category" in df.columns:
        print("\n6. CATEGORY DISTRIBUTION")

        category_counts = df["category"].value_counts(
            dropna=False
        )

        category_percentages = (
            df["category"]
            .value_counts(normalize=True, dropna=False)
            .mul(100)
            .round(2)
        )

        category_summary = pd.DataFrame({
            "Count": category_counts,
            "Percentage": category_percentages,
        })

        print(category_summary)

    # 7. Intent distribution.
    if "intent" in df.columns:
        print("\n7. INTENT DISTRIBUTION")

        print(
            df["intent"]
            .value_counts(dropna=False)
            .to_string()
        )

        print(
            "\nTotal unique intents:",
            df["intent"].nunique()
        )

    # 8. Sample customer requests.
    if {"instruction", "category", "intent"}.issubset(
        df.columns
    ):
        print("\n8. SAMPLE CUSTOMER REQUESTS")

        print(
            df[
                ["instruction", "category", "intent"]
            ].head(10).to_string(index=False)
        )

    print("\n=== DATASET INSPECTION COMPLETED ===")


if __name__ == "__main__":
    inspect_dataset()
