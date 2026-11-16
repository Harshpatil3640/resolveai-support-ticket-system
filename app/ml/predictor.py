from functools import lru_cache
from pathlib import Path

import joblib


# --------------------------------------------------
# 1. MODEL PATH
# --------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]

MODEL_PATH = (
    PROJECT_ROOT
    / "models"
    / "ticket_category_classifier.joblib"
)


# --------------------------------------------------
# 2. LOAD MODEL ONCE
# --------------------------------------------------

@lru_cache(maxsize=1)
def load_model():
    """
    Load the trained ML model once and reuse it.

    The model is loaded lazily, not when the FastAPI
    application starts.
    """

    if not MODEL_PATH.is_file():
        raise FileNotFoundError(
            f"Trained ML model not found: {MODEL_PATH}"
        )

    model = joblib.load(MODEL_PATH)

    return model


# --------------------------------------------------
# 3. PREDICT TICKET CATEGORY
# --------------------------------------------------

def predict_category(text: str) -> tuple[str, float]:
    """
    Predict the category of a support ticket.

    Returns:
        tuple:
            predicted_category: Predicted category label.
            confidence: Highest class probability.

    Note:
        Confidence is the model's maximum probability,
        not a calibrated guarantee of correctness.
    """

    if not isinstance(text, str) or not text.strip():
        raise ValueError(
            "Ticket text cannot be empty."
        )

    model = load_model()

    cleaned_text = text.strip()

    # Obtain probabilities for all possible categories.
    probabilities = model.predict_proba([cleaned_text])[0]

    # Retrieve the category labels from Logistic Regression.
    categories = model.named_steps["classifier"].classes_

    # Find the category with the highest probability.
    best_index = int(probabilities.argmax())

    predicted_category = str(categories[best_index])

    confidence = float(probabilities[best_index])

    return predicted_category, confidence
