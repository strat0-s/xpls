from types import SimpleNamespace

from didac_app import bird, media
from didac_app.config import DidacConfig
from didac_app.server import app
from bird_helpers import bird_event, post_bird, real_send_message

NUMBER = "whatsapp:+919800000001"


def test_parse_text_image_and_voice_note():
    text = bird.parse_inbound(bird_event(NUMBER, "hello", message_id="m1"))
    assert text == {"message_id": "m1", "from_number": NUMBER, "body": "hello", "media": []}
    image = bird.parse_inbound(bird_event(NUMBER, "caption", [("https://m/1", "image/jpeg")]))
    assert image["body"] == "caption"
    assert image["media"] == [("https://m/1", "image/jpeg")]
    voice = bird.parse_inbound(bird_event(NUMBER, "", [("https://m/2", "audio/ogg")]))
    assert voice["media"] == [("https://m/2", "audio/ogg")]


def test_parse_ignores_other_events():
    assert bird.parse_inbound({"event": "whatsapp.outbound", "payload": {}}) is None
    assert bird.parse_inbound({"event": "whatsapp.inbound", "payload": {}}) is None


def test_webhook_rejects_wrong_or_missing_token():
    client = app.test_client()
    assert client.post("/bird/webhook?token=nope", json=bird_event(NUMBER, "hi")).status_code == 401
    assert client.post("/bird/webhook", json=bird_event(NUMBER, "hi")).status_code == 401


def test_webhook_replies_through_bird_and_ignores_duplicates(sent):
    client = app.test_client()
    first = post_bird(client, NUMBER, "hi", message_id="dup-1")
    assert first.status_code == 200
    assert len(sent) == 1
    assert sent[0][0] == NUMBER
    second = post_bird(client, NUMBER, "hi", message_id="dup-1")
    assert second.data == b"duplicate"
    assert len(sent) == 1


def test_failed_reply_is_recorded_and_still_returns_200(store, monkeypatch):
    def boom(config, to_number, text):
        raise RuntimeError("bird down")

    monkeypatch.setattr(bird, "send_message", boom)
    response = post_bird(app.test_client(), NUMBER, "hi")
    assert response.status_code == 200
    event = store.list_inbound_events()[0]
    assert event["status"] == "reply_failed"
    assert "bird down" in event["error"]


def test_send_message_posts_to_channel_with_access_key(monkeypatch):
    calls = []
    monkeypatch.setattr(bird.requests, "post", lambda url, **kw: calls.append((url, kw)) or SimpleNamespace(raise_for_status=lambda: None))
    config = DidacConfig(bird_workspace_id="ws", bird_channel_id="ch", bird_access_key="key")
    real_send_message(config, NUMBER, "hello")
    url, kwargs = calls[0]
    assert url == "https://api.bird.com/workspaces/ws/channels/ch/messages"
    assert kwargs["headers"] == {"Authorization": "AccessKey key"}
    assert kwargs["json"]["receiver"]["contacts"][0]["identifierValue"] == "+919800000001"
    assert kwargs["json"]["body"]["text"]["text"] == "hello"


def test_media_download_follows_redirect_without_auth_header(monkeypatch):
    gets = []
    redirect = SimpleNamespace(is_redirect=True, headers={"Location": "https://presigned/x"})
    final = SimpleNamespace(is_redirect=False, content=b"img", raise_for_status=lambda: None)
    monkeypatch.setattr(media.requests, "get", lambda url, **kw: gets.append((url, kw)) or (redirect if len(gets) == 1 else final))
    monkeypatch.setattr(media.blobstore, "upload", lambda config, key, data, content_type: gets.append(("upload", key)))
    key, tmp = media.save_media("https://media.bird.com/abc", "image/jpeg", DidacConfig(), {"Authorization": "AccessKey k"}, sender=NUMBER)
    assert gets[0][1]["headers"] == {"Authorization": "AccessKey k"}
    assert gets[1][0] == "https://presigned/x"
    assert "headers" not in gets[1][1]
    assert key.endswith("/919800000001/abc.jpg")
    assert open(tmp, "rb").read() == b"img"
    import os

    os.unlink(tmp)
