
from pathlib import Path

import pandas as pd


# --------------------------------------------------
# 1. PROJECT PATHS
# --------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

TRAIN_PATH = PROCESSED_DIR / "train.csv"
VALIDATION_PATH = PROCESSED_DIR / "validation.csv"


# --------------------------------------------------
# 2. LOAD ONLY TRAINING AND VALIDATION DATA
# --------------------------------------------------

def audit_intent_overlap():

    train_df = pd.read_csv(TRAIN_PATH)
    validation_df = pd.read_csv(VALIDATION_PATH)

    for name, df in [
        ("Training", train_df),
        ("Validation", validation_df),
    ]:
        required_columns = {"intent", "category"}

        missing = required_columns - set(df.columns)

        if missing:
            raise ValueError(
                f"{name} dataset is missing columns: {missing}"
            )

        if df["intent"].isna().any():
            raise ValueError(f"{name} dataset contains missing intents.")

    # Normalise intent names for comparison.
    train_intents = set(
        train_df["intent"].astype(str).str.strip().str.lower()
    )

    validation_intents = set(
        validation_df["intent"].astype(str).str.strip().str.lower()
    )

    shared_intents = train_intents & validation_intents

    training_only = train_intents - validation_intents

    validation_only = validation_intents - train_intents

    # --------------------------------------------------
    # 3. INTENT OVERLAP
    # --------------------------------------------------

    print("\n==========================================")
    print(" RESOLVEAI INTENT OVERLAP AUDIT")
    print("==========================================")

    print("\n=== UNIQUE INTENT COUNTS ===")
    print(f"Training unique intents: {len(train_intents)}")
    print(f"Validation unique intents: {len(validation_intents)}")
    print(f"Shared intents: {len(shared_intents)}")

    if validation_intents:
        coverage = (
            len(shared_intents) / len(validation_intents)
        ) * 100

        print(f"Validation intents also seen in training: {coverage:.2f}%")

    print("\n=== INTENT OVERLAP DETAILS ===")
    print(f"Training-only intents: {len(training_only)}")
    print(f"Validation-only intents: {len(validation_only)}")

    print("\nShared intents:")

    for intent in sorted(shared_intents):
        print(f"  - {intent}")

    if validation_only:
        print("\nValidation-only intents:")

        for intent in sorted(validation_only):
            print(f"  - {intent}")

    # --------------------------------------------------
    # 4. UNIQUE INTENTS PER CATEGORY
    # --------------------------------------------------

    print("\n=== TRAINING INTENTS PER CATEGORY ===")

    for category in sorted(train_df["category"].unique()):

        category_df = train_df[
            train_df["category"] == category
        ]

        intents = sorted(
            category_df["intent"]
            .astype(str)
            .str.strip()
            .str.lower()
            .unique()
        )

        print(f"\n{category}: {len(intents)} unique intents")

        for intent in intents:
            print(f"  - {intent}")

    # --------------------------------------------------
    # 5. VERIFY INTENT-CATEGORY MAPPING
    # --------------------------------------------------

    combined_df = pd.concat(
        [
            train_df[["intent", "category"]],
            validation_df[["intent", "category"]],
        ],
        ignore_index=True,
    )

    mapping_counts = combined_df.groupby("intent")["category"].nunique()

    conflicting_intents = mapping_counts[mapping_counts > 1]

    print("\n=== INTENT-CATEGORY CONSISTENCY ===")

    if conflicting_intents.empty:
        print("Every observed intent maps to exactly one category.")
    else:
        print("WARNING: Some intents map to multiple categories.")
        print(conflicting_intents)

    print("\n=== AUDIT COMPLETED ===")


# --------------------------------------------------
# 6. MAIN
# --------------------------------------------------

if __name__ == "__main__":
    audit_intent_overlap()
