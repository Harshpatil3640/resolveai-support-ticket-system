from enum import Enum


class UserRole(str, Enum):
    """Standard business roles supported by ResolveAI.

    Values match what is stored in ``User.role``, so members compare
    equal to the stored strings (e.g. ``UserRole.AGENT == "agent"``).
    """

    ADMIN = "admin"
    AGENT = "agent"
    CUSTOMER = "customer"
