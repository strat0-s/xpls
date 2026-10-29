from __future__ import annotations

import logging
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from didac_app import blobstore
from didac_app.config import DidacConfig
from didac_app.openai_services import split_lead_texts, transcribe_audio, vision_ocr_to_lead_text

logger = logging.getLogger(__name__)

QR_MAX_SIDE = 1600
QR_TIME_BUDGET_SECONDS = 8


def save_media(
    url: str,
    content_type: str,
    config: DidacConfig,
    headers: dict[str, str] | None = None,
    sender: str = "",
    received_at: datetime | None = None,
) -> tuple[str, str]:
    """Upload the media to the bucket. Returns (bucket key, temp file path for OCR); the caller deletes the temp file."""
    suffix = suffix_for(content_type, url)
    name = Path(urlparse(url).path).name or "media"
    key = f"{media_storage_prefix(sender, received_at)}/{name}{suffix if not name.endswith(suffix) else ''}"
    # Bird answers with a redirect to a presigned URL; follow it without forwarding the auth header.
    response = requests.get(url, headers=headers, timeout=20, allow_redirects=False)
    if response.is_redirect:
        response = requests.get(response.headers["Location"], timeout=20)
    response.raise_for_status()
    blobstore.upload(config, key, response.content, content_type)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(response.content)
    return key, tmp.name


def media_storage_prefix(sender: str = "", received_at: datetime | None = None) -> str:
    received_at = received_at or datetime.now(ZoneInfo("Asia/Kolkata"))
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    date_folder = received_at.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
    return f"{date_folder}/{sender_folder(sender)}"


def sender_folder(sender: str) -> str:
    cleaned = sender.replace("whatsapp:", "").strip()
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in cleaned)
    return cleaned.strip("_") or "unknown_sender"


def extract_media_text(local_path: str, content_type: str) -> str:
    if not local_path:
        return ""
    # An OpenAI failure must not lose the media: fall back to the *_PENDING placeholders the callers already handle.
    if content_type.startswith("image/"):
        try:
            extracted = vision_ocr_to_lead_text(local_path, content_type)
        except Exception:
            logger.exception("Vision OCR failed")
            extracted = ""
        return append_qr_details(local_path, extracted or optional_ocr(local_path))
    if content_type.startswith("audio/"):
        try:
            transcript = transcribe_audio(local_path)
        except Exception:
            logger.exception("Transcription failed")
            transcript = ""
        return transcript or "VOICE_NOTE_SAVED_TRANSCRIPTION_PENDING"
    return ""


def append_qr_details(local_path: str, extracted_text: str) -> str:
    qr_values = decode_qr_values(local_path)
    if not qr_values:
        return extracted_text
    lines = [extracted_text.strip()] if extracted_text else []
    for value in qr_values[:3]:
        if is_safe_http_url(value):
            lines.append(f"QR URL: {value}")
            page_text = fetch_qr_page_text(value)
            if page_text:
                lines.append(f"QR Page Text: {page_text}")
        else:
            lines.append(f"QR Page Text: {value}")
    return "\n".join(line for line in lines if line).strip()


def decode_qr_values(local_path: str) -> list[str]:
    try:
        import cv2
        import numpy as np
    except Exception:
        return []
    image = cv2.imread(local_path)
    if image is None:
        return []
    # Phone photos are huge and the candidates are upscaled copies: cap the size and the time spent.
    longest = max(image.shape[:2])
    if longest > QR_MAX_SIDE:
        scale = QR_MAX_SIDE / longest
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    detector = cv2.QRCodeDetector()
    values = []
    deadline = time.monotonic() + QR_TIME_BUDGET_SECONDS
    for candidate in qr_candidates(image, cv2, np):
        values.extend(decode_with_detector(detector, candidate))
        if values or time.monotonic() > deadline:
            break
    return [value for value in dict.fromkeys(values) if value]


def decode_qr_urls(local_path: str) -> list[str]:
    return [value for value in decode_qr_values(local_path) if is_safe_http_url(value)]


def decode_with_detector(detector, image) -> list[str]:
    values = []
    try:
        ok, decoded_info, _points, _straight_qrcode = detector.detectAndDecodeMulti(image)
        if ok:
            values.extend(item.strip() for item in decoded_info if item and item.strip())
    except Exception:
        pass
    try:
        data, _points, _straight_qrcode = detector.detectAndDecode(image)
        if data and data.strip():
            values.append(data.strip())
    except Exception:
        pass
    return values


def qr_candidates(image, cv2, np):
    height, width = image.shape[:2]
    crops = [image]
    # Badges often have QR codes near the upper half; try coarse regions too.
    regions = [
        (0, 0, width, height // 2),
        (0, 0, width // 2, height),
        (width // 2, 0, width, height),
        (0, height // 4, width, (height * 3) // 4),
    ]
    for x1, y1, x2, y2 in regions:
        crop = image[y1:y2, x1:x2]
        if crop.size:
            crops.append(crop)
    for crop in crops:
        for scale in (1, 2, 3, 4):
            resized = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC) if scale > 1 else crop
            yield resized
            gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
            yield gray
            yield cv2.equalizeHist(gray)
            blur = cv2.GaussianBlur(gray, (3, 3), 0)
            yield cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 2)
            _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            yield otsu
            sharpen_kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
            yield cv2.filter2D(resized, -1, sharpen_kernel)


def is_safe_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def fetch_qr_page_text(url: str) -> str:
    if not is_safe_http_url(url):
        return ""
    try:
        response = requests.get(
            url,
            timeout=8,
            allow_redirects=True,
            headers={"User-Agent": "Xperimentor-DIDAC-lead-scanner/1.0"},
        )
        response.raise_for_status()
    except Exception:
        return ""
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type and "text/plain" not in content_type:
        return ""
    soup = BeautifulSoup(response.text[:200_000], "html.parser")
    for item in soup(["script", "style", "noscript"]):
        item.decompose()
    text = " ".join(soup.get_text(" ").split())
    return text[:1200]


def extract_media_lead_texts(local_path: str, content_type: str) -> list[str]:
    text = extract_media_text(local_path, content_type)
    if not text:
        return []
    return split_lead_texts(text)


def optional_ocr(local_path: str) -> str:
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return "IMAGE_SAVED_OCR_PENDING"
    try:
        return pytesseract.image_to_string(Image.open(local_path)).strip()
    except Exception:
        return "IMAGE_SAVED_OCR_FAILED"


def suffix_for(content_type: str, url: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "application/pdf": ".pdf",
    }
    if content_type in mapping:
        return mapping[content_type]
    suffix = Path(urlparse(url).path).suffix
    return suffix or ".bin"
