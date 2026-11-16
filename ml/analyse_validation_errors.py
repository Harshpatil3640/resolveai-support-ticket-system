
from pathlib import Path

import numpy as np
import pandas as pd

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline


# --------------------------------------------------
# 1. PROJECT PATHS
# --------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
REPORTS_DIR = PROJECT_ROOT / "reports"

TRAIN_PATH = PROCESSED_DIR / "train.csv"
VALIDATION_PATH = PROCESSED_DIR / "validation.csv"

ERRORS_PATH = REPORTS_DIR / "validation_errors.csv"


# --------------------------------------------------
# 2. BUILD THE SAME BASELINE MODEL
# --------------------------------------------------

def build_model():

    return Pipeline([
        (
            "tfidf",
            TfidfVectorizer(
                lowercase=True,
                strip_accents="unicode",
                ngram_range=(1, 2),
                min_df=2,
                max_df=0.95,
                sublinear_tf=True,
            ),
        ),
        (
            "classifier",
            LogisticRegression(
                max_iter=1000,
                random_state=42,
            ),
        ),
    ])


# --------------------------------------------------
# 3. TRAIN AND IDENTIFY INCORRECT PREDICTIONS
# --------------------------------------------------

def analyse_errors():

    train_df = pd.read_csv(TRAIN_PATH)
    validation_df = pd.read_csv(VALIDATION_PATH)

    print("\n=== VALIDATION ERROR ANALYSIS ===")
    print(f"Training records: {len(train_df)}")
    print(f"Validation records: {len(validation_df)}")

    model = build_model()

    print("\nTraining baseline model...")
    model.fit(
        train_df["instruction"].astype(str),
        train_df["category"],
    )

    print("Training completed.")

    X_validation = validation_df["instruction"].astype(str)
    actual_categories = validation_df["category"].to_numpy()

    predictions = model.predict(X_validation)
    probabilities = model.predict_proba(X_validation)

    classes = model.named_steps["classifier"].classes_

    # Identify incorrectly classified records.
    incorrect_indices = np.where(
        predictions != actual_categories
    )[0]

    print("\n=== ERROR SUMMARY ===")
    print(f"Total validation records: {len(validation_df)}")
    print(f"Incorrect predictions: {len(incorrect_indices)}")
    print(
        f"Correct predictions: "
        f"{len(validation_df) - len(incorrect_indices)}"
    )

    if len(incorrect_indices) == 0:
        print("\nNo incorrect predictions found.")
        return

    # --------------------------------------------------
    # 4. EXTRACT ERROR DETAILS
    # --------------------------------------------------

    error_records = []

    for index in incorrect_indices:

        probability_row = probabilities[index]

        top_three_indices = np.argsort(probability_row)[::-1][:3]

        actual_category = actual_categories[index]

        actual_category_index = np.where(
            classes == actual_category
        )[0][0]

        error_records.append({
            "instruction": validation_df.iloc[index]["instruction"],
            "actual_category": actual_category,
            "predicted_category": predictions[index],
            "predicted_probability": round(
                float(probability_row[top_three_indices[0]]), 4
            ),
            "actual_category_probability": round(
                float(probability_row[actual_category_index]), 4
            ),
            "second_choice_category": classes[top_three_indices[1]],
            "second_choice_probability": round(
                float(probability_row[top_three_indices[1]]), 4
            ),
            "third_choice_category": classes[top_three_indices[2]],
            "third_choice_probability": round(
                float(probability_row[top_three_indices[2]]), 4
            ),
        })

    errors_df = pd.DataFrame(error_records)

    # Put the most confidently wrong predictions first.
    errors_df = errors_df.sort_values(
        by="predicted_probability",
        ascending=False,
    )

    # --------------------------------------------------
    # 5. SAVE AND DISPLAY RESULTS
    # --------------------------------------------------

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    errors_df.to_csv(ERRORS_PATH, index=False)

    pd.set_option("display.max_columns", None)
    pd.set_option("display.max_colwidth", 120)
    pd.set_option("display.width", 220)

    print("\n=== MISCLASSIFIED TICKETS ===")

    print(
        errors_df[
            [
                "actual_category",
                "predicted_category",
                "predicted_probability",
                "instruction",
            ]
        ].to_string(index=False)
    )

    print("\n=== ERROR ANALYSIS FILE SAVED ===")
    print(ERRORS_PATH)


# --------------------------------------------------
# 6. MAIN
# --------------------------------------------------

if __name__ == "__main__":
    analyse_errors()
