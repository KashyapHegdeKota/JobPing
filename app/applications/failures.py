"""Failure and transition errors for application workflows."""

from app.db.models import ApplicationFailure


class InvalidApplicationTransition(ValueError):
    """Raised when an application state change is not permitted."""


__all__ = ["ApplicationFailure", "InvalidApplicationTransition"]
