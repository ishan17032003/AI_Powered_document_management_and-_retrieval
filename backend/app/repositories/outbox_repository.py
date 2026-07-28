"""Compatibility facade for outbox-event persistence APIs."""

from .job_repository import (
    JobRepositoryError,
    create_outbox_event,
    get_outbox_event,
    list_outbox_events,
)

__all__ = [
    "JobRepositoryError",
    "create_outbox_event",
    "get_outbox_event",
    "list_outbox_events",
]
