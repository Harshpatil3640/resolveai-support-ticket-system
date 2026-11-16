import json
from pathlib import Path


import numpy as np
import pandas as pd

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import Pipeline, FeatureUnion
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    f1_score,
    classification_report,
    confusion_matrix,
)


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

HYBRID_METRICS_PATH = REPORTS_DIR / "hybrid_validation_metrics.json"
HYBRID_ERRORS_PATH = REPORTS_DIR / "hybrid_validation_errors.csv"
HYBRID_MATRIX_PATH = REPORTS_DIR / "hybrid_validation_confusion_matrix.csv"


# --------------------------------------------------
# 2. LOAD DATASETS
# --------------------------------------------------

def load_datasets():

    train_df = pd.read_csv(TRAIN_PATH)
    validation_df = pd.read_csv(VALIDATION_PATH)

    for name, df in [
        ("Training", train_df),
        ("Validation", validation_df),
    ]:

        required = {"instruction", "category"}
        missing = required - set(df.columns)

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
# 3. BUILD HYBRID TF-IDF MODEL
# --------------------------------------------------

def build_model():

    word_features = TfidfVectorizer(
        lowercase=True,
        strip_accents="unicode",
        ngram_range=(1, 2),
        min_df=2,
        max_df=0.95,
        sublinear_tf=True,
    )

    character_features = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(3, 5),
        min_df=2,
        max_df=0.95,
        sublinear_tf=True,
    )

    combined_features = FeatureUnion([
        ("word_features", word_features),
        ("character_features", character_features),
    ])

    model = Pipeline([
        ("features", combined_features),
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
# 4. TRAIN MODEL
# --------------------------------------------------

def train_model(model, train_df):

    X_train = train_df["instruction"].astype(str)
    y_train = train_df["category"]

    print("\n=== HYBRID MODEL TRAINING ===")
    print("Combining word and character TF-IDF features...")

    model.fit(X_train, y_train)

    print("Hybrid model training completed.")

    return model


# --------------------------------------------------
# 5. EVALUATE MODEL
# --------------------------------------------------

def evaluate_model(model, validation_df):

    X_validation = validation_df["instruction"].astype(str)
    y_validation = validation_df["category"]

    predictions = model.predict(X_validation)
    probabilities = model.predict_proba(X_validation)

    labels = sorted(y_validation.unique())
    classes = model.named_steps["classifier"].classes_

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
        output_dict=True,
        zero_division=0,
    )

    print("\n=== HYBRID VALIDATION RESULTS ===")
    print(f"Accuracy:    {accuracy:.6f}")
    print(f"Macro F1:    {macro_f1:.6f}")
    print(f"Weighted F1: {weighted_f1:.6f}")

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

    # Find incorrect predictions.
    actual = y_validation.to_numpy()

    incorrect_indices = np.where(
        predictions != actual
    )[0]

    error_records = []

    for index in incorrect_indices:

        probability_row = probabilities[index]

        top_index = np.argmax(probability_row)

        actual_index = np.where(
            classes == actual[index]
        )[0][0]

        error_records.append({
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

    errors_df = pd.DataFrame(error_records)

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
        "report": report,
        "confusion_matrix": confusion_df,
        "errors": errors_df,
        "categories": labels,
    }


# --------------------------------------------------
# 6. COMPARE ALL THREE MODELS
# --------------------------------------------------

def compare_models(hybrid_results):

    print("\n=== THREE-MODEL COMPARISON ===")

    comparisons = []

    metric_files = [
        ("Word-level TF-IDF", WORD_METRICS_PATH),
        ("Character-level TF-IDF", CHAR_METRICS_PATH),
    ]

    for model_name, path in metric_files:

        if not path.exists():
            print(f"WARNING: Missing metrics file: {path}")
            continue

        with open(path, "r", encoding="utf-8") as file:
            previous = json.load(file)

        metrics = previous["metrics"]

        comparisons.append({
            "Model": model_name,
            "Accuracy": metrics["accuracy"],
            "Macro F1": metrics["macro_f1"],
            "Weighted F1": metrics["weighted_f1"],
        })

    comparisons.append({
        "Model": "Hybrid TF-IDF",
        "Accuracy": hybrid_results["accuracy"],
        "Macro F1": hybrid_results["macro_f1"],
        "Weighted F1": hybrid_results["weighted_f1"],
    })

    comparison_df = pd.DataFrame(comparisons)

    print(
        comparison_df.to_string(
            index=False,
            formatters={
                "Accuracy": "{:.6f}".format,
                "Macro F1": "{:.6f}".format,
                "Weighted F1": "{:.6f}".format,
            },
        )
    )


# --------------------------------------------------
# 7. SAVE REPORTS
# --------------------------------------------------

def save_reports(results, train_df, validation_df):

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    metrics = {
        "model": "Word + Character TF-IDF + Logistic Regression",
        "training_records": int(len(train_df)),
        "validation_records": int(len(validation_df)),
        "categories": results["categories"],
        "metrics": {
            "accuracy": results["accuracy"],
            "macro_f1": results["macro_f1"],
            "weighted_f1": results["weighted_f1"],
        },
        "classification_report": results["report"],
        "vectorizer_configuration": {
            "word_lowercase": True,
            "word_strip_accents": "unicode",
            "word_ngram_range": [1, 2],
            "character_analyzer": "char_wb",
            "character_ngram_range": [3, 5],
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

    with open(HYBRID_METRICS_PATH, "w", encoding="utf-8") as file:
        json.dump(metrics, file, indent=4)

    results["confusion_matrix"].to_csv(HYBRID_MATRIX_PATH)

    results["errors"].to_csv(
        HYBRID_ERRORS_PATH,
        index=False,
    )

    print("\n=== REPORTS SAVED ===")
    print(HYBRID_METRICS_PATH)
    print(HYBRID_MATRIX_PATH)
    print(HYBRID_ERRORS_PATH)


# --------------------------------------------------
# 8. MAIN EXECUTION
# --------------------------------------------------

def main():

    print("\n==========================================")
    print(" RESOLVEAI HYBRID MODEL EXPERIMENT")
    print("==========================================")

    train_df, validation_df = load_datasets()

    model = build_model()

    model = train_model(model, train_df)

    results = evaluate_model(model, validation_df)

    compare_models(results)

    save_reports(results, train_df, validation_df)

    print("\n=== HYBRID EXPERIMENT COMPLETED ===")


if __name__ == "__main__":
    main()
