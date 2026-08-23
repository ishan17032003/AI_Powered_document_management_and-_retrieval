"""Ask AI repository — all MongoDB CRUD operations.

All async functions accept the Motor database handle returned by
``app.mongodb.get_db()``.  When ``db`` is ``None`` (MongoDB disabled /
stateless mode), every function that would return data returns sensible
defaults and every write is silently skipped.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from ..services.ask_ai.mongo_models import (
    ActiveScope,
    MongoConversation,
    MongoHistoryMessage,
    MongoTurnRun,
    MongoUser,
    RunMetrics,
    SourcesUsed,
)


_log = logging.getLogger(__name__)

_USERS = "ask_ai_users"
_CONVERSATIONS = "ask_ai_conversations"
_HISTORY = "ask_ai_conversation_history"
_TURN_RUNS = "ask_ai_turn_runs"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uid() -> str:
    return str(uuid.uuid4())


# ── User management ────────────────────────────────────────────────────────────


async def upsert_mongo_user(db, postgres_user_id: int, name: str, email: str) -> MongoUser | None:
    """Create or update the Ask AI user record that mirrors a Postgres user."""
    if db is None:
        return None
    try:
        await db[_USERS].update_one(
            {"postgres_user_id": postgres_user_id},
            {
                "$set": {"name": name, "email": email, "updated_at": _now()},
                "$setOnInsert": {
                    "postgres_user_id": postgres_user_id,
                    "google_drive_connected": False,
                    "google_drive_email": None,
                    "google_drive_token": None,
                    "created_at": _now(),
                },
            },
            upsert=True,
        )
        return await get_mongo_user(db, postgres_user_id)
    except Exception as exc:
        _log.warning("upsert_mongo_user failed: %s", exc)
        return None



async def get_mongo_user(db, postgres_user_id: int) -> MongoUser | None:
    if db is None:
        return None
    try:
        raw = await db[_USERS].find_one({"postgres_user_id": postgres_user_id})
        if not raw:
            return None
        raw_clean = {k: v for k, v in raw.items() if k != "_id"}
        return MongoUser(**raw_clean)
    except Exception as exc:
        _log.warning("get_mongo_user for postgres_user_id=%s failed: %s", postgres_user_id, exc)
        return None



async def set_drive_connected(
    db,
    postgres_user_id: int,
    connected: bool,
    email: str | None = None,
    token: dict | None = None,
) -> None:
    """Update Google Drive connection state for a user."""
    if db is None:
        return
    try:
        update = {
            "google_drive_connected": connected,
            "google_drive_email": email,
            "google_drive_token": token,
            "updated_at": _now(),
        }
        await db[_USERS].update_one(
            {"postgres_user_id": postgres_user_id},
            {"$set": update},
            upsert=True,
        )
    except Exception as exc:
        _log.warning("set_drive_connected failed: %s", exc)


# ── Conversation management ────────────────────────────────────────────────────


async def create_conversation(
    db,
    user_id: int,
    *,
    company_kb_enabled: bool = True,
    google_drive_enabled: bool = False,
) -> str | None:
    """Create a new conversation and return its _id UUID string."""
    if db is None:
        return None
    try:
        conversation_id = _uid()
        doc = MongoConversation(
            **{"_id": conversation_id},
            user_id=user_id,
            company_kb_enabled=True,  # company KB is always enabled
            google_drive_enabled=google_drive_enabled,
        )
        await db[_CONVERSATIONS].insert_one(doc.model_dump(by_alias=True))
        return conversation_id
    except Exception as exc:
        _log.warning("create_conversation failed: %s", exc)
        return None


async def get_conversation(db, conversation_id: str, user_id: int) -> MongoConversation | None:
    """Load a conversation, verifying ownership. Returns None on miss or access denied."""
    if db is None:
        return None
    try:
        raw = await db[_CONVERSATIONS].find_one({"_id": conversation_id, "user_id": user_id})
        if raw is None:
            return None
        return MongoConversation(**raw)
    except Exception as exc:
        _log.warning("get_conversation failed: %s", exc)
        return None


async def list_conversations(db, user_id: int, limit: int = 50) -> list[dict]:
    """Return conversation summaries sorted by most recent activity."""
    if db is None:
        return []
    try:
        cursor = (
            db[_CONVERSATIONS]
            .find({"user_id": user_id}, {"active_scope": 0, "scope_start_message_id": 0})
            .sort("last_message_at", -1)
            .limit(limit)
        )
        return [doc async for doc in cursor]
    except Exception as exc:
        _log.warning("list_conversations failed: %s", exc)
        return []


async def update_conversation_scope(
    db,
    conversation_id: str,
    user_id: int,
    active_scope: ActiveScope,
    scope_start_message_id: str | None,
) -> None:
    """Persist the updated scope and scope boundary message ID."""
    if db is None:
        return
    try:
        await db[_CONVERSATIONS].update_one(
            {"_id": conversation_id, "user_id": user_id},
            {
                "$set": {
                    "active_scope": active_scope.model_dump(),
                    "scope_start_message_id": scope_start_message_id,
                }
            },
        )
    except Exception as exc:
        _log.warning("update_conversation_scope failed: %s", exc)


async def update_conversation_meta(
    db,
    conversation_id: str,
    user_id: int,
    *,
    title: str | None = None,
    last_message_at: datetime | None = None,
) -> None:
    """Update conversation title and/or last_message_at timestamp."""
    if db is None:
        return
    try:
        update: dict = {}
        if title:
            update["title"] = title[:80]
        if last_message_at:
            update["last_message_at"] = last_message_at
        if update:
            await db[_CONVERSATIONS].update_one(
                {"_id": conversation_id, "user_id": user_id},
                {"$set": update},
            )
    except Exception as exc:
        _log.warning("update_conversation_meta failed: %s", exc)


async def update_source_flags(
    db,
    conversation_id: str,
    user_id: int,
    *,
    google_drive_enabled: bool,
) -> None:
    """Update per-turn source flags.  company_kb_enabled is always True."""
    if db is None:
        return
    try:
        await db[_CONVERSATIONS].update_one(
            {"_id": conversation_id, "user_id": user_id},
            {"$set": {"company_kb_enabled": True, "google_drive_enabled": google_drive_enabled}},
        )
    except Exception as exc:
        _log.warning("update_source_flags failed: %s", exc)


# ── History management ─────────────────────────────────────────────────────────


async def append_message(
    db,
    conversation_id: str,
    user_id: int,
    *,
    role: str,
    content: str,
    scope_snapshot: ActiveScope,
    sources_used: SourcesUsed,
    provider: str | None = None,
    model_id: str | None = None,
    metrics: RunMetrics | None = None,
) -> str | None:
    """Append one message to the conversation history and return its _id."""
    if db is None:
        return None
    try:
        message_id = _uid()
        doc = MongoHistoryMessage(
            **{"_id": message_id},
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,  # type: ignore[arg-type]
            content=content,
            scope_snapshot=scope_snapshot,
            sources_used=sources_used,
            provider=provider,
            model_id=model_id,
            metrics=metrics,
        )
        await db[_HISTORY].insert_one(doc.model_dump(by_alias=True))
        return message_id
    except Exception as exc:
        _log.warning("append_message failed: %s", exc)
        return None


async def get_history_window(
    db,
    conversation_id: str,
    scope_start_message_id: str | None,
    limit: int = 20,
) -> list[dict]:
    """Return conversation turns from the scope boundary up to ``limit`` turns.

    When ``scope_start_message_id`` is None the full history is loaded (up to
    ``limit`` most recent turns, oldest first).
    """
    if db is None:
        return []
    try:
        query: dict = {"conversation_id": conversation_id}

        if scope_start_message_id is not None:
            # Find the boundary message's timestamp to filter after it
            boundary = await db[_HISTORY].find_one({"_id": scope_start_message_id})
            if boundary:
                query["created_at"] = {"$gte": boundary["created_at"]}

        cursor = (
            db[_HISTORY]
            .find(query, {"_id": 1, "role": 1, "content": 1, "created_at": 1, "sources_used": 1})
            .sort("created_at", 1)
            .limit(limit)
        )
        return [doc async for doc in cursor]
    except Exception as exc:
        _log.warning("get_history_window failed: %s", exc)
        return []


async def delete_conversation(db, conversation_id: str, user_id: int) -> bool:
    """Delete a conversation and all its history messages."""
    if db is None:
        return False
    try:
        res = await db[_CONVERSATIONS].delete_one({"_id": conversation_id, "user_id": user_id})
        if res.deleted_count > 0:
            await db[_HISTORY].delete_many({"conversation_id": conversation_id, "user_id": user_id})
            return True
        return False
    except Exception as exc:
        _log.warning("delete_conversation failed: %s", exc)
        return False



# ── Turn runs (multi-model comparison grid) ───────────────────────────────────


async def append_turn_runs(
    db,
    conversation_id: str,
    user_id: int,
    turn_message_id: str,
    runs: list[dict],
) -> list[str]:
    """Persist every candidate model run of one turn. Returns inserted ids."""
    if db is None or not runs:
        return []
    try:
        docs = []
        for run in runs:
            doc = MongoTurnRun(
                **{"_id": _uid()},
                conversation_id=conversation_id,
                turn_message_id=turn_message_id,
                user_id=user_id,
                provider=str(run.get("provider", "")),
                model_id=str(run.get("model_id", "")),
                display_version=str(run.get("display_version", "")),
                reasoning=str(run.get("reasoning", "none")),
                body=str(run.get("body", "")),
                status="error" if run.get("status") == "error" else "ok",
                tool_events=list(run.get("tool_events") or []),
                artifacts=list(run.get("artifacts") or []),
                metrics=RunMetrics(**(run.get("metrics") or {})),
            )
            docs.append(doc.model_dump(by_alias=True))
        await db[_TURN_RUNS].insert_many(docs)
        return [d["_id"] for d in docs]
    except Exception as exc:
        _log.warning("append_turn_runs failed: %s", exc)
        return []


async def mark_run_selected(
    db,
    conversation_id: str,
    user_id: int,
    provider: str,
    turn_message_id: str | None = None,
) -> None:
    """Flag the selected candidate run for a turn (latest turn when id omitted)."""
    if db is None:
        return
    try:
        query: dict = {"conversation_id": conversation_id, "user_id": user_id, "provider": provider}
        if turn_message_id:
            query["turn_message_id"] = turn_message_id
        run = await db[_TURN_RUNS].find_one(query, sort=[("created_at", -1)])
        if run:
            await db[_TURN_RUNS].update_one({"_id": run["_id"]}, {"$set": {"selected": True}})
    except Exception as exc:
        _log.warning("mark_run_selected failed: %s", exc)


async def list_turn_runs(db, conversation_id: str, user_id: int) -> list[dict]:
    """All persisted candidate runs for a conversation (comparison log)."""
    if db is None:
        return []
    try:
        cursor = db[_TURN_RUNS].find(
            {"conversation_id": conversation_id, "user_id": user_id}
        ).sort("created_at", 1)
        return [run async for run in cursor]
    except Exception as exc:
        _log.warning("list_turn_runs failed: %s", exc)
        return []
