import pytest

from bird_helpers import post_bird
from didac_app import bird, flows
from didac_app.server import app

real_read_documents = flows.read_documents  # the autouse fixture replaces it

STAFF = "whatsapp:+919800000001"  # the admin from conftest
VISITOR = "whatsapp:+919811111111"
CARD = {"fields": {"name": "Asha Rao", "org": "Sunrise School", "role": "Principal", "phone": "9845011111", "email": "asha@sunrise.edu", "organisation_type": "SCHOOL"},
        "check": ["phone digits unclear"]}


@pytest.fixture(autouse=True)
def fake_io(monkeypatch):
    """No bucket, no OpenAI: a stored photo is just its URL tail, and a read returns whatever `reads` holds."""
    monkeypatch.setattr(flows, "fetch_media", lambda config, url, content_type, number: (f"k/{url.rsplit('/', 1)[-1]}", "heard: call me" if content_type.startswith("audio/") else ""))
    reads = {"result": CARD, "calls": []}
    monkeypatch.setattr(flows, "read_documents", lambda config, photos, kind: reads["calls"].append((len(photos), kind)) or reads["result"])
    return reads


def say(number, body="", media=()):
    return post_bird(app.test_client(), number, body, list(media))


def photo(name):
    return [(f"https://media.bird.com/{name}", "image/jpeg")]


def save_scan(number):
    """Save asks for a priority before it stores the lead."""
    say(number, "Save")
    say(number, "High")


def last(sent):
    return sent[-1][1], [label for label, _ in sent[-1][2]]


def test_staff_menu_is_a_four_row_list_and_hi_creates_no_lead(store, sent):
    say(STAFF, "menu")
    assert last(sent) == ("Hi Test. What would you like to do?\n\nType menu any time. Type cancel to stop.", ["Contact form", "Other docs (cards, etc.)", "Event badge / QR", "Add a note"])
    say(VISITOR, "hi")
    assert store.list_leads() == []


def test_visitor_menu_is_a_list_without_demo_or_badge(sent):
    say(VISITOR, "menu")
    text, rows = last(sent)
    assert rows == ["Download brochure", "Share my requirement", "Feedback / voice note"]
    assert "reply with a number" not in text
    say(VISITOR, "Book a demo")
    assert last(sent)[1] == rows  # no longer an option: the menu is shown again


def test_document_with_two_photos_edit_then_save(store, sent, fake_io):
    say(STAFF, "Other document")
    say(STAFF, "", photo("front"))
    assert "Photo 1 received" in last(sent)[0]
    say(STAFF, "", photo("back"))
    assert "Photo 2 received" in last(sent)[0]
    say(STAFF, "done")
    text, labels = last(sent)
    assert fake_io["calls"] == [(2, "document")]
    assert "Name: Asha Rao" in text and "Type: School" in text and "Check: phone digits unclear" in text
    assert labels == ["Save", "Edit", "Retake"]
    say(STAFF, "Edit")
    say(STAFF, "Phone: 98450 11112")
    text, labels = last(sent)
    assert "Phone: 98450 11112" in text and "Check:" not in text and labels == ["Save", "Edit", "Retake"]
    save_scan(STAFF)
    lead = store.list_leads()[0]
    assert (lead.org, lead.phone, lead.doc_type, lead.captured_by, lead.review_reason) == ("Sunrise School", "98450 11112", "other", "Test Admin", "")
    assert "Phone: 9845011111" in lead.raw_message  # the scanner's first reading is kept apart from the correction
    assert [m["local_path"] for m in store.list_media(lead.id)] == ["k/back", "k/front"]
    assert f"Saved as #{lead.id}, Sunrise School (High priority)." in last(sent)[0]
    say(STAFF, "", photo("next"))  # stays in the flow for the next document
    assert "Photo 1 received" in last(sent)[0]


def test_edit_can_be_ended_with_back_or_a_button_tap(store, sent, fake_io):
    say(STAFF, "Other document")
    say(STAFF, "", photo("a"))
    say(STAFF, "done")
    say(STAFF, "Edit")
    say(STAFF, "back")  # ends the edit, summary shown again
    assert last(sent)[1] == ["Save", "Edit", "Retake"] and "Name: Asha Rao" in last(sent)[0]
    say(STAFF, "Edit")
    say(STAFF, "Retake")  # a stale button tap while editing is not read as a correction
    assert "Photos cleared" in last(sent)[0]
    say(STAFF, "", photo("b"))
    assert "Photo 1 received" in last(sent)[0]


def test_saving_with_a_check_left_shows_the_badge(store, sent):
    say(STAFF, "Other document")
    say(STAFF, "", photo("a"))
    say(STAFF, "DONE")
    save_scan(STAFF)
    assert store.list_leads()[0].review_reason == "phone digits unclear"
    assert "Check badge" in last(sent)[0]


def test_contact_form_is_filed_as_a_form(store, sent, fake_io):
    fake_io["result"] = {"fields": {"name": "Meera Iyer", "org": "Bright School", "notes": "Students: PUC 81; Remarks: Call back Friday"}, "check": ["student count is handwritten"]}
    say(STAFF, "Contact form")
    say(STAFF, "", photo("form"))
    say(STAFF, "DONE")
    assert fake_io["calls"] == [(1, "form")]
    save_scan(STAFF)
    lead = store.list_leads()[0]
    assert lead.doc_type == "form" and "PUC 81" in lead.notes


def test_nothing_is_shortened_in_the_scan_summary(store, sent, fake_io):
    short_notes = "Remarks: " + "wants a science lab for every school in the district " * 5  # fits one button message
    fake_io["result"] = {"fields": {"name": "Meera Iyer", "notes": short_notes}, "check": []}
    say(STAFF, "Contact form")
    say(STAFF, "", photo("form"))
    say(STAFF, "DONE")
    assert short_notes in sent[-1][1] and "..." not in sent[-1][1] and [label for label, _ in sent[-1][2]] == ["Save", "Edit", "Retake"]

    long_notes = "Remarks: " + "wants a science lab for every school in the district " * 30  # too long for a button message
    fake_io["result"] = {"fields": {"name": "Meera Iyer", "notes": long_notes}, "check": []}
    say(STAFF, "cancel")
    say(STAFF, "Contact form")
    say(STAFF, "", photo("form"))
    say(STAFF, "DONE")
    assert long_notes in sent[-2][1] and sent[-2][2] == [] and [label for label, _ in sent[-1][2]] == ["Save", "Edit", "Retake"]
    save_scan(STAFF)
    assert store.list_leads()[0].notes == long_notes


def test_other_documents_row_and_the_plain_label_both_start_a_scan(store, sent):
    say(STAFF, "Other docs (cards, etc.)")
    assert "Send photos of the document" in last(sent)[0]
    say(STAFF, "cancel")
    say(STAFF, "Other document")
    assert "Send photos of the document" in last(sent)[0]


def test_priority_can_be_set_in_edit_and_then_save_does_not_ask(store, sent, fake_io):
    say(STAFF, "Contact form")
    say(STAFF, "", photo("form"))
    say(STAFF, "DONE")
    say(STAFF, "Edit")
    say(STAFF, "Priority: urgent")
    assert "I could not read a correction" in last(sent)[0]
    say(STAFF, "Priority: medium")
    assert "Priority: Medium" in last(sent)[0]
    say(STAFF, "Save")
    assert "Saved as #" in last(sent)[0] and [lead.priority for lead in store.list_leads()] == ["Medium"]


def test_save_asks_for_a_priority_and_stores_it(store, sent, fake_io):
    say(STAFF, "Contact form")
    say(STAFF, "", photo("form"))
    say(STAFF, "DONE")
    assert "Priority: (not set, asked on Save)" in sent[-1][1]
    say(STAFF, "Save")
    assert last(sent) == ("How important is this lead?", ["High", "Medium", "Low"]) and store.list_leads() == []
    say(STAFF, "maybe")
    assert last(sent)[0] == "Please tap High, Medium or Low." and store.list_leads() == []
    say(STAFF, "Low")
    assert [lead.priority for lead in store.list_leads()] == ["Low"]


def test_visitor_leads_start_unclassified_and_staff_tag_them(store, sent):
    say(VISITOR, "Share my requirement")
    for answer in ("Asha", "asha@sunrise.edu", "Sunrise School", "School", "Mysuru, Karnataka", "Science labs"):
        say(VISITOR, answer)
    lead = store.list_leads()[0]
    assert lead.priority == ""
    client = app.test_client()
    assert "Unclassified" in client.get("/dashboard").data.decode()
    from conftest import csrf
    client.post(f"/leads/{lead.id}", data={"csrf": csrf(client), "name": "Asha", "priority": "High", "doc_type": "other"})
    assert store.get_lead(lead.id).priority == "High" and 'chip high">High' in client.get("/dashboard").data.decode()


def test_all_tab_is_the_default_and_lists_every_type(store, sent):
    store.create_lead(flows.Lead(name="A Form", doc_type="form"))
    store.create_lead(flows.Lead(name="A Badge", doc_type="badge"))
    store.create_lead(flows.Lead(name="A Card"))
    client = app.test_client()
    page = client.get("/dashboard").data.decode()
    assert all(name in page for name in ("A Form", "A Badge", "A Card"))
    assert "A Card" not in client.get("/dashboard?type=forms").data.decode()


def test_repeat_scan_merges_and_name_org_match_is_flagged(store, sent, fake_io):
    def scan(fields):
        fake_io["result"] = {"fields": fields, "check": []}
        say(STAFF, "Other document")
        say(STAFF, "", photo("p"))
        say(STAFF, "DONE")
        save_scan(STAFF)

    scan({"name": "Kavya Nair", "org": "Dedupe Academy", "phone": "9845033333"})
    first = store.list_leads()[0]
    scan({"name": "Kavya N", "phone": "+91 98450 33333", "email": "kavya@dedupe.edu"})
    assert f"Already saved as #{first.id}" in last(sent)[0] and "added the new details" in last(sent)[0]
    assert len(store.list_leads()) == 1 and store.get_lead(first.id).email == "kavya@dedupe.edu"
    scan({"name": "Kavya Nair", "phone": "+91 98450 33333"})  # nothing new
    assert "nothing new" in last(sent)[0] and len(store.list_leads()) == 1
    scan({"name": "Tara Menon", "org": "Flag School", "phone": "9845055555"})
    scan({"name": "Tara Menon", "org": "Flag School", "phone": "9845066666"})
    assert store.list_leads()[0].review_reason.startswith("possible duplicate of #")


def test_photo_without_a_choice_asks_once_then_follows_the_tap(store, sent, fake_io):
    say(STAFF, "", photo("one"))
    say(STAFF, "", photo("two"))
    asks = [m for m in sent if m[1].startswith("Is this a contact form")]
    assert len(asks) == 1 and [label for label, _ in asks[0][2]] == ["Contact form", "Other document", "Event badge / QR"]
    say(STAFF, "Contact form")
    assert "Got 2 photo(s)" in last(sent)[0]
    say(STAFF, "DONE")
    assert fake_io["calls"] == [(2, "form")]


def test_photo_limit_done_without_photos_and_menu_discards(store, sent):
    say(STAFF, "Other document")
    say(STAFF, "DONE")
    assert last(sent)[0] == "No photos yet. Send a photo first."
    for index in range(11):
        say(STAFF, "", photo(f"p{index}"))
    assert "limit of 10 photos" in last(sent)[0]
    say(STAFF, "menu")
    assert sent[-2][1] == "Discarded 10 photo(s) that were not saved."
    assert store.get_session(STAFF) is None and store.list_leads() == []


def test_idle_session_times_out(store, sent):
    say(STAFF, "Other document")
    say(STAFF, "", photo("p"))
    with store.connect() as conn:
        conn.execute("UPDATE sessions SET updated_at = now() - interval '31 minutes'")
    say(STAFF, "DONE")
    assert last(sent)[0] == "That scan timed out and was not saved. Type menu to start again."
    assert store.get_session(STAFF) is None


def test_photos_in_one_batch_are_appended_atomically(store):
    store.put_session(STAFF, "document", "photos", {"photos": []})
    counts = [store.add_session_photo(STAFF, {"key": f"k{i}"}, 3) for i in range(5)]
    assert counts == [1, 2, 3, None, None]


def test_note_goes_to_the_last_lead_and_voice_is_transcribed(store, sent):
    lead = store.create_lead(flows.Lead(from_number=STAFF, name="Meera", org="Bright School"))
    say(STAFF, "Add a note")
    assert f"added to #{lead.id}, Bright School" in last(sent)[0]
    say(STAFF, "Principal wants a callback")
    say(STAFF, "", [("https://media.bird.com/v1", "audio/ogg")])
    noted = store.get_lead(lead.id)
    assert "callback" in noted.text_notes and noted.audio_transcript == "heard: call me"
    assert f"Added to #{lead.id}" in last(sent)[0]
    assert any(m["content_type"] == "audio/ogg" and m["extracted_text"] == "heard: call me" for m in store.list_media(lead.id))


def test_note_without_a_recent_lead_needs_a_name_or_phone(store, sent):
    say(STAFF, "Add a note")
    assert "no lead from the last 30 minutes" in last(sent)[0]
    say(STAFF, "just chatting")
    assert "name or a phone number" in last(sent)[0] and store.list_leads() == []
    say(STAFF, "Name: Ravi Menon\nPhone: 9845099999")
    lead = store.list_leads()[0]
    assert lead.name == "Ravi Menon" and lead.review_reason == "note only"


def test_visitor_requirement_becomes_an_orbit_ready_lead(store, sent):
    say(VISITOR, "Share my requirement")
    say(VISITOR, "Anil Kumar")
    assert "email address" in last(sent)[0]
    say(VISITOR, "not an email")
    assert "does not look like an email" in last(sent)[0]
    say(VISITOR, "Anil@GVT.org")
    say(VISITOR, "Green Valley Trust")
    text, rows = last(sent)
    assert text == "What best describes it?" and len(rows) == 9
    say(VISITOR, "School chain / group")
    say(VISITOR, "Pune, Maharashtra")
    say(VISITOR, "STEM labs for 12 schools")
    assert last(sent)[0].startswith("Thanks Anil.")
    lead = store.list_leads()[0]
    assert (lead.name, lead.org, lead.organisation_type, lead.city, lead.state, lead.schools_count) == ("Anil Kumar", "Green Valley Trust", "SCHOOL_CHAIN", "Pune", "Maharashtra", "12")
    assert lead.email == "anil@gvt.org"
    assert lead.phone == "+919811111111" and lead.captured_by == "" and store.get_session(VISITOR) is None
    assert [f["body"] for f in store.list_feedback(lead.id)] == ["STEM labs for 12 schools"]


def test_visitor_can_skip_the_email(store, sent):
    for answer in ("Share my requirement", "Anil", "skip", "Org", "School", "Pune, Maharashtra", "Labs"):
        say(VISITOR, answer)
    assert store.list_leads()[0].email == ""


def test_visitor_type_step_reasks_on_nonsense(sent):
    for answer in ("Share my requirement", "Anil", "skip", "Org", "purple"):
        say(VISITOR, answer)
    assert last(sent)[0] == "Please pick one from the list."


def test_visitor_brochure_and_feedback(store, sent, monkeypatch):
    monkeypatch.setattr(flows, "brochure_catalog", lambda config: {"1": {"key": "tiqer", "name": "TiQer", "url": "https://x/tiqer.pdf"}})
    say(VISITOR, "Download brochure")
    assert last(sent)[1] == ["TiQer"]
    say(VISITOR, "TiQer")
    assert "https://x/tiqer.pdf" in last(sent)[0]
    say(VISITOR, "Feedback / voice note")
    say(VISITOR, "Really useful demo stall")
    say(VISITOR, "", [("https://media.bird.com/fb", "audio/ogg")])
    lead = store.list_leads()[0]
    assert "TiQer" in lead.interest and lead.feedback_notes == "Really useful demo stall" and lead.audio_transcript == "heard: call me"


def test_tapping_the_menu_inside_a_visitor_flow_switches_flow(store, sent):
    say(VISITOR, "Share my requirement")
    say(VISITOR, "Feedback / voice note")
    assert store.get_session(VISITOR)["flow"] == "feedback"


def test_deactivated_staff_get_the_visitor_menu_and_lose_their_scan(store, sent, admin):
    other = store.get_user(flows.auth.add_user(store, "Ravi Kumar", "9845000001", "", "long-enough-pw"))
    number = "whatsapp:+919845000001"
    say(number, "Other document")
    store.set_user_active(other["id"], False)
    say(number, "DONE")
    assert last(sent)[1][0] == "Download brochure"


def test_slash_commands_are_for_staff_only(sent):
    say(STAFF, "/summary")
    assert sent[-1][1].startswith("DIDAC summary")
    say(VISITOR, "/summary")
    assert not sent[-1][1].startswith("DIDAC summary")
    say(STAFF, "/followups")
    assert sent[-1][1].startswith("Unknown command")


def test_bird_message_bodies():
    assert bird.message_body("hi") == {"type": "text", "text": {"text": "hi"}}
    buttons = bird.message_body("Pick", [("Save", ""), ("Edit", "")])
    assert buttons["text"]["actions"] == [{"type": "reply", "reply": {"text": "Save"}}, {"type": "reply", "reply": {"text": "Edit"}}]
    listing = bird.message_body("Pick", [("School", "A single school")], as_list=True)
    assert listing["type"] == "list"
    assert listing["list"]["items"] == [{"title": "Choose an option", "actions": [{"type": "postback", "postback": {"text": "School", "payload": "School", "description": "A single school"}}]}]


def test_reading_failure_still_gives_a_saveable_summary(monkeypatch):
    monkeypatch.setattr(flows.blobstore, "download", lambda config, key: (_ for _ in ()).throw(RuntimeError("bucket down")))
    result = real_read_documents(flows.DidacConfig(), [{"key": "k", "ct": "image/jpeg"}], "document")
    assert result == {"fields": {}, "check": ["could not read the photos"]}


def test_voice_note_with_no_menu_choice_goes_to_the_last_lead(store, sent):
    lead = store.create_lead(flows.Lead(from_number=STAFF, name="Meera"))
    say(STAFF, "", [("https://media.bird.com/v2", "audio/ogg")])
    assert store.get_lead(lead.id).audio_transcript == "heard: call me"
    assert f"Added to #{lead.id}" in last(sent)[0]


def test_event_badge_is_its_own_type_and_flags_a_missing_qr(store, sent, fake_io):
    fake_io["result"] = {"fields": {"name": "Nisha Rao", "org": "Expo Org", "qr_page_text": "EXB0987"}, "check": []}
    say(STAFF, "Event badge / QR")
    assert "QR code in view" in last(sent)[0]
    say(STAFF, "", photo("badge"))
    say(STAFF, "DONE")
    assert fake_io["calls"] == [(1, "badge")] and "QR text: EXB0987" in last(sent)[0]
    save_scan(STAFF)
    lead = store.list_leads()[0]
    assert lead.doc_type == "badge" and lead.qr_page_text == "EXB0987"
    assert "Send the next badge" in last(sent)[0]
    assert "Event badges" in app.test_client().get("/dashboard?type=badges").data.decode()


def test_badge_without_a_decodable_qr_gets_a_check(monkeypatch):
    monkeypatch.setattr(flows.blobstore, "download", lambda config, key: b"x")
    monkeypatch.setattr(flows, "vision_read", lambda paths, kind: '{"name": "Nisha Rao", "org": "Expo Org"}')
    monkeypatch.setattr(flows, "append_qr_details", lambda path, text: "")
    badge = real_read_documents(flows.DidacConfig(), [{"key": "k", "ct": "image/jpeg"}], "badge")
    assert badge["fields"]["name"] == "Nisha Rao" and "QR code could not be scanned" in badge["check"][0]
    assert real_read_documents(flows.DidacConfig(), [{"key": "k", "ct": "image/jpeg"}], "document")["check"] == []


def test_lead_page_shows_texts_kept_only_on_the_lead(store):
    from didac_app import ui

    lead = store.create_lead(flows.Lead(from_number=VISITOR, text_notes="Wants a callback", raw_message="New lead\nName: Asha\nOrg: Sunrise"))
    store.add_feedback(VISITOR, "Wants a callback; thanks", lead.id, feedback_type="text_message")
    texts = [item["text"] for item in ui.lead_items(lead, store, {}) if item["kind"] == "text"]
    assert texts == ["Wants a callback; thanks", "Name: Asha\nOrg: Sunrise"]  # the callback text is not repeated; the typed lead is added
    page = app.test_client().get(f"/leads/{lead.id}").data.decode()
    assert "Name: Asha" in page


def test_staff_typed_lead_is_kept_as_a_text_item(store, sent):
    say(STAFF, "New lead\nName: Ravi\nOrg: Expo School")
    lead = store.list_leads()[0]
    assert [f["body"] for f in store.list_feedback(lead.id)] == ["New lead\nName: Ravi\nOrg: Expo School"]


def test_gallery_filters_team_and_visitor_leads(store):
    store.create_lead(flows.Lead(from_number=STAFF, name="Team Capture", doc_type="form"))
    store.create_lead(flows.Lead(from_number=VISITOR, name="Visitor Person", doc_type="form"))
    client = app.test_client()
    everyone = client.get("/dashboard?type=forms").data.decode()
    team = client.get("/dashboard?type=forms&who=team").data.decode()
    visitors = client.get("/dashboard?type=forms&who=visitors").data.decode()
    assert "Team Capture" in everyone and "Visitor Person" in everyone
    assert "Team Capture" in team and "Visitor Person" not in team
    assert "Visitor Person" in visitors and "Team Capture" not in visitors


def test_export_includes_incomplete_leads_and_says_what_is_missing(store):
    store.create_lead(flows.Lead(name="No Org Person"))
    client = app.test_client()
    page = client.get("/export").data.decode()
    assert "<b>1</b> lead in the file" in page and "1 without an organisation name" in page
    assert b"No" in client.get("/export/orbit.csv").data


def test_unknown_field_doubts_are_not_checks():
    from didac_app.flows import NOT_ON_DOCUMENT
    for noise in ("udise_code unknown", "district not printed", "curriculum missing", "budget N/A", "qr_url unreadable (QR content not decoded)"):
        assert NOT_ON_DOCUMENT.search(noise)
    for real in ("phone digits unclear", "district unclear (Basavanagudi is a locality)"):
        assert not NOT_ON_DOCUMENT.search(real)


def test_form_with_a_visiting_card_is_one_read_and_one_lead(store, sent, fake_io):
    fake_io["result"] = {"fields": {"name": "Meera Iyer", "org": "Bright School", "role": "Principal", "email": "meera@bright.edu"}, "check": []}
    say(STAFF, "Contact form")
    for name in ("form-p1", "form-p2", "card"):
        say(STAFF, "", photo(name))
    say(STAFF, "DONE")
    assert fake_io["calls"] == [(3, "form")]  # form pages and the card go through one read
    save_scan(STAFF)
    leads = store.list_leads()
    assert len(leads) == 1 and leads[0].doc_type == "form" and leads[0].email == "meera@bright.edu"
    assert len(store.list_media(leads[0].id)) == 3


def test_form_read_merges_card_details_and_the_prompt_asks_for_it(monkeypatch):
    from didac_app import openai_services

    assert "visiting card" in openai_services.FORM_PROMPT and "the form wins" in openai_services.FORM_PROMPT
    seen = {}
    monkeypatch.setattr(flows.blobstore, "download", lambda config, key: b"x")
    monkeypatch.setattr(flows, "append_qr_details", lambda path, text: "")
    monkeypatch.setattr(flows, "vision_read", lambda paths, kind: seen.update(n=len(paths), kind=kind) or '{"name": "Meera Iyer", "org": "Bright School", "role": "Principal", "email": "meera@bright.edu", "remarks": "call Friday"}')
    result = real_read_documents(flows.DidacConfig(), [{"key": k, "ct": "image/jpeg"} for k in ("form", "card")], "form")
    assert seen == {"n": 2, "kind": "form"}
    assert result["fields"]["role"] == "Principal" and result["fields"]["email"] == "meera@bright.edu"
    assert "call Friday" in result["fields"]["notes"]


def test_one_filter_controls_who_and_priority(store, sent):
    store.create_lead(flows.Lead(name="Hot Lead", priority="High", captured_by="Test Admin"))
    store.create_lead(flows.Lead(name="Cold Visitor", priority="Low"))
    store.create_lead(flows.Lead(name="Open Visitor"))
    client = app.test_client()

    def names(query):
        page = client.get(f"/dashboard?{query}").data.decode()
        return {n for n in ("Hot Lead", "Cold Visitor", "Open Visitor") if n in page}

    page = client.get("/dashboard").data.decode()
    assert 'data-prio="High"' in page and 'data-prio="Unclassified"' in page and 'id="p-prio"' in page
    assert names("") == {"Hot Lead", "Cold Visitor", "Open Visitor"}
    assert names("prio=High") == {"Hot Lead"}
    assert names("prio=none") == {"Open Visitor"}
    assert names("who=visitors") == {"Cold Visitor", "Open Visitor"}
    assert names("who=visitors&prio=Low") == {"Cold Visitor"}
    assert names("prio=bogus") == {"Hot Lead", "Cold Visitor", "Open Visitor"}


def test_photo_rotation_is_saved_per_photo(store, sent):
    from conftest import csrf

    lead = store.create_lead(flows.Lead(name="Rotated"))
    media_id = store.add_media(lead.id, "https://x/y.jpg", "image/jpeg", "k/y.jpg")
    client = app.test_client()
    turn = lambda direction: client.post(f"/media/{media_id}/rotate", data={"csrf": csrf(client), "dir": direction}).get_json()["rotation"]
    assert [turn("right"), turn("right"), turn("left"), turn("left"), turn("left")] == [90, 180, 90, 0, 270]
    page = client.get(f"/leads/{lead.id}").data.decode()
    assert 'data-rot="270"' in page
    assert client.post("/media/99999/rotate", data={"csrf": csrf(client)}).status_code == 404
