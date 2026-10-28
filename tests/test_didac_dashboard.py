import pytest

from didac_app.server import app


@pytest.fixture(autouse=True)
def isolated_store(store, monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "store", store)


def test_dashboard_routes_load():
    client = app.test_client()
    home = client.get("/")
    dashboard = client.get("/dashboard")
    assert home.status_code == 200
    assert dashboard.status_code == 200
    html = dashboard.data.decode()
    assert 'data-tab="summary"' in html
    assert 'data-tab="leads"' in html
    assert 'data-tab="feedback"' in html
    assert 'data-tab="media"' in html
    assert 'data-tab="library"' in html
    assert client.get("/brochures").status_code == 200
    assert client.get("/media-library").status_code == 200
    assert client.get("/api/leads").status_code == 200
    assert client.get("/api/feedback").status_code == 200
    assert client.get("/api/media").status_code == 200
    assert client.get("/api/inbound").status_code == 200


def test_media_message_is_processed_inside_feedback_flow(monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": "/tmp/fake-card.jpg")
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: "First Name: Priya\nLast Name: Rao\nOrg: ABC Foundation\nDesignation: CSR Lead\nPhone: 9876543210",
    )
    client = app.test_client()
    number = "whatsapp:+910000000123"
    client.post("/twilio/whatsapp", data={"From": number, "Body": "hi", "NumMedia": "0"})
    client.post("/twilio/whatsapp", data={"From": number, "Body": "3", "NumMedia": "0"})
    response = client.post(
        "/twilio/whatsapp",
        data={
            "From": number,
            "Body": "",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/fake-media",
            "MediaContentType0": "image/jpeg",
        },
    )
    assert response.status_code == 200
    lead = server.store.latest_lead_for_number(number)
    assert lead.first_name == "Priya"
    assert lead.last_name == "Rao"
    assert lead.org == "ABC Foundation"


def test_media_message_cap(monkeypatch):
    from dataclasses import replace

    import didac_app.server as server

    monkeypatch.setattr(server, "config", replace(server.config, max_media_per_message=1))
    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": "/tmp/fake-card.jpg")
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: "First Name: Asha\nOrg: One Card School",
    )
    client = app.test_client()
    number = "whatsapp:+910000000124"
    response = client.post(
        "/twilio/whatsapp",
        data={
            "From": number,
            "Body": "",
            "NumMedia": "2",
            "MediaUrl0": "https://api.twilio.com/fake-media-1",
            "MediaContentType0": "image/jpeg",
            "MediaUrl1": "https://api.twilio.com/fake-media-2",
            "MediaContentType1": "image/jpeg",
        },
    )
    assert response.status_code == 200
    assert "Skipped 1 extra file" in response.data.decode()


def test_single_image_with_two_cards_creates_two_leads(monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": "/tmp/fake-two-cards.jpg")
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: (
            '{"contacts":['
            '{"first_name":"Priya","last_name":"Rao","org":"ABC Foundation","phone":"9876543210"},'
            '{"first_name":"Ramesh","last_name":"Kumar","org":"XYZ School","phone":"9876543211"}'
            "]}"
        ),
    )
    client = app.test_client()
    number = "whatsapp:+910000000125"
    response = client.post(
        "/twilio/whatsapp",
        data={
            "From": number,
            "Body": "",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/fake-two-cards",
            "MediaContentType0": "image/jpeg",
        },
    )
    text = response.data.decode()
    assert response.status_code == 200
    assert "Saved 2 lead record" in text
    leads = server.store.list_leads("from_number=%s", (number,))
    orgs = {lead.org for lead in leads}
    assert {"ABC Foundation", "XYZ School"}.issubset(orgs)


def test_image_caption_is_saved_to_created_lead(monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": "/tmp/fake-card.jpg")
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: "First Name: Meera\nOrg: Bright School\nPhone: 9876543212",
    )
    client = app.test_client()
    number = "whatsapp:+910000000129"
    response = client.post(
        "/twilio/whatsapp",
        data={
            "From": number,
            "Body": "School management interested in TiQer assessment for 15 schools",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/fake-card-caption",
            "MediaContentType0": "image/jpeg",
        },
    )
    assert response.status_code == 200
    lead = server.store.latest_lead_for_number(number)
    assert lead.org == "Bright School"
    assert "School management interested" in lead.text_notes
    assert "TiQer" in lead.interest
    assert lead.schools_count == "15"
    inbound = server.store.list_inbound_events()
    assert inbound[0]["from_number"] == number
    assert inbound[0]["status"] == "processed"
    assert str(lead.id) in inbound[0]["lead_ids"]


def test_team_caption_metadata_is_saved(monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": "/tmp/fake-team-card.jpg")
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: "First Name: Leela\nOrg: Partner School\nPhone: 9876543214",
    )
    client = app.test_client()
    number = "whatsapp:+910000000132"
    response = client.post(
        "/twilio/whatsapp",
        data={
            "From": number,
            "Body": "By: Magesh\nContext: DIDAC Day 1 - Stall A12\nTags: hot, principal",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/fake-team-card",
            "MediaContentType0": "image/jpeg",
        },
    )
    assert response.status_code == 200
    lead = server.store.latest_lead_for_number(number)
    assert lead.captured_by == "Magesh"
    assert lead.capture_context == "DIDAC Day 1 - Stall A12"
    assert "hot" in lead.tags


def test_event_badge_qr_fields_are_saved(monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": "/tmp/fake-badge.jpg")
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: (
            "Name: Nisha Rao\n"
            "Designation: Principal\n"
            "Org: Green Valley School\n"
            "QR URL: https://event.example/profile/123\n"
            "QR Page Text: Email nisha@example.com Phone 9876543213 Interested in science lab"
        ),
    )
    client = app.test_client()
    number = "whatsapp:+910000000131"
    response = client.post(
        "/twilio/whatsapp",
        data={
            "From": number,
            "Body": "",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/fake-badge",
            "MediaContentType0": "image/jpeg",
        },
    )
    assert response.status_code == 200
    lead = server.store.latest_lead_for_number(number)
    assert lead.name == "Nisha Rao"
    assert lead.org == "Green Valley School"
    assert lead.qr_url == "https://event.example/profile/123"
    assert "nisha@example.com" in lead.qr_page_text


def test_report_downloads_xlsx_and_csv():
    client = app.test_client()
    xlsx = client.get("/export/report.xlsx")
    csv_response = client.get("/export/report.csv")
    assert xlsx.status_code == 200
    assert "spreadsheet" in xlsx.content_type or "application/vnd.openxmlformats" in xlsx.content_type
    assert csv_response.status_code == 200
    assert "text/csv" in csv_response.content_type
    assert b"first_name" in csv_response.data


def test_two_cards_create_two_leads_even_with_existing_context(monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": "/tmp/fake-two-cards.jpg")
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: (
            "First Name: Kunal\n"
            "Last Name: Mahajan\n"
            "Org: BIOCRAFT SCIENTIFIC SYSTEMS PVT. LTD.\n"
            "\n---CONTACT---\n\n"
            "First Name: Abhishek\n"
            "Last Name: P M\n"
            "Org: xperimentor\n"
        ),
    )
    client = app.test_client()
    number = "whatsapp:+910000000126"
    client.post(
        "/twilio/whatsapp",
        data={
            "From": number,
            "Body": "New lead\nName: Existing Context\nOrg: Existing Org",
            "NumMedia": "0",
        },
    )
    response = client.post(
        "/twilio/whatsapp",
        data={
            "From": number,
            "Body": "",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/fake-two-cards-context",
            "MediaContentType0": "image/jpeg",
        },
    )
    text = response.data.decode()
    assert response.status_code == 200
    assert "Saved 2 lead record" in text
    leads = server.store.list_leads("from_number=%s", (number,))
    orgs = {lead.org for lead in leads}
    assert {"BIOCRAFT SCIENTIFIC SYSTEMS PVT. LTD.", "xperimentor"}.issubset(orgs)


def test_audio_note_updates_intake_lead(monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": "/tmp/fake-audio.ogg")
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: "I am school management from Bengaluru interested in TiQer assessment and science lab for 12 schools",
    )
    client = app.test_client()
    number = "whatsapp:+910000000127"
    client.post("/twilio/whatsapp", data={"From": number, "Body": "hi", "NumMedia": "0"})
    lead = server.store.latest_lead_for_number(number)
    client.post("/twilio/whatsapp", data={"From": number, "Body": "2", "NumMedia": "0"})
    response = client.post(
        "/twilio/whatsapp",
        data={
            "From": number,
            "Body": "",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/fake-audio",
            "MediaContentType0": "audio/ogg",
        },
    )
    assert response.status_code == 200
    lead = server.store.latest_lead_for_number(number)
    assert lead.id
    assert lead.status == "New"
    assert lead.lead_type == "School"
    assert "TiQer" in lead.interest
    assert "Assessment / analytics" in lead.interest
    assert lead.schools_count == "12"
    assert "school management from Bengaluru" in lead.audio_transcript


def test_audio_feedback_is_saved_on_lead(monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": "/tmp/fake-feedback.ogg")
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: "Feedback from school principal. Interested in demo and proposal for science lab.",
    )
    client = app.test_client()
    number = "whatsapp:+910000000130"
    client.post("/twilio/whatsapp", data={"From": number, "Body": "hi", "NumMedia": "0"})
    client.post("/twilio/whatsapp", data={"From": number, "Body": "3", "NumMedia": "0"})
    response = client.post(
        "/twilio/whatsapp",
        data={
            "From": number,
            "Body": "Please call tomorrow",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/fake-feedback-audio",
            "MediaContentType0": "audio/ogg",
        },
    )
    assert response.status_code == 200
    lead = server.store.latest_lead_for_number(number)
    assert "Please call tomorrow" in lead.feedback_notes
    assert "school principal" in lead.audio_transcript
    assert "Demo requested" in lead.interest


def test_distributor_note_routes_to_channel_owner(monkeypatch):
    import didac_app.server as server

    client = app.test_client()
    number = "whatsapp:+910000000128"
    client.post("/twilio/whatsapp", data={"From": number, "Body": "hi", "NumMedia": "0"})
    client.post("/twilio/whatsapp", data={"From": number, "Body": "2", "NumMedia": "0"})
    response = client.post(
        "/twilio/whatsapp",
        data={
            "From": number,
            "Body": "I am a distributor interested in STREAM for 25 schools",
            "NumMedia": "0",
        },
    )
    assert response.status_code == 200
    lead = server.store.latest_lead_for_number(number)
    assert lead.lead_type == "Distributor"
    assert lead.owner == "Channel sales"

