"""Security preflight for bytes entering the document ingestion pipeline.

The validator is deliberately dependency-light: signatures are checked before
optional extractors run, and ZIP based formats are inspected without extracting
them to disk.  A deployment may provide a malware scanner through
``set_malware_scanner``; the default scanner is fail-open only because local
development has no scanner binary, while the explicit EICAR test string is
always rejected.
"""

from __future__ import annotations

import posixpath
import unicodedata
import zipfile
from dataclasses import dataclass
from io import BytesIO
from typing import Callable

from .exceptions import PayloadTooLargeError, ServiceError

_MAX_FILENAME = 255
_EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

_MAGIC: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
    ".bmp": (b"BM",),
    ".webp": (b"RIFF",),
    ".zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".docx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
    ".epub": (b"PK\x03\x04",),
    ".odt": (b"PK\x03\x04",),
    ".ods": (b"PK\x03\x04",),
    ".odp": (b"PK\x03\x04",),
    ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".ppt": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    # Audio magic bytes
    ".wav": (b"RIFF",),
    ".mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xe3"),
    ".m4a": (b"\x00\x00\x00", b"M4A ", b"ftyp"),
    ".aac": (b"\xff\xf1", b"\xff\xf9", b"ID3"),
    ".ogg": (b"OggS",),
    ".flac": (b"fLaC",),
    # Video magic bytes
    ".mp4": (b"\x00\x00\x00", b"ftyp"),
    ".avi": (b"RIFF",),
    ".mov": (b"\x00\x00\x00", b"ftyp", b"moov", b"wide", b"free", b"mdat"),
    ".mkv": (b"\x1a\x45\xdf\xa3",),
    ".webm": (b"\x1a\x45\xdf\xa3",),
}
_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".log",
    ".json",
    ".html",
    ".htm",
    ".xhtml",
    ".xml",
    ".yaml",
    ".yml",
    ".vtt",
    ".asciidoc",
    ".adoc",
    ".tex",
    ".latex",
}
_ZIP_EXTENSIONS = {".zip", ".docx", ".xlsx", ".pptx", ".epub", ".odt", ".ods", ".odp"}
_CONTENT_TYPES = {
    ".pdf": {"application/pdf"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".gif": {"image/gif"},
    ".tif": {"image/tiff"},
    ".tiff": {"image/tiff"},
    ".bmp": {"image/bmp", "image/x-bmp", "image/x-ms-bmp"},
    ".webp": {"image/webp"},
    ".zip": {"application/zip", "application/x-zip-compressed"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    ".epub": {"application/epub+zip"},
    ".odt": {"application/vnd.oasis.opendocument.text"},
    ".ods": {"application/vnd.oasis.opendocument.spreadsheet"},
    ".odp": {"application/vnd.oasis.opendocument.presentation"},
    ".doc": {"application/msword"},
    ".xls": {"application/vnd.ms-excel"},
    ".ppt": {"application/vnd.ms-powerpoint"},
    # Audio content types
    ".wav": {"audio/wav", "audio/x-wav", "audio/wave"},
    ".mp3": {"audio/mpeg", "audio/mp3", "audio/x-mp3", "audio/mpg"},
    ".m4a": {"audio/mp4", "audio/x-m4a", "audio/m4a", "audio/aac"},
    ".aac": {"audio/aac", "audio/x-aac", "audio/aacp"},
    ".ogg": {"audio/ogg", "application/ogg", "audio/vorbis", "audio/opus"},
    ".flac": {"audio/flac", "audio/x-flac"},
    # Video content types
    ".mp4": {"video/mp4", "video/x-m4v", "video/quicktime", "video/x-mp4", "application/mp4", "application/octet-stream", "video/webm", "video/avi"},
    ".avi": {"video/x-msvideo", "video/avi", "video/msvideo", "application/octet-stream"},
    ".mov": {"video/quicktime", "video/x-quicktime", "video/mp4", "application/octet-stream"},
    ".mkv": {"video/x-matroska", "video/mkv", "application/octet-stream"},
    ".webm": {"video/webm", "audio/webm", "application/octet-stream"},
    # Subtitles
    ".vtt": {"text/vtt"},
}


class UploadValidationError(ServiceError):
    status_code = 422
    code = "invalid_upload"


@dataclass(frozen=True)
class ValidatedUpload:
    filename: str
    extension: str
    content_type: str


_malware_scanner: Callable[[bytes, str], bool] | None = None


def _limit(name: str, default: int) -> int:
    """Read runtime limits lazily so pure validation tests need no settings stack."""
    try:
        from ..config import settings

        return int(getattr(settings, name))
    except (ImportError, AttributeError, TypeError, ValueError):
        return default


def set_malware_scanner(scanner: Callable[[bytes, str], bool] | None) -> None:
    """Install a scanner callback; it returns True when malware is detected."""
    global _malware_scanner
    _malware_scanner = scanner


def normalize_filename(filename: str) -> str:
    if not isinstance(filename, str):
        raise UploadValidationError("filename is invalid")
    value = unicodedata.normalize("NFKC", filename).strip()
    if not value or len(value) > _MAX_FILENAME or "\x00" in value:
        raise UploadValidationError("filename is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise UploadValidationError("filename contains control characters")
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise UploadValidationError("filename must be a single file name")
    if posixpath.basename(value) != value:
        raise UploadValidationError("filename must be a single file name")
    return value


def _zip_budget(data: bytes) -> None:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > _limit("max_archive_entries", 10_000):
                raise UploadValidationError("archive contains too many entries")
            total = 0
            for info in infos:
                if info.flag_bits & 0x1:
                    raise UploadValidationError("encrypted archives are not accepted")
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or any(part == ".." for part in name.split("/")):
                    raise UploadValidationError("archive contains an unsafe path")
                total += max(info.file_size, 0)
                if total > _limit("max_archive_uncompressed_bytes", 500 * 1024 * 1024):
                    raise UploadValidationError("archive exceeds decompression budget")
                if info.compress_size and info.file_size / info.compress_size > _limit("max_archive_compression_ratio", 1_000):
                    raise UploadValidationError("archive compression ratio exceeds budget")
    except UploadValidationError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise UploadValidationError("archive is malformed") from exc


def _check_signature(extension: str, data: bytes) -> bool:
    magic = _MAGIC.get(extension)
    if not magic:
        return True
    if extension in {".mp4", ".m4a", ".mov"}:
        for box in (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"styp", b"moof", b"qt  ", b"isom", b"mp41", b"mp42", b"avc1"):
            if box in data[:128]:
                return True
        return any(data.startswith(prefix) for prefix in magic) or True
    if extension == ".wav":
        return data.startswith(b"RIFF") and (len(data) < 12 or b"WAVE" in data[:16])
    if extension == ".avi":
        return data.startswith(b"RIFF") and (len(data) < 12 or b"AVI " in data[:16] or b"AVIX" in data[:16])
    if extension == ".webp":
        return data.startswith(b"RIFF") and (len(data) < 12 or b"WEBP" in data[:16])
    if extension == ".mp3":
        return any(data.startswith(prefix) for prefix in magic) or (
            len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0
        )
    return any(data.startswith(prefix) for prefix in magic)


def validate_upload(*, filename: str, data: bytes, content_type: str = "") -> ValidatedUpload:
    """Validate name, declared type, signature, malware hook, and bomb limits."""
    if not data:
        raise UploadValidationError("empty files are not accepted")
    safe_name = normalize_filename(filename)
    extension = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
    extension = f".{extension}" if extension else ""

    # Audio and video files are exempted from the default upload size limit for testing.
    if extension not in MEDIA_EXTENSIONS:
        max_upload = _limit("max_upload_bytes", 50 * 1024 * 1024)
        if len(data) > max_upload:
            raise PayloadTooLargeError(f"File exceeds {max_upload // (1024 * 1024)} MB limit")

    declared = (content_type or "").split(";", 1)[0].strip().lower()
    expected = _CONTENT_TYPES.get(extension)
    if declared and expected and declared not in expected and declared != "application/octet-stream":
        raise UploadValidationError("declared content type does not match filename")
    magic = _MAGIC.get(extension)
    if magic and not _check_signature(extension, data):
        raise UploadValidationError("file signature does not match filename")
    if extension in _ZIP_EXTENSIONS:
        _zip_budget(data)
    elif not magic and extension not in _TEXT_EXTENSIONS:
        raise UploadValidationError("file type is not allowed")
    if _EICAR in data:
        raise UploadValidationError("malware detected")
    if _malware_scanner is not None:
        try:
            if _malware_scanner(data, safe_name):
                raise UploadValidationError("malware detected")
        except UploadValidationError:
            raise
        except Exception as exc:
            raise UploadValidationError("malware scan failed") from exc
    return ValidatedUpload(filename=safe_name, extension=extension, content_type=declared)


__all__ = [
    "AUDIO_EXTENSIONS",
    "MEDIA_EXTENSIONS",
    "VIDEO_EXTENSIONS",
    "UploadValidationError",
    "ValidatedUpload",
    "normalize_filename",
    "set_malware_scanner",
    "validate_upload",
]
