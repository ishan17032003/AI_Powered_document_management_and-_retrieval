"""MongoDB async client singleton for Ask AI conversation persistence.

When ``settings.mongodb_url`` is empty the module degrades gracefully:
every function becomes a no-op and callers receive ``None`` from ``get_db()``.
This keeps the application fully functional without MongoDB (stateless mode,
history is passed inline in each request payload).
"""

from __future__ import annotations

import logging

from .runtime import settings

_log = logging.getLogger(__name__)

_client = None
_db = None


def _motor_available() -> bool:
    try:
        import motor.motor_asyncio  # noqa: F401
        return True
    except ImportError:
        return False


def get_db():
    """Return the ``ask_ai`` Motor database, or ``None`` if MongoDB is disabled."""
    return _db


def get_client():
    """Return the Motor client, or ``None`` if MongoDB is disabled."""
    return _client


async def init_mongodb() -> None:
    """Initialise the Motor client and create indexes.

    Called once from the FastAPI startup event.  Safe to call multiple times
    (subsequent calls are no-ops because the client is already initialised).
    """
    global _client, _db

    if not settings.mongodb_url:
        _log.info("DOCVAULT_MONGODB_URL not set — Ask AI running in stateless mode (no conversation persistence).")
        return

    if not _motor_available():
        _log.warning(
            "motor package is not installed. "
            "Install it with: pip install motor  "
            "Ask AI will run in stateless mode until motor is available."
        )
        return

    if _client is not None:
        return  # already initialised

    try:
        import motor.motor_asyncio

        _client = motor.motor_asyncio.AsyncIOMotorClient(
            settings.mongodb_url,
            serverSelectionTimeoutMS=5_000,
            connectTimeoutMS=5_000,
        )
        _db = _client.get_default_database()
        await _ensure_indexes(_db)
        _log.info("MongoDB connected — Ask AI conversation persistence enabled.")
    except Exception as exc:
        _log.warning("MongoDB connection failed (%s). Ask AI will run in stateless mode.", exc)
        _client = None
        _db = None


async def _ensure_indexes(db) -> None:
    """Create collection indexes idempotently.

    Index creation is safe to call on an already-indexed collection; MongoDB
    silently ignores duplicate index definitions.
    """
    try:
        from pymongo import ASCENDING, DESCENDING

        # ask_ai_users: one record per Postgres user
        await db["ask_ai_users"].create_index(
            [("postgres_user_id", ASCENDING)], unique=True, name="ix_users_postgres_id"
        )

        # ask_ai_conversations: owned by user, sorted by recency
        await db["ask_ai_conversations"].create_index(
            [("user_id", ASCENDING), ("last_message_at", DESCENDING)],
            name="ix_conv_user_recency",
        )


        # ask_ai_conversation_history: ordered turns per conversation
        await db["ask_ai_conversation_history"].create_index(
            [("conversation_id", ASCENDING), ("created_at", ASCENDING)],
            name="ix_history_conv_time",
        )

        _log.info("MongoDB indexes ensured.")
    except Exception as exc:
        _log.warning("MongoDB index creation failed: %s", exc)
