"""Events: one is live at a time, and the WhatsApp menus, document types and brochures follow it."""
import psycopg
import pytest

from bird_helpers import post_bird
from didac_app import flows
from didac_app.config import default_brochures, load_config
from didac_app.server import app
from didac_app.storage import unique_key

STAFF = "whatsapp:+919800000001"  # the admin from conftest
VISITOR = "whatsapp:+919811111111"


def say(number, body=""):
    return post_bird(app.test_client(), number, body)


def last(sent):
    return sent[-1][1], [label for label, _ in sent[-1][2]]


def test_the_first_run_creates_one_live_event_with_todays_setup(store):
    event = store.live_event()
    assert event["name"] == "DIDAC 2026"
    assert [row["label"] for row in store.list_doc_types(event["id"])] == ["Contact form", "Visiting card", "Event badge / QR"]
    assert [row["name"] for row in store.list_brochures(event["id"])] == ["SIA", "TiQer", "Play Lab", "STREAM", "Lab-in-Classroom"]
    assert store.ensure_default_event(default_brochures(load_config())) is None  # never a second default


def test_only_one_event_can_be_live(store):
    first = store.live_event()["id"]
    second = store.create_event("Bett India 2027")
    assert store.get_event(second)["status"] == "draft"
    store.start_event(second)
    assert store.live_event()["id"] == second and store.get_event(first)["status"] == "ended"
    with pytest.raises(psycopg.errors.UniqueViolation), store.connect() as conn:  # the database itself refuses a second live event
        conn.execute("UPDATE events SET status='live' WHERE id=%s", (first,))


def test_stopping_the_event_leaves_none_live(store):
    store.stop_event(store.live_event()["id"])
    assert store.live_event() is None


def test_a_lead_belongs_to_the_event_that_was_live_when_it_was_captured(store):
    first = store.live_event()["id"]
    a = store.create_lead(flows.Lead(name="At DIDAC"))
    second = store.create_event("Bett India 2027")
    store.start_event(second)
    b = store.create_lead(flows.Lead(name="At Bett"))
    assert (store.get_lead(a.id).event_id, store.get_lead(b.id).event_id) == (first, second)
    assert [e["lead_count"] for e in store.list_events()] == [1, 1]


def test_the_team_menu_follows_the_live_event(store, sent):
    say(STAFF, "menu")
    assert last(sent) == ("Hi Test. You are at DIDAC 2026. What would you like to do?\n\nType menu any time. Type cancel to stop.", ["Contact form", "Visiting card", "Event badge / QR", "Send event photos", "Brochures", "Add a note"])
    second = store.create_event("Bett India 2027")
    store.add_doc_type(second, "Registration form", "Printed sign-up sheet", "form")
    store.start_event(second)
    say(STAFF, "menu")
    assert last(sent)[0].startswith("Hi Test. You are at Bett India 2027.") and last(sent)[1] == ["Registration form", "Send event photos", "Add a note"]
    say(STAFF, "Registration form")
    assert store.get_session(STAFF)["flow"] == "registration_form"  # the key made from the label
    say(STAFF, "menu")
    say(STAFF, "Contact form")  # not part of this event, so it is not a menu choice
    assert store.get_session(STAFF) is None and last(sent)[1] == ["Registration form", "Send event photos", "Add a note"]


def test_a_deactivated_type_is_hidden_but_its_leads_stay(store, sent):
    event = store.live_event()
    lead = store.create_lead(flows.Lead(name="Card Person", doc_type="other"))
    card = next(row for row in store.list_doc_types(event["id"]) if row["key"] == "other")
    store.set_doc_type_active(card["id"], False)
    say(STAFF, "menu")
    assert last(sent)[1] == ["Contact form", "Event badge / QR", "Send event photos", "Brochures", "Add a note"]
    assert store.get_lead(lead.id).doc_type == "other"  # nothing was wiped
    store.set_doc_type_active(card["id"], True)
    say(STAFF, "menu")
    assert "Visiting card" in last(sent)[1]


def test_a_scan_in_progress_when_its_type_is_switched_off_is_ended_politely(store, sent):
    say(STAFF, "Visiting card")
    assert store.get_session(STAFF)["flow"] == "other"
    card = next(row for row in store.list_doc_types(store.live_event()["id"]) if row["key"] == "other")
    store.set_doc_type_active(card["id"], False)
    say(STAFF, "DONE")
    assert any(m[1] == flows.NOT_AVAILABLE for m in sent) and last(sent)[1] == ["Contact form", "Event badge / QR", "Send event photos", "Brochures", "Add a note"]
    assert store.get_session(STAFF) is None


def test_more_than_three_types_come_as_a_list_when_asking_what_a_photo_is(store, sent):
    event = store.live_event()
    store.add_doc_type(event["id"], "Raffle slip")
    say(STAFF, "menu")
    assert "Raffle slip" in last(sent)[1]
    reply = flows.Chat(store, load_config(), STAFF).ask_kind()
    assert reply.as_list and [label for label, _ in reply.options][-1] == "Raffle slip"


def test_with_no_live_event_nothing_is_saved_and_everyone_is_told(store, sent):
    store.stop_event(store.live_event()["id"])
    say(STAFF, "menu")
    assert last(sent)[0] == "Hi Test. No event is live right now, so I cannot save leads or photos. Please ask an admin to start the next event."
    say(VISITOR, "hi")
    assert last(sent)[0] == flows.NO_EVENT_VISITOR
    say(VISITOR, "Share my requirement")
    assert last(sent)[0] == flows.NO_EVENT_VISITOR and store.get_session(VISITOR) is None and store.list_leads() == []


def test_the_welcome_text_is_per_event(store, sent):
    say(VISITOR, "hi")
    assert last(sent)[0].startswith("Welcome to Xperimentor at DIDAC 2026.")
    store.update_event(store.live_event()["id"], welcome_text="Hello from the DIDAC stall!")
    say(VISITOR, "hi")
    assert last(sent)[0] == "Hello from the DIDAC stall!"


def test_visitors_only_see_active_brochures_meant_for_them(store, sent):
    event = store.live_event()
    for row in store.list_brochures(event["id"]):
        store.set_brochure_active(row["id"], False)
    store.add_brochure(event["id"], "Public flyer", "https://x/flyer.pdf", for_visitors=True)
    store.add_brochure(event["id"], "Internal price list", "https://x/prices.pdf", for_visitors=False)
    say(VISITOR, "Download brochure")
    assert last(sent)[1] == ["Public flyer"]
    assert [row["name"] for row in store.list_brochures(event["id"], "team")] == ["Public flyer", "Internal price list"]


def test_a_visitor_is_told_when_there_are_no_brochures(store, sent):
    event = store.live_event()
    for row in store.list_brochures(event["id"]):
        store.set_brochure_active(row["id"], False)
    say(VISITOR, "hi")
    assert last(sent)[1] == ["Share my requirement", "Feedback / voice note"]  # no brochure row to tap


def test_a_new_event_can_start_from_the_last_ones_setup(store):
    first = store.live_event()["id"]
    card = next(row for row in store.list_doc_types(first) if row["key"] == "other")
    store.set_doc_type_active(card["id"], False)
    second = store.create_event("Bett India 2027")
    store.copy_setup(first, second)
    copied = store.list_doc_types(second)
    assert [(row["label"], row["active"]) for row in copied] == [("Contact form", True), ("Visiting card", False), ("Event badge / QR", True)]
    assert len(store.list_brochures(second)) == 5
    assert store.list_doc_types(first)[0]["id"] != copied[0]["id"]  # copies, not shared rows


def test_a_type_key_is_made_from_the_label_and_never_collides():
    assert unique_key("Raffle slip!", set()) == "raffle_slip"
    assert unique_key("Raffle slip", {"raffle_slip"}) == "raffle_slip_2"
    assert unique_key("Note", set()) == "note_2"  # "note" is a session flow name
    assert unique_key("???", set()) == "type"


def test_editing_a_lead_keeps_a_type_that_belongs_to_its_event(store, admin):
    lead = store.create_lead(flows.Lead(name="Asha", doc_type="other"))
    client = app.test_client()
    with client.session_transaction() as sess:
        token = sess.setdefault("csrf", "test-csrf")
    client.post(f"/leads/{lead.id}", data={"csrf": token, "name": "Asha", "doc_type": "form"})
    assert store.get_lead(lead.id).doc_type == "form"
    client.post(f"/leads/{lead.id}", data={"csrf": token, "name": "Asha", "doc_type": "nonsense"})
    assert store.get_lead(lead.id).doc_type == "form"  # an unknown type leaves it as it was
