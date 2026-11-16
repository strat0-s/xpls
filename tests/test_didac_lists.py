"""The long lists: pages with numbers, the event filter, whole-row links, event photos, who the follow-up is going to."""
import re

from conftest import csrf
from didac_app import blobstore, flows, followup
from didac_app.models import NOT_SCANNED
from didac_app.pager import paginate
from didac_app.server import app


def test_paginate_shows_the_right_slice_and_numbers_with_gaps():
    page = paginate(list(range(312)), page=5, per=25)
    assert (page["pages"], page["first"], page["last"], page["total"]) == (13, 101, 125, 312)
    assert page["numbers"] == [1, None, 4, 5, 6, None, 13] and page["items"][0] == 100
    assert paginate([], 3)["pages"] == 1 and paginate([], 3)["first"] == 0
    assert paginate(list(range(5)), 99, 25)["page"] == 1 and paginate(list(range(30)), 1, 7)["per"] == 25  # a wild page or size falls back


def test_the_leads_list_shows_every_lead_a_page_at_a_time(store):
    for number in range(60):
        store.create_lead(flows.Lead(name=f"Person {number:02d}", phone=f"+9198000{number:05d}"))
    client = app.test_client()
    first = client.get("/dashboard").data.decode()
    assert "Showing 1–25 of 60" in first and "Person 59" in first and "Person 34" not in first  # newest first
    second = client.get("/dashboard?page=2").data.decode()
    assert "Showing 26–50 of 60" in second and "Person 34" in second and 'aria-current=page' in second
    assert "Showing 1–50 of 60" in client.get("/dashboard?per=50").data.decode()
    assert "Person 00" in client.get("/dashboard?page=3").data.decode()


def test_a_whole_row_is_one_link_to_the_lead(store):
    lead = store.create_lead(flows.Lead(name="Clickable Person", org="Some School"))
    html = app.test_client().get("/dashboard").data.decode()
    row = re.search(r'<a class="lead[^"]*" href="/leads/%d">(.*?)</a>' % lead.id, html, re.S)
    assert row and "Clickable Person" in row.group(1) and "Some School" in row.group(1) and "DIDAC 2026" in row.group(1)


def test_the_event_filter_defaults_to_the_live_event(store):
    first = store.live_event()
    store.create_lead(flows.Lead(name="At DIDAC"))
    second = store.create_event("Bett India 2027")
    store.start_event(second)
    store.create_lead(flows.Lead(name="At Bett"))
    client = app.test_client()
    live = client.get("/dashboard").data.decode()
    assert "At Bett" in live and "At DIDAC" not in live
    assert "At DIDAC" in client.get(f"/dashboard?event={first['id']}").data.decode()
    everything = client.get("/dashboard?event=all").data.decode()
    assert "At Bett" in everything and "At DIDAC" in everything


def test_type_tabs_come_from_the_events_document_types_and_a_check_filter_exists(store):
    store.create_lead(flows.Lead(name="Needs A Look", doc_type="form", review_reason="phone unclear"))
    store.create_lead(flows.Lead(name="All Fine", doc_type="form"))
    html = app.test_client().get("/dashboard").data.decode()
    assert "Contact form · 2" in html and "Other docs · 0" in html and "Only leads that need checking (1)" in html
    only = app.test_client().get("/dashboard?check=1").data.decode()
    assert "Needs A Look" in only and "All Fine" not in only


def test_event_photos_page_lists_them_and_serves_them_through_a_signed_link(store, monkeypatch):
    event = store.live_event()
    photo_id = store.add_event_photo(event["id"], "whatsapp:+919800000001", "Test Admin", "2026-11-16/x/a.jpg", "image/jpeg", "https://media/x")
    monkeypatch.setattr(blobstore, "signed_url", lambda config, key, expires_in=3600: f"https://bucket.example/{key}")
    client = app.test_client()
    page = client.get("/photos").data.decode()
    assert f"/event-photo/{photo_id}" in page and "Test Admin" in page and "Showing 1–1 of 1" in page
    assert client.get(f"/event-photo/{photo_id}").headers["Location"] == "https://bucket.example/2026-11-16/x/a.jpg"
    assert client.get("/event-photo/999").status_code == 404


def test_followup_says_who_is_waiting_who_is_skipped_and_why(store, admin):
    store.create_lead(flows.Lead(name="Gets It", phone="+919845011111"))
    store.create_lead(flows.Lead(name="Landline Lady", phone="+918023456789"))
    store.create_lead(flows.Lead(name="No Phone"))
    store.create_lead(flows.Lead(name="Unread", review_reason=NOT_SCANNED))
    store.create_lead(flows.Lead(name="Team Mate", phone=admin["phone"]))
    store.create_lead(flows.Lead(name="Said Stop", phone="+919845022222"))
    store.opt_out("+919845022222")
    waiting, skipped = followup.overview(store, flows.DidacConfig(followup_template_id="t"), store.live_event()["id"])
    assert [row["lead"].name for row in waiting] == ["Gets It"] and waiting[0]["next"]
    assert {row["lead"].name: row["why"] for row in skipped} == {"Landline Lady": "Landline", "No Phone": "No phone number", "Unread": "Not scanned", "Team Mate": "Team member", "Said Stop": "Replied STOP"}
    page = app.test_client().get("/followup").data.decode()
    assert "Will be sent · 1" in page and "Skipped · 5" in page and "Replied STOP" in page and "Next batch" in page


def test_export_filters_by_event_and_type_and_lists_every_row_in_pages(store):
    for number in range(30):
        store.create_lead(flows.Lead(name=f"Exporter {number:02d}", org=f"School {number:02d}", phone=f"+9198000{number:05d}", doc_type="form" if number % 2 else "other"))
    client = app.test_client()
    page = client.get("/export").data.decode()
    assert "Showing 1–25 of 30" in page and "Document type" in page
    assert "Showing 1–15 of 15" in client.get("/export?doc=form").data.decode()
    other = store.create_event("Bett India 2027")
    assert "No leads match these filters." in client.get(f"/export?event={other}").data.decode()
    assert client.get("/export/orbit.csv?doc=form").status_code == 200
