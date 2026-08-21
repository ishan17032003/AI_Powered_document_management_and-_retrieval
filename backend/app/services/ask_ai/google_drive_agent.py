"""Google Drive Agent for Ask AI.

Directly queries Google Drive REST API using the user's OAuth credentials stored
in MongoDB, downloads relevant documents, extracts readable text, and returns
up to 5 enriched document dicts ready for answer synthesis.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from ...config import settings
from .document_extractor import DocumentExtractor

_log = logging.getLogger(__name__)

_DRIVE_MAX_DOCS = 5  # Hard cap: maximum 5 documents ever retrieved from Google Drive


async def get_valid_access_token(drive_token: dict) -> str | None:
    """Return a valid Google access token, refreshing it if expired."""
    if not drive_token:
        return None

    access_token = drive_token.get("token")
    refresh_token = drive_token.get("refresh_token")
    client_id = drive_token.get("client_id") or settings.google_client_id
    client_secret = drive_token.get("client_secret") or settings.google_client_secret

    # Verify if access token is working
    if access_token:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if res.status_code == 200:
                    return access_token
        except Exception:
            pass

    # Refresh the token if refresh_token is present
    if refresh_token and client_id and client_secret:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
                if res.status_code == 200:
                    data = res.json()
                    new_token = data.get("access_token")
                    if new_token:
                        _log.info("Successfully refreshed Google Drive OAuth token.")
                        return new_token
                else:
                    _log.warning("Failed to refresh Google Drive token: %s", res.text)
        except Exception as exc:
            _log.warning("Exception refreshing Google Drive token: %s", exc)

    return access_token


def extract_search_keywords(question: str, hint_documents: list[str] | None = None) -> list[str]:
    """Extract search terms and phrases from user question and mentions."""
    terms: list[str] = []
    
    # 1. Cleaned full question
    clean_q = re.sub(r"[^a-zA-Z0-9\s_-]", " ", question).strip()
    if clean_q:
        terms.append(clean_q)

    # 2. Add document hints from mentions
    if hint_documents:
        for doc_name in hint_documents:
            name_no_ext = re.sub(r"\.[a-zA-Z0-9]+$", "", doc_name).strip()
            if name_no_ext and name_no_ext not in terms:
                terms.append(name_no_ext)

    # 3. Extract meaningful multi-word phrases or key nouns/verbs (remove stop words)
    stop_words = {
        "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
        "a", "an", "the", "and", "or", "but", "if", "because", "as", "until",
        "while", "of", "at", "by", "for", "with", "about", "against", "between",
        "into", "through", "during", "before", "after", "above", "below", "to",
        "from", "up", "down", "in", "out", "on", "off", "over", "under", "again",
        "further", "then", "once", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "can", "could", "should", "would",
        "get", "me", "show", "find", "list", "tell", "give", "please", "all",
    }
    words = [w for w in clean_q.split() if w.lower() not in stop_words and len(w) >= 3]
    if words:
        phrase = " ".join(words)
        if phrase not in terms:
            terms.append(phrase)
        for w in words:
            if w not in terms:
                terms.append(w)

    return terms[:6]


async def search_drive_files(
    client: httpx.AsyncClient,
    token: str,
    keywords: list[str],
    max_results: int = 50,
) -> list[dict[str, Any]]:
    """Query Google Drive API with unified keyword clauses."""
    clean_kws = [k.replace("'", "\\'").strip() for k in keywords if k.strip()]
    if not clean_kws:
        return []

    clauses = []
    for kw in clean_kws:
        clauses.append(f"(name contains '{kw}' or fullText contains '{kw}')")

    unified_query = f"({' or '.join(clauses)}) and trashed=false"
    params = {
        "q": unified_query,
        "fields": "files(id, name, mimeType, modifiedTime, webViewLink, iconLink, size)",
        "pageSize": min(100, max_results),
        "corpora": "allDrives",
        "includeItemsFromAllDrives": "true",
        "supportsAllDrives": "true",
    }

    try:
        res = await client.get(
            "https://www.googleapis.com/drive/v3/files",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
        if res.status_code == 200:
            return res.json().get("files", [])
        else:
            _log.warning("Google Drive unified search error (%s): %s", res.status_code, res.text)
    except Exception as exc:
        _log.warning("Google Drive unified search exception: %s", exc)

    # Fallback to name-only search
    try:
        name_clauses = [f"name contains '{kw}'" for kw in clean_kws]
        fallback_query = f"({' or '.join(name_clauses)}) and trashed=false"
        res = await client.get(
            "https://www.googleapis.com/drive/v3/files",
            params={
                "q": fallback_query,
                "fields": "files(id, name, mimeType, modifiedTime, webViewLink, iconLink, size)",
                "pageSize": min(100, max_results),
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )
        if res.status_code == 200:
            return res.json().get("files", [])
    except Exception as exc:
        _log.warning("Google Drive name fallback search exception: %s", exc)

    return []


async def fetch_file_content(
    client: httpx.AsyncClient,
    token: str,
    file_meta: dict[str, Any],
) -> str:
    """Download/export Google Drive file and extract plain text."""
    file_id = file_meta.get("id")
    mime_type = file_meta.get("mimeType", "")
    file_name = file_meta.get("name", "")

    if mime_type in ["application/vnd.google-apps.folder", "application/vnd.google-apps.shortcut"]:
        return f"[Folder: {file_name}]"

    headers = {"Authorization": f"Bearer {token}"}

    try:
        # Google Workspace Docs
        if mime_type == "application/vnd.google-apps.document":
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export"
            res = await client.get(url, params={"mimeType": "text/plain"}, headers=headers, timeout=20.0)
            if res.status_code == 200:
                return DocumentExtractor.extract_from_bytes(res.content, "text/plain", file_name)

        # Google Workspace Sheets
        elif mime_type == "application/vnd.google-apps.spreadsheet":
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export"
            res = await client.get(url, params={"mimeType": "text/csv"}, headers=headers, timeout=20.0)
            if res.status_code == 200:
                return DocumentExtractor.extract_from_bytes(res.content, "text/csv", file_name)

        # Google Workspace Slides
        elif mime_type == "application/vnd.google-apps.presentation":
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export"
            res = await client.get(url, params={"mimeType": "text/plain"}, headers=headers, timeout=20.0)
            if res.status_code == 200:
                return DocumentExtractor.extract_from_bytes(res.content, "text/plain", file_name)

        # Standard binary files (PDFs, DOCX, XLSX, TXT, CSV)
        else:
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
            res = await client.get(url, params={"alt": "media"}, headers=headers, timeout=25.0)
            if res.status_code == 200:
                return DocumentExtractor.extract_from_bytes(res.content, mime_type, file_name)
            else:
                return f"[Could not download {file_name}: HTTP {res.status_code}]"

    except Exception as exc:
        _log.warning("Error fetching content for file %s (%s): %s", file_name, file_id, exc)
        return f"[Error fetching content: {exc}]"

    return ""


async def run(
    question: str,
    mentions: dict,
    drive_token: dict | None = None,
) -> list[dict]:
    """Retrieve matching documents from Google Drive.

    Parameters
    ----------
    question:    The user's query.
    mentions:    Parsed mentions dict from mention_parser.parse_mentions().
    drive_token: Stored OAuth token dictionary from MongoDB.

    Returns
    -------
    List of enriched file dicts (at most 5) with content and metadata.
    """
    if not drive_token:
        _log.info("No Google Drive token provided for user.")
        return []

    token = await get_valid_access_token(drive_token)
    if not token:
        _log.warning("Could not obtain valid Google Drive access token.")
        return []

    keywords = extract_search_keywords(question, hint_documents=mentions.get("documents", []))
    _log.info("Google Drive search keywords: %s", keywords)

    async with httpx.AsyncClient(timeout=30.0) as client:
        files = await search_drive_files(client, token, keywords, max_results=50)
        if not files:
            _log.info("No files matched Google Drive query for keywords: %s", keywords)
            return []

        # Deduplicate and score matched files
        file_map: dict[str, dict[str, Any]] = {}
        file_scores: dict[str, int] = {}
        file_matched_keywords: dict[str, list[str]] = {}

        for f in files:
            fid = f.get("id")
            if not fid:
                continue
            file_map[fid] = f
            file_name_lower = (f.get("name") or "").lower()
            matched = [kw for kw in keywords if kw.lower() in file_name_lower]
            if not matched:
                matched = keywords[:2]
            file_matched_keywords[fid] = matched
            file_scores[fid] = len(matched)

        # Sort by match score and recency
        sorted_file_ids = sorted(
            file_map.keys(),
            key=lambda fid: (file_scores.get(fid, 1), file_map[fid].get("modifiedTime", "")),
            reverse=True,
        )

        # Hard cap: select at most 5 files
        selected_files = [file_map[fid] for fid in sorted_file_ids[:_DRIVE_MAX_DOCS]]

        # Fetch and extract file contents concurrently
        tasks = [fetch_file_content(client, token, f) for f in selected_files]
        contents = await asyncio.gather(*tasks)

        enriched_files = []
        for file_meta, content in zip(selected_files, contents):
            fid = file_meta["id"]
            content_lower = content.lower()
            matched = list(file_matched_keywords.get(fid, []))
            for kw in keywords:
                if kw.lower() in content_lower and kw not in matched:
                    matched.append(kw)

            enriched_files.append({
                **file_meta,
                "matched_keywords": matched if matched else keywords[:1],
                "match_score": max(1, len(matched)),
                "content": content,
            })

        _log.info("Google Drive retrieved %d enriched files.", len(enriched_files))
        return enriched_files[:_DRIVE_MAX_DOCS]
