
from pathlib import Path

import numpy as np
import pandas as pd

from sklearn.feature_extraction.text import TfidfVectorizer


# --------------------------------------------------
# 1. PROJECT PATHS
# --------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
REPORTS_DIR = PROJECT_ROOT / "reports"

TRAIN_PATH = PROCESSED_DIR / "train.csv"
VALIDATION_PATH = PROCESSED_DIR / "validation.csv"
ERRORS_PATH = REPORTS_DIR / "char_validation_errors.csv"


# --------------------------------------------------
# 2. LOAD DATA
# --------------------------------------------------

def load_data():

    train_df = pd.read_csv(TRAIN_PATH)
    validation_df = pd.read_csv(VALIDATION_PATH)
    errors_df = pd.read_csv(ERRORS_PATH)

    required_columns = {"instruction", "category", "intent"}

    for name, df in [
        ("Training", train_df),
        ("Validation", validation_df),
    ]:
        missing = required_columns - set(df.columns)

        if missing:
            raise ValueError(
                f"{name} dataset is missing columns: {missing}"
            )

    if "instruction" not in errors_df.columns:
        raise ValueError("Error report does not contain instructions.")

    return train_df, validation_df, errors_df


# --------------------------------------------------
# 3. FIND SIMILAR TRAINING EXAMPLES
# --------------------------------------------------

def inspect_similar_examples(train_df, validation_df, errors_df):

    print("\n=== BUILDING CHARACTER TF-IDF INDEX ===")

    vectorizer = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(3, 5),
        min_df=1,
        sublinear_tf=True,
    )

    training_matrix = vectorizer.fit_transform(
        train_df["instruction"].astype(str)
    )

    print("Training text indexed successfully.")

    # --------------------------------------------------
    # 4. INSPECT EACH INCORRECT PREDICTION
    # --------------------------------------------------

    for number, error in enumerate(errors_df.itertuples(), start=1):

        instruction = error.instruction

        print("\n" + "=" * 75)
        print(f"ERROR CASE {number}")
        print("=" * 75)

        matching_rows = validation_df[
            validation_df["instruction"] == instruction
        ]

        if matching_rows.empty:
            print("Could not find instruction in validation dataset.")
            continue

        actual_row = matching_rows.iloc[0]

        print(f"\nCustomer instruction: {instruction}")
        print(f"Actual category:      {actual_row['category']}")
        print(f"Actual intent:        {actual_row['intent']}")
        print(f"Predicted category:   {error.predicted_category}")
        print(f"Predicted probability: {error.predicted_probability}")
        print(
            f"Actual category probability: "
            f"{error.actual_category_probability}"
        )

        # Convert the validation instruction into features.
        query_matrix = vectorizer.transform([instruction])

        # Calculate cosine similarity using normalized TF-IDF vectors.
        similarities = (query_matrix @ training_matrix.T).toarray()[0]

        top_indices = np.argsort(similarities)[::-1][:5]

        print("\n=== FIVE MOST SIMILAR TRAINING EXAMPLES ===")

        for rank, index in enumerate(top_indices, start=1):

            row = train_df.iloc[index]

            print(f"\nExample {rank}")
            print(f"Similarity: {similarities[index]:.4f}")
            print(f"Category:   {row['category']}")
            print(f"Intent:     {row['intent']}")
            print(f"Instruction: {row['instruction']}")

        nearest_df = train_df.iloc[top_indices]

        print("\n=== CATEGORY COUNTS AMONG THESE FIVE EXAMPLES ===")
        print(nearest_df["category"].value_counts().to_string())


# --------------------------------------------------
# 5. MAIN
# --------------------------------------------------

def main():

    print("\n==========================================")
    print(" RESOLVEAI REMAINING ERROR INVESTIGATION")
    print("==========================================")

    train_df, validation_df, errors_df = load_data()

    print(f"\nErrors to investigate: {len(errors_df)}")

    inspect_similar_examples(
        train_df,
        validation_df,
        errors_df,
    )

    print("\n=== INVESTIGATION COMPLETED ===")


if __name__ == "__main__":
    main()
