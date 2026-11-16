import re
from pathlib import Path

import pandas as pd
from sklearn.model_selection import StratifiedGroupKFold


PROJECT_ROOT = Path(__file__).resolve().parents[1]

RAW_DATA_PATH = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv"
)

PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

TRAIN_PATH = PROCESSED_DIR / "train.csv"
VALIDATION_PATH = PROCESSED_DIR / "validation.csv"
TEST_PATH = PROCESSED_DIR / "test.csv"


def normalize_instruction(text: str) -> str:
    """Normalize customer instructions for duplicate grouping."""

    text = text.lower()

    text = re.sub(
        r"[^\w\s{}]",
        "",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def create_stratified_group_split(
    df: pd.DataFrame,
    random_state: int,
) -> tuple[list[int], list[int]]:
    """Create an approximately 80:20 group-aware split."""

    splitter = StratifiedGroupKFold(
        n_splits=5,
        shuffle=True,
        random_state=random_state,
    )

    train_indices, other_indices = next(
        splitter.split(
            X=df["instruction"],
            y=df["category"],
            groups=df["group_key"],
        )
    )

    return train_indices.tolist(), other_indices.tolist()


def prepare_dataset() -> None:
    """Clean the dataset and create train, validation and test sets."""

    if not RAW_DATA_PATH.exists():
        raise FileNotFoundError(
            f"Dataset not found: {RAW_DATA_PATH}"
        )

    print("\n=== RESOLVEAI DATASET PREPARATION ===\n")

    df = pd.read_csv(RAW_DATA_PATH)

    original_count = len(df)

    print(f"Original records: {original_count}")

    # Remove repeated exact instructions.
    df = (
        df.drop_duplicates(
            subset=["instruction"],
            keep="first",
        )
        .copy()
        .reset_index(drop=True)
    )

    print(f"Records after exact deduplication: {len(df)}")

    # Create normalised instruction groups.
    df["group_key"] = df["instruction"].apply(
        normalize_instruction
    )

    # Ensure each normalised group has one category.
    group_category_counts = (
        df.groupby("group_key")["category"]
        .nunique()
    )

    if (group_category_counts > 1).any():
        raise ValueError(
            "Conflicting categories found within normalised groups."
        )

    # First split: 80% development pool, 20% final test.
    development_indices, test_indices = (
        create_stratified_group_split(
            df,
            random_state=42,
        )
    )

    development_df = (
        df.iloc[development_indices]
        .copy()
        .reset_index(drop=True)
    )

    test_df = df.iloc[test_indices].copy()

    # Second split: split development pool into 80% train, 20% validation.
    train_indices, validation_indices = (
        create_stratified_group_split(
            development_df,
            random_state=43,
        )
    )

    train_df = development_df.iloc[train_indices].copy()
    validation_df = development_df.iloc[
        validation_indices
    ].copy()

    splits = {
        "training": train_df,
        "validation": validation_df,
        "testing": test_df,
    }

    # Verify that no normalised group overlaps.
    group_sets = {
        name: set(frame["group_key"])
        for name, frame in splits.items()
    }

    train_validation_overlap = (
        group_sets["training"]
        & group_sets["validation"]
    )

    train_test_overlap = (
        group_sets["training"]
        & group_sets["testing"]
    )

    validation_test_overlap = (
        group_sets["validation"]
        & group_sets["testing"]
    )

    if any([
        train_validation_overlap,
        train_test_overlap,
        validation_test_overlap,
    ]):
        raise RuntimeError(
            "Data leakage detected between dataset splits."
        )

    # Verify that every category is represented in every split.
    all_categories = set(df["category"].unique())

    for name, frame in splits.items():
        if set(frame["category"].unique()) != all_categories:
            raise RuntimeError(
                f"Missing category in {name} dataset."
            )

    # Remove the internal grouping column before saving.
    PROCESSED_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    paths = {
        "training": TRAIN_PATH,
        "validation": VALIDATION_PATH,
        "testing": TEST_PATH,
    }

    print("\n=== FINAL DATASET SPLIT ===")

    for name, frame in splits.items():
        output_df = frame.drop(
            columns=["group_key"]
        )

        output_df.to_csv(
            paths[name],
            index=False,
        )

        print(f"\n{name.upper()}")
        print(f"Records: {len(frame)}")
        print(
            f"Percentage: "
            f"{len(frame) / len(df) * 100:.2f}%"
        )
        print(
            f"Normalised groups: "
            f"{frame['group_key'].nunique()}"
        )

    print("\n=== CATEGORY DISTRIBUTION ===")

    for name, frame in splits.items():
        print(f"\n{name.upper()}")
        print(
            frame["category"]
            .value_counts()
            .to_string()
        )

    print("\n=== DATA LEAKAGE VERIFICATION ===")
    print(
        "Training-validation overlap:",
        len(train_validation_overlap),
    )
    print(
        "Training-testing overlap:",
        len(train_test_overlap),
    )
    print(
        "Validation-testing overlap:",
        len(validation_test_overlap),
    )

    print("\n=== DATASET FILES ===")

    for name, path in paths.items():
        print(f"{name}: {path}")

    print("\n=== PREPARATION COMPLETED ===")


if __name__ == "__main__":
    prepare_dataset()
