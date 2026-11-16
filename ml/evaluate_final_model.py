import json
from pathlib import Path

import joblib
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
MODELS_DIR = PROJECT_ROOT / "models"

TRAIN_PATH = PROCESSED_DIR / "train.csv"
VALIDATION_PATH = PROCESSED_DIR / "validation.csv"
TEST_PATH = PROCESSED_DIR / "test.csv"

METRICS_PATH = REPORTS_DIR / "final_test_metrics.json"
CONFUSION_MATRIX_PATH = REPORTS_DIR / "final_test_confusion_matrix.csv"
ERRORS_PATH = REPORTS_DIR / "final_test_errors.csv"

MODEL_PATH = MODELS_DIR / "ticket_category_classifier.joblib"


# --------------------------------------------------
# 2. LOAD AND VALIDATE DATASETS
# --------------------------------------------------

def load_datasets():

    paths = {
        "training": TRAIN_PATH,
        "validation": VALIDATION_PATH,
        "testing": TEST_PATH,
    }

    for name, path in paths.items():
        if not path.exists():
            raise FileNotFoundError(
                f"{name.capitalize()} dataset not found: {path}"
            )

    train_df = pd.read_csv(TRAIN_PATH)
    validation_df = pd.read_csv(VALIDATION_PATH)
    test_df = pd.read_csv(TEST_PATH)

    required_columns = {"instruction", "category"}

    for name, df in [
        ("Training", train_df),
        ("Validation", validation_df),
        ("Testing", test_df),
    ]:

        missing = required_columns - set(df.columns)

        if missing:
            raise ValueError(
                f"{name} dataset is missing columns: {missing}"
            )

        if df["instruction"].isna().any():
            raise ValueError(
                f"{name} dataset contains missing instructions."
            )

        if df["category"].isna().any():
            raise ValueError(
                f"{name} dataset contains missing categories."
            )

    combined_df = pd.concat(
        [train_df, validation_df],
        ignore_index=True,
    )

    train_categories = set(combined_df["category"])
    test_categories = set(test_df["category"])

    if train_categories != test_categories:
        raise ValueError(
            "Training/validation and test categories do not match."
        )

    print("\n=== FINAL DATASET LOADING ===")
    print(f"Training records: {len(train_df)}")
    print(f"Validation records: {len(validation_df)}")
    print(f"Combined training records: {len(combined_df)}")
    print(f"Untouched test records: {len(test_df)}")
    print(f"Number of categories: {len(train_categories)}")

    return combined_df, test_df


# --------------------------------------------------
# 3. BUILD SELECTED CHARACTER-LEVEL MODEL
# --------------------------------------------------

def build_model():

    return Pipeline([
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


# --------------------------------------------------
# 4. TRAIN ON TRAINING + VALIDATION DATA
# --------------------------------------------------

def train_final_model(model, training_data):

    X_train = training_data["instruction"].astype(str)
    y_train = training_data["category"]

    print("\n=== FINAL MODEL TRAINING ===")
    print("Training selected character-level model...")
    print("Using training + validation records.")

    model.fit(X_train, y_train)

    print("Final model training completed.")

    return model


# --------------------------------------------------
# 5. EVALUATE ON UNTOUCHED TEST DATA
# --------------------------------------------------

def evaluate_final_model(model, test_df):

    X_test = test_df["instruction"].astype(str)
    y_test = test_df["category"]

    predictions = model.predict(X_test)
    probabilities = model.predict_proba(X_test)

    labels = sorted(y_test.unique())
    classes = model.named_steps["classifier"].classes_

    accuracy = accuracy_score(y_test, predictions)

    macro_f1 = f1_score(
        y_test,
        predictions,
        average="macro",
    )

    weighted_f1 = f1_score(
        y_test,
        predictions,
        average="weighted",
    )

    report = classification_report(
        y_test,
        predictions,
        labels=labels,
        zero_division=0,
        output_dict=True,
    )

    print("\n==========================================")
    print(" FINAL TEST RESULTS")
    print("==========================================")

    print(f"Accuracy:    {accuracy:.6f}")
    print(f"Macro F1:    {macro_f1:.6f}")
    print(f"Weighted F1: {weighted_f1:.6f}")

    print("\n=== PER-CATEGORY CLASSIFICATION REPORT ===")

    print(
        classification_report(
            y_test,
            predictions,
            labels=labels,
            zero_division=0,
        )
    )

    # Confusion matrix.
    matrix = confusion_matrix(
        y_test,
        predictions,
        labels=labels,
    )

    confusion_df = pd.DataFrame(
        matrix,
        index=labels,
        columns=labels,
    )

    print("\n=== CONFUSION MATRIX ===")
    print(confusion_df.to_string())

    # Identify incorrect predictions.
    actual = y_test.to_numpy()

    incorrect_indices = np.where(
        predictions != actual
    )[0]

    error_records = []

    for index in incorrect_indices:

        probability_row = probabilities[index]

        top_indices = np.argsort(probability_row)[::-1][:3]

        actual_index = np.where(
            classes == actual[index]
        )[0][0]

        error_records.append({
            "instruction": test_df.iloc[index]["instruction"],
            "actual_category": actual[index],
            "predicted_category": predictions[index],
            "predicted_probability": round(
                float(probability_row[top_indices[0]]), 4
            ),
            "actual_category_probability": round(
                float(probability_row[actual_index]), 4
            ),
        })

    errors_df = pd.DataFrame(error_records)

    if not errors_df.empty:
        errors_df = errors_df.sort_values(
            by="predicted_probability",
            ascending=False,
        )

    print("\n=== TEST ERROR SUMMARY ===")
    print(f"Total test records: {len(test_df)}")
    print(f"Correct predictions: {len(test_df) - len(errors_df)}")
    print(f"Incorrect predictions: {len(errors_df)}")

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
# 6. SAVE TEST REPORTS
# --------------------------------------------------

def save_reports(results, training_data, test_df):

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    metrics = {
        "model": "Character TF-IDF + Logistic Regression",
        "training_records": int(len(training_data)),
        "test_records": int(len(test_df)),
        "categories": results["categories"],
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

    with open(METRICS_PATH, "w", encoding="utf-8") as file:
        json.dump(metrics, file, indent=4)

    results["confusion_matrix"].to_csv(
        CONFUSION_MATRIX_PATH
    )

    results["errors"].to_csv(
        ERRORS_PATH,
        index=False,
    )

    print("\n=== TEST REPORTS SAVED ===")
    print(METRICS_PATH)
    print(CONFUSION_MATRIX_PATH)
    print(ERRORS_PATH)


# --------------------------------------------------
# 7. SAVE TRAINED MODEL
# --------------------------------------------------

def save_model(model):

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    joblib.dump(model, MODEL_PATH)

    print("\n=== FINAL MODEL SAVED ===")
    print(MODEL_PATH)


# --------------------------------------------------
# 8. MAIN EXECUTION
# --------------------------------------------------

def main():

    print("\n==========================================")
    print(" RESOLVEAI FINAL MODEL EVALUATION")
    print("==========================================")

    training_data, test_df = load_datasets()

    model = build_model()

    model = train_final_model(model, training_data)

    results = evaluate_final_model(model, test_df)

    save_reports(results, training_data, test_df)

    save_model(model)

    print("\n=== FINAL EVALUATION COMPLETED ===")


if __name__ == "__main__":
    main()
