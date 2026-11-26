"""Notification model behaviour (revision 0005 schema).

Uses conftest's in-memory SQLite session, with foreign keys enforced and
the schema created from ``Base.metadata``. resolveai.db is never used.
"""

import importlib.util
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import Notification, Ticket, TicketMessage, User
from app.models.notification import (
    NOTIFICATION_TYPE_TICKET_MESSAGE,
    NOTIFICATION_TYPES,
)


T0 = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)

MIGRATION_0005 = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0005_notifications.py"
)


def _load_migration_0005():
    spec = importlib.util.spec_from_file_location("migration_0005", MIGRATION_0005)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    return migration


@pytest.fixture
def world(db_session: Session, create_user: Callable[..., User]) -> SimpleNamespace:
    """A customer's ticket with one message, an agent and an administrator."""

    customer = create_user(email="owner@example.test", role="customer", name="Owner")
    agent = create_user(email="helper@example.test", role="agent", name="Helper")
    admin = create_user(email="boss@example.test", role="admin", name="Boss")

    ticket = Ticket(
        title="Payment failed",
        description="Card was declined.",
        creator=customer,
        assigned_to_user_id=agent.id,
    )
    db_session.add(ticket)
    db_session.flush()

    message = TicketMessage(
        ticket=ticket,
        sender=customer,
        sender_role="customer",
        content="Any update on this?",
    )
    db_session.add(message)
    db_session.commit()

    return SimpleNamespace(
        db=db_session,
        customer=customer,
        agent=agent,
        admin=admin,
        ticket=ticket,
        message=message,
    )


def _notification(w: SimpleNamespace, **fields) -> Notification:
    """The agent notified of the customer's message, unless overridden."""

    values = {
        "recipient_user_id": w.agent.id,
        "actor_user_id": w.customer.id,
        "ticket_id": w.ticket.id,
        "message_id": w.message.id,
        "notification_type": NOTIFICATION_TYPE_TICKET_MESSAGE,
        **fields,
    }

    return Notification(**values)


def _count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Notification))


def _expect_rejected(w: SimpleNamespace, notification: Notification) -> None:
    w.db.add(notification)

    with pytest.raises(IntegrityError):
        w.db.commit()

    w.db.rollback()

    assert _count(w.db) == 0


# ---------------------------------------------------------------------------
# Registration and table definition
# ---------------------------------------------------------------------------


def test_model_is_registered_on_the_shared_metadata() -> None:
    assert Base.metadata.tables["notifications"] is Notification.__table__


def test_columns_and_nullability() -> None:
    columns = Notification.__table__.c

    assert [column.name for column in columns] == [
        "id",
        "recipient_user_id",
        "actor_user_id",
        "ticket_id",
        "message_id",
        "notification_type",
        "created_at",
        "read_at",
    ]

    for name in (
        "recipient_user_id",
        "actor_user_id",
        "ticket_id",
        "notification_type",
        "created_at",
    ):
        assert columns[name].nullable is False

    assert columns.message_id.nullable is True
    assert columns.read_at.nullable is True

    assert columns.notification_type.type.length == 40
    assert columns.created_at.type.timezone is True
    assert columns.read_at.type.timezone is True


def test_constraint_and_index_names_match_the_migration() -> None:
    table = Notification.__table__

    assert {
        c.name for c in table.constraints if isinstance(c, CheckConstraint)
    } == {
        "ck_notifications_notification_type",
        "ck_notifications_message_required",
        "ck_notifications_not_self",
    }
    assert {
        c.name for c in table.constraints if isinstance(c, ForeignKeyConstraint)
    } == {
        "fk_notifications_recipient_user_id_users",
        "fk_notifications_actor_user_id_users",
        "fk_notifications_ticket_id_tickets",
        "fk_notifications_message_id_ticket_messages",
    }
    assert [
        (c.name, [column.name for column in c.columns])
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
    ] == [
        (
            "uq_notifications_message_id_recipient_user_id",
            ["message_id", "recipient_user_id"],
        )
    ]

    [recipient_fk] = table.c.recipient_user_id.foreign_keys
    [actor_fk] = table.c.actor_user_id.foreign_keys
    [ticket_fk] = table.c.ticket_id.foreign_keys
    [message_fk] = table.c.message_id.foreign_keys

    assert recipient_fk.target_fullname == "users.id"
    assert actor_fk.target_fullname == "users.id"
    assert ticket_fk.target_fullname == "tickets.id"
    assert message_fk.target_fullname == "ticket_messages.id"

    assert recipient_fk.ondelete is None
    assert actor_fk.ondelete is None
    assert ticket_fk.ondelete == "CASCADE"
    assert message_fk.ondelete == "CASCADE"

    assert sorted(
        (index.name, [column.name for column in index.columns])
        for index in table.indexes
    ) == [
        ("ix_notifications_recipient_user_id_id", ["recipient_user_id", "id"]),
        (
            "ix_notifications_recipient_user_id_read_at",
            ["recipient_user_id", "read_at"],
        ),
        ("ix_notifications_ticket_id", ["ticket_id"]),
    ]
    assert not any(index.unique for index in table.indexes)


def test_model_constraint_text_matches_the_migration_verbatim() -> None:
    """The model's named CHECK constraints are the migration's, verbatim.

    Alembic's autogenerate comparison ignores CHECK constraints, so the
    model and the migration texts are compared directly.
    """

    migration = _load_migration_0005()
    source = MIGRATION_0005.read_text(encoding="utf-8")

    model_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in Notification.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert model_checks == {
        "ck_notifications_notification_type": (
            "notification_type IN ('ticket_message')"
        ),
        "ck_notifications_message_required": (
            "notification_type <> 'ticket_message' OR message_id IS NOT NULL"
        ),
        "ck_notifications_not_self": "recipient_user_id <> actor_user_id",
    }

    for name, sql in model_checks.items():
        assert f'"{sql}",\n            name="{name}"' in source.replace("\r\n", "\n")

    assert migration.revision == "0005"
    assert migration.down_revision == "0004"


def test_notification_types() -> None:
    assert NOTIFICATION_TYPE_TICKET_MESSAGE == "ticket_message"
    assert NOTIFICATION_TYPES == ("ticket_message",)


def test_relationships_and_no_reverse_collections() -> None:
    relationships = inspect(Notification).relationships

    assert {
        name: relationships[name].mapper.class_
        for name in ("recipient", "actor", "ticket", "message")
    } == {
        "recipient": User,
        "actor": User,
        "ticket": Ticket,
        "message": TicketMessage,
    }
    assert [c.name for c in relationships["recipient"].local_columns] == [
        "recipient_user_id"
    ]
    assert [c.name for c in relationships["actor"].local_columns] == [
        "actor_user_id"
    ]

    # Nothing points back at notifications.
    for model in (User, Ticket, TicketMessage):
        assert not any(
            relationship.mapper.class_ is Notification
            for relationship in inspect(model).relationships
        )


# ---------------------------------------------------------------------------
# Valid rows
# ---------------------------------------------------------------------------


def test_valid_notification_is_stored(world: SimpleNamespace) -> None:
    notification = _notification(world, created_at=T0)
    world.db.add(notification)
    world.db.commit()

    stored = world.db.get(Notification, notification.id)

    assert stored.recipient is world.agent
    assert stored.actor is world.customer
    assert stored.ticket is world.ticket
    assert stored.message is world.message
    assert stored.notification_type == "ticket_message"
    assert stored.read_at is None


def test_created_at_defaults_to_utc_now(world: SimpleNamespace) -> None:
    before = datetime.now(timezone.utc)
    notification = _notification(world)
    world.db.add(notification)
    world.db.flush()

    assert notification.created_at.tzinfo is not None
    assert before <= notification.created_at <= datetime.now(timezone.utc)
    assert notification.read_at is None


def test_read_at_can_be_recorded(world: SimpleNamespace) -> None:
    notification = _notification(world, created_at=T0)
    world.db.add(notification)
    world.db.commit()

    notification.read_at = T0
    world.db.commit()
    world.db.expire_all()

    assert world.db.get(Notification, notification.id).read_at is not None


def test_one_message_can_notify_several_recipients(world: SimpleNamespace) -> None:
    world.db.add_all(
        [
            _notification(world),
            _notification(world, recipient_user_id=world.admin.id),
        ]
    )
    world.db.commit()

    assert _count(world.db) == 2


def test_one_recipient_can_be_notified_of_several_messages(
    world: SimpleNamespace,
) -> None:
    second = TicketMessage(
        ticket=world.ticket,
        sender=world.customer,
        sender_role="customer",
        content="Hello again.",
    )
    world.db.add(second)
    world.db.flush()
    world.db.add_all(
        [_notification(world), _notification(world, message_id=second.id)]
    )
    world.db.commit()

    assert _count(world.db) == 2


# ---------------------------------------------------------------------------
# Rejected rows
# ---------------------------------------------------------------------------


def test_self_notification_is_rejected(world: SimpleNamespace) -> None:
    _expect_rejected(
        world,
        _notification(world, recipient_user_id=world.customer.id),
    )


def test_duplicate_message_recipient_pair_is_rejected(
    world: SimpleNamespace,
) -> None:
    world.db.add(_notification(world))
    world.db.commit()

    world.db.add(_notification(world))

    with pytest.raises(IntegrityError):
        world.db.commit()

    world.db.rollback()

    assert _count(world.db) == 1


@pytest.mark.parametrize(
    "notification_type",
    ["status_changed", "Ticket_Message", "ticket_message ", ""],
    ids=["unknown", "wrong-case", "trailing-space", "empty"],
)
def test_unknown_notification_type_is_rejected(
    world: SimpleNamespace,
    notification_type: str,
) -> None:
    _expect_rejected(
        world,
        _notification(world, notification_type=notification_type),
    )


def test_ticket_message_without_a_message_is_rejected(
    world: SimpleNamespace,
) -> None:
    _expect_rejected(world, _notification(world, message_id=None))


@pytest.mark.parametrize(
    "column",
    ["recipient_user_id", "actor_user_id", "ticket_id", "notification_type"],
)
def test_required_fields(world: SimpleNamespace, column: str) -> None:
    _expect_rejected(world, _notification(world, **{column: None}))


@pytest.mark.parametrize(
    "column",
    ["recipient_user_id", "actor_user_id", "ticket_id", "message_id"],
)
def test_foreign_keys_reject_missing_rows(
    world: SimpleNamespace,
    column: str,
) -> None:
    _expect_rejected(world, _notification(world, **{column: 999}))


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def _other_ticket_notification(w: SimpleNamespace) -> Notification:
    """A notification on a second ticket, which must survive."""

    other = Ticket(title="Other", description="Other ticket.", creator=w.customer)
    w.db.add(other)
    w.db.flush()
    other_message = TicketMessage(
        ticket=other,
        sender=w.customer,
        sender_role="customer",
        content="Different ticket.",
    )
    w.db.add(other_message)
    w.db.flush()

    return _notification(w, ticket_id=other.id, message_id=other_message.id)


def test_deleting_a_ticket_through_the_orm_deletes_its_notifications(
    world: SimpleNamespace,
) -> None:
    kept = _other_ticket_notification(world)
    world.db.add_all(
        [
            kept,
            _notification(world),
            _notification(world, recipient_user_id=world.admin.id),
        ]
    )
    world.db.commit()
    kept_id = kept.id

    # Unloaded messages and notifications are deleted by the database.
    world.db.expire_all()
    world.db.delete(world.db.get(Ticket, world.ticket.id))
    world.db.commit()

    assert world.db.scalars(select(Notification.id)).all() == [kept_id]
    # Recipients and actors are kept.
    for user in (world.customer, world.agent, world.admin):
        assert world.db.get(User, user.id) is not None


def test_deleting_a_ticket_with_loaded_messages(world: SimpleNamespace) -> None:
    world.db.add(_notification(world))
    world.db.commit()

    # The ORM deletes the loaded message; the database cascades from it.
    assert len(world.ticket.messages) == 1

    world.db.delete(world.ticket)
    world.db.commit()

    assert _count(world.db) == 0


def test_database_cascade_from_ticket_with_raw_sql(world: SimpleNamespace) -> None:
    world.db.add(_notification(world))
    world.db.commit()

    world.db.execute(
        text("DELETE FROM tickets WHERE id = :id"), {"id": world.ticket.id}
    )
    world.db.commit()

    assert _count(world.db) == 0


def test_deleting_a_message_deletes_only_its_notifications(
    world: SimpleNamespace,
) -> None:
    second = TicketMessage(
        ticket=world.ticket,
        sender=world.customer,
        sender_role="customer",
        content="Second message.",
    )
    world.db.add(second)
    world.db.flush()
    kept = _notification(world, message_id=second.id)
    world.db.add_all([_notification(world), kept])
    world.db.commit()
    kept_id = kept.id

    world.db.execute(
        text("DELETE FROM ticket_messages WHERE id = :id"),
        {"id": world.message.id},
    )
    world.db.commit()

    assert world.db.scalars(select(Notification.id)).all() == [kept_id]
    # The ticket is kept.
    assert world.db.get(Ticket, world.ticket.id) is not None


@pytest.mark.parametrize("role", ["recipient", "actor"])
def test_a_user_with_notifications_cannot_be_deleted(
    world: SimpleNamespace,
    create_user: Callable[..., User],
    role: str,
) -> None:
    # Users referenced by nothing but the notification.
    recipient = create_user(email="r@example.test", role="admin", name="R")
    actor = create_user(email="a@example.test", role="admin", name="A")
    world.db.add(
        _notification(
            world,
            recipient_user_id=recipient.id,
            actor_user_id=actor.id,
        )
    )
    world.db.commit()
    user_id = recipient.id if role == "recipient" else actor.id

    with pytest.raises(IntegrityError):
        world.db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    world.db.rollback()

    assert _count(world.db) == 1
    assert world.db.get(User, user_id) is not None
