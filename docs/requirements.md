# ResolveAI — Software Requirements

**Version:** 1.0
**Status:** Initial specification

**Project type:** Portfolio project

## 1. Purpose

ResolveAI is a web application for managing customer support tickets. It provides standard ticket-management functionality and uses a machine-learning model to suggest a category for each new ticket. Support agents retain control over the final category and ticket status.

## 2. Problem statement

Support teams need a consistent way to record, categorise, prioritise and track customer issues. Manual categorisation can be inconsistent and time-consuming. ResolveAI demonstrates how a conventional business application can use a trained ML model to assist with that workflow.

## 3. Project objectives

- Build a working full-stack application using Python, FastAPI, HTML, CSS and JavaScript.
- Store and retrieve application data using a relational database.
- Train and evaluate a genuine text-classification model.
- Integrate the model into the ticket-creation workflow.
- Provide automated tests and continuous integration.
- Demonstrate professional Git and GitHub practices.
- Keep local development and core functionality free of charge.

## 4. Users

**Support agent:** Logs in, creates and reviews tickets, corrects category suggestions, changes priority and updates ticket status.

**Administrator:** Has authorised access to the application and its operational functions. Public self-registration is not part of the first release; initial user creation will be documented and controlled.

## 5. Functional requirements

### FR-01: Authentication

Users must authenticate before accessing protected application functions. Passwords must never be stored in plain text. Invalid credentials must produce a safe, understandable error message.

### FR-02: Ticket creation

A user can create a ticket with a title and description, plus an optional requester name. The application must validate required fields and reject invalid input.

### FR-03: ML category suggestion

When a ticket is created, the application must use a trained text-classification pipeline to suggest a category. It must record the predicted category and a model confidence estimate. The ticket must remain usable if the prediction service encounters an error.

### FR-04: Ticket management

Users can view a ticket, change its priority, update its status and correct its final category. Supported statuses are Open, In Progress and Resolved. Supported priorities are Low, Medium and High.

### FR-05: Search and filtering

Users can search tickets by relevant text and filter by status, final category and priority.

### FR-06: Prediction correction

The original predicted category must be stored separately from the final category. A user's correction must not overwrite the original prediction.

### FR-07: Dashboard

The dashboard must show total tickets, open tickets, resolved tickets, priority counts and ticket distribution by final category. Counts must be calculated from database records.

### FR-08: Model evaluation

The application must make actual model evaluation results available, including accuracy, macro-F1, per-category precision and recall, and a confusion matrix. Results must come from a held-out test set rather than invented values.

### FR-09: REST API

Ticket operations must be available through documented FastAPI endpoints. Interactive API documentation must be available in the development environment.

### FR-10: Automated testing and CI

Automated tests must cover important API, database and prediction workflows. GitHub Actions must run the test suite when relevant code is pushed or a pull request is opened.

## 6. Machine-learning requirements

- Begin with TF-IDF text features and a Logistic Regression classifier.
- Use labelled customer-service text from a documented dataset whose licence and permitted use have been reviewed.
- Prevent test data from leaking into training or model selection.
- Evaluate the model using a reproducible training and test workflow.
- Treat the prediction score as an estimate, not a guarantee of correctness.
- Allow support agents to correct model suggestions.
- Finalise the user-facing category mapping after inspecting the source dataset's actual labels.
- Keep training and evaluation separate from normal application startup.

## 7. Non-functional requirements

### Usability

The interface must be clear and usable on common desktop and mobile screen sizes.

### Maintainability

Frontend, API routes, business services, database access and ML logic must be organised into understandable modules.

### Reliability

Invalid inputs and unexpected errors must be handled safely. Important workflows must have automated tests.

### Security

Passwords must be securely hashed, secrets must be kept out of source control, and protected endpoints must enforce authentication. Public deployment must use HTTPS and appropriate production configuration.

### Cost

The project must run locally without paid APIs or subscriptions. Optional hosting must not be required for testing or evaluation.

### Reproducibility

The repository must document setup, dependencies, dataset preparation, model training and testing instructions.

## 8. Scope exclusions for Version 1

The first release will not include autonomous ticket resolution, automatic customer replies, paid LLM APIs, email integrations, real-time chat or a distributed microservices architecture. These can be considered after the core application is working.

## 9. Acceptance criteria

The first release is accepted when:

1. A fresh clone can be configured using the documented instructions.
2. An authorised user can log in and create a ticket.
3. Tickets are saved in the database and can be retrieved and updated.
4. The ML model suggests a category, and the final category can be corrected independently.
5. Search, filters and dashboard statistics work against database records.
6. The model report shows reproducible results from held-out data.
7. Automated tests pass locally and in GitHub Actions.
8. The README, architecture documentation and dataset attribution are complete.
9. No credentials, secrets or unintended private data are committed to the public repository.

## 10. Assumptions and decisions to verify

- SQLite will be used for the initial local development version; PostgreSQL may be introduced for deployment.
- The first model will run on the CPU and will not require an external inference API.
- The customer-service dataset's labels must be inspected before the final category taxonomy is approved.
- Dataset attribution and licence requirements must be documented before redistribution or deployment.
- Free hosting is optional and subject to the provider's current limits and terms.
