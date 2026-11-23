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
- Role-based access control with three roles: `admin`, `agent` and `customer`.

### User Administration
- Admin-only API to list, search, filter, retrieve and create user accounts.
- Role changes and account activation/deactivation, effective immediately.
- Safeguards against self-demotion, self-deactivation and removing the last active administrator.
- Administrative actions recorded in a dedicated audit log (`resolveai.audit`).

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
│   │       ├── admin.py
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

### Administration (admin only)

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/admin/users` | List, search and filter users |
| GET | `/api/admin/users/{user_id}` | Retrieve a user |
| POST | `/api/admin/users` | Create a user |
| PATCH | `/api/admin/users/{user_id}/role` | Change a user's role |
| PATCH | `/api/admin/users/{user_id}/status` | Activate or deactivate a user |

Request and response details, security rules and error responses are in
[Admin User Management](#admin-user-management).

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

Then set up the database. For a brand-new database run `alembic upgrade head`.
If you already have a `resolveai.db` from before Alembic was introduced, follow
[Database Setup and Migrations](#database-setup-and-migrations) instead.

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

## Database Setup and Migrations

The schema is managed with Alembic migrations in `alembic/versions/`.

Brand-new database:

```cmd
alembic upgrade head
```

Existing database created before Alembic (back it up first):

```cmd
python -m app.db.check_baseline
python -m app.db.check_baseline --stamp
alembic upgrade head
```

The check is read-only and refuses to stamp if the schema differs from the
baseline. Inspect the state with `alembic current` and create new migrations
with `alembic revision --autogenerate -m "message"`.

Migrations are always an explicit step; nothing runs them automatically.
The provisioning scripts (`python -m app.scripts.create_admin` and
`python -m app.scripts.create_agent`) require the database to already be at
the Alembic head revision, with exactly the schema the migrations produce, and
stop, without changing anything, if it is not.
SQL generated offline with `--sql` skips the baseline's safety check, so never
apply it to an existing database.

Full procedure, exit codes and troubleshooting:
[docs/database-migrations.md](docs/database-migrations.md).

## Creating the First Administrator

ResolveAI has no default administrator account and no public sign-up for
administrators. The first administrator is created once, from the server's
command line, after the database has been migrated:

```cmd
python -m app.scripts.create_admin
```

The script:

1. Checks, read-only, that the database is at the Alembic head revision with
   the expected schema. It never runs migrations.
2. Refuses to continue if **any** administrator account already exists,
   active or inactive, before asking for any input.
3. Asks for the administrator's name and email, then for a password twice
   (hidden input, 12 to 128 characters).
4. Creates the account with the `admin` role and prints its ID, name, email
   and role. Passwords and password hashes are never printed, including in
   database error messages: the application engine hides SQL parameters,
   and the scripts print only a short sanitised diagnostic.

It exits with code `0` on success and `1` otherwise.

**Concurrency (SQLite only).** If two bootstraps run at the same time, at
most one administrator is created: the script re-counts administrators
inside its write transaction and commits only if there is exactly one. This
relies on SQLite allowing a single writer at a time. It is **not**
guaranteed on PostgreSQL or other databases; moving to one will require an
explicit lock or constraint for the bootstrap.

**Why only from the command line?** An HTTP endpoint that creates the first
administrator would be reachable by anyone who finds the server before the
real owner does. Requiring local access to the server and its database means
only an operator who already controls the deployment can create the first
administrator. Once one exists, the script permanently refuses to run.

**Further administrators**, agents and customers are created by an existing,
signed-in administrator through `POST /api/admin/users` (see
[Admin User Management](#admin-user-management)). Support agents can still be
created locally with `python -m app.scripts.create_agent`.

Choose a strong, unique password when prompted. Never commit credentials,
and never share the administrator's password.

### Resetting an agent's password

```cmd
python -m app.scripts.reset_agent_password
```

The script checks the database is at the Alembic head revision before asking
for anything, finds the agent by email (ignoring case) and only resets
accounts with the agent role. The new password is saved and every existing
session for the account is signed out (its token version is incremented) in
a single transaction, so either both changes happen or neither does.

### Password rules

Passwords must be 12 to 128 characters long. The limits are enforced by the
shared user service (`app/services/user_service.py`), whatever the caller.
HTTP request schemas that accept a password (such as the admin create-user
request) must also declare `max_length=128`, so oversized values are rejected
before hashing.

## Admin User Management

The admin user-management API (`app/api/routes/admin.py`, mounted at
`/api/admin`) lets an active administrator manage accounts over HTTP. The
business rules live in the shared user service
(`app/services/user_service.py`); the design of the protected transactions is
described in [docs/architecture.md](docs/architecture.md#14-admin-user-management).

### Roles

| Role | Stored value | User-management API |
|---|---|---|
| Administrator | `admin` | Allowed while the account is active |
| Support agent | `agent` | Forbidden (`403`) |
| Customer | `customer` | Forbidden (`403`) |

Role values are lowercase and case-sensitive (`"Admin"` is rejected). A stored
role the application does not recognise is denied by default.

### Authentication and authorisation

Every endpoint under `/api/admin` requires an access token from
`POST /api/auth/login` (form fields `username` = email, and `password`),
sent as `Authorization: Bearer <access-token>`. In Swagger UI
(`http://127.0.0.1:8000/docs`) use the **Authorize** button.

| Situation | Status | Response body |
|---|---|---|
| No `Authorization` header | `401` | `{"detail": "Not authenticated"}` |
| Invalid, expired or revoked token, inactive account, or a token issued before the account's sessions were invalidated | `401` | `{"detail": "Could not validate credentials."}` |
| Valid token, but the account is not an administrator | `403` | `{"detail": "You do not have permission to perform this action."}` |

`401` responses include the header `WWW-Authenticate: Bearer`. Authorisation is
checked before the request is validated, so a non-administrator receives `403`
(never `422`) and cannot use validation errors to probe the API. The
administrator requirement is attached to the whole router, so any endpoint
added to it later is protected too.

The user and role are loaded from the database on every request; the role is
never read from the token.

### Response model

Every endpoint that returns a user uses the same model:

```json
{
  "id": 3,
  "name": "Jordan Example",
  "email": "jordan.example@example.com",
  "role": "agent",
  "is_active": true,
  "created_at": "2026-11-23T05:12:16.456996"
}
```

`password`, `password_hash` and `token_version` are never returned.

### `GET /api/admin/users` — list, search and filter users

Query parameters (all optional):

| Parameter | Type | Default | Rules |
|---|---|---|---|
| `skip` | integer | `0` | Number of matching users to skip; must be `0` or greater |
| `limit` | integer | `20` | Page size, `1` to `100` |
| `search` | string | none | `1` to `200` characters; case-insensitive substring match on **name or email** |
| `role` | `admin` \| `agent` \| `customer` | none | Exact role filter |
| `is_active` | boolean | none | `true` for active accounts, `false` for deactivated accounts |

- Filters combine with AND, for example `role=agent&is_active=false`.
- `search` is trimmed. A value of only spaces is ignored, but an empty value
  (`search=`) is rejected with `422`. `%` and `_` match literally; they are
  not wildcards.
- Results are ordered newest first by `created_at`, then by `id` (both
  descending), so pages are stable even when accounts share a creation time.
- `total` is the number of users matching the filters before `skip` and
  `limit` are applied.

Example request:

```http
GET /api/admin/users?search=example.com&role=agent&is_active=true&skip=0&limit=20
Authorization: Bearer <admin-access-token>
```

Response `200 OK`:

```json
{
  "items": [
    {
      "id": 3,
      "name": "Jordan Example",
      "email": "jordan.example@example.com",
      "role": "agent",
      "is_active": true,
      "created_at": "2026-11-23T05:12:16.456996"
    }
  ],
  "total": 1,
  "skip": 0,
  "limit": 20
}
```

Errors: `401`, `403`, and `422` for an out-of-range `skip` or `limit`, an
empty or over-long `search`, an unknown `role` or a non-boolean `is_active`.

### `GET /api/admin/users/{user_id}` — retrieve a user

`user_id` must be a positive integer.

```http
GET /api/admin/users/3
Authorization: Bearer <admin-access-token>
```

Response `200 OK`: the user (see [Response model](#response-model)).

Errors: `401`, `403`, `404 {"detail": "User not found."}`, and `422` if
`user_id` is not a positive integer.

### `POST /api/admin/users` — create a user

Request body (all four fields are required; there is no default role):

| Field | Rules |
|---|---|
| `name` | Trimmed; 1 to 100 characters |
| `email` | Trimmed and lowercased; at most 255 characters; exactly one `@` with text on both sides and no spaces; unique, compared case-insensitively |
| `password` | 12 to 128 characters; kept exactly as typed (spaces are **not** trimmed) |
| `role` | `admin`, `agent` or `customer` |

Any other field (for example `is_active`, `password_hash` or
`token_version`) is rejected with `422`. New accounts are always active.

```http
POST /api/admin/users
Authorization: Bearer <admin-access-token>
Content-Type: application/json

{
  "name": "Jordan Example",
  "email": "Jordan.Example@example.com",
  "password": "<a 12-128 character password>",
  "role": "agent"
}
```

Response `201 Created`: the new user, with the email stored as
`jordan.example@example.com`.

Errors:

| Status | When | Body |
|---|---|---|
| `401` / `403` | See [Authentication and authorisation](#authentication-and-authorisation) | |
| `403` | The acting administrator was demoted or deactivated after the request was authorised (re-checked inside the creating transaction; nothing is created) | `{"detail": "You do not have permission to perform this action."}` |
| `409` | An account with this email already exists (any letter case) | `{"detail": "An account with this email already exists."}` |
| `422` | Field missing, wrong type, out of range, or not permitted | FastAPI validation list (see [Validation errors](#validation-errors)) |
| `422` | The email address is not valid | `{"detail": "A valid email address is required."}` |

### `PATCH /api/admin/users/{user_id}/role` — change a user's role

```http
PATCH /api/admin/users/3/role
Authorization: Bearer <admin-access-token>
Content-Type: application/json

{"role": "customer"}
```

Response `200 OK`: the updated user.

- The change applies **immediately**, including to tokens the user already
  holds: a promoted user can use the admin API with their existing token,
  and a demoted administrator receives `403` on their next request. A role
  change does not sign the user out.
- Setting the role the user already has is a no-op: `200`, nothing is
  written and nothing is logged.

Errors:

| Status | When | Body |
|---|---|---|
| `401` / `403` | See [Authentication and authorisation](#authentication-and-authorisation); `403` also if the acting administrator lost access before the change | `{"detail": "You do not have permission to perform this action."}` |
| `404` | No such user | `{"detail": "User not found."}` |
| `409` | `user_id` is the acting administrator | `{"detail": "Administrators cannot change their own role."}` |
| `409` | The change would demote the last active administrator | `{"detail": "At least one active administrator must remain."}` |
| `422` | Missing or unknown `role`, extra fields, or invalid `user_id` | FastAPI validation list |

### `PATCH /api/admin/users/{user_id}/status` — activate or deactivate a user

`is_active` must be a JSON boolean; `"false"`, `0` and similar values are
rejected with `422`.

```http
PATCH /api/admin/users/3/status
Authorization: Bearer <admin-access-token>
Content-Type: application/json

{"is_active": false}
```

Response `200 OK`: the updated user.

- A real status change increments the user's `token_version` in the same
  transaction, so **every token issued before the change stops working**
  (`401`). This also applies to **reactivation**: tokens issued before a
  deactivation never become valid again, and the user must sign in again.
- Repeating the current status is idempotent: `200`, nothing is written,
  `token_version` is not incremented and nothing is logged.
- A deactivated account cannot sign in or use existing tokens.

Errors:

| Status | When | Body |
|---|---|---|
| `401` / `403` | See [Authentication and authorisation](#authentication-and-authorisation); `403` also if the acting administrator lost access before the change | `{"detail": "You do not have permission to perform this action."}` |
| `404` | No such user | `{"detail": "User not found."}` |
| `409` | `user_id` is the acting administrator | `{"detail": "Administrators cannot change their own status."}` |
| `409` | The change would deactivate the last active administrator | `{"detail": "At least one active administrator must remain."}` |
| `422` | Missing or non-boolean `is_active`, extra fields, or invalid `user_id` | FastAPI validation list |

### Administrator safeguards

- **No self-demotion or self-deactivation.** An administrator cannot change
  their own role or status (`409`); another administrator must do it.
- **At least one active administrator always remains.** Demoting or
  deactivating the last active administrator is refused (`409`). Only
  accounts that are active and have the role `admin` count; an inactive
  administrator can be demoted freely.
- **The acting administrator is re-validated.** Each create, role change and
  status change re-checks, inside its protected transaction, that the actor is
  still an active administrator. If a concurrent request removed their access
  after the request was authorised, the change is refused with `403` and
  nothing is written.
- These checks and the write happen in one serialised transaction, so two
  administrators removing each other at the same moment cannot leave the
  system without an active administrator. See
  [docs/architecture.md](docs/architecture.md#14-admin-user-management).

### Why accounts are deactivated, not deleted

The API has no endpoint for deleting users. Deactivation:

- immediately blocks sign-in and revokes every existing token;
- is reversible through the same endpoint;
- keeps the records that refer to the account intact, such as the tickets it
  created (`tickets.created_by_user_id`), and keeps the user IDs in the audit
  log meaningful.

### Validation errors

Request validation failures return `422` in FastAPI's list format, but **the
submitted values are never echoed back** (no `input` field), so a rejected
password or smuggled field never appears in a response:

```json
{
  "detail": [
    {
      "type": "string_too_short",
      "loc": ["body", "password"],
      "msg": "String should have at least 12 characters",
      "ctx": {"min_length": 12}
    },
    {
      "type": "extra_forbidden",
      "loc": ["body", "is_active"],
      "msg": "Extra inputs are not permitted"
    }
  ]
}
```

Rules checked by the user service (such as the email format) return a fixed
message instead: `{"detail": "A valid email address is required."}`.

Unexpected database errors return `500 {"detail": "Internal server error."}`.
The driver's message and SQL are never sent to the client, and the server
logs only a short sanitised diagnostic. If a protected change cannot obtain
the database write lock in time, it also fails with this `500` and changes
nothing.

### Operational limitations

- Protected changes are serialised with SQLite's `BEGIN IMMEDIATE`. PostgreSQL
  row locking is implemented but has **not** been tested against a live
  PostgreSQL server, and PostgreSQL is not a supported production database
  yet.
- While an account is being created, its password is hashed with the SQLite
  write lock held, so other writes wait for that brief period.
- The audit log is a log stream, not a database audit trail (see
  [Audit Logging](#audit-logging)).

## Audit Logging

Every successful administrative change is recorded through the dedicated
`resolveai.audit` logger (`app/core/audit_logging.py`), after its transaction
has committed.

### What is logged

| Action | Logged when | Extra fields |
|---|---|---|
| `user.create` | An account is created | `role`, `is_active` |
| `user.role_change` | A role actually changes | `role_before`, `role_after` |
| `user.deactivate` | An active account is deactivated | `is_active_before`, `is_active_after` |
| `user.activate` | A deactivated account is reactivated | `is_active_before`, `is_active_after` |

Every record also contains `actor_id` (the administrator) and
`target_user_id`. Rejected requests and no-op changes (same role or same
status) are not logged as actions. A stored role the application does not
recognise is logged as `unrecognised`.

Records **never** contain passwords, password hashes, JWTs, names, emails or
request bodies.

Default output (one line per action, on standard error):

```text
2026-11-23T05:12:16.456Z INFO resolveai.audit: admin_action action=user.create actor_id=1 target_user_id=3 role=agent is_active=True
2026-11-23T05:12:16.644Z INFO resolveai.audit: admin_action action=user.deactivate actor_id=1 target_user_id=2 is_active_before=True is_active_after=False
```

The same fields are attached to each log record as a dictionary,
`record.admin_action`, for handlers or formatters that build structured
output themselves.

### Default behaviour

`configure_audit_logging()` runs when `app.main` is imported, at every
application start-up and before every audit record. It is idempotent and
never overrides operator configuration:

- **Output:** if the `resolveai.audit` logger has no handlers, one handler is
  added that writes to standard error (where Uvicorn writes its server log),
  and `propagate` is set to `false` so a root handler does not print each
  record twice.
- **Level:** `INFO`, but only if no level was configured. An explicit level
  (for example `WARNING`) is kept.
- **Timestamps:** UTC ISO-8601 with milliseconds, for example
  `2026-11-23T05:12:16.456Z`, independent of the server's time zone.
- **Operator configuration is respected:** handlers you configure, your level
  and your `propagate` value are never removed or changed, and the default
  handler is never added next to your handlers.
- The logger is re-enabled if a configuration with
  `disable_existing_loggers: true` disabled it.

Without this, Python would drop INFO records from an unconfigured logger, and
Uvicorn's default logging configuration only configures its own loggers.

### Configuring the audit logger

Pass a JSON logging configuration to Uvicorn with `--log-config`. Uvicorn
applies it before it imports the application. A configuration given with
`--log-config` replaces Uvicorn's default logging configuration, so include
handlers for the other logs you need too.

**Send audit records to their own destination**, for example a file collected
by an external log-shipping agent. Save as `logging.json`:

```json
{
  "version": 1,
  "disable_existing_loggers": false,
  "formatters": {
    "audit": {
      "()": "app.core.audit_logging.UtcIsoFormatter",
      "fmt": "%(asctime)s %(levelname)s %(name)s: %(message)s"
    },
    "console": {
      "format": "%(levelname)s %(name)s: %(message)s"
    }
  },
  "handlers": {
    "audit_file": {
      "class": "logging.FileHandler",
      "filename": "audit.log",
      "encoding": "utf-8",
      "formatter": "audit"
    },
    "console": {
      "class": "logging.StreamHandler",
      "stream": "ext://sys.stderr",
      "formatter": "console"
    }
  },
  "root": {"level": "INFO", "handlers": ["console"]},
  "loggers": {
    "resolveai.audit": {
      "level": "INFO",
      "handlers": ["audit_file"],
      "propagate": false
    }
  }
}
```

```cmd
python -m uvicorn app.main:app --log-config logging.json
```

`UtcIsoFormatter` (`app.core.audit_logging.UtcIsoFormatter`) is a standard
`logging.Formatter` whose `%(asctime)s` is UTC ISO-8601. When it is used
through `"()"`, the format string key is `fmt`, not `format`. To feed a
pipeline that reads standard output, replace the file handler with a
`logging.StreamHandler` using `"stream": "ext://sys.stdout"`. `*.log` files are
already ignored by Git.

**Route audit records only through the root logger's handlers** (for example a
single application-wide pipeline). Give the audit logger a `NullHandler` and
`propagate: true`:

```json
{
  "version": 1,
  "disable_existing_loggers": false,
  "formatters": {
    "utc": {
      "()": "app.core.audit_logging.UtcIsoFormatter",
      "fmt": "%(asctime)s %(levelname)s %(name)s: %(message)s"
    }
  },
  "handlers": {
    "pipeline": {
      "class": "logging.StreamHandler",
      "stream": "ext://sys.stdout",
      "formatter": "utc"
    },
    "null": {"class": "logging.NullHandler"}
  },
  "root": {"level": "INFO", "handlers": ["pipeline"]},
  "loggers": {
    "resolveai.audit": {
      "handlers": ["null"],
      "propagate": true
    }
  }
}
```

The `NullHandler` is required: a logger with no handlers at all receives the
default standard-error handler (with `propagate: false`).

**Quieter output:** setting the audit logger's `level` above `INFO` (for
example `"WARNING"`) is respected, but because all audit records are INFO,
it turns the audit log off.

**Reconfiguring after import.** If your code calls
`logging.config.dictConfig` after `app.main` has been imported, the default
handler has already set `propagate` to `false`, and `dictConfig` keeps that
value when the key is omitted. In that case **always state `propagate`
explicitly** for `resolveai.audit`; otherwise, for example, the root-only
configuration above silently drops every audit record.

### What the audit log is not

The audit log is an operational log stream, **not a persistent database audit
trail**. Nothing is stored in the database, the default output on standard
error is kept only if the process manager keeps it, and records are written
after the commit, so a committed change can be missing from the log if the
process stops or logging fails at that moment. If you need durable audit
records, send them to durable storage with your own handler.

## Running Tests

Execute the complete automated test suite:

```cmd
python -m pytest -v
```

The current project test suite contains 680 passing tests.

Run only the admin user-management API tests (endpoints, safeguards,
concurrency and error handling):

```cmd
python -m pytest tests/test_admin_users_api.py -v
```

Run only the audit-logging tests (default output, operator configuration and
fresh-process scenarios):

```cmd
python -m pytest tests/test_audit_logging.py -v
```

The admin and audit tests use in-memory or temporary SQLite databases and
never touch `resolveai.db`. The audit tests start fresh Python interpreters to
apply real logging configurations, so they take longer than the others.
As for running the application, `SECRET_KEY` (at least 32 characters) must be
set, normally in `.env` (see `.env.example`).

To run the application locally, follow
[8. Run the FastAPI application](#8-run-the-fastapi-application):

```cmd
python -m uvicorn app.main:app --reload
```

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
- Role-based access control (`admin`, `agent`, `customer`)
- Admin user management API with administrator safeguards
- Audit logging of administrative actions (log stream only)
- Automated API and integration tests

Further development:
- Frontend dashboard
- Ticket analytics and reporting
- Model monitoring and retraining workflow
- Evaluation using real-world support-ticket data
- Concurrency testing of admin changes against a live PostgreSQL server
- A persistent (database-backed) audit trail

## Author

**Harshvardhan Patil**

GitHub: https://github.com/Harshpatil3640

Repository:
https://github.com/Harshpatil3640/resolveai-support-ticket-system
