#!/usr/bin/env python3
"""Create and verify a read-only DocVault WP-00 baseline.

The source SQLite database is opened in read-only/query-only mode and copied with
SQLite's online backup API. Application imports run only in child processes whose
database, storage, provider, vector, and model settings point to isolated paths.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select, table
from sqlalchemy.dialects import sqlite
from sqlalchemy.sql.elements import quoted_name

SCRIPT_PATH = Path(__file__).resolve()
BACKEND_DIR = SCRIPT_PATH.parents[1]
PROJECT_ROOT = SCRIPT_PATH.parents[2]

PUBLIC_TABLES = (
    "users",
    "roles",
    "permissions",
    "role_permissions",
    "assignments",
    "cabinets",
    "folders",
    "doc_classes",
    "documents",
    "doc_versions",
    "doc_metadata",
    "dup_groups",
    "dup_members",
    "audit_log",
    "doc_fts",
)

RUNTIME_PACKAGES = (
    "alembic",
    "fastapi",
    "uvicorn",
    "gunicorn",
    "sqlalchemy",
    "pydantic",
    "pydantic-settings",
    "python-multipart",
    "PyJWT",
    "passlib",
    "docling",
    "pytesseract",
    "Pillow",
    "pypdf",
    "PyMuPDF",
    "python-docx",
    "openpyxl",
    "python-pptx",
    "FlagEmbedding",
    "sentence-transformers",
    "qdrant-client",
    "anthropic",
)

SENSITIVE_SETTING_FRAGMENTS = ("secret", "password", "token", "api_key", "credential")


class BaselineError(RuntimeError):
    """Raised when the evidence set cannot be proven safe or consistent."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def relative_to_project(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def stat_signature(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    item = path.stat()
    return {
        "path": relative_to_project(path),
        "size": item.st_size,
        "mtime_ns": item.st_mtime_ns,
        "mode": oct(item.st_mode & 0o777),
        "inode": item.st_ino,
    }


def regular_files(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        raise BaselineError(f"required directory is missing: {root}")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BaselineError(f"symlink is not allowed in evidence source: {path}")
        if path.is_file():
            files.append(path)
    return files


def tree_manifest(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in regular_files(root):
        item = path.stat()
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": item.st_size,
                "mtime_ns": item.st_mtime_ns,
                "mode": oct(item.st_mode & 0o777),
                "sha256": sha256_file(path),
            }
        )
    return entries


def copy_tree_verified(source: Path, destination: Path) -> list[dict[str, Any]]:
    if destination.exists():
        raise BaselineError(f"backup destination already exists: {destination}")
    destination.mkdir(parents=True, mode=0o700)
    before = tree_manifest(source)
    for entry in before:
        source_file = source / entry["path"]
        destination_file = destination / entry["path"]
        destination_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source_file, destination_file)
        destination_file.chmod(0o600)
        if sha256_file(destination_file) != entry["sha256"]:
            raise BaselineError(f"copied file checksum mismatch: {entry['path']}")
    after = tree_manifest(source)
    if before != after:
        raise BaselineError(f"source tree changed during snapshot: {source}")
    return before


def readonly_connection(database: Path) -> sqlite3.Connection:
    uri = f"{database.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def sqlite_online_backup(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        raise BaselineError(
            f"database backup destination already exists: {destination}"
        )
    with closing(readonly_connection(source)) as source_connection:
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection, pages=128)
            quick_check = [
                row[0] for row in destination_connection.execute("PRAGMA quick_check")
            ]
            foreign_keys = [
                list(row)
                for row in destination_connection.execute("PRAGMA foreign_key_check")
            ]
        finally:
            destination_connection.close()
    destination.chmod(0o600)
    if quick_check != ["ok"]:
        raise BaselineError(f"backup SQLite quick_check failed: {quick_check}")
    return {
        "path": relative_to_project(destination),
        "size": destination.stat().st_size,
        "sha256": sha256_file(destination),
        "quick_check": quick_check,
        "foreign_key_violations": foreign_keys,
    }


def sqlite_table_count(
    connection: sqlite3.Connection,
    table_name: str,
    *,
    known_table_names: frozenset[str],
) -> int:
    """Count a discovered SQLite table without interpolating its identifier."""
    if table_name not in known_table_names:
        raise BaselineError("table count requested for an undiscovered table")
    relation = table(quoted_name(table_name, quote=True))
    statement = select(func.count()).select_from(relation)
    sql = str(statement.compile(dialect=sqlite.dialect()))
    row = connection.execute(sql).fetchone()
    if row is None:
        raise BaselineError(f"table count returned no row: {table_name!r}")
    return int(row[0])


def database_report(database: Path) -> tuple[dict[str, Any], str]:
    with closing(readonly_connection(database)) as connection:
        quick_check = [row[0] for row in connection.execute("PRAGMA quick_check")]
        integrity_check = [
            row[0] for row in connection.execute("PRAGMA integrity_check")
        ]
        foreign_keys = [
            {
                "table": row[0],
                "rowid": row[1],
                "parent": row[2],
                "fkid": row[3],
            }
            for row in connection.execute("PRAGMA foreign_key_check")
        ]
        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        known_table_names = frozenset(table_names)
        table_counts = {
            table_name: sqlite_table_count(
                connection,
                table_name,
                known_table_names=known_table_names,
            )
            for table_name in table_names
        }

        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        schema = (
            "\n\n".join(
                f"-- {row['type']} {row['name']} on {row['tbl_name']}\n"
                f"{row['sql'].rstrip(';')};"
                for row in schema_rows
            )
            + "\n"
        )

        document_status: dict[str, int] = {}
        ocr_status: dict[str, int] = {}
        if "documents" in table_names:
            document_status = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT status, count(*) FROM documents GROUP BY status ORDER BY status"
                )
            }
            ocr_status = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT ocr_status, count(*) FROM documents "
                    "GROUP BY ocr_status ORDER BY ocr_status"
                )
            }

        report = {
            "database": relative_to_project(database),
            "captured_at": utc_now(),
            "sqlite_library_version": sqlite3.sqlite_version,
            "quick_check": quick_check,
            "integrity_check": integrity_check,
            "foreign_key_violations": foreign_keys,
            "pragma": {
                "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                "page_count": connection.execute("PRAGMA page_count").fetchone()[0],
                "freelist_count": connection.execute(
                    "PRAGMA freelist_count"
                ).fetchone()[0],
                "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
                "application_id": connection.execute(
                    "PRAGMA application_id"
                ).fetchone()[0],
            },
            "table_counts": table_counts,
            "public_table_counts": {
                table: table_counts.get(table, 0) for table in PUBLIC_TABLES
            },
            "document_status_counts": document_status,
            "ocr_status_counts": ocr_status,
            "schema_sha256": hashlib.sha256(schema.encode("utf-8")).hexdigest(),
        }
    return report, schema


def safe_storage_path(root: Path, key: str) -> Path | None:
    relative = Path(key)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    root_resolved = root.resolve()
    candidate = (root_resolved / relative).resolve()
    if not candidate.is_relative_to(root_resolved):
        return None
    return candidate


def reconciliation_report(
    database: Path,
    storage: Path,
    *,
    vector_state: dict[str, Any],
) -> dict[str, Any]:
    storage_files = tree_manifest(storage)
    storage_by_key = {entry["path"]: entry for entry in storage_files}

    with closing(readonly_connection(database)) as connection:
        documents = {
            int(row[0])
            for row in connection.execute("SELECT id FROM documents ORDER BY id")
        }
        versions = [
            dict(row)
            for row in connection.execute(
                "SELECT id, document_id, version_no, file_key, size, checksum "
                "FROM doc_versions ORDER BY document_id, version_no, id"
            )
        ]

        referenced_keys: dict[str, list[int]] = {}
        invalid_keys: list[dict[str, Any]] = []
        missing_files: list[dict[str, Any]] = []
        size_mismatches: list[dict[str, Any]] = []
        checksum_mismatches: list[dict[str, Any]] = []

        for version in versions:
            key = str(version["file_key"])
            referenced_keys.setdefault(key, []).append(int(version["id"]))
            resolved = safe_storage_path(storage, key)
            if resolved is None:
                invalid_keys.append(
                    {
                        "document_id": version["document_id"],
                        "version_id": version["id"],
                        "file_key": key,
                    }
                )
                continue
            actual = storage_by_key.get(key)
            if actual is None:
                missing_files.append(
                    {
                        "document_id": version["document_id"],
                        "version_id": version["id"],
                        "file_key": key,
                    }
                )
                continue
            if int(version["size"]) != int(actual["size"]):
                size_mismatches.append(
                    {
                        "document_id": version["document_id"],
                        "version_id": version["id"],
                        "file_key": key,
                        "database_size": version["size"],
                        "file_size": actual["size"],
                    }
                )
            if str(version["checksum"]).lower() != str(actual["sha256"]).lower():
                checksum_mismatches.append(
                    {
                        "document_id": version["document_id"],
                        "version_id": version["id"],
                        "file_key": key,
                        "database_checksum": version["checksum"],
                        "file_checksum": actual["sha256"],
                    }
                )

        duplicate_file_keys = [
            {"file_key": key, "version_ids": ids}
            for key, ids in sorted(referenced_keys.items())
            if len(ids) > 1
        ]
        orphan_files = sorted(set(storage_by_key) - set(referenced_keys))

        documents_without_versions = [
            int(row[0])
            for row in connection.execute(
                "SELECT d.id FROM documents d "
                "LEFT JOIN doc_versions v ON v.document_id=d.id "
                "WHERE v.id IS NULL ORDER BY d.id"
            )
        ]

        current_hash_mismatches = [
            {
                "document_id": int(row[0]),
                "version_id": int(row[2]) if row[2] is not None else None,
            }
            for row in connection.execute(
                "SELECT d.id, d.content_hash, v.id, v.checksum "
                "FROM documents d "
                "LEFT JOIN doc_versions v ON v.id=("
                "  SELECT v2.id FROM doc_versions v2 "
                "  WHERE v2.document_id=d.id "
                "  ORDER BY v2.version_no DESC, v2.id DESC LIMIT 1"
                ") "
                "WHERE v.id IS NULL OR lower(d.content_hash) != lower(v.checksum) "
                "ORDER BY d.id"
            )
        ]

        fts_counts: dict[int, int] = {}
        invalid_fts_document_ids: list[str] = []
        for row in connection.execute(
            "SELECT document_id, count(*) FROM doc_fts "
            "GROUP BY document_id ORDER BY document_id"
        ):
            try:
                document_id = int(row[0])
            except (TypeError, ValueError):
                invalid_fts_document_ids.append(str(row[0]))
                continue
            fts_counts[document_id] = int(row[1])

        fts_ids = set(fts_counts)
        fts_missing_documents = sorted(documents - fts_ids)
        fts_extra_documents = sorted(fts_ids - documents)
        fts_duplicate_documents = {
            str(document_id): count
            for document_id, count in sorted(fts_counts.items())
            if count != 1
        }

        fts_title_mismatches: set[int] = set()
        fts_content_mismatches: set[int] = set()
        for row in connection.execute(
            "SELECT d.id, d.title AS document_title, "
            "       f.title AS fts_title, f.content AS fts_content, "
            "       v.ocr_text AS version_content "
            "FROM documents d "
            "LEFT JOIN doc_versions v ON v.id=("
            "  SELECT v2.id FROM doc_versions v2 "
            "  WHERE v2.document_id=d.id "
            "  ORDER BY v2.version_no DESC, v2.id DESC LIMIT 1"
            ") "
            "LEFT JOIN doc_fts f ON CAST(f.document_id AS INTEGER)=d.id "
            "ORDER BY d.id"
        ):
            document_id = int(row["id"])
            if row["fts_title"] is None:
                continue
            if str(row["document_title"]) != str(row["fts_title"]):
                fts_title_mismatches.add(document_id)
            version_content = row["version_content"] or ""
            fts_content = row["fts_content"] or ""
            if (
                hashlib.sha256(version_content.encode("utf-8")).digest()
                != hashlib.sha256(fts_content.encode("utf-8")).digest()
            ):
                fts_content_mismatches.add(document_id)

    blocking_findings = {
        "invalid_file_keys": len(invalid_keys),
        "missing_files": len(missing_files),
        "size_mismatches": len(size_mismatches),
        "checksum_mismatches": len(checksum_mismatches),
        "orphan_files": len(orphan_files),
        "documents_without_versions": len(documents_without_versions),
        "current_hash_mismatches": len(current_hash_mismatches),
        "fts_missing_documents": len(fts_missing_documents),
        "fts_extra_documents": len(fts_extra_documents),
        "fts_duplicate_documents": len(fts_duplicate_documents),
        "fts_title_mismatches": len(fts_title_mismatches),
        "fts_content_mismatches": len(fts_content_mismatches),
        "vector_state_unavailable": 0 if vector_state.get("assessed") else 1,
    }
    return {
        "captured_at": utc_now(),
        "database": relative_to_project(database),
        "storage": relative_to_project(storage),
        "summary": {
            "documents": len(documents),
            "versions": len(versions),
            "referenced_file_keys": len(referenced_keys),
            "stored_files": len(storage_files),
            "stored_bytes": sum(int(entry["size"]) for entry in storage_files),
            "fts_document_ids": len(fts_ids),
            "clean": not any(blocking_findings.values()),
        },
        "blocking_finding_counts": blocking_findings,
        "database_to_storage": {
            "invalid_file_keys": invalid_keys,
            "missing_files": missing_files,
            "size_mismatches": size_mismatches,
            "checksum_mismatches": checksum_mismatches,
            "duplicate_file_keys": duplicate_file_keys,
            "orphan_files": orphan_files,
            "documents_without_versions": documents_without_versions,
            "current_hash_mismatches": current_hash_mismatches,
        },
        "fts": {
            "missing_document_ids": fts_missing_documents,
            "extra_document_ids": fts_extra_documents,
            "duplicate_document_ids": fts_duplicate_documents,
            "invalid_document_ids": invalid_fts_document_ids,
            "title_mismatch_document_ids": sorted(fts_title_mismatches),
            "content_mismatch_document_ids": sorted(fts_content_mismatches),
        },
        "vector": vector_state,
    }


def reconciliation_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    findings = report["blocking_finding_counts"]
    storage = report["database_to_storage"]
    fts = report["fts"]
    vector = report["vector"]

    lines = [
        "# Read-only database, storage, FTS, and vector reconciliation",
        "",
        f"Captured: {report['captured_at']}",
        "",
        "## Summary",
        "",
        f"- Documents: {summary['documents']}",
        f"- Document versions: {summary['versions']}",
        f"- Referenced file keys: {summary['referenced_file_keys']}",
        f"- Stored files: {summary['stored_files']}",
        f"- Stored bytes: {summary['stored_bytes']}",
        f"- FTS document IDs: {summary['fts_document_ids']}",
        f"- Clean: {str(summary['clean']).lower()}",
        "",
        "## Finding counts",
        "",
    ]
    lines.extend(f"- {name}: {count}" for name, count in findings.items())
    lines.extend(
        [
            "",
            "## Preserved drift identifiers",
            "",
            "- Missing database-referenced files: "
            + json.dumps(storage["missing_files"], sort_keys=True),
            "- Orphan storage files: " + json.dumps(storage["orphan_files"]),
            "- FTS-missing document IDs: " + json.dumps(fts["missing_document_ids"]),
            "- FTS-extra document IDs: " + json.dumps(fts["extra_document_ids"]),
            "- FTS content-mismatch document IDs: "
            + json.dumps(fts["content_mismatch_document_ids"]),
            "",
            "## Vector state",
            "",
            f"- Assessed: {str(bool(vector.get('assessed'))).lower()}",
            f"- Status: {vector.get('status', 'unknown')}",
            f"- Explanation: {vector.get('explanation', '')}",
            "",
            "No item in this report was repaired, deleted, reindexed, or altered.",
            "",
        ]
    )
    return "\n".join(lines)


def settings_shape(config_path: Path) -> dict[str, Any]:
    tree = ast.parse(config_path.read_text(encoding="utf-8"), filename=str(config_path))
    fields: list[dict[str, Any]] = []
    settings_class = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Settings"
        ),
        None,
    )
    if settings_class is None:
        raise BaselineError("Settings class was not found")
    for node in settings_class.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        name = node.target.id
        if node.value is None:
            default = "<required>"
        elif any(fragment in name.lower() for fragment in SENSITIVE_SETTING_FRAGMENTS):
            default = "<redacted>"
        else:
            default = ast.unparse(node.value)
        fields.append(
            {
                "name": name,
                "annotation": ast.unparse(node.annotation),
                "default_expression": default,
                "environment_variable": f"DOCVAULT_{name.upper()}",
            }
        )
    return {
        "captured_at": utc_now(),
        "source": relative_to_project(config_path),
        "environment_prefix": "DOCVAULT_",
        "secret_values_included": False,
        "fields": fields,
    }


def selected_code_manifest() -> list[dict[str, Any]]:
    paths: set[Path] = set()
    paths.update((BACKEND_DIR / "app").rglob("*.py"))
    paths.update((BACKEND_DIR / "tests").rglob("*.py"))
    for path in (
        BACKEND_DIR / "requirements.txt",
        BACKEND_DIR / "Dockerfile",
        BACKEND_DIR / "entrypoint.sh",
        BACKEND_DIR / "start.sh",
        PROJECT_ROOT / "docker-compose.yml",
        PROJECT_ROOT / "docker-compose.dev.yml",
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "PROJECT_GUIDE.md",
    ):
        if path.exists():
            paths.add(path)
    entries: list[dict[str, Any]] = []
    for path in sorted(paths):
        if not path.is_file():
            continue
        entries.append(
            {
                "path": relative_to_project(path),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return entries


def command_version(arguments: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            arguments,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        output = (result.stdout or result.stderr).strip().splitlines()
        return {
            "command": arguments,
            "exit_code": result.returncode,
            "first_line": output[0] if output else "",
        }
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"command": arguments, "error": type(exc).__name__}


def runtime_report(quiescent_confirmed: bool) -> dict[str, Any]:
    packages: dict[str, str] = {}
    for package in RUNTIME_PACKAGES:
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = "not_installed"

    git_root = command_version(["git", "rev-parse", "--show-toplevel"])
    tracked_probe = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "ls-files", "--error-unmatch", "README.md"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return {
        "captured_at": utc_now(),
        "quiescent_confirmed": quiescent_confirmed,
        "python": {
            "version": sys.version,
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
        },
        "platform": platform.platform(),
        "sqlite": {
            "library_version": sqlite3.sqlite_version,
        },
        "packages": packages,
        "commands": {
            "git": command_version(["git", "--version"]),
            "docker": command_version(["docker", "--version"]),
            "sqlite3": command_version(["sqlite3", "--version"]),
            "tesseract": command_version(["tesseract", "--version"]),
            "uv": command_version(["uv", "--version"]),
        },
        "source_control": {
            "detected_git_root": git_root,
            "workspace_readme_is_tracked": tracked_probe.returncode == 0,
            "note": (
                "The workspace is below a parent Git root and is not currently tracked; "
                "the code manifest is the baseline source fingerprint."
            ),
        },
    }


def isolated_environment(database: Path, storage: Path, okf: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "DOCVAULT_DATABASE_URL": f"sqlite:///{database.resolve()}",
            "DOCVAULT_STORAGE_DIR": str(storage.resolve()),
            "DOCVAULT_OKF_BUNDLE_DIR": str(okf.resolve()),
            "DOCVAULT_SECRET_KEY": "baseline-isolation-only-not-production",
            "DOCVAULT_LLM_PROVIDER": "none",
            "DOCVAULT_ANTHROPIC_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            "OPENAI_API_KEY": "",
            "HF_TOKEN": "",
            "HUGGING_FACE_HUB_TOKEN": "",
            "DOCVAULT_VLLM_URL": "http://127.0.0.1:9/v1",
            "DOCVAULT_OLLAMA_URL": "http://127.0.0.1:9",
            "DOCVAULT_USE_QDRANT": "false",
            "DOCVAULT_QDRANT_URL": "http://127.0.0.1:9",
            "DOCVAULT_EMBEDDING_MODEL": "",
            "DOCVAULT_RERANKER_MODEL": "",
            "DOCVAULT_USE_DOCLING": "false",
        }
    )
    return environment


def generate_openapi(output_path: Path, isolation: Path) -> dict[str, Any]:
    isolation.mkdir(parents=True, mode=0o700)
    database = isolation / "openapi.db"
    storage = isolation / "storage"
    okf = isolation / "okf_bundle"
    storage.mkdir(mode=0o700)
    okf.mkdir(mode=0o700)
    child_code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from app.main import app\n"
        "Path(sys.argv[1]).write_text("
        "json.dumps(app.openapi(), indent=2, sort_keys=True) + '\\n', encoding='utf-8')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", child_code, str(output_path)],
        cwd=BACKEND_DIR,
        env=isolated_environment(database, storage, okf),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0 or not output_path.exists():
        raise BaselineError(
            "isolated OpenAPI generation failed: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    output_path.chmod(0o600)
    document = json.loads(output_path.read_text(encoding="utf-8"))
    return {
        "path": relative_to_project(output_path),
        "sha256": sha256_file(output_path),
        "title": document.get("info", {}).get("title"),
        "version": document.get("info", {}).get("version"),
        "path_count": len(document.get("paths", {})),
        "isolated": True,
        "source_database_touched": False,
    }


def api_read_smoke(
    database: Path,
    storage: Path,
    okf: Path,
    result_path: Path,
) -> dict[str, Any]:
    with closing(readonly_connection(database)) as connection:
        username_row = connection.execute(
            "SELECT username FROM users WHERE status='active' ORDER BY id LIMIT 1"
        ).fetchone()
    if username_row is None:
        raise BaselineError("restored database has no active user for API read smoke")
    username = str(username_row[0])

    progress_path = result_path.with_suffix(".progress.log")
    child_code = (
        "import asyncio, faulthandler, json, os, sys\n"
        "from pathlib import Path\n"
        "progress = Path(sys.argv[2])\n"
        "def mark(value):\n"
        "    progress.write_text(value + '\\n', encoding='utf-8')\n"
        "mark('child-started')\n"
        "from httpx import ASGITransport, AsyncClient\n"
        "mark('asgi-client-imported')\n"
        "from app.main import app\n"
        "mark('application-imported')\n"
        "from app.utils.security import create_access_token\n"
        "token = create_access_token(os.environ['BASELINE_SMOKE_USERNAME'])\n"
        "mark('token-created')\n"
        "async def smoke():\n"
        "    transport = ASGITransport(app=app, raise_app_exceptions=True)\n"
        "    async with AsyncClient(transport=transport, base_url='http://baseline') as client:\n"
        "        response = await client.get('/api/v1/documents?limit=1', "
        "headers={'Authorization': f'Bearer {token}'})\n"
        "        mark('response-received')\n"
        "        payload = response.json() if response.headers.get('content-type', '')."
        "startswith('application/json') else None\n"
        "        return {'status_code': response.status_code, "
        "'item_count': len(payload) if isinstance(payload, list) else None, "
        "'response_is_list': isinstance(payload, list)}\n"
        "faulthandler.dump_traceback_later(30, repeat=False)\n"
        "result = asyncio.run(asyncio.wait_for(smoke(), timeout=45))\n"
        "faulthandler.cancel_dump_traceback_later()\n"
        "Path(sys.argv[1]).write_text("
        "json.dumps(result, indent=2, sort_keys=True) + '\\n', encoding='utf-8')\n"
        "mark('complete')\n"
    )
    environment = isolated_environment(database, storage, okf)
    environment["BASELINE_SMOKE_USERNAME"] = username
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                child_code,
                str(result_path),
                str(progress_path),
            ],
            cwd=BACKEND_DIR,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        progress = (
            progress_path.read_text(encoding="utf-8").strip()
            if progress_path.exists()
            else "not-started"
        )
        raise BaselineError(
            f"isolated restored API read exceeded 180 seconds; progress={progress}"
        ) from exc
    if result.returncode != 0 or not result_path.exists():
        raise BaselineError(
            "isolated restored API read failed: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    result_path.chmod(0o600)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if payload.get("status_code") != 200 or not payload.get("response_is_list"):
        raise BaselineError(f"restored API read smoke failed: {payload}")
    return payload


def manifest_for_directory(
    root: Path,
    *,
    exclude: Iterable[Path] = (),
) -> list[dict[str, Any]]:
    excluded = {path.resolve() for path in exclude}
    entries: list[dict[str, Any]] = []
    for path in regular_files(root):
        if path.resolve() in excluded:
            continue
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return entries


def make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(0o400)
        elif path.is_dir():
            path.chmod(0o500)
    root.chmod(0o500)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New timestamped artifact directory; it must not already exist.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=BACKEND_DIR / "docvault.db",
    )
    parser.add_argument(
        "--storage",
        type=Path,
        default=BACKEND_DIR / "storage",
    )
    parser.add_argument(
        "--okf",
        type=Path,
        default=BACKEND_DIR / "okf_bundle",
    )
    parser.add_argument(
        "--quiescent-confirmed",
        action="store_true",
        help="Required acknowledgement that no DocVault web/worker/container is running.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.quiescent_confirmed:
        raise BaselineError(
            "refusing snapshot without --quiescent-confirmed; verify DocVault is stopped"
        )

    database = args.database.resolve()
    storage = args.storage.resolve()
    okf = args.okf.resolve()
    output = args.output.resolve()

    if not database.is_file():
        raise BaselineError(f"source database is missing: {database}")
    for source_root in (storage, okf):
        if not source_root.is_dir():
            raise BaselineError(f"source directory is missing: {source_root}")
        if output.is_relative_to(source_root):
            raise BaselineError("output must not be inside a source data directory")
    if output.exists():
        raise BaselineError(f"output already exists: {output}")

    old_umask = os.umask(0o077)
    output.mkdir(parents=True, mode=0o700)
    try:
        started_at = utc_now()
        authoritative_source_files = [database, Path(str(database) + "-wal")]
        transient_source_files = [Path(str(database) + "-shm")]
        source_signatures_before = {
            path.name: stat_signature(path) for path in authoritative_source_files
        }
        transient_signatures_before = {
            path.name: stat_signature(path) for path in transient_source_files
        }

        write_json(output / "runtime.json", runtime_report(args.quiescent_confirmed))
        write_json(
            output / "settings-shape.json",
            settings_shape(BACKEND_DIR / "app" / "config.py"),
        )
        write_json(output / "code-manifest.json", selected_code_manifest())

        openapi = generate_openapi(
            output / "openapi.json",
            output / "isolation" / "openapi",
        )
        write_json(output / "openapi-generation.json", openapi)

        source_database_report, schema = database_report(database)
        write_json(output / "source-database-summary.json", source_database_report)
        write_text(output / "database-schema.sql", schema)

        vector_state = {
            "configured_in_source": True,
            "assessed": False,
            "status": "no_persistent_docvault_vector_store_found",
            "explanation": (
                "No DocVault/Qdrant container was running and no Docker volume named for "
                "DocVault or DDMS was present at snapshot time. Vector writes are "
                "best-effort in the current code, so derived vector coverage cannot be "
                "proven and must be rebuilt/reconciled before release."
            ),
        }
        source_reconciliation = reconciliation_report(
            database,
            storage,
            vector_state=vector_state,
        )
        write_json(output / "reconciliation.json", source_reconciliation)
        write_text(
            output / "RECONCILIATION.md",
            reconciliation_markdown(source_reconciliation),
        )

        backup_database = output / "backup" / "database" / "docvault.db"
        database_backup = sqlite_online_backup(database, backup_database)
        storage_backup = copy_tree_verified(storage, output / "backup" / "storage")
        okf_backup = copy_tree_verified(okf, output / "backup" / "okf_bundle")
        write_json(
            output / "backup-manifest.json",
            {
                "created_at": utc_now(),
                "method": "SQLite online backup API plus verified file copy",
                "source_quiescent_confirmed": True,
                "database": database_backup,
                "storage": storage_backup,
                "okf_bundle": okf_backup,
            },
        )

        backup_database_report, _ = database_report(backup_database)
        write_json(output / "backup-database-summary.json", backup_database_report)

        restore = output / "restore"
        shutil.copytree(output / "backup", restore, copy_function=shutil.copy2)
        backup_before_smoke = manifest_for_directory(output / "backup")
        restore_before_smoke = manifest_for_directory(restore)
        normalized_backup = [
            {key: value for key, value in entry.items() if key != "path"}
            | {"path": entry["path"]}
            for entry in backup_before_smoke
        ]
        if normalized_backup != restore_before_smoke:
            raise BaselineError("restored files do not match the backup manifest")

        restore_database = restore / "database" / "docvault.db"
        restore_storage = restore / "storage"
        restore_okf = restore / "okf_bundle"
        restored_before_report, _ = database_report(restore_database)
        smoke_path = output / "restored-api-read.json"
        smoke = api_read_smoke(
            restore_database,
            restore_storage,
            restore_okf,
            smoke_path,
        )
        restored_after_report, _ = database_report(restore_database)
        counts_unchanged = (
            restored_before_report["table_counts"]
            == restored_after_report["table_counts"]
        )
        if not counts_unchanged:
            raise BaselineError("API read changed restored database table counts")

        restored_reconciliation = reconciliation_report(
            restore_database,
            restore_storage,
            vector_state=vector_state,
        )
        write_json(
            output / "restored-reconciliation.json",
            restored_reconciliation,
        )

        source_signatures_after = {
            path.name: stat_signature(path) for path in authoritative_source_files
        }
        transient_signatures_after = {
            path.name: stat_signature(path) for path in transient_source_files
        }
        source_stable = source_signatures_before == source_signatures_after
        if not source_stable:
            raise BaselineError(
                "source database or WAL signature changed during baseline"
            )

        database_counts_match = (
            source_database_report["table_counts"]
            == backup_database_report["table_counts"]
            == restored_before_report["table_counts"]
        )
        if not database_counts_match:
            raise BaselineError(
                "source, backup, and restored table counts do not match"
            )

        verification = {
            "verified_at": utc_now(),
            "source_signatures_before": source_signatures_before,
            "source_signatures_after": source_signatures_after,
            "source_stable": source_stable,
            "transient_shm_signatures_before": transient_signatures_before,
            "transient_shm_signatures_after": transient_signatures_after,
            "transient_shm_may_change_on_sqlite_read": True,
            "source_backup_restore_table_counts_match": database_counts_match,
            "backup_file_count": len(backup_before_smoke),
            "restore_file_count_before_smoke": len(restore_before_smoke),
            "backup_restore_checksums_match_before_smoke": True,
            "restored_sqlite_quick_check": restored_after_report["quick_check"],
            "restored_foreign_key_violation_count": len(
                restored_after_report["foreign_key_violations"]
            ),
            "restored_api_read": smoke,
            "restored_table_counts_unchanged_by_api_read": counts_unchanged,
            "reconciliation_reproduced": (
                source_reconciliation["blocking_finding_counts"]
                == restored_reconciliation["blocking_finding_counts"]
            ),
        }
        if not verification["reconciliation_reproduced"]:
            raise BaselineError(
                "restored reconciliation does not reproduce source findings"
            )
        write_json(output / "restore-verification.json", verification)

        marker = (
            "DOCVAULT SOURCE EVIDENCE — DO NOT MODIFY\n\n"
            "The source database/storage/OKF data were read only. Backup and restore "
            "payloads in this directory contain application data. Do not use this "
            "artifact as a repair target and do not commit its payload directories.\n"
        )
        write_text(output / "SOURCE-EVIDENCE-DO-NOT-MODIFY.txt", marker)

        result = {
            "status": "passed",
            "started_at": started_at,
            "completed_at": utc_now(),
            "tasks_supported": [
                "BASE-001",
                "BASE-002",
                "BASE-003",
                "BASE-004",
                "SAFE-001",
            ],
            "source_data_modified": False,
            "source_stable": source_stable,
            "backup_verified": True,
            "restore_verified": True,
            "restored_api_read_verified": True,
            "known_drift_preserved_not_repaired": True,
            "reconciliation_clean": source_reconciliation["summary"]["clean"],
            "finding_counts": source_reconciliation["blocking_finding_counts"],
        }
        write_json(output / "RESULT.json", result)

        manifest_path = output / "manifest.json"
        artifact_files = manifest_for_directory(output, exclude=(manifest_path,))
        write_json(
            manifest_path,
            {
                "created_at": utc_now(),
                "root": relative_to_project(output),
                "algorithm": "sha256",
                "self_excluded": "manifest.json",
                "file_count": len(artifact_files),
                "files": artifact_files,
            },
        )

        make_read_only(output)
        print(json.dumps(result, indent=2, sort_keys=True))
        print(f"artifact={output}")
        return 0
    except Exception as exc:
        try:
            write_text(
                output / "FAILED.txt",
                f"Baseline creation failed at {utc_now()}: {type(exc).__name__}: {exc}\n",
            )
        except Exception:
            pass
        raise
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    raise SystemExit(main())
