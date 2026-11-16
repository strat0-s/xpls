"""When reading fails: a calm message, photos kept, nothing lost, and the lead can be read again or filled in by hand."""
import pytest

from bird_helpers import post_bird
from conftest import csrf
from didac_app import flows
from didac_app.models import NOT_SCANNED
from didac_app.openai_services import classify_failure
from didac_app.server import app

STAFF = "whatsapp:+919800000001"  # the admin from conftest
GOOD = {"fields": {"name": "Asha Rao", "org": "Sunrise School", "phone": "9845011111"}, "check": [], "photos": [1], "more": [], "ms": 3000}
TIMEOUT = {"fields": {}, "check": ["could not read the photos"], "failed": "timeout", "reason": "reading service did not answer in time"}
QUOTA = {"fields": {}, "check": ["could not read the photos"], "failed": "quota", "reason": "reading service quota used up"}


@pytest.fixture
def reads(monkeypatch):
    state = {"result": GOOD, "calls": 0}
    monkeypatch.setattr(flows, "fetch_media", lambda config, url, ct, number: (f"k/{url.rsplit('/', 1)[-1]}", ""))
    monkeypatch.setattr(flows, "read_documents", lambda config, photos, kind: state.__setitem__("calls", state["calls"] + 1) or state["result"])
    return state


def say(body="", media=()):
    return post_bird(app.test_client(), STAFF, body, list(media))


def last(sent):
    return sent[-1][1], [label for label, _ in sent[-1][2]]


def start_scan(photo_name="a"):
    say("Visiting card")
    say("", [(f"https://media.bird.com/{photo_name}", "image/jpeg")])
    say("DONE")


def test_failures_are_classified_for_admins_not_for_the_team():
    class Timeout(Exception):
        pass

    class RateLimitError(Exception):
        status_code, code = 429, "insufficient_quota"

    class APIConnectionError(Exception):
        pass

    assert classify_failure(type("APITimeoutError", (Exception,), {})())[0] == "timeout"
    assert classify_failure(RateLimitError("You exceeded your current quota")) == ("quota", "reading service quota used up")
    assert classify_failure(APIConnectionError())[0] == "outage"
    assert classify_failure(ValueError("x"))[0] == "error"


def test_one_timed_out_read_keeps_the_photo_and_offers_three_ways_on(store, sent, reads):
    reads["result"] = TIMEOUT
    start_scan()
    assert last(sent) == ("That one took longer than I like, and I could not finish it. Your photo is safe.", ["Try again", "Save to read later", "Type the details"])
    assert store.get_session(STAFF)["step"] == "failed" and len(store.get_session(STAFF)["data"]["photos"]) == 1 and store.list_leads() == []
    assert store.reader_status()["state"] == "ok"  # one failure is not an outage


def test_after_five_failures_in_a_row_reading_is_paused_and_a_success_clears_it(store, sent, reads):
    reads["result"] = QUOTA
    start_scan()
    for _ in range(4):
        say("Try again")
    assert store.reader_status()["state"] == "down"
    assert last(sent)[0].startswith("Still not working. Your photos are safe.")
    reads["result"] = GOOD
    say("Try again")
    assert last(sent)[0].startswith("I read this as:") and store.reader_status()["state"] == "ok"


def test_the_first_paused_message_says_nothing_technical(store, sent, reads):
    reads["result"] = QUOTA
    for _ in range(5):
        store.record_read_failure("reading service quota used up")
    start_scan()
    text = last(sent)[0]
    assert text.startswith("I cannot read this just now, but your 1 photo(s) is saved safely to DIDAC 2026. Nothing is lost.")
    assert "quota" not in text.lower() and "openai" not in text.lower()


def test_save_to_read_later_keeps_a_not_scanned_lead_with_its_photos(store, sent, reads):
    reads["result"] = QUOTA
    for _ in range(5):
        store.record_read_failure("reading service quota used up")
    start_scan("p1")
    say("Save to read later")
    lead = store.list_leads()[0]
    assert lead.review_reason == NOT_SCANNED and lead.doc_type == "other" and lead.captured_by == "Test Admin" and lead.event_id == store.live_event()["id"]
    assert [row["local_path"] for row in store.list_media(lead.id)] == ["k/p1"]
    text = last(sent)[0]
    assert f"Saved as lead #{lead.id} with 1 photo(s)." in text and 'marked "Not scanned"' in text and "Please tell someone to check. Reference RD-" in text
    assert store.get_session(STAFF)["step"] == "photos"  # ready for the next scan
    assert store.reader_status()["waiting"] == 1


def test_type_the_details_goes_through_the_normal_summary_and_save(store, sent, reads):
    reads["result"] = TIMEOUT
    start_scan()
    say("Type the details")
    assert last(sent)[0].startswith("No problem. Send the details one per line")
    say("something unreadable")
    assert last(sent)[0].startswith("I could not read that.")
    say("Name: Meera Nair\nPhone: 98450 11112")
    assert last(sent)[0].startswith("I read this as:") and "Meera Nair" in last(sent)[0] and last(sent)[1] == ["Save", "Edit", "Retake"]
    say("Save")
    say("High")
    lead = store.list_leads()[0]
    assert (lead.name, lead.priority, lead.review_reason) == ("Meera Nair", "High", "") and [row["local_path"] for row in store.list_media(lead.id)] == ["k/a"]


def test_a_photo_the_reader_cannot_make_out_is_not_an_outage(store, sent, reads):
    reads["result"] = {"fields": {}, "check": ["could not read the photos"], "unclear": True, "ms": 2000}
    start_scan()
    assert last(sent) == (flows.UNCLEAR, ["Retake", "Type the details"])
    assert store.reader_status()["failures"] == 0
    say("Retake")
    assert store.get_session(STAFF)["data"]["photos"] == []


def test_a_slow_last_read_adds_the_hang_tight_line(store, sent, reads):
    reads["result"] = {**GOOD, "ms": 30000}
    start_scan()
    assert store.reader_status()["state"] == "slow"
    say("Save")
    say("High")
    start_scan("b")
    assert any("This is taking a little longer than usual, so hang tight. Your photos are safe." in m[1] for m in sent)


def test_the_dashboard_banner_and_the_read_again_button(store, sent, monkeypatch):
    lead = store.create_lead(flows.Lead(name="", doc_type="other", review_reason=NOT_SCANNED))
    store.add_media(lead.id, "https://media.bird.com/p", "image/jpeg", "k/p")
    client = app.test_client()
    assert "error reading documents" not in client.get("/").data.decode()
    for _ in range(5):
        store.record_read_failure("reading service quota used up")
    page = client.get(f"/leads/{lead.id}").data.decode()
    assert "There is an error reading documents right now." in page and "For admins only: reading service quota used up" in page
    assert "This document was not scanned" in page and "Read again" in page
    assert "Not scanned" in client.get("/dashboard").data.decode()
    monkeypatch.setattr(flows, "read_documents", lambda config, photos, kind: {"fields": {}, "check": [], "failed": "quota", "reason": "reading service quota used up"})
    client.post(f"/leads/{lead.id}/read", data={"csrf": csrf(client)})
    assert store.get_lead(lead.id).review_reason == NOT_SCANNED  # still failing: nothing changed
    monkeypatch.setattr(flows, "read_documents", lambda config, photos, kind: {"fields": {"name": "Asha Rao", "phone": "9845011111"}, "check": ["phone digits unclear"], "more": [], "ms": 4000})
    client.post(f"/leads/{lead.id}/read", data={"csrf": csrf(client)})
    fixed = store.get_lead(lead.id)
    assert (fixed.name, fixed.phone, fixed.review_reason) == ("Asha Rao", "9845011111", "phone digits unclear") and store.reader_status()["state"] == "ok"


def test_a_lead_nobody_has_read_is_left_out_of_the_export(store):
    store.create_lead(flows.Lead(name="Unread", doc_type="other", review_reason=NOT_SCANNED))
    store.create_lead(flows.Lead(name="Read Person", org="Some School", phone="9845011111", doc_type="other"))
    from didac_app import ui

    leads, _ = ui.export_leads(store, {})
    assert [lead.name for lead in leads] == ["Read Person"]
