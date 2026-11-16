
# Third-Party Notices

This document acknowledges third-party datasets and materials referenced or used in the ResolveAI project.

## 1. Bitext Customer Support Dataset

**Dataset name:** Bitext - Customer Service Tagged Training Dataset for LLM-based Virtual Assistants

**Provider:** Bitext Innovations

**Copyright notice:** Copyright (c) Bitext Innovations, 2024

**Dataset source:**

https://huggingface.co/datasets/bitext/Bitext-customer-support-llm-chatbot-training-dataset

**Dataset licence:** Community Data License Agreement – Sharing, Version 1.0 (CDLA-Sharing-1.0)

**Full licence text:**

https://cdla.dev/sharing-1-0/

### Usage in ResolveAI

The dataset was used to develop and evaluate the machine-learning ticket-category classification component.

The downloaded CSV used during development was:

`Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv`

The model development workflow used:

- `instruction` as the input text.
- `category` as the prediction target.
- Training, validation and test datasets created through group-aware splitting.

The dataset was used for local experimentation and model development.

### Dataset Distribution

The original dataset and its processed copies are not included in this repository.

Users who wish to reproduce the experiments must obtain the dataset directly from the original Bitext dataset source and review its applicable licence terms.

### Attribution

We acknowledge Bitext Innovations as the provider of the customer-support dataset used in this project.

The dataset is a hybrid synthetic dataset. Results obtained using it should not be interpreted as verified performance on real-world customer-support traffic.

## 2. Machine Learning Dependencies

ResolveAI uses third-party open-source Python packages, including:

- NumPy
- Pandas
- Scikit-learn
- Joblib

These packages retain their respective licences and copyright notices.

Please refer to the respective package distributions for their licensing terms.

## 3. Project Licence

The MIT licence in the root `LICENSE` file applies to the original source code of this project.

It does not replace or modify the licence terms applicable to third-party datasets or dependencies.

## 4. Model and Evaluation Artefacts

The trained machine-learning model is generated locally by the project's training and evaluation workflow.

The aggregate evaluation metrics and confusion matrices are derived from the dataset experiments.

The trained model binary and individual instruction-level error reports are excluded from the public repository in the current release.
