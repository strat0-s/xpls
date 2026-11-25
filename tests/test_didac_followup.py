from dataclasses import replace
from datetime import datetime

import pytest

from bird_helpers import post_bird
from conftest import csrf, get_or_create_user
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


def test_followup_page_sends_and_needs_login(store, templates, monkeypatch, anon_client):
    import didac_app.server as server

    monkeypatch.setattr(server, "config", CONFIG)
    monkeypatch.setattr(followup, "SEND_HOURS", range(24))
    lead(store, "98450 11111")
    client = app.test_client()
    page = client.get("/followup")
    assert page.status_code == 200 and b"<b>1</b><span>waiting" in page.data
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


def test_staff_can_follow_up_but_not_delete(store, templates, monkeypatch, anon_client):
    import didac_app.server as server

    monkeypatch.setattr(server, "config", CONFIG)
    monkeypatch.setattr(followup, "SEND_HOURS", range(24))
    item = lead(store, "98450 11111")
    get_or_create_user(store, "Sam Staff", "+919800000002", "sam@example.com", "staff-password-1")
    anon_client.post("/login", data={"login": "sam@example.com", "password": "staff-password-1", "csrf": csrf(anon_client)})
    page = anon_client.get("/followup")
    assert page.status_code == 200 and b"Follow-up" in anon_client.get("/").data
    assert anon_client.post(f"/leads/{item.id}/followup", data={"csrf": csrf(anon_client)}).status_code == 302
    assert templates == ["+919845011111"]
    assert b"Send follow-up" in anon_client.get(f"/leads/{item.id}").data and b"Delete lead" not in anon_client.get(f"/leads/{item.id}").data
    assert anon_client.post(f"/leads/{item.id}/delete", data={"csrf": csrf(anon_client)}).status_code == 403


def test_auto_followup_only_messages_leads_captured_since_it_was_switched_on(store, templates):
    before = lead(store, "98450 11111", "old")
    with store.connect() as conn:
        conn.execute("UPDATE leads SET created_at = '2026-01-01T00:00:00+00:00', updated_at = '2026-01-01T00:00:00+00:00' WHERE id = %s", (before.id,))
    lead(store, "98450 22222", "new")
    followup.auto_send(store, CONFIG, DAY)
    assert templates == []  # off: nothing goes out by itself
    store.set_setting(followup.AUTO_KEY, "2026-06-01T00:00:00+00:00")
    followup.auto_send(store, CONFIG, DAY)
    assert templates == ["+919845022222"]  # the older lead is left for a person to send
    followup.auto_send(store, CONFIG, DAY)
    assert templates == ["+919845022222"]  # once per number
    lead(store, "98450 33333", "night")
    followup.auto_send(store, CONFIG, datetime(2026, 11, 10, 22, 0, tzinfo=followup.IST))
    assert templates == ["+919845022222"]  # quiet hours: it waits
    followup.auto_send(store, CONFIG, DAY)
    assert templates == ["+919845022222", "+919845033333"]  # and goes out with the next one


def test_saving_a_scan_sends_the_followup_when_automatic_is_on(store, templates, monkeypatch):
    from didac_app import flows

    monkeypatch.setattr(followup, "blocked", lambda *args: "")
    monkeypatch.setattr(flows, "fetch_media", lambda config, url, ct, number: ("k/x.jpg", ""))
    monkeypatch.setattr(flows, "read_documents", lambda config, photos, kind, products=(): {"fields": {"name": "Asha Rao", "phone": "9845011111"}, "check": [], "more": [], "ms": 1})
    staff = "whatsapp:+919800000001"

    def scan():
        for body, media in (("Contact form", ()), ("", [("https://media.bird.com/p", "image/jpeg")]), ("done", ()), ("Save", ()), ("High", ()), ("No", ())):
            post_bird(app.test_client(), staff, body, list(media))

    scan()
    assert templates == []  # automatic follow-up is off by default
    store.set_setting(followup.AUTO_KEY, "2026-01-01T00:00:00+00:00")
    with store.connect() as conn:
        conn.execute("DELETE FROM followups")
    scan()
    assert templates == ["+919845011111"]


def test_admin_can_switch_automatic_followup_on_and_off(store):
    client = app.test_client()
    assert "Off" in client.get("/admin/messages").data.decode()
    client.post("/admin/messages/auto-followup", data={"on": "1", "csrf": csrf(client)})
    since = store.get_setting(followup.AUTO_KEY)
    assert since and "On since" in client.get("/admin/messages").data.decode()
    client.post("/admin/messages/auto-followup", data={"on": "1", "csrf": csrf(client)})
    assert store.get_setting(followup.AUTO_KEY) == since  # switching on again does not move the start
    client.post("/admin/messages/auto-followup", data={"on": "0", "csrf": csrf(client)})
    assert store.get_setting(followup.AUTO_KEY) == ""


def test_follow_up_goes_out_however_the_lead_was_captured(store, templates, monkeypatch):
    monkeypatch.setattr(followup, "blocked", lambda store, config, now: "")  # the app's config has no template; the send itself is faked
    store.set_setting(followup.AUTO_KEY, "2026-06-01T00:00:00+00:00")
    client = app.test_client()
    post_bird(client, "whatsapp:+919800000001", "New lead\nName: Typed By Staff\nPhone: 98450 44444")  # a typed lead over WhatsApp
    assert templates == ["+919845044444"]
    client.post("/leads/new", data={"csrf": csrf(client), "name": "Added In App", "phone": "98450 55555"})  # added in the app
    assert templates[-1] == "+919845055555"
    waiting = store.create_lead(Lead(name="Waited", phone="98450 66666"))  # saved without any trigger, e.g. during quiet hours
    client.post(f"/leads/{waiting.id}", data={"csrf": csrf(client), "name": "Waited", "phone": "98450 66666"})  # a lead edited in the app
    assert templates[-1] == "+919845066666"
    late = store.create_lead(Lead(name="Late", phone="98450 77777"))
    client.get("/")  # opening the Leads page catches up anything left waiting
    assert templates[-1] == "+919845077777" and len(templates) == 4 and late.id


def test_a_corrected_phone_number_gets_its_own_follow_up(store, templates, monkeypatch):
    monkeypatch.setattr(followup, "blocked", lambda store, config, now: "")
    store.set_setting(followup.AUTO_KEY, "2026-06-01T00:00:00+00:00")
    client = app.test_client()
    wrong = lead(store, "98450 11111", "Misread")
    client.post(f"/leads/{wrong.id}", data={"csrf": csrf(client), "name": "Misread", "phone": "98450 11111"})
    assert templates == ["+919845011111"]
    client.post(f"/leads/{wrong.id}", data={"csrf": csrf(client), "name": "Misread", "phone": "98450 22222"})  # a person fixes the number
    assert templates == ["+919845011111", "+919845022222"]
    client.post(f"/leads/{wrong.id}", data={"csrf": csrf(client), "name": "Misread Again", "phone": "98450 22222"})  # any other edit sends nothing more
    assert len(templates) == 2


def test_a_number_added_to_an_older_lead_of_the_live_event_is_messaged_but_not_one_of_an_ended_event(store, templates, monkeypatch):
    monkeypatch.setattr(followup, "blocked", lambda store, config, now: "")
    old_event = store.create_event("Old Expo")
    store.set_setting(followup.AUTO_KEY, "2026-06-01T00:00:00+00:00")
    with store.connect() as conn:
        conn.execute("UPDATE leads SET created_at = '2026-01-01T00:00:00+00:00', updated_at = '2026-01-01T00:00:00+00:00'")
    early = lead(store, "", "Badge only")
    elsewhere = store.create_lead(Lead(name="Elsewhere", phone="", event_id=old_event))
    with store.connect() as conn:
        conn.execute("UPDATE leads SET created_at = '2026-01-01T00:00:00+00:00', updated_at = '2026-01-01T00:00:00+00:00'")
    client = app.test_client()
    client.post(f"/leads/{elsewhere.id}", data={"csrf": csrf(client), "name": "Elsewhere", "phone": "98450 33333"})
    assert templates == []  # an ended event's lead is never messaged by the automatic follow-up
    client.post(f"/leads/{early.id}", data={"csrf": csrf(client), "name": "Badge only", "phone": "98450 44444"})
    assert templates == ["+919845044444"]
