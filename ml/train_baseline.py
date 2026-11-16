import json
from pathlib import Path

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

METRICS_PATH = REPORTS_DIR / "validation_metrics.json"
CONFUSION_MATRIX_PATH = REPORTS_DIR / "validation_confusion_matrix.csv"


# --------------------------------------------------
# 2. LOAD DATASETS
# --------------------------------------------------

def load_datasets():

    if not TRAIN_PATH.exists():
        raise FileNotFoundError(f"Training dataset not found: {TRAIN_PATH}")

    if not VALIDATION_PATH.exists():
        raise FileNotFoundError(
            f"Validation dataset not found: {VALIDATION_PATH}"
        )

    train_df = pd.read_csv(TRAIN_PATH)
    validation_df = pd.read_csv(VALIDATION_PATH)

    required_columns = {"instruction", "category"}

    for name, df in [
        ("Training", train_df),
        ("Validation", validation_df),
    ]:
        missing_columns = required_columns - set(df.columns)

        if missing_columns:
            raise ValueError(
                f"{name} dataset is missing columns: {missing_columns}"
            )

        if df["instruction"].isna().any():
            raise ValueError(f"{name} dataset contains missing instructions.")

        if df["category"].isna().any():
            raise ValueError(f"{name} dataset contains missing categories.")

    if set(train_df["category"]) != set(validation_df["category"]):
        raise ValueError(
            "Training and validation datasets do not contain the same categories."
        )

    print("\n=== DATASET LOADING ===")
    print(f"Training records: {len(train_df)}")
    print(f"Validation records: {len(validation_df)}")
    print(f"Number of categories: {train_df['category'].nunique()}")

    return train_df, validation_df


# --------------------------------------------------
# 3. BUILD TF-IDF + LOGISTIC REGRESSION PIPELINE
# --------------------------------------------------

def build_model():

    model = Pipeline([
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

    return model


# --------------------------------------------------
# 4. TRAIN THE MODEL
# --------------------------------------------------

def train_model(model, train_df):

    X_train = train_df["instruction"].astype(str)
    y_train = train_df["category"]

    print("\n=== MODEL TRAINING ===")
    print("Training TF-IDF + Logistic Regression...")

    model.fit(X_train, y_train)

    print("Model training completed.")

    return model


# --------------------------------------------------
# 5. EVALUATE USING VALIDATION DATA
# --------------------------------------------------

def evaluate_model(model, validation_df):

    X_validation = validation_df["instruction"].astype(str)
    y_validation = validation_df["category"]

    predictions = model.predict(X_validation)

    class_labels = sorted(y_validation.unique())

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

    report_text = classification_report(
        y_validation,
        predictions,
        labels=class_labels,
        zero_division=0,
    )

    report_dict = classification_report(
        y_validation,
        predictions,
        labels=class_labels,
        output_dict=True,
        zero_division=0,
    )

    matrix = confusion_matrix(
        y_validation,
        predictions,
        labels=class_labels,
    )

    print("\n=== VALIDATION RESULTS ===")
    print(f"Accuracy:    {accuracy:.4f}")
    print(f"Macro F1:    {macro_f1:.4f}")
    print(f"Weighted F1: {weighted_f1:.4f}")

    print("\n=== PER-CATEGORY CLASSIFICATION REPORT ===")
    print(report_text)

    print("\n=== CONFUSION MATRIX ===")

    confusion_df = pd.DataFrame(
        matrix,
        index=class_labels,
        columns=class_labels,
    )

    print(confusion_df)

    return {
        "accuracy": float(accuracy),
        "macro_f1": float(macro_f1),
        "weighted_f1": float(weighted_f1),
        "classification_report": report_dict,
        "confusion_matrix": confusion_df,
        "class_labels": class_labels,
    }


# --------------------------------------------------
# 6. SAVE VALIDATION REPORTS
# --------------------------------------------------

def save_reports(results, train_df, validation_df):

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    metrics = {
        "model": "TF-IDF + Logistic Regression",
        "training_records": int(len(train_df)),
        "validation_records": int(len(validation_df)),
        "categories": results["class_labels"],
        "metrics": {
            "accuracy": results["accuracy"],
            "macro_f1": results["macro_f1"],
            "weighted_f1": results["weighted_f1"],
        },
        "classification_report": results["classification_report"],
        "vectorizer_configuration": {
            "lowercase": True,
            "strip_accents": "unicode",
            "ngram_range": [1, 2],
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

    results["confusion_matrix"].to_csv(CONFUSION_MATRIX_PATH)

    print("\n=== REPORTS SAVED ===")
    print(f"Metrics: {METRICS_PATH}")
    print(f"Confusion matrix: {CONFUSION_MATRIX_PATH}")


# --------------------------------------------------
# 7. MAIN EXECUTION
# --------------------------------------------------

def main():

    print("\n==========================================")
    print(" RESOLVEAI BASELINE MODEL TRAINING")
    print("==========================================")

    train_df, validation_df = load_datasets()

    model = build_model()

    model = train_model(model, train_df)

    results = evaluate_model(model, validation_df)

    save_reports(results, train_df, validation_df)

    print("\n=== BASELINE TRAINING COMPLETED ===")


if __name__ == "__main__":
    main()
