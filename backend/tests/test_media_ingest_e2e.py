"""Integration test for media file ingestion in DocVault."""

import unittest
from pathlib import Path
from app.database import SessionLocal
from app.models import User, Document, DocVersion, IngestionJob
from app.services import document_service, extraction_service
from app.services.upload_validation import validate_upload
from app.utils.request_context import RequestContext


class TestMediaIngestE2E(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.user = self.db.query(User).first()
        if not self.user:
            self.user = User(
                username="testadmin",
                full_name="Test Admin",
                password_hash="test",
                role="ADMIN",
            )
            self.db.add(self.user)
            self.db.commit()
            self.db.refresh(self.user)

    def tearDown(self):
        self.db.close()

    def test_ingest_audio_and_vtt(self):
        context = RequestContext()

        # 1. Test Ingest Audio
        audio_data = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 1024
        result_audio = document_service.ingest_document(
            self.db,
            self.user,
            filename="field_interview.mp3",
            data=audio_data,
            content_type="audio/mpeg",
            context=context,
        )
        self.assertIsNotNone(result_audio.id)
        self.assertIn(result_audio.status, ("PROCESSING", "READY"))

        # 2. Test Ingest WebVTT
        vtt_data = b"WEBVTT\n\n00:00:01.000 --> 00:00:05.000\nDiscussion on PVVNL substation maintenance.\n"
        result_vtt = document_service.ingest_document(
            self.db,
            self.user,
            filename="substation_subtitles.vtt",
            data=vtt_data,
            content_type="text/vtt",
            context=context,
        )
        self.assertIsNotNone(result_vtt.id)
        self.assertIn(result_vtt.status, ("PROCESSING", "READY"))

        # 3. Test Ingest Video
        video_data = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00" + b"\x00" * 2048
        result_video = document_service.ingest_document(
            self.db,
            self.user,
            filename="site_inspection.mp4",
            data=video_data,
            content_type="video/mp4",
            context=context,
        )
        self.assertIsNotNone(result_video.id)
        self.assertIn(result_video.status, ("PROCESSING", "READY"))

        # 4. Test Ingest Large Media (60 MB)
        large_audio = b"ID3\x03\x00\x00\x00\x00\x00\x00" + (b"\x00" * (60 * 1024 * 1024))
        result_large = document_service.ingest_document(
            self.db,
            self.user,
            filename="large_recording_test.mp3",
            data=large_audio,
            content_type="audio/mpeg",
            context=context,
        )
        self.assertIsNotNone(result_large.id)
        self.assertIn(result_large.status, ("PROCESSING", "READY"))

        print("\n✓ E2E Ingestion of Audio, Video, VTT, and 60MB Media succeeded!")


if __name__ == "__main__":
    unittest.main()
