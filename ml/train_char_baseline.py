import json
from pathlib import Path

import numpy as np
import pandas as pd

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
)
from sklearn.pipeline import Pipeline


# --------------------------------------------------
# 1. PROJECT PATHS
# --------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
REPORTS_DIR = PROJECT_ROOT / "reports"

TRAIN_PATH = PROCESSED_DIR / "train.csv"
VALIDATION_PATH = PROCESSED_DIR / "validation.csv"

WORD_METRICS_PATH = REPORTS_DIR / "validation_metrics.json"

CHAR_METRICS_PATH = REPORTS_DIR / "char_validation_metrics.json"
CHAR_ERRORS_PATH = REPORTS_DIR / "char_validation_errors.csv"
CHAR_MATRIX_PATH = REPORTS_DIR / "char_validation_confusion_matrix.csv"


# --------------------------------------------------
# 2. LOAD DATA
# --------------------------------------------------

def load_datasets():

    train_df = pd.read_csv(TRAIN_PATH)
    validation_df = pd.read_csv(VALIDATION_PATH)

    required_columns = {"instruction", "category"}

    for name, df in [
        ("Training", train_df),
        ("Validation", validation_df),
    ]:
        missing = required_columns - set(df.columns)

        if missing:
            raise ValueError(
                f"{name} dataset is missing columns: {missing}"
            )

        if df["instruction"].isna().any():
            raise ValueError(f"{name} has missing instructions.")

        if df["category"].isna().any():
            raise ValueError(f"{name} has missing categories.")

    if set(train_df["category"]) != set(validation_df["category"]):
        raise ValueError("Training and validation categories do not match.")

    print("\n=== DATASET LOADING ===")
    print(f"Training records: {len(train_df)}")
    print(f"Validation records: {len(validation_df)}")

    return train_df, validation_df


# --------------------------------------------------
# 3. BUILD CHARACTER-LEVEL MODEL
# --------------------------------------------------

def build_model():

    model = Pipeline([
        (
            "tfidf",
            TfidfVectorizer(
                analyzer="char_wb",
                ngram_range=(3, 5),
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

    return model


# --------------------------------------------------
# 4. TRAIN
# --------------------------------------------------

def train_model(model, train_df):

    X_train = train_df["instruction"].astype(str)
    y_train = train_df["category"]

    print("\n=== CHARACTER-LEVEL MODEL TRAINING ===")
    print("Training character TF-IDF + Logistic Regression...")

    model.fit(X_train, y_train)

    print("Training completed.")

    return model


# --------------------------------------------------
# 5. EVALUATE
# --------------------------------------------------

def evaluate_model(model, validation_df):

    X_validation = validation_df["instruction"].astype(str)
    y_validation = validation_df["category"]

    predictions = model.predict(X_validation)
    probabilities = model.predict_proba(X_validation)

    classes = model.named_steps["classifier"].classes_
    labels = sorted(y_validation.unique())

    accuracy = accuracy_score(y_validation, predictions)

    macro_f1 = f1_score(
        y_validation,
        predictions,
        average="macro",
    )

    weighted_f1 = f1_score(
        y_validation,
        predictions,
        average="weighted",
    )

    report = classification_report(
        y_validation,
        predictions,
        labels=labels,
        zero_division=0,
        output_dict=True,
    )

    print("\n=== CHARACTER MODEL VALIDATION RESULTS ===")
    print(f"Accuracy:    {accuracy:.4f}")
    print(f"Macro F1:    {macro_f1:.4f}")
    print(f"Weighted F1: {weighted_f1:.4f}")

    print("\n=== PER-CATEGORY REPORT ===")

    print(
        classification_report(
            y_validation,
            predictions,
            labels=labels,
            zero_division=0,
        )
    )

    matrix = confusion_matrix(
        y_validation,
        predictions,
        labels=labels,
    )

    confusion_df = pd.DataFrame(
        matrix,
        index=labels,
        columns=labels,
    )

    # Extract incorrect predictions.
    actual = y_validation.to_numpy()
    incorrect_indices = np.where(predictions != actual)[0]

    errors = []

    for index in incorrect_indices:

        probability_row = probabilities[index]
        top_index = np.argmax(probability_row)

        actual_index = np.where(classes == actual[index])[0][0]

        errors.append({
            "instruction": validation_df.iloc[index]["instruction"],
            "actual_category": actual[index],
            "predicted_category": predictions[index],
            "predicted_probability": round(
                float(probability_row[top_index]), 4
            ),
            "actual_category_probability": round(
                float(probability_row[actual_index]), 4
            ),
        })

    errors_df = pd.DataFrame(errors)

    if not errors_df.empty:
        errors_df = errors_df.sort_values(
            by="predicted_probability",
            ascending=False,
        )

    print("\n=== ERROR SUMMARY ===")
    print(f"Incorrect predictions: {len(incorrect_indices)}")
    print(f"Correct predictions: {len(validation_df) - len(incorrect_indices)}")

    return {
        "accuracy": float(accuracy),
        "macro_f1": float(macro_f1),
        "weighted_f1": float(weighted_f1),
        "classification_report": report,
        "confusion_matrix": confusion_df,
        "errors": errors_df,
        "categories": labels,
    }


# --------------------------------------------------
# 6. COMPARE WITH WORD-LEVEL BASELINE
# --------------------------------------------------

def compare_models(char_results):

    print("\n=== MODEL COMPARISON ===")

    if not WORD_METRICS_PATH.exists():
        print("Previous word-level metrics file was not found.")
        return

    with open(WORD_METRICS_PATH, "r", encoding="utf-8") as file:
        word_results = json.load(file)

    word_metrics = word_results["metrics"]

    comparison = pd.DataFrame(
        [
            {
                "Model": "Word-level TF-IDF",
                "Accuracy": word_metrics["accuracy"],
                "Macro F1": word_metrics["macro_f1"],
                "Weighted F1": word_metrics["weighted_f1"],
            },
            {
                "Model": "Character-level TF-IDF",
                "Accuracy": char_results["accuracy"],
                "Macro F1": char_results["macro_f1"],
                "Weighted F1": char_results["weighted_f1"],
            },
        ]
    )

    print(comparison.to_string(index=False))


# --------------------------------------------------
# 7. SAVE REPORTS
# --------------------------------------------------

def save_reports(results, train_df, validation_df):

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    metrics = {
        "model": "Character TF-IDF + Logistic Regression",
        "training_records": int(len(train_df)),
        "validation_records": int(len(validation_df)),
        "metrics": {
            "accuracy": results["accuracy"],
            "macro_f1": results["macro_f1"],
            "weighted_f1": results["weighted_f1"],
        },
        "classification_report": results["classification_report"],
        "vectorizer_configuration": {
            "analyzer": "char_wb",
            "ngram_range": [3, 5],
            "min_df": 2,
            "max_df": 0.95,
            "sublinear_tf": True,
        },
        "classifier_configuration": {
            "algorithm": "LogisticRegression",
            "max_iter": 1000,
            "random_state": 42,
        },
    }

    with open(CHAR_METRICS_PATH, "w", encoding="utf-8") as file:
        json.dump(metrics, file, indent=4)

    results["confusion_matrix"].to_csv(CHAR_MATRIX_PATH)
    results["errors"].to_csv(CHAR_ERRORS_PATH, index=False)

    print("\n=== REPORTS SAVED ===")
    print(CHAR_METRICS_PATH)
    print(CHAR_MATRIX_PATH)
    print(CHAR_ERRORS_PATH)


# --------------------------------------------------
# 8. MAIN
# --------------------------------------------------

def main():

    print("\n==========================================")
    print(" RESOLVEAI CHARACTER-LEVEL MODEL")
    print("==========================================")

    train_df, validation_df = load_datasets()

    model = build_model()

    model = train_model(model, train_df)

    results = evaluate_model(model, validation_df)

    compare_models(results)

    save_reports(results, train_df, validation_df)

    print("\n=== CHARACTER MODEL EXPERIMENT COMPLETED ===")


if __name__ == "__main__":
    main()
