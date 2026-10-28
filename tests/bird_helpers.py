import itertools

from didac_app.bird import send_message as real_send_message  # the autouse `sent` fixture replaces bird.send_message

_message_ids = itertools.count(1)


def bird_event(number, body="", media=(), message_id=None):
    """Build a Bird whatsapp.inbound webhook event."""
    if not media:
        message = {"type": "text", "text": {"text": body}}
    elif media[0][1].startswith("image/"):
        message = {"type": "image", "image": {"images": [{"mediaUrl": url} for url, _ in media], "text": body}}
    else:
        message = {"type": "file", "file": {"files": [{"mediaUrl": url, "contentType": ct} for url, ct in media], "text": body}}
    return {
        "service": "channels",
        "event": "whatsapp.inbound",
        "payload": {
            "id": message_id or f"msg-{next(_message_ids)}",
            "sender": {"contact": {"identifierValue": number.replace("whatsapp:", "")}},
            "body": message,
        },
    }


def post_bird(client, number, body="", media=(), **kwargs):
    return client.post("/bird/webhook?token=test-token", json=bird_event(number, body, media, **kwargs))
