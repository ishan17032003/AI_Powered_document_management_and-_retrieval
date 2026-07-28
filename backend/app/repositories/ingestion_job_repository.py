"""Compatibility facade for ingestion-job persistence APIs."""

from .job_repository import (
    JobRepositoryError,
    create_ingestion_job,
    get_ingestion_job,
    list_ingestion_jobs,
)

__all__ = [
    "JobRepositoryError",
    "create_ingestion_job",
    "get_ingestion_job",
    "list_ingestion_jobs",
]
