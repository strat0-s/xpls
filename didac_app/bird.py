from __future__ import annotations

import requests

from didac_app.config import DidacConfig

API_BASE = "https://api.bird.com"


def parse_inbound(event: dict) -> dict | None:
    """Turn a Bird `whatsapp.inbound` webhook event into message fields; None for any other event."""
    if event.get("event") != "whatsapp.inbound":
        return None
    payload = event.get("payload") or {}
    sender = ((payload.get("sender") or {}).get("contact") or {}).get("identifierValue")
    if not sender:
        return None
    body = payload.get("body") or {}
    kind = body.get("type")
    content = (body.get(kind) or {}) if kind else {}
    if kind == "text":
        text, media = content.get("text", ""), []
    elif kind == "image":
        text = content.get("text") or ""
        media = [(item["mediaUrl"], item.get("contentType") or "image/jpeg") for item in content.get("images", [])]
    elif kind == "file":
        text = content.get("text") or ""
        media = [(item["mediaUrl"], item.get("contentType") or "") for item in content.get("files", [])]
    else:
        text, media = "", []
    return {
        "message_id": payload.get("id", ""),
        "from_number": f"whatsapp:{sender}",
        "body": text.strip(),
        "media": media,
    }


def media_headers(config: DidacConfig) -> dict[str, str]:
    return {"Authorization": f"AccessKey {config.bird_access_key}"}


def send_message(config: DidacConfig, to_number: str, text: str) -> None:
    response = requests.post(
        f"{API_BASE}/workspaces/{config.bird_workspace_id}/channels/{config.bird_channel_id}/messages",
        headers=media_headers(config),
        json={
            "receiver": {"contacts": [{"identifierKey": "phonenumber", "identifierValue": to_number.replace("whatsapp:", "")}]},
            "body": {"type": "text", "text": {"text": text}},
        },
        timeout=15,
    )
    response.raise_for_status()
