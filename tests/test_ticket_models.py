"""Ticket, TicketEvent and User model behaviour (revision 0003 schema).

Relationships, ON DELETE CASCADE and optimistic versioning are exercised
through the ORM. Most tests use conftest's in-memory SQLite session (with
foreign keys enforced); the stale-write tests use a temporary SQLite file
so two sessions have independent connections. resolveai.db is never used.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    create_engine,
    event,
    func,
    inspect,
    select,
)
from sqlalchemy.orm import Session, configure_mappers
from sqlalchemy.orm.exc import StaleDataError

from app.db.base import Base
from app.models import Ticket, TicketEvent, User


T0 = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def make_ticket(db_session: Session) -> Callable[..., Ticket]:
    def _make(creator: User, **fields) -> Ticket:
        ticket = Ticket(
            title=fields.pop("title", "Payment failed"),
            description=fields.pop("description", "Card was declined."),
            creator=creator,
            **fields,
        )
        db_session.add(ticket)
        db_session.commit()

        return ticket

    return _make


def _event(ticket: Ticket, actor: User, **fields) -> TicketEvent:
    return TicketEvent(
        ticket=ticket,
        actor=actor,
        event_type=fields.pop("event_type", "created"),
        **fields,
    )


def _event_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(TicketEvent))


# ---------------------------------------------------------------------------
# Mapper configuration
# ---------------------------------------------------------------------------


def test_mappers_configure_with_explicit_foreign_keys() -> None:
    configure_mappers()

    tickets = Ticket.__table__.c
    events = TicketEvent.__table__.c
    ticket_rels = inspect(Ticket).relationships
    user_rels = inspect(User).relationships
    event_rels = inspect(TicketEvent).relationships

    # Each relationship uses exactly the intended foreign key column.
    assert list(ticket_rels["creator"].local_columns) == [tickets.created_by_user_id]
    assert list(ticket_rels["assignee"].local_columns) == [tickets.assigned_to_user_id]
    assert set(user_rels["tickets"].remote_side) == {tickets.created_by_user_id}
    assert set(user_rels["assigned_tickets"].remote_side) == {tickets.assigned_to_user_id}
    assert list(event_rels["ticket"].local_columns) == [events.ticket_id]
    assert list(event_rels["actor"].local_columns) == [events.actor_user_id]
    assert list(event_rels["from_assignee"].local_columns) == [events.from_assignee_id]
    assert list(event_rels["to_assignee"].local_columns) == [events.to_assignee_id]

    assert ticket_rels["events"].passive_deletes is True
    assert ticket_rels["events"].cascade.delete_orphan is True

    # Optimistic versioning uses tickets.version.
    assert inspect(Ticket).version_id_col is tickets.version


def test_model_constraints_match_the_migration_names() -> None:
    def names(table, kind) -> set[str]:
        return {c.name for c in table.constraints if isinstance(c, kind)}

    tickets = Ticket.__table__
    events = TicketEvent.__table__

    assert names(tickets, CheckConstraint) == {"ck_tickets_priority", "ck_tickets_status"}
    assert "fk_tickets_assigned_to_user_id_users" in names(tickets, ForeignKeyConstraint)
    assert {index.name for index in tickets.indexes} == {
        "ix_tickets_created_by_user_id",
        "ix_tickets_assigned_to_user_id",
    }
    assert names(events, CheckConstraint) == {
        "ck_ticket_events_event_type",
        "ck_ticket_events_from_status",
        "ck_ticket_events_to_status",
        "ck_ticket_events_reason",
    }
    assert names(events, ForeignKeyConstraint) == {
        "fk_ticket_events_ticket_id_tickets",
        "fk_ticket_events_actor_user_id_users",
        "fk_ticket_events_from_assignee_id_users",
        "fk_ticket_events_to_assignee_id_users",
    }

    [ticket_fk] = events.c.ticket_id.foreign_keys
    assert ticket_fk.ondelete == "CASCADE"
    assert [
        [column.name for column in index.columns] for index in events.indexes
    ] == [["ticket_id", "created_at", "id"]]


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


def test_creator_and_assignee_relationships_are_independent(
    db_session: Session,
    create_user: Callable[..., User],
    make_ticket: Callable[..., Ticket],
) -> None:
    creator = create_user(email="creator@example.test", role="customer")
    agent = create_user(email="agent@example.test", role="agent")

    ticket = make_ticket(creator)

    # New tickets are unassigned at version 1.
    assert (ticket.assigned_to_user_id, ticket.assigned_at, ticket.version) == (
        None,
        None,
        1,
    )
    assert ticket.assignee is None

    ticket.assignee = agent
    ticket.assigned_at = T0
    db_session.commit()
    db_session.expire_all()

    assert ticket.creator is creator
    assert ticket.assignee is agent
    assert ticket.assigned_to_user_id == agent.id
    assert ticket.created_by_user_id == creator.id
    assert creator.tickets == [ticket]
    assert creator.assigned_tickets == []
    assert agent.tickets == []
    assert agent.assigned_tickets == [ticket]

    # Unassigning leaves the creator untouched.
    ticket.assignee = None
    ticket.assigned_at = None
    db_session.commit()
    db_session.expire_all()

    assert ticket.assigned_to_user_id is None
    assert ticket.creator is creator
    assert agent.assigned_tickets == []
    assert creator.tickets == [ticket]


def test_ticket_event_relationships_and_chronological_order(
    db_session: Session,
    create_user: Callable[..., User],
    make_ticket: Callable[..., Ticket],
) -> None:
    admin = create_user(email="admin@example.test", role="admin")
    first = create_user(email="first@example.test", role="agent")
    second = create_user(email="second@example.test", role="agent")
    ticket = make_ticket(admin)

    # Inserted out of order; two share a timestamp (ordered by id).
    late = _event(
        ticket,
        admin,
        event_type="reassigned",
        from_assignee=first,
        to_assignee=second,
        created_at=T0 + timedelta(minutes=5),
    )
    db_session.add(late)
    db_session.flush()
    tie_a = _event(ticket, admin, event_type="created", created_at=T0)
    db_session.add(tie_a)
    db_session.flush()
    tie_b = _event(
        ticket,
        first,
        event_type="status_changed",
        from_status="Open",
        to_status="In Progress",
        created_at=T0,
    )
    db_session.add(tie_b)
    db_session.commit()
    db_session.expire_all()

    assert [e.id for e in ticket.events] == [tie_a.id, tie_b.id, late.id]

    assert late.ticket is ticket
    assert late.actor is admin
    assert late.from_assignee is first
    assert late.to_assignee is second
    assert (late.actor_user_id, late.from_assignee_id, late.to_assignee_id) == (
        admin.id,
        first.id,
        second.id,
    )
    assert tie_b.actor is first
    assert tie_b.from_assignee is None and tie_b.to_assignee is None
    assert tie_a.created_at is not None


def test_event_created_at_defaults_to_utc_now(
    db_session: Session,
    create_user: Callable[..., User],
    make_ticket: Callable[..., Ticket],
) -> None:
    admin = create_user(email="admin@example.test", role="admin")
    ticket = make_ticket(admin)
    before = datetime.now(timezone.utc)

    created = _event(ticket, admin)
    db_session.add(created)
    db_session.commit()

    # SQLite returns naive datetimes; the stored value is UTC.
    stored = created.created_at.replace(tzinfo=timezone.utc)

    assert before - timedelta(seconds=5) <= stored <= datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# ON DELETE CASCADE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("events_loaded", [True, False], ids=["loaded", "unloaded"])
def test_deleting_a_ticket_deletes_its_events(
    db_session: Session,
    create_user: Callable[..., User],
    make_ticket: Callable[..., Ticket],
    events_loaded: bool,
) -> None:
    admin = create_user(email="admin@example.test", role="admin")
    doomed = make_ticket(admin, title="Doomed ticket")
    kept = make_ticket(admin, title="Kept ticket")
    db_session.add_all(
        [
            _event(doomed, admin),
            _event(doomed, admin, event_type="assigned", to_assignee=admin),
            _event(kept, admin),
        ]
    )
    db_session.commit()
    doomed_id, kept_id = doomed.id, kept.id
    db_session.expunge_all()

    ticket = db_session.get(Ticket, doomed_id)

    if events_loaded:
        assert len(ticket.events) == 2
    else:
        assert "events" not in inspect(ticket).dict

    db_session.delete(ticket)
    db_session.commit()

    assert db_session.get(Ticket, doomed_id) is None
    assert db_session.scalars(select(TicketEvent.ticket_id)).all() == [kept_id]
    assert _event_count(db_session) == 1


# ---------------------------------------------------------------------------
# Optimistic versioning
# ---------------------------------------------------------------------------


def test_version_increments_on_every_orm_update(
    db_session: Session,
    create_user: Callable[..., User],
    make_ticket: Callable[..., Ticket],
) -> None:
    agent = create_user(email="agent@example.test", role="agent")
    ticket = make_ticket(agent)

    assert ticket.version == 1

    ticket.title = "Payment failed twice"
    db_session.commit()
    assert ticket.version == 2

    ticket.assignee = agent
    ticket.assigned_at = T0
    db_session.commit()
    assert ticket.version == 3

    # A flush with no changes does not bump the version.
    db_session.commit()
    assert ticket.version == 3


@pytest.fixture
def file_engine(tmp_path):
    """A temporary SQLite file; each Session gets its own connection."""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'versioning.db'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)

    with Session(engine) as db:
        agent = User(
            name="Agent",
            email="agent@example.test",
            password_hash="unused",
            role="agent",
        )
        db.add(agent)
        db.flush()
        db.add(
            Ticket(
                id=1,
                title="Payment failed",
                description="Card was declined.",
                created_by_user_id=agent.id,
            )
        )
        db.commit()

    yield engine

    engine.dispose()


def test_stale_update_raises_instead_of_overwriting(file_engine) -> None:
    with Session(file_engine) as first, Session(file_engine) as second:
        mine = first.get(Ticket, 1)
        theirs = second.get(Ticket, 1)

        assert mine.version == theirs.version == 1

        mine.priority = "High"
        first.commit()

        assert mine.version == 2

        theirs.priority = "Low"

        with pytest.raises(StaleDataError):
            second.commit()

        second.rollback()

    with Session(file_engine) as check:
        stored = check.get(Ticket, 1)

        # The first write survived; the stale one changed nothing.
        assert (stored.priority, stored.version) == ("High", 2)


def test_stale_delete_raises_instead_of_deleting(file_engine) -> None:
    with Session(file_engine) as first, Session(file_engine) as second:
        mine = first.get(Ticket, 1)
        theirs = second.get(Ticket, 1)

        mine.status = "In Progress"
        first.commit()

        second.delete(theirs)

        with pytest.raises(StaleDataError):
            second.commit()

        second.rollback()

    with Session(file_engine) as check:
        stored = check.get(Ticket, 1)

        assert (stored.status, stored.version) == ("In Progress", 2)
