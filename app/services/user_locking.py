"""Row locks on user accounts shared by the user and ticket services.

User administration (``app.services.user_service``) and the ticket
workflow (``app.services.ticket_workflow``) both lock the active
administrator rows before anything else, with the same query in the same
id order, so their changes always take row locks in one global order:

1. the active administrator rows, ascending id (this module);
2. the other user row the change concerns (target, assignee or actor);
3. ticket rows, ascending id.

On databases with row locks (``SELECT ... FOR UPDATE``) this order means
two changes cannot deadlock. SQLite ignores ``FOR UPDATE``; there the
whole change is serialised by the ``BEGIN IMMEDIATE`` the caller has
already issued (``app.db.locking.begin_serialised_change``), and this
module adds nothing.

This module only builds and runs the locking query; it never begins,
commits or rolls back. It imports models and roles only, so both services
can use it without circular imports.
"""

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.roles import UserRole
from app.models import User


def active_administrator_ids_for_update() -> Select:
    """IDs of the active administrators, in id order, locked where supported.

    An administrator is a user whose stored role is exactly ``admin`` and
    who is active.
    """

    return (
        select(User.id)
        .where(
            User.role == UserRole.ADMIN.value,
            User.is_active.is_(True),
        )
        .order_by(User.id)
        .with_for_update()
    )


def lock_active_administrator_ids(db: Session) -> list[int]:
    """Read (and, where supported, lock) the active administrator IDs.

    Must run inside the caller's serialised transaction, before any other
    row of the change is read.
    """

    return list(db.scalars(active_administrator_ids_for_update()))
