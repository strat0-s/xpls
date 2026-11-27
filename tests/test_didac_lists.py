"""The long lists: pages with numbers, the event filter, whole-row links, event photos, who the follow-up is going to."""
import re

from conftest import csrf, get_or_create_user
from didac_app import blobstore, flows, followup
from didac_app.models import NOT_SCANNED
from didac_app.pager import paginate
from didac_app.server import app


def test_paginate_shows_the_right_slice_and_numbers_with_gaps():
    page = paginate(list(range(312)), page=5, per=25)
    assert (page["pages"], page["first"], page["last"], page["total"]) == (13, 101, 125, 312)
    assert page["numbers"] == [1, None, 4, 5, 6, None, 13] and page["items"][0] == 100
    assert paginate([], 3)["pages"] == 1 and paginate([], 3)["first"] == 0
    assert paginate(list(range(5)), 99, 25)["page"] == 1 and paginate(list(range(30)), 1, 7)["per"] == 10  # a wild page or size falls back


def test_the_leads_list_shows_every_lead_a_page_at_a_time(store):
    for number in range(60):
        store.create_lead(flows.Lead(name=f"Person {number:02d}", phone=f"+9198000{number:05d}"))
    client = app.test_client()
    first = client.get("/dashboard").data.decode()
    assert "Showing 1–10 of 60" in first and "Person 59" in first and "Person 49" not in first  # newest first
    second = client.get("/dashboard?page=2").data.decode()
    assert "Showing 11–20 of 60" in second and "Person 49" in second and 'aria-current=page' in second
    assert "Showing 1–50 of 60" in client.get("/dashboard?per=50").data.decode()
    assert "Person 00" in client.get("/dashboard?page=6").data.decode()


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
    assert "Showing 1–10 of 30" in page and "Document type" in page
    assert "Showing 1–10 of 15" in client.get("/export?doc=form").data.decode()
    other = store.create_event("Bett India 2027")
    assert "No leads match these filters." in client.get(f"/export?event={other}").data.decode()
    assert client.get("/export/orbit.csv?doc=form").status_code == 200


def test_leads_list_and_lead_page_show_the_capture_date_with_the_time(store):
    from datetime import datetime

    from didac_app.ui import IST

    lead = store.create_lead(flows.Lead(name="Dated Person"))
    when = datetime.fromisoformat(lead.created_at).astimezone(IST)
    client = app.test_client()
    assert f"<b style=\"font-weight:600\">{when.strftime('%d %b')}</b><small>{when.strftime('%H:%M')}</small>" in client.get("/dashboard").data.decode()
    assert when.strftime("captured %d %b %Y, %H:%M") in client.get(f"/leads/{lead.id}").data.decode()


def test_lead_numbers_show_in_the_list_and_on_the_lead_page(store):
    lead = store.create_lead(flows.Lead(name="Numbered Person"))
    client = app.test_client()
    assert f"#{lead.id}</span> Numbered Person" in client.get("/dashboard").data.decode()
    assert f"#{lead.id}</small>" in client.get(f"/leads/{lead.id}").data.decode()


def test_lead_owner_is_a_column_in_the_leads_list(store):
    store.create_lead(flows.Lead(name="Owned Person", lead_owner="Magesh Kumar AP"))
    page = app.test_client().get("/dashboard").data.decode()
    assert "<span>Lead owner</span>" in page and "<span>Magesh Kumar AP</span>" in page


def test_the_admin_follow_up_report_counts_what_went_out_and_what_was_held_back(store, admin):
    store.create_lead(flows.Lead(name="Gets It", phone="+919845011111"))
    store.create_lead(flows.Lead(name="Half Number", phone="99024*****"))
    store.create_lead(flows.Lead(name="No Phone"))
    sent = store.create_lead(flows.Lead(name="Refused", phone="+919845033333"))
    store.claim_followup("+919845033333", sent.id)
    store.update_followup_status("+919845033333", "delivery_failed", "facebook: (code: 131049; title: not delivered)")
    page = app.test_client().get("/admin/followup").data.decode()
    assert "Number incomplete" in page and "No phone number" in page and "WhatsApp declined it" in page and "Not sent, by reason · 2" in page
    assert "When the number is corrected" in page


def test_the_admin_follow_up_report_has_a_row_for_every_lead(store, admin):
    store.create_lead(flows.Lead(name="Gets It", phone="+919845011111"))
    store.create_lead(flows.Lead(name="No Phone"))
    sent = store.create_lead(flows.Lead(name="Refused", phone="+919845033333"))
    store.claim_followup("+919845033333", sent.id)
    store.update_followup_status("+919845033333", "delivery_failed", "facebook: (code: 131026; title: x)")
    client = app.test_client()
    page = client.get("/admin/followup").data.decode()
    assert "Every lead" in page and "Number is not on WhatsApp" in page and "Waiting · 1" in page and "Failed · 1" in page
    only = client.get("/admin/followup?show=failed").data.decode()
    table = only.split('id="every"')[1].split("Failed after")[0]
    assert table.count('class="fr"') == 1 and "Refused" in table and "Captured" in table and "Sent " in table


def test_the_admin_follow_up_report_explains_states_on_hover_and_says_when_the_cap_is_reached(store, admin, monkeypatch):
    store.create_lead(flows.Lead(name="Half Number", phone="99024*****"))
    client = app.test_client()
    page = client.get("/admin/followup").data.decode()
    assert 'title="A number was read but it is not a full mobile number' in page and "Daily cap reached" not in page
    monkeypatch.setattr(store, "followups_last_day", lambda: 100)
    assert "Daily cap reached: 100 of 100" in client.get("/admin/followup").data.decode()


def test_leads_of_one_person_can_be_merged_into_one(store, admin):
    from didac_app.models import Lead
    first = store.create_lead(Lead(name="Gangineni Dhananjhay", phone="+919391319721", from_number="a", text_notes="Wants a demo"))
    second = store.create_lead(Lead(name="Dr. Gangineni D", org="Narayana Group", email="deanmba@narayanagroup.com", designation="Dean", phone="+919999999999", from_number="a", review_reason="possible duplicate of #1"))
    store.add_media(second.id, "http://u", "image/jpeg", "k/card.jpg", "")
    store.add_feedback("a", "Loved it", second.id)
    store.claim_followup("+919999999999", second.id)
    client = app.test_client()
    page = client.get(f"/leads/merge?ids={first.id},{second.id}").data.decode()
    assert "Merge 2 leads into one" in page and "deanmba@narayanagroup.com" in page and f'value="{second.id}" checked' in page  # the fuller lead is the default
    assert client.get(f"/leads/merge?ids={first.id}").status_code == 400 and client.get(f"/leads/merge?ids={first.id},999").status_code == 400
    done = client.post("/leads/merge", data={"csrf": csrf(client), "ids": f"{first.id},{second.id}", "keep": str(first.id)})
    assert done.status_code == 302
    kept = store.get_lead(first.id)
    assert store.get_lead(second.id) is None and kept.org == "Narayana Group" and kept.email == "deanmba@narayanagroup.com" and kept.phone == "+919391319721"
    assert "Also on #" in kept.notes and "+919999999999" in kept.notes and kept.text_notes == "Wants a demo" and kept.review_reason == ""
    assert len(store.list_media(first.id)) == 1 and [row["lead_id"] for row in store.list_followups() if row["phone"] == "+919999999999"] == [first.id]
    gallery = client.get("/dashboard").data.decode()
    assert "Select to merge" in gallery and 'class="sel"' in gallery


def test_the_leads_list_filters_by_who_created_the_lead_and_who_owns_it(store, admin):
    store.create_lead(flows.Lead(name="Made By Asha", captured_by="Asha", lead_owner="Ravi", from_number="a"))
    store.create_lead(flows.Lead(name="Made By Bina", captured_by="Bina", lead_owner="Ravi", from_number="b"))
    store.create_lead(flows.Lead(name="Nobody Owns", captured_by="Asha", from_number="a"))
    client = app.test_client()
    page = client.get("/dashboard").data.decode()
    assert 'id="fl-by"' in page and 'id="fl-owner"' in page and ">Ravi</option>" in page
    names = lambda html: [n for n in ("Made By Asha", "Made By Bina", "Nobody Owns") if n in html.split('class="grid"')[1]]
    assert names(client.get("/dashboard?by=Asha").data.decode()) == ["Made By Asha", "Nobody Owns"]
    assert names(client.get("/dashboard?owner=Ravi").data.decode()) == ["Made By Asha", "Made By Bina"]
    assert names(client.get("/dashboard?owner=none").data.decode()) == ["Nobody Owns"]
    assert names(client.get("/dashboard?by=Asha&owner=Ravi").data.decode()) == ["Made By Asha"]
    assert len(names(client.get("/dashboard?by=Nobody+Real").data.decode())) == 3  # an unknown name is ignored, not an empty list


def test_duplicate_review_stores_verdicts_apart_and_leaves_the_leads_alone(store, admin):
    first = store.create_lead(flows.Lead(name="Asha Rao", org="Zenith School", phone="+919845011111", captured_by="Rahul"))
    second = store.create_lead(flows.Lead(name="Asha Rao", org="Zenith School", phone="+919845011111", captured_by="Sahana"))
    lone = store.create_lead(flows.Lead(name="Zed Quill", org="Nowhere Ltd", phone="+919000000001", captured_by="Sahana"))
    before = [(lead.id, lead.name, lead.notes, lead.phone) for lead in store.list_leads()]
    client = app.test_client()
    page = client.get("/dupes").data.decode()
    assert "0 of 2 reviewed" in page and "Clear duplicate" in page and "Same person as #1" in page
    save = lambda lead, **data: client.post(f"/dupes/{lead.id}", data={"csrf": csrf(client), **data})
    assert save(second, act="match", other=first.id).status_code == 302
    assert store.dup_reviews()[second.id]["verdict"] == "duplicate" and store.dup_reviews()[second.id]["matches"] == str(first.id)
    assert "Duplicate of #1" in client.get("/dupes?show=done").data.decode()
    save(second, act="add", number="3")  # a lead typed in by number is added as a match
    assert store.dup_reviews()[second.id]["matches"] == f"{first.id},{lone.id}"
    save(second, act="unmatch", other=first.id)
    save(second, act="unmatch", other=lone.id)
    assert second.id not in store.dup_reviews()
    save(lone, act="new", note="walked in alone")
    assert store.dup_reviews()[lone.id]["verdict"] == "new" and store.dup_reviews()[lone.id]["note"] == "walked in alone"
    exported = client.get("/dupes/export.csv").data.decode()
    assert "Zed Quill,new" in exported and "walked in alone" in exported and "Asha" not in exported
    assert [(lead.id, lead.name, lead.notes, lead.phone) for lead in store.list_leads()] == before


def test_duplicate_review_is_for_admins_and_staff_an_admin_picks(store, admin):
    staff = get_or_create_user(store, name="Staffer", phone="+919845022222", email="", password="a-long-password", is_admin=False)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["uid"], sess["sv"] = staff["id"], staff["session_version"]
    assert client.get("/dupes").status_code == 403
    assert "Duplicate review" not in client.get("/dashboard").data.decode()
    admin_client = app.test_client()
    assert admin_client.post(f"/team/{staff['id']}/dupes", data={"csrf": csrf(admin_client)}).status_code == 302
    assert client.get("/dupes").status_code == 200 and "Duplicate review" in client.get("/dashboard").data.decode()
    assert client.get("/dupes/export.csv").status_code == 403  # the list is for admins
    assert client.post(f"/team/{staff['id']}/dupes", data={"csrf": csrf(client)}).status_code == 403
    admin_client.post(f"/team/{staff['id']}/dupes", data={"csrf": csrf(admin_client)})
    assert client.get("/dupes").status_code == 403


def test_a_lead_page_returns_to_the_same_page_number_page_size_and_filters(store):
    for i in range(30):
        store.create_lead(flows.Lead(name=f"Person {i:02d}"))
    client = app.test_client()
    client.get("/dashboard?page=3&per=10&q=person")
    page = client.get("/leads/1").data.decode()
    assert 'href="/dashboard?page=3&amp;per=10&amp;q=person"' in page and "All leads" in page
    assert client.post("/leads/1", data={"csrf": csrf(client), "name": "Person Edited", "export_checked": "Yes"}).status_code == 302
    assert 'href="/dashboard?page=3&amp;per=10&amp;q=person"' in client.get("/leads/1").data.decode()  # saving does not lose the place
    assert client.post("/leads/1/delete", data={"csrf": csrf(client)}).headers["Location"].endswith("/dashboard?page=3&per=10&q=person")


def test_checked_for_export_turns_the_row_green_and_nothing_in_specific_is_a_product(store):
    lead = store.create_lead(flows.Lead(name="Greenable Person"))
    client = app.test_client()
    from didac_app.storage import DEFAULT_PRODUCTS

    assert "Nothing in specific" in DEFAULT_PRODUCTS  # a new install starts with it; an existing one gets the row added once
    client.post(f"/leads/{lead.id}", data={"csrf": csrf(client), "name": "Greenable Person", "export_checked": "Yes", "products": "Nothing in specific"})
    saved = store.get_lead(lead.id)
    assert saved.export_checked == "Yes" and saved.products == "Nothing in specific"
    assert re.search(r'class="lead\s+exp"[^>]*href="/leads/%d"' % lead.id, client.get("/dashboard").data.decode())
    client.post(f"/leads/{lead.id}", data={"csrf": csrf(client), "name": "Greenable Person"})  # unticked
    assert store.get_lead(lead.id).export_checked == ""
