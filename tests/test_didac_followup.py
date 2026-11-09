from dataclasses import replace
from datetime import datetime

import pytest

from bird_helpers import post_bird
from conftest import csrf
from didac_app import bird, followup
from didac_app.config import load_config
from didac_app.models import Lead
from didac_app.server import app

DAY = datetime(2026, 11, 10, 12, 0, tzinfo=followup.IST)
CONFIG = replace(load_config(), followup_template_id="tpl", followup_daily_cap=3)


@pytest.fixture
def templates(monkeypatch):
    sent = []
    monkeypatch.setattr(bird, "send_template", lambda config, to: sent.append(to))
    return sent


def lead(store, phone, name="A"):
    return store.create_lead(Lead(name=name, phone=phone))


def test_sends_once_to_mobiles_only_never_staff_or_landline(store, templates):
    lead(store, "98450 11111")
    lead(store, "+91 9845011111", "dup")        # same number as above
    lead(store, "080 2345 6789")                  # landline
    lead(store, "+91 98000 00001", "staff")       # the admin from conftest
    lead(store, "")
    assert followup.send_batch(store, CONFIG, DAY) == "Sent 1 message(s)."
    assert templates == ["+919845011111"]
    assert followup.send_batch(store, CONFIG, DAY) == "Sent 0 message(s)."


def test_quiet_hours_cap_and_missing_template(store, templates):
    for n in range(5):
        lead(store, f"98450 0000{n}")
    night = datetime(2026, 11, 10, 22, 0, tzinfo=followup.IST)
    assert "Quiet hours" in followup.send_batch(store, CONFIG, night)
    assert "not set up" in followup.send_batch(store, replace(CONFIG, followup_template_id=""), DAY)
    assert followup.send_batch(store, CONFIG, DAY) == "Sent 3 message(s)."
    assert "Daily cap" in followup.send_batch(store, CONFIG, DAY)


def test_failure_stops_the_batch_and_the_number_can_be_retried(store, monkeypatch):
    lead(store, "98450 11111")
    lead(store, "98450 22222")
    monkeypatch.setattr(bird, "send_template", lambda config, to: 1 / 0)
    assert followup.send_batch(store, CONFIG, DAY).startswith("Sent 0, then stopped")
    assert len(followup.eligible(store)) == 2


def test_stop_reply_opts_out_and_is_never_messaged(store, templates, sent):
    lead(store, "98450 11111")
    post_bird(app.test_client(), "whatsapp:+919845011111", "STOP")
    assert "will not get any more" in sent[-1][1]
    assert followup.send_batch(store, CONFIG, DAY) == "Sent 0 message(s)."
    assert store.list_leads()[0].phone == "98450 11111"


def test_followup_page_is_admin_only_and_sends(store, templates, monkeypatch, anon_client):
    import didac_app.server as server

    monkeypatch.setattr(server, "config", CONFIG)
    monkeypatch.setattr(followup, "SEND_HOURS", range(24))
    lead(store, "98450 11111")
    client = app.test_client()
    page = client.get("/followup")
    assert page.status_code == 200 and b"1</b> lead" in page.data
    assert client.post("/followup", data={"csrf": csrf(client)}).status_code == 302
    assert templates == ["+919845011111"]
    assert anon_client.get("/followup").status_code == 302


def test_send_to_one_lead_follows_the_same_rules(store, templates, monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "config", CONFIG)
    monkeypatch.setattr(followup, "SEND_HOURS", range(24))
    mobile, landline, staff = lead(store, "98450 11111"), lead(store, "080 2345 6789"), lead(store, "+91 98000 00001")
    client = app.test_client()

    def press(item):
        client.post(f"/leads/{item.id}/followup", data={"csrf": csrf(client)})
        return [m for m in get_flashed(client)]

    def get_flashed(c):
        with c.session_transaction() as sess:
            return sess.pop("_flashes", [])

    assert press(mobile) == [("message", "Message handed to WhatsApp. Check delivery on the Follow-up page.")] and templates == ["+919845011111"]
    assert "already messaged" in press(mobile)[0][1]
    assert "no mobile number" in press(landline)[0][1]
    assert "team member" in press(staff)[0][1]
    assert templates == ["+919845011111"]


def test_delivery_status_is_recorded_and_shown(store, monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "config", CONFIG)
    monkeypatch.setattr(followup, "SEND_HOURS", range(24))
    monkeypatch.setattr(bird, "send_template", lambda config, to: "msg-1")
    calls = []
    monkeypatch.setattr(bird, "message_status", lambda config, mid: calls.append(mid) or ("delivery_failed", "code 131049"))
    lead(store, "98450 11111")
    followup.send_batch(store, CONFIG, DAY)
    page = app.test_client().get("/followup").data.decode()
    assert "Failed" in page and "code 131049" in page and calls == ["msg-1"]
    app.test_client().get("/followup")
    assert calls == ["msg-1"]  # a decided status is not asked about again
