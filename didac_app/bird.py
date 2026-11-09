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


def message_body(text: str, options: list[tuple[str, str]] | None = None, as_list: bool = False) -> dict:
    """Plain text; up to 3 tappable reply buttons (label max 20 chars); or a tap-list of up to 10 rows (title max 24, description max 72).
    A tap comes back as an ordinary inbound text whose body is the label, so typing the label does the same."""
    if options and as_list:
        items = [
            {"title": label[:24], "description": description[:72], "actions": [{"type": "postback", "postback": {"text": label[:24], "payload": label}}]}
            for label, description in options[:10]
        ]
        return {"type": "list", "list": {"text": text, "items": items}}
    if options:
        actions = [{"type": "reply", "reply": {"text": label[:20]}} for label, _ in options[:3]]
        return {"type": "text", "text": {"text": text, "actions": actions}}
    return {"type": "text", "text": {"text": text}}


def post_message(config: DidacConfig, to_number: str, content: dict) -> None:
    response = requests.post(
        f"{API_BASE}/workspaces/{config.bird_workspace_id}/channels/{config.bird_channel_id}/messages",
        headers=media_headers(config),
        json={"receiver": {"contacts": [{"identifierKey": "phonenumber", "identifierValue": to_number.replace("whatsapp:", "")}]}, **content},
        timeout=15,
    )
    response.raise_for_status()


def send_message(config: DidacConfig, to_number: str, text: str, options: list[tuple[str, str]] | None = None, as_list: bool = False) -> None:
    post_message(config, to_number, {"body": message_body(text, options, as_list)})


def send_template(config: DidacConfig, to_number: str) -> None:
    """The approved parameterless follow-up template. Needed because WhatsApp only lets us message first with a template; its two
    quick-reply buttons are labelled exactly like the visitor menu rows ("Share my requirement", "Download brochure"), so a tap lands in that flow."""
    template = {"projectId": config.followup_template_id, "version": config.followup_template_version, "locale": "en", "parameters": []}
    post_message(config, to_number, {"template": template})
