"""Standard library unittest suite for media and docling format support."""

import unittest
import tempfile
from pathlib import Path

from app.services.upload_validation import (
    validate_upload,
    UploadValidationError,
    MEDIA_EXTENSIONS,
    AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from app.services.extraction_service import (
    extract_text,
    _extract_vtt,
    _extract_media_fallback,
    AUDIO_EXTS,
    VIDEO_EXTS,
    MEDIA_EXTS,
    DOCLING_EXTS,
)


class TestMediaSupport(unittest.TestCase):
    def test_media_extensions_coverage(self):
        self.assertIn(".mp3", AUDIO_EXTENSIONS)
        self.assertIn(".wav", AUDIO_EXTENSIONS)
        self.assertIn(".m4a", AUDIO_EXTENSIONS)
        self.assertIn(".aac", AUDIO_EXTENSIONS)
        self.assertIn(".ogg", AUDIO_EXTENSIONS)
        self.assertIn(".flac", AUDIO_EXTENSIONS)

        self.assertIn(".mp4", VIDEO_EXTENSIONS)
        self.assertIn(".avi", VIDEO_EXTENSIONS)
        self.assertIn(".mov", VIDEO_EXTENSIONS)
        self.assertIn(".mkv", VIDEO_EXTENSIONS)
        self.assertIn(".webm", VIDEO_EXTENSIONS)

    def test_audio_validation(self):
        samples = [
            ("meeting.mp3", b"ID3\x03\x00\x00\x00\x00\x00\x00audio-data", "audio/mpeg"),
            ("call.wav", b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00", "audio/wav"),
            ("voice.ogg", b"OggS\x00\x02\x00\x00\x00\x00\x00\x00", "audio/ogg"),
            ("track.flac", b"fLaC\x00\x00\x00\x22", "audio/flac"),
            ("stream.aac", b"\xff\xf1\x50\x80audio-aac", "audio/aac"),
        ]
        for filename, data, ct in samples:
            res = validate_upload(filename=filename, data=data, content_type=ct)
            self.assertEqual(res.filename, filename)
            self.assertEqual(res.content_type, ct)

    def test_video_validation(self):
        samples = [
            ("presentation.mp4", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00", "video/mp4"),
            ("clip.avi", b"RIFF\x24\x00\x00\x00AVI LIST\x00\x00\x00\x00", "video/x-msvideo"),
            ("demo.mov", b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00", "video/quicktime"),
            ("movie.mkv", b"\x1a\x45\xdf\xa3\x93\x42\x86\x81\x01\x42\xf7\x81\x01", "video/x-matroska"),
            ("web.webm", b"\x1a\x45\xdf\xa3\x93\x42\x86\x81\x01\x42\xf7\x81\x01", "video/webm"),
        ]
        for filename, data, ct in samples:
            res = validate_upload(filename=filename, data=data, content_type=ct)
            self.assertEqual(res.filename, filename)
            self.assertEqual(res.content_type, ct)

    def test_vtt_validation(self):
        vtt_data = b"WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nHello world\n"
        res = validate_upload(filename="subs.vtt", data=vtt_data, content_type="text/vtt")
        self.assertEqual(res.filename, "subs.vtt")
        self.assertEqual(res.extension, ".vtt")

    def test_media_size_limit_exemption(self):
        # 60 MB file (exceeds default 50 MB limit)
        large_audio = b"ID3" + (b"\x00" * (60 * 1024 * 1024))
        res = validate_upload(filename="big_audio.mp3", data=large_audio, content_type="audio/mpeg")
        self.assertEqual(res.filename, "big_audio.mp3")

    def test_vtt_text_extraction(self):
        vtt_content = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\nWelcome to XENIUS DocVault.\n\n2\n00:00:05.000 --> 00:00:09.000\n<v Speaker>Media transcription active.</v>\n"
        with tempfile.NamedTemporaryFile(suffix=".vtt", mode="w", delete=False, encoding="utf-8") as f:
            f.write(vtt_content)
            path = Path(f.name)

        try:
            res = _extract_vtt(path)
            self.assertEqual(res.status, "native")
            self.assertIn("Welcome to XENIUS DocVault.", res.text)
            self.assertIn("Media transcription active.", res.text)
            self.assertNotIn("WEBVTT", res.text)
            self.assertNotIn("00:00:01.000", res.text)
        finally:
            path.unlink(missing_ok=True)

    def test_media_fallback_extraction(self):
        with tempfile.NamedTemporaryFile(suffix=".mp3", mode="wb", delete=False) as f:
            f.write(b"ID3\x03\x00\x00\x00\x00\x00\x00sample-audio")
            path = Path(f.name)

        try:
            res = _extract_media_fallback(path, ".mp3")
            self.assertIn(res.status, ("native", "ocr"))
            self.assertTrue(len(res.text) > 0)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
