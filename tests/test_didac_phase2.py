"""Two documents in one photo, event photos for the team, and brochures for the team."""
import pytest

from bird_helpers import post_bird
from didac_app import flows
from didac_app.server import app

STAFF = "whatsapp:+919800000001"  # the admin from conftest
VISITOR = "whatsapp:+919811111111"
ONE = {"fields": {"name": "Asha Rao", "org": "Sunrise School", "phone": "9845011111"}, "check": [], "photos": [1], "more": []}
TWO = {**ONE, "more": [{"fields": {"name": "Ravi Menon", "org": "Menon Trust", "phone": "9845022222"}, "check": ["phone digits unclear"], "photos": [1]}]}


@pytest.fixture
def reads(monkeypatch):
    state = {"result": ONE, "calls": []}
    monkeypatch.setattr(flows, "fetch_media", lambda config, url, ct, number: (f"k/{url.rsplit('/', 1)[-1]}", ""))
    monkeypatch.setattr(flows, "read_documents", lambda config, photos, kind: state["calls"].append((len(photos), kind)) or state["result"])
    return state


def say(number, body="", media=()):
    return post_bird(app.test_client(), number, body, list(media))


def photo(name):
    return [(f"https://media.bird.com/{name}", "image/jpeg")]


def last(sent):
    return sent[-1][1], [label for label, _ in sent[-1][2]]


def scan_two(reads):
    reads["result"] = TWO
    say(STAFF, "Other docs")
    say(STAFF, "", photo("both"))
    say(STAFF, "DONE")


def test_two_documents_are_shown_together_and_saved_as_two_leads(store, sent, reads):
    scan_two(reads)
    text = last(sent)[0]
    assert text.startswith("I read 2 documents:") and "Asha Rao" in text and "Ravi Menon" in text and "phone digits unclear" in text
    say(STAFF, "Save")
    assert last(sent) == ("How important are these leads?", ["High", "Medium", "Low"])
    say(STAFF, "High")
    leads = sorted(store.list_leads(), key=lambda lead: lead.id)
    assert [(lead.name, lead.priority, lead.doc_type, lead.event_id) for lead in leads] == [("Asha Rao", "High", "other", store.live_event()["id"]), ("Ravi Menon", "High", "other", store.live_event()["id"])]
    assert leads[1].review_reason == "phone digits unclear" and last(sent)[0].startswith("Saved 2 documents:")


def test_a_correction_line_can_start_with_the_document_number(store, sent, reads):
    scan_two(reads)
    say(STAFF, "Edit")
    assert "Start a line with 1 or 2" in last(sent)[0]
    say(STAFF, "2 Phone: 98450 11113\nPriority: High")
    assert "Ravi Menon" in last(sent)[0] and "98450 11113" in last(sent)[0]  # the numbered line went to the second document
    say(STAFF, "Save")
    assert last(sent)[1] == ["High", "Medium", "Low"]  # Priority: High reached only the first one


def test_a_photo_with_too_many_documents_is_cleared_and_asked_again(store, sent, reads):
    reads["result"] = {**ONE, "too_many": (1, 3)}
    say(STAFF, "Other docs")
    say(STAFF, "", photo("crowd"))
    say(STAFF, "DONE")
    assert "I can see 3 documents in photo 1. Two at most per photo" in last(sent)[0]
    assert store.get_session(STAFF)["data"]["photos"] == [] and store.list_leads() == []


def test_a_single_document_flow_reads_exactly_as_before(store, sent, reads):
    say(STAFF, "Other docs")
    say(STAFF, "", photo("one"))
    say(STAFF, "DONE")
    assert last(sent)[0].startswith("I read this as:")


def test_lead_fields_keeps_only_whole_photo_numbers():
    doc = flows.lead_fields({"name": "Asha Rao", "phone": "9845011111", "photos": [1, "x", 2], "check": []})
    assert doc["photos"] == [1, 2]


def test_event_photos_are_stored_for_the_live_event(store, sent, reads):
    say(STAFF, "Send event photos")
    assert "saved to DIDAC 2026 and are not read" in last(sent)[0]
    say(STAFF, "", photo("a"))
    say(STAFF, "", photo("b"))
    assert last(sent)[0] == "1 photo saved to DIDAC 2026."
    rows = store.list_event_photos(store.live_event()["id"])
    assert [row["key"] for row in rows] == ["k/b", "k/a"] and rows[0]["uploaded_by"] == "Test Admin" and store.list_leads() == []
    say(STAFF, "text instead")
    assert last(sent)[0] == "Send photos, or type menu when you are done."
    say(STAFF, "menu")
    assert store.get_session(STAFF) is None


def test_visitors_cannot_send_photos_but_voice_notes_are_kept(store, sent, reads):
    say(VISITOR, "", photo("x"))
    assert last(sent)[0] == flows.NO_PHOTOS and store.list_event_photos(store.live_event()["id"]) == [] and store.list_leads() == []
    say(VISITOR, "", [("https://media.bird.com/v", "audio/ogg")])
    assert last(sent)[0] == "Saved for the team. Type menu for options."


def test_the_team_sees_team_only_brochures_and_looking_one_up_is_not_a_lead(store, sent):
    event = store.live_event()
    store.add_brochure(event["id"], "Internal price list", "https://x/prices.pdf", for_visitors=False)
    say(STAFF, "Brochures")
    assert "Internal price list" in last(sent)[1]
    say(STAFF, "Internal price list")
    assert "https://x/prices.pdf" in last(sent)[0] and "This one is for the team only." in last(sent)[0]
    assert store.list_leads() == []
    say(VISITOR, "Download brochure")
    assert "Internal price list" not in last(sent)[1]  # visitors never see it


def test_the_brochures_row_is_missing_when_the_team_has_none(store, sent):
    for row in store.list_brochures(store.live_event()["id"]):
        store.set_brochure_active(row["id"], False)
    say(STAFF, "menu")
    assert "Brochures" not in last(sent)[1]
