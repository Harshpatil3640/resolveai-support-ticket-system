# ResolveAI – AI-Powered Support Ticket Management System

ResolveAI is an AI-powered customer support ticket management system built using Python, FastAPI, SQLAlchemy, SQLite and Machine Learning.

The system allows support agents to create, manage and classify customer support tickets. A machine-learning classifier predicts the most likely ticket category when a ticket is created, while the human agent retains control over the final classification.

## Key Features

### Ticket Management
- Create support tickets.
- Retrieve individual tickets.
- List tickets with filtering and pagination.
- Search tickets by title and description.
- Update ticket information.
- Delete tickets.

### Authentication and Security
- JWT-based authentication.
- Password hashing.
- Protected API endpoints.
- Token revocation during logout.

### AI Ticket Classification
- Automatic category prediction during ticket creation.
- Character-level TF-IDF feature extraction.
- Logistic Regression classification.
- Prediction probability stored separately from the final category.
- Graceful handling of prediction failures.
- Human agents can correct the final ticket category without overwriting the original AI prediction.

### Machine Learning
- Dataset inspection and quality analysis.
- Duplicate instruction analysis.
- Group-aware training, validation and test splitting.
- Comparison of word-level, character-level and hybrid TF-IDF models.
- Classification reports and confusion matrices.
- Final model evaluation on an untouched test set.

## Technology Stack

| Area | Technologies |
|---|---|
| Programming Language | Python 3.12 |
| Backend Framework | FastAPI |
| Database | SQLite |
| ORM | SQLAlchemy |
| Data Processing | Pandas, NumPy |
| Machine Learning | Scikit-learn |
| Text Vectorisation | TF-IDF |
| Classification | Logistic Regression |
| Model Persistence | Joblib |
| Authentication | JWT |
| Testing | Pytest |
| API Documentation | Swagger UI / OpenAPI |
| Version Control | Git and GitHub |

## Project Architecture

The project follows a modular monolithic architecture.

```text
resolveai-support-ticket-system/
│
├── app/
│   ├── api/
│   │   └── routes/
│   │       ├── auth.py
│   │       └── tickets.py
│   │
│   ├── core/
│   ├── db/
│   ├── models/
│   ├── schemas/
│   ├── services/
│   │
│   └── ml/
│       ├── __init__.py
│       └── predictor.py
│
├── ml/
│   ├── inspect_dataset.py
│   ├── analyse_dataset_quality.py
│   ├── inspect_text_variation.py
│   ├── inspect_unknown_flags.py
│   ├── prepare_dataset.py
│   ├── train_baseline.py
│   ├── analyse_validation_errors.py
│   ├── audit_intent_overlap.py
│   ├── train_char_baseline.py
│   ├── inspect_remaining_errors.py
│   ├── train_hybrid_baseline.py
│   └── evaluate_final_model.py
│
├── data/
│   ├── raw/                 # Downloaded dataset (not committed)
│   └── processed/           # Prepared datasets (not committed)
│
├── models/                  # Locally generated ML model (not committed)
│
├── reports/
│   ├── validation_metrics.json
│   ├── validation_confusion_matrix.csv
│   ├── char_validation_metrics.json
│   ├── char_validation_confusion_matrix.csv
│   ├── hybrid_validation_metrics.json
│   ├── hybrid_validation_confusion_matrix.csv
│   ├── final_test_metrics.json
│   └── final_test_confusion_matrix.csv
│
├── tests/
│
├── docs/
│
├── .gitignore
├── requirements.txt
├── LICENSE
└── README.md
```

The trained model, raw dataset, processed datasets and individual error-analysis reports are generated or maintained locally and are not included in the repository.

## Machine Learning Pipeline

### Dataset

The project uses the Bitext Customer Support LLM Chatbot Training Dataset.

Dataset source:

https://huggingface.co/datasets/bitext/Bitext-customer-support-llm-chatbot-training-dataset

The original dataset contains 26,872 records and includes:

- `flags`
- `instruction`
- `category`
- `intent`
- `response`

For the initial category-classification model:

- Input feature: `instruction`
- Target variable: `category`

The `flags`, `intent` and `response` columns are not used as model input features.

The downloaded CSV contains 11 observed category labels:

1. ACCOUNT
2. CANCEL
3. CONTACT
4. DELIVERY
5. FEEDBACK
6. INVOICE
7. ORDER
8. PAYMENT
9. REFUND
10. SHIPPING
11. SUBSCRIPTION

### Data Preparation

The preparation workflow includes:

1. Loading and inspecting the source dataset.
2. Removing duplicate instructions.
3. Normalising instructions for grouping.
4. Checking instruction groups for conflicting categories.
5. Performing stratified, group-aware splitting.
6. Verifying that normalised instruction groups do not overlap between splits.

After exact instruction deduplication:

| Dataset | Records | Percentage |
|---|---:|---:|
| Training | 15,766 | 64% |
| Validation | 3,942 | 16% |
| Testing | 4,927 | 20% |
| Total | 24,635 | 100% |

Normalised instruction groups are kept separate across the three splits.

**Dataset metadata note:** The upstream dataset card describes 27 intents across 10 categories. However, inspection of the downloaded v11 CSV identified 11 distinct category labels. This project preserves the actual labels found in the CSV without silently renaming or merging categories.

### Model Experiments

Three machine-learning approaches were evaluated.

**Experiment 1: Word-level TF-IDF + Logistic Regression**

Uses word unigrams and bigrams to represent ticket instructions.

**Experiment 2: Character-level TF-IDF + Logistic Regression**

Uses character n-grams with the `char_wb` analyzer and an n-gram range of 3–5.

**Experiment 3: Hybrid TF-IDF + Logistic Regression**

Combines word-level and character-level features using Scikit-learn's `FeatureUnion`.

### Validation Results

| Model | Accuracy | Macro F1 |
|---|---:|---:|
| Word-level TF-IDF | 99.6449% | 0.996573 |
| Character-level TF-IDF | 99.9493% | 0.999664 |
| Hybrid TF-IDF | 99.9239% | 0.999432 |

Character-level TF-IDF achieved the strongest validation performance and was selected for final evaluation.

### Final Test Results

The selected character-level model was retrained using the combined training and validation datasets and evaluated on the untouched test dataset.

| Metric | Result |
|---|---:|
| Test records | 4,927 |
| Accuracy | 99.9188% |
| Macro F1 | 0.999200 |
| Weighted F1 | 0.999188 |
| Incorrect predictions | 4 |

These results represent performance on the held-out portion of the selected Bitext dataset. They should not be interpreted as verified performance on real-world customer-support traffic.

### Final Model Configuration

**Feature extraction:**
- Analyzer: `char_wb`
- N-gram range: `(3, 5)`
- Minimum document frequency: `2`
- Maximum document frequency: `0.95`
- Sublinear term frequency: enabled

**Classifier:**
- Algorithm: Logistic Regression
- Maximum iterations: `1000`
- Random state: `42`

The final model is saved locally as:

```text
models/ticket_category_classifier.joblib
```

## AI Integration with FastAPI

The trained classifier is integrated into the ticket creation endpoint.

### Ticket Creation Workflow

1. An authenticated agent submits a ticket.
2. The API sends the ticket description to the AI predictor.
3. The predictor returns a category and probability estimate.
4. The API stores the prediction separately from the final category.
5. The ticket is saved in the database.
6. The agent can confirm or correct the final category.

Prediction failure does not prevent ticket creation. In that situation, the prediction fields remain empty.

### Human-in-the-Loop Classification

ResolveAI maintains two separate fields:

| Field | Purpose |
|---|---|
| `predicted_category` | Original AI-generated category |
| `category` | Final category assigned or confirmed by the human agent |
| `prediction_confidence` | Highest predicted class probability |

An agent can change the final category without modifying the original AI prediction.

The probability returned by the classifier is a model estimate and has not been calibrated as a guarantee of correctness.

## API Endpoints

All ticket endpoints require authentication.

### Authentication

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/auth/login` | Authenticate an agent |
| GET | `/api/auth/me` | Retrieve the authenticated agent profile |
| POST | `/api/auth/logout` | Revoke the current access token |

### Tickets

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/tickets` | Create a ticket with AI prediction |
| GET | `/api/tickets` | List and filter tickets |
| GET | `/api/tickets/{ticket_id}` | Retrieve a ticket |
| PATCH | `/api/tickets/{ticket_id}` | Update ticket information |
| DELETE | `/api/tickets/{ticket_id}` | Delete a ticket |

Ticket listing supports pagination, status, priority, category and text-search filters.

### Example: Create a Ticket

Endpoint:

`POST /api/tickets`

Request body:

```json
{
  "title": "Card payment not working",
  "description": "My debit card payment was declined. Which payment methods do you accept?",
  "priority": "High"
}
```

Example response fields:

```json
{
  "id": 1,
  "title": "Card payment not working",
  "predicted_category": "PAYMENT",
  "category": null,
  "prediction_confidence": 0.984,
  "priority": "High",
  "status": "Open"
}
```

### Example: Agent Category Correction

Endpoint:

`PATCH /api/tickets/1`

Request body:

```json
{
  "category": "PAYMENT"
}
```

The agent-assigned category is stored independently of the original AI prediction.

## Installation and Setup

### 1. Clone the repository

```bash
git clone https://github.com/Harshpatil3640/resolveai-support-ticket-system.git

cd resolveai-support-ticket-system
```

### 2. Create a virtual environment

Windows:

```cmd
python -m venv .venv

.venv\Scripts\activate
```

### 3. Install dependencies

```cmd
python -m pip install --upgrade pip

python -m pip install -r requirements.txt
```

### 4. Download the dataset

The dataset is not included in this repository.

Download the CSV from the official Hugging Face dataset page:

https://huggingface.co/datasets/bitext/Bitext-customer-support-llm-chatbot-training-dataset

Create the raw-data directory:

```cmd
mkdir data\raw
```

Place the downloaded CSV inside `data/raw/` using this exact filename:

```text
Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv
```

### 5. Inspect and prepare the dataset

```cmd
python -m ml.inspect_dataset

python -m ml.prepare_dataset
```

### 6. Run model experiments

```cmd
python -m ml.train_baseline

python -m ml.train_char_baseline

python -m ml.train_hybrid_baseline
```

### 7. Train and evaluate the selected model

```cmd
python -m ml.evaluate_final_model
```

This command:
- Combines training and validation datasets.
- Trains the selected character-level classifier.
- Evaluates it on the untouched test dataset.
- Saves evaluation metrics and the confusion matrix.
- Generates the model artifact locally.

The generated model is required by the FastAPI predictor.

### 8. Run the FastAPI application

```cmd
python -m uvicorn app.main:app --reload
```

Open the API documentation:

http://127.0.0.1:8000/docs

Authenticate using an agent account configured in your application, then use the protected ticket endpoints.

## Running Tests

Execute the complete automated test suite:

```cmd
python -m pytest -v
```

The current project test suite contains 79 passing tests.

AI integration tests cover:

- Successful ticket creation with AI prediction.
- Ticket creation when AI prediction fails.
- Human category correction without changing the original AI prediction.

## Reports and Evaluation Artefacts

Aggregate evaluation results and confusion matrices are maintained in the `reports/` directory.

Individual validation and test error records are excluded because they contain original dataset instructions.

## Limitations

- The model is trained using a synthetic/template-based customer-support dataset.
- High performance on this dataset does not establish equivalent real-world performance.
- The validation audit found all 27 dataset intents represented in both training and validation. Therefore, the current evaluation does not measure generalisation to entirely unseen intents.
- The model may perform differently on unfamiliar wording, domain-specific requests, new categories and real customer conversations.
- Prediction probabilities are not calibrated confidence guarantees.
- Production deployment should include evaluation on representative, independently collected customer-support data.

## Dataset Attribution and Licensing

The original dataset is provided by Bitext Innovations.

Dataset: Bitext Customer Support LLM Chatbot Training Dataset

Source:
https://huggingface.co/datasets/bitext/Bitext-customer-support-llm-chatbot-training-dataset

Dataset licence: CDLA-Sharing-1.0

The raw and processed dataset files are not redistributed in this repository.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution and third-party licensing information.

The root [LICENSE](LICENSE) applies to this project's original code and does not replace the terms applicable to third-party materials.

## Project Status

Implemented:
- JWT authentication
- Ticket management REST API
- SQLite database integration
- Dataset inspection and preparation
- ML model experiments and evaluation
- AI ticket category prediction
- Human-in-the-loop category correction
- Automated API and integration tests

Further development:
- Frontend dashboard
- Ticket analytics and reporting
- Model monitoring and retraining workflow
- Evaluation using real-world support-ticket data

## Author

**Harshvardhan Patil**

GitHub: https://github.com/Harshpatil3640

Repository:
https://github.com/Harshpatil3640/resolveai-support-ticket-system
