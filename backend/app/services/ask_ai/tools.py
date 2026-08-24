"""Ask AI agent tools — Phase 2.

Two authorized, read-only tools every tool-capable model may call during a run:

  search_documents  — re-query the vault retrieval lane (LanceDB/FTS) for
                      additional passages. Same VIEW-ID prefilter as the
                      chat context; results can never exceed the caller's
                      authorization.
  list_documents    — named, read-only catalogue queries (recent documents /
                      documents of a class), row-capped, filtered to the
                      caller's visible document set. No free-form SQL.
  execute_python    — Phase 3: run Python in a resource-limited sandbox;
                      files written to ./out become downloadable artifacts.
                      Only offered to model versions with the "code"
                      capability.

Execution is serialized with an asyncio lock because the request-scoped
SQLAlchemy Session must not be used concurrently by parallel model runs.
"""

from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy.orm import Session

from .mongo_models import ActiveScope

_log = logging.getLogger(__name__)

_MAX_SEARCH_LIMIT = 10
_MAX_LIST_LIMIT = 25
_MAX_ARGS_BYTES = 4000
# execute_python carries whole scripts in its arguments; align with the
# sandbox's own 20k-char code limit (plus JSON-escaping overhead).
_MAX_CODE_ARGS_BYTES = 32000
_MAX_RESULT_CHARS = 8000

TOOL_LABELS = {
    "search_documents": "Retrieval · re-query vault",
    "list_documents": "Catalogue · read-only query",
    "execute_python": "Sandbox · python",
}

_OPENAI_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": (
                "Search the company document vault for passages relevant to a query. "
                "Use when the provided context is insufficient or you need to verify "
                "a claim against the archive. Results are limited to documents the "
                "current user is authorized to view."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_SEARCH_LIMIT},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": (
                "List documents from the vault catalogue (read-only). kind='recent' "
                "returns the newest visible documents; kind='by_class' returns "
                "documents of the given class name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["recent", "by_class"]},
                    "doc_class": {"type": "string", "description": "Class name for kind='by_class'"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIST_LIMIT},
                },
                "required": ["kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": (
                "Execute Python code in a resource-limited sandbox (60s CPU, 1GB "
                "RAM, 120s wall). stdout/stderr are returned. Save every output "
                "file into the ./out directory — it is stored and automatically "
                "attached to your answer as a downloadable artifact with an "
                "in-app preview (HTML, PPTX, XLSX, DOCX, PDF, CSV, PNG, JSON…). "
                "python-pptx, openpyxl, python-docx, pillow and pymupdf are "
                "preinstalled. If you need another package, install it "
                "temporarily inside the same run: "
                "subprocess.run([sys.executable, '-m', 'pip', 'install', "
                "'--target', './pkgs', '<package>']) then "
                "sys.path.insert(0, './pkgs') before importing. "
                "NEVER invent download links (no sandbox:/ or /mnt/data paths) — "
                "the platform attaches saved files automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to run"},
                },
                "required": ["code"],
            },
        },
    },
]

_CODE_TOOLS = {"execute_python"}


def openai_tool_schemas(capabilities: tuple[str, ...] | None = None) -> list[dict]:
    from ...config import settings

    caps = set(capabilities or ())
    sandbox_ok = settings.ask_ai_sandbox_enabled
    return [
        t for t in _OPENAI_SCHEMAS
        if t["function"]["name"] not in _CODE_TOOLS or ("code" in caps and sandbox_ok)
    ]


def anthropic_tool_schemas(capabilities: tuple[str, ...] | None = None) -> list[dict]:
    return [
        {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "input_schema": t["function"]["parameters"],
        }
        for t in openai_tool_schemas(capabilities)
    ]


class ToolContext:
    """Authorized tool executor bound to one /ask request."""

    def __init__(
        self,
        db: Session,
        user_id: int,
        allowed_ids: set[int] | None,
        active_scope: ActiveScope | None = None,
        mongo_db=None,
        conversation_id: str | None = None,
    ) -> None:
        self._db = db
        self._user_id = user_id
        self._mongo_db = mongo_db
        self._conversation_id = conversation_id
        self._lock = asyncio.Lock()
        # Apply the conversation scope exactly like the company KB agent:
        # scoped ids are always intersected with the caller's authorized set.
        if active_scope is not None and not active_scope.is_empty():
            scoped = active_scope.all_document_ids()
            self._allowed_ids: set[int] | None = (
                scoped & allowed_ids if allowed_ids is not None else scoped
            )
        else:
            self._allowed_ids = allowed_ids

    async def run(self, name: str, raw_args: str, provider: str = "") -> dict:
        """Execute one tool call; always returns a JSON-safe dict."""
        limit = _MAX_CODE_ARGS_BYTES if name == "execute_python" else _MAX_ARGS_BYTES
        if len(raw_args or "") > limit:
            return {"error": f"arguments too large (max {limit} bytes for {name})"}
        try:
            args = json.loads(raw_args) if raw_args else {}
            if not isinstance(args, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            return {"error": "invalid tool arguments"}
        if name == "execute_python":
            # CPU-bound in its own process; must not hold the DB lock.
            try:
                return await self._execute_python(args, provider)
            except Exception as exc:
                _log.warning("tool %s failed: %s", name, exc)
                return {"error": f"tool execution failed: {type(exc).__name__}"}
        async with self._lock:
            try:
                return await asyncio.to_thread(self._dispatch, name, args)
            except Exception as exc:
                _log.warning("tool %s failed: %s", name, exc)
                return {"error": f"tool execution failed: {type(exc).__name__}"}

    async def _execute_python(self, args: dict, provider: str = "") -> dict:
        from ...config import settings
        from ...repositories import ask_ai_repository
        from . import artifact_store, sandbox_executor

        if not settings.ask_ai_sandbox_enabled:
            return {"error": "the python sandbox is disabled by the administrator"}
        cap = settings.ask_ai_daily_sandbox_limit
        if cap > 0 and self._mongo_db is not None:
            usage = await ask_ai_repository.get_daily_usage(self._mongo_db, self._user_id)
            if usage["sandbox_execs"] >= cap:
                return {"error": f"daily sandbox execution limit reached ({cap}/day)"}
        code = str(args.get("code", ""))
        if not code.strip():
            return {"error": "code is required"}
        result = await asyncio.to_thread(sandbox_executor.run_python, code)
        if self._mongo_db is not None:
            await ask_ai_repository.add_daily_usage(self._mongo_db, self._user_id, sandbox_execs=1)
        artifacts_meta: list[dict] = []
        for a in result.artifacts:
            meta = await artifact_store.save_artifact(
                self._mongo_db,
                user_id=self._user_id,
                conversation_id=self._conversation_id,
                provider=provider,
                src_path=a["path"],
                name=a["name"],
            )
            if meta:
                artifacts_meta.append(meta)
        payload: dict = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
        if result.timed_out:
            payload["timed_out"] = True
        if artifacts_meta:
            payload["artifacts_saved"] = [
                {"name": m["name"], "artifact_id": m["id"]} for m in artifacts_meta
            ]
            payload["_artifacts"] = artifacts_meta  # stripped before model sees it
        if result.exit_code != 0 and not result.stdout:
            payload["error"] = result.stderr[:400] or "execution failed"
        return payload

    # ── sync dispatch (runs in worker thread, serialized by the lock) ──

    def _dispatch(self, name: str, args: dict) -> dict:
        if name == "search_documents":
            return self._search_documents(args)
        if name == "list_documents":
            return self._list_documents(args)
        return {"error": f"unknown tool: {name}"}

    def _search_documents(self, args: dict) -> dict:
        from ..search_service import search_with_documents

        query = str(args.get("query", "")).strip()[:500]
        if not query:
            return {"error": "query is required"}
        limit = min(int(args.get("limit") or 6), _MAX_SEARCH_LIMIT)
        _, hydrated = search_with_documents(self._db, query, self._allowed_ids, limit=limit)
        passages = [
            {
                "document_id": h["document_id"],
                "title": h["title"],
                "doc_class": h.get("doc_class"),
                "snippet": (h.get("snippet") or "")[:600],
            }
            for h in hydrated[:limit]
        ]
        return {"passages": passages, "count": len(passages)}

    def _list_documents(self, args: dict) -> dict:
        from ... import models

        kind = str(args.get("kind", "recent"))
        doc_class = str(args.get("doc_class", "")).strip()
        limit = min(int(args.get("limit") or 15), _MAX_LIST_LIMIT)
        q = self._db.query(models.Document).filter(models.Document.lifecycle_state != "DELETED")
        if self._allowed_ids is not None:
            if not self._allowed_ids:
                return {"documents": [], "count": 0}
            q = q.filter(models.Document.id.in_(self._allowed_ids))
        if kind == "by_class" or doc_class:
            if not doc_class and kind == "by_class":
                return {"error": "doc_class is required for kind='by_class'"}
            if doc_class:
                q = q.join(models.DocClass, models.Document.class_id == models.DocClass.id).filter(
                    models.DocClass.name.ilike(doc_class)
                )
        rows = q.order_by(models.Document.created_at.desc()).limit(limit).all()
        documents = [
            {
                "document_id": d.id,
                "title": d.title,
                "doc_class": d.doc_class.name if d.doc_class else None,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in rows
        ]
        return {"documents": documents, "count": len(documents)}


def summarize_result(name: str, result: dict) -> str:
    """Short human summary for the card's tool timeline."""
    if "error" in result:
        return f"error: {result['error']}"
    if name == "search_documents":
        return f"{result.get('count', 0)} passages"
    if name == "list_documents":
        return f"{result.get('count', 0)} documents"
    if name == "execute_python":
        n = len(result.get("artifacts_saved", []))
        base = "timed out" if result.get("timed_out") else f"exit {result.get('exit_code', 0)}"
        return f"{base} · {n} artifact{'s' if n != 1 else ''}" if n else base
    return "done"


def clip_result_json(result: dict) -> str:
    payload = json.dumps({k: v for k, v in result.items() if k != "_artifacts"}, default=str)
    return payload[:_MAX_RESULT_CHARS]
