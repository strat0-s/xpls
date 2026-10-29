from datetime import datetime
from zoneinfo import ZoneInfo

from didac_app.media import append_qr_details, media_storage_prefix, sender_folder


def test_sender_folder_uses_whatsapp_number():
    assert sender_folder("whatsapp:+919845604740") == "919845604740"


def test_sender_folder_handles_unknown_values():
    assert sender_folder("") == "unknown_sender"


def test_media_storage_prefix_uses_date_then_sender():
    received_at = datetime(2026, 10, 18, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert media_storage_prefix("whatsapp:+919845604740", received_at) == "2026-10-18/919845604740"


def test_append_qr_details_records_non_url_qr_value(monkeypatch):
    monkeypatch.setattr("didac_app.media.decode_qr_values", lambda local_path: ["EXB0987"])
    text = append_qr_details("/tmp/badge.jpg", "Name: Magesh")
    assert "Name: Magesh" in text
    assert "QR Page Text: EXB0987" in text


def test_blobstore_uploads_and_signs_with_secret_key(monkeypatch):
    from types import SimpleNamespace

    from didac_app import blobstore
    from didac_app.config import DidacConfig

    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"signedURL": "/object/sign/b/a%20b.jpg?token=t"})

    monkeypatch.setattr(blobstore.requests, "post", fake_post)
    config = DidacConfig(supabase_url="https://x.supabase.co/", supabase_bucket="b", supabase_secret_key="secret")
    blobstore.upload(config, "2026-10-18/91/a b.jpg", b"img", "image/jpeg")
    assert calls[0][0] == "https://x.supabase.co/storage/v1/object/b/2026-10-18/91/a%20b.jpg"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer secret"
    assert blobstore.signed_url(config, "a b.jpg") == "https://x.supabase.co/storage/v1/object/sign/b/a%20b.jpg?token=t"


def test_openai_failure_keeps_media_with_pending_placeholder(monkeypatch):
    from didac_app import media

    def boom(*args, **kwargs):
        raise RuntimeError("openai down")

    monkeypatch.setattr(media, "vision_ocr_to_lead_text", boom)
    monkeypatch.setattr(media, "transcribe_audio", boom)
    monkeypatch.setattr(media, "optional_ocr", lambda path: "IMAGE_SAVED_OCR_PENDING")
    monkeypatch.setattr(media, "decode_qr_values", lambda path: [])
    assert media.extract_media_text("/tmp/x.jpg", "image/jpeg") == "IMAGE_SAVED_OCR_PENDING"
    assert media.extract_media_text("/tmp/x.ogg", "audio/ogg") == "VOICE_NOTE_SAVED_TRANSCRIPTION_PENDING"


def test_qr_candidates_are_built_lazily():
    import inspect

    import cv2
    import numpy as np

    from didac_app import media

    assert inspect.isgenerator(media.qr_candidates(np.zeros((10, 10, 3), dtype="uint8"), cv2, np))
