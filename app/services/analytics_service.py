import csv
import json
from pathlib import Path
from sqlalchemy import case, func, literal, select
from sqlalchemy.orm import Session

from app.models import Ticket


def get_ticket_summary(db: Session) -> dict[str, int]:
    """Calculate overall support ticket statistics."""

    statement = select(
        func.count(Ticket.id).label("total_tickets"),

        func.count(
            case((Ticket.status == "Open", Ticket.id))
        ).label("open_tickets"),

        func.count(
            case((Ticket.status == "In Progress", Ticket.id))
        ).label("in_progress_tickets"),

        func.count(
            case((Ticket.status == "Resolved", Ticket.id))
        ).label("resolved_tickets"),
    )

    result = db.execute(statement).one()

    return {
        "total_tickets": result.total_tickets,
        "open_tickets": result.open_tickets,
        "in_progress_tickets": result.in_progress_tickets,
        "resolved_tickets": result.resolved_tickets,
    }


def get_category_distribution(
    db: Session,
) -> list[dict[str, str | int]]:
    """Calculate ticket counts for each confirmed category."""

    category_expression = func.coalesce(
        func.nullif(Ticket.category, ""),
        literal("Unassigned"),
    )

    statement = (
        select(
            category_expression.label("category"),
            func.count(Ticket.id).label("count"),
        )
        .group_by(category_expression)
        .order_by(category_expression)
    )

    results = db.execute(statement).all()

    return [
        {
            "category": row.category,
            "count": row.count,
        }
        for row in results
    ]


def get_ai_prediction_distribution(
    db: Session,
) -> list[dict[str, str | int]]:
    """Calculate ticket counts for each AI-predicted category."""

    prediction_expression = func.coalesce(
        func.nullif(Ticket.predicted_category, ""),
        literal("Unavailable"),
    )

    statement = (
        select(
            prediction_expression.label("category"),
            func.count(Ticket.id).label("count"),
        )
        .group_by(prediction_expression)
        .order_by(prediction_expression)
    )

    results = db.execute(statement).all()

    return [
        {
            "category": row.category,
            "count": row.count,
        }
        for row in results
    ]


def get_priority_distribution(
    db: Session,
) -> list[dict[str, str | int]]:
    """Calculate ticket counts for each priority level."""

    priority_counts = {
        "Low": 0,
        "Medium": 0,
        "High": 0,
    }

    statement = (
        select(
            Ticket.priority.label("priority"),
            func.count(Ticket.id).label("count"),
        )
        .group_by(Ticket.priority)
    )

    results = db.execute(statement).all()

    for row in results:
        priority_counts[row.priority] = row.count

    return [
        {
            "priority": priority,
            "count": count,
        }
        for priority, count in priority_counts.items()
    ]


def get_ticket_trends(
    db: Session,
) -> list[dict[str, str | int]]:
    """Calculate actual ticket creation counts grouped by date."""

    date_expression = func.date(Ticket.created_at)

    statement = (
        select(
            date_expression.label("date"),
            func.count(Ticket.id).label("count"),
        )
        .group_by(date_expression)
        .order_by(date_expression)
    )

    results = db.execute(statement).all()

    return [
        {
            "date": row.date,
            "count": row.count,
        }
        for row in results
    ]


def get_operations_analytics(
    db: Session,
) -> dict:
    """Retrieve the complete operational analytics summary."""

    return {
        "summary": get_ticket_summary(db),

        "category_distribution": get_category_distribution(db),

        "ai_prediction_distribution": get_ai_prediction_distribution(db),

        "priority_distribution": get_priority_distribution(db),

        "ticket_trends": get_ticket_trends(db),
    }


def get_model_performance() -> dict:
    """
    Load final held-out test metrics and confusion matrix
    from the reports directory.
    """
    project_root = Path(__file__).resolve().parents[2]

    metrics_path = project_root / "reports" / "final_test_metrics.json"
    confusion_matrix_path = (
        project_root / "reports" / "final_test_confusion_matrix.csv"
    )

    if not metrics_path.is_file() or not confusion_matrix_path.is_file():
        raise FileNotFoundError("Model performance report files are missing.")

    with metrics_path.open("r", encoding="utf-8") as file:
        report = json.load(file)

    categories = report["categories"]
    classification_report = report["classification_report"]

    category_metrics = []

    for category in categories:
        category_result = classification_report[category]

        category_metrics.append(
            {
                "category": category,
                "precision": float(category_result["precision"]),
                "recall": float(category_result["recall"]),
                "f1_score": float(category_result["f1-score"]),
                "support": int(category_result["support"]),
            }
        )

    with confusion_matrix_path.open(
        "r", encoding="utf-8", newline=""
    ) as file:
        csv_rows = list(csv.reader(file))

    if not csv_rows or len(csv_rows[0]) < 2:
        raise ValueError("The confusion matrix CSV is empty or invalid.")

    labels = csv_rows[0][1:]
    row_labels = []
    matrix_values = []

    for row in csv_rows[1:]:
        if not row:
            continue

        if len(row) != len(labels) + 1:
            raise ValueError("The confusion matrix has an invalid row length.")

        row_labels.append(row[0])
        matrix_values.append([int(value) for value in row[1:]])

    if labels != categories or row_labels != labels:
        raise ValueError("The confusion matrix categories do not match the metrics report.")

    return {
        "model": report["model"],
        "training_records": int(report["training_records"]),
        "test_records": int(report["test_records"]),
        "accuracy": float(report["metrics"]["accuracy"]),
        "macro_f1": float(report["metrics"]["macro_f1"]),
        "weighted_f1": float(report["metrics"]["weighted_f1"]),
        "category_metrics": category_metrics,
        "confusion_matrix": {
            "labels": labels,
            "values": matrix_values,
        },
    }