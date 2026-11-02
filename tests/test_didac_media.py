from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from didac_app import media
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


def fake_dns(monkeypatch, address):
    monkeypatch.setattr(media.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", (address, port))])


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "::1"])
def test_private_addresses_are_not_safe(monkeypatch, address):
    fake_dns(monkeypatch, address)
    assert not media.is_safe_http_url("http://example.com/x")


def test_public_address_and_scheme_checks(monkeypatch):
    fake_dns(monkeypatch, "93.184.216.34")
    assert media.is_safe_http_url("https://example.com/x")
    assert not media.is_safe_http_url("file:///etc/passwd")
    assert not media.is_safe_http_url("javascript:alert(1)")


def test_redirect_to_private_address_is_blocked(monkeypatch):
    addresses = {"public.example": "93.184.216.34", "internal.example": "10.0.0.5"}
    monkeypatch.setattr(media.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", (addresses[host], port))])
    calls = []

    class Redirect:
        is_redirect = True
        headers = {"Location": "http://internal.example/secret"}

    monkeypatch.setattr(media.requests, "get", lambda url, **kw: calls.append(url) or Redirect())
    assert media.fetch_qr_page_text("http://public.example/") == ""
    assert calls == ["http://public.example/"]


def test_vcard_qr_becomes_lead_fields():
    vcard = "BEGIN:VCARD\nVERSION:3.0\nN:Rao;Asha;;;\nFN:Asha Rao\nORG:Sunrise School\nTITLE:Principal\nTEL;TYPE=CELL:+919845604740\nEMAIL:asha@sunrise.edu\nEND:VCARD"
    assert media.parse_contact_qr(vcard) == (
        "Name: Asha Rao\nOrg: Sunrise School\nRole: Principal\nPhone: +919845604740\nEmail: asha@sunrise.edu"
    )


def test_mecard_qr_becomes_lead_fields():
    assert media.parse_contact_qr(r"MECARD:N:Rao,Asha;TEL:+919845604740;EMAIL:asha@sunrise.edu;ORG:Sun\;rise;;") == (
        "Name: Asha Rao\nPhone: +919845604740\nEmail: asha@sunrise.edu\nOrg: Sun;rise"
    )
    assert media.parse_contact_qr("https://xperimentor.science") == ""


def test_photo_without_qr_skips_expensive_candidates(tmp_path, monkeypatch):
    import cv2
    import numpy as np

    path = tmp_path / "blank.jpg"
    cv2.imwrite(str(path), np.full((600, 800, 3), 255, dtype=np.uint8))
    monkeypatch.setattr(media, "qr_candidates", lambda *a: pytest.fail("expensive candidates ran"))
    assert media.decode_qr_values(str(path)) == []


def test_generated_qr_still_decodes(tmp_path):
    import cv2

    qr = cv2.QRCodeEncoder.create().encode("https://xperimentor.science")
    path = tmp_path / "qr.png"
    cv2.imwrite(str(path), cv2.resize(qr, None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST))
    assert media.decode_qr_values(str(path)) == ["https://xperimentor.science"]
