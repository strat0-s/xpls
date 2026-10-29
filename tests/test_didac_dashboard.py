import pytest

from bird_helpers import post_bird
from didac_app.models import Lead
from didac_app.server import app


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

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": ("2026-10-18/910000000123/fake-card.jpg", "/tmp/fake-card.jpg"))
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: "First Name: Priya\nLast Name: Rao\nOrg: ABC Foundation\nDesignation: CSR Lead\nPhone: 9876543210",
    )
    client = app.test_client()
    number = "whatsapp:+910000000123"
    post_bird(client, number, 'hi')
    post_bird(client, number, '3')
    response = post_bird(client, number, '', [('https://media.bird.com/fake-media', 'image/jpeg')])
    assert response.status_code == 200
    lead = server.store.latest_lead_for_number(number)
    assert lead.first_name == "Priya"
    assert lead.last_name == "Rao"
    assert lead.org == "ABC Foundation"


def test_media_message_cap(monkeypatch, sent):
    from dataclasses import replace

    import didac_app.server as server

    monkeypatch.setattr(server, "config", replace(server.config, max_media_per_message=1))
    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": ("2026-10-18/910000000123/fake-card.jpg", "/tmp/fake-card.jpg"))
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: "First Name: Asha\nOrg: One Card School",
    )
    client = app.test_client()
    number = "whatsapp:+910000000124"
    response = post_bird(client, number, '', [('https://media.bird.com/fake-media-1', 'image/jpeg'), ('https://media.bird.com/fake-media-2', 'image/jpeg')])
    assert response.status_code == 200
    assert "Skipped 1 extra file" in sent[-1][1]


def test_single_image_with_two_cards_creates_two_leads(monkeypatch, sent):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": ("2026-10-18/910000000123/fake-two-cards.jpg", "/tmp/fake-two-cards.jpg"))
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
    response = post_bird(client, number, '', [('https://media.bird.com/fake-two-cards', 'image/jpeg')])
    text = sent[-1][1]
    assert response.status_code == 200
    assert "Saved 2 lead record" in text
    leads = server.store.list_leads("from_number=%s", (number,))
    orgs = {lead.org for lead in leads}
    assert {"ABC Foundation", "XYZ School"}.issubset(orgs)


def test_image_caption_is_saved_to_created_lead(monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": ("2026-10-18/910000000123/fake-card.jpg", "/tmp/fake-card.jpg"))
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: "First Name: Meera\nOrg: Bright School\nPhone: 9876543212",
    )
    client = app.test_client()
    number = "whatsapp:+910000000129"
    response = post_bird(client, number, 'School management interested in TiQer assessment for 15 schools', [('https://media.bird.com/fake-card-caption', 'image/jpeg')])
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

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": ("2026-10-18/910000000123/fake-team-card.jpg", "/tmp/fake-team-card.jpg"))
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: "First Name: Leela\nOrg: Partner School\nPhone: 9876543214",
    )
    client = app.test_client()
    number = "whatsapp:+910000000132"
    response = post_bird(client, number, 'By: Magesh\nContext: DIDAC Day 1 - Stall A12\nTags: hot, principal', [('https://media.bird.com/fake-team-card', 'image/jpeg')])
    assert response.status_code == 200
    lead = server.store.latest_lead_for_number(number)
    assert lead.captured_by == "Magesh"
    assert lead.capture_context == "DIDAC Day 1 - Stall A12"
    assert "hot" in lead.tags


def test_event_badge_qr_fields_are_saved(monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": ("2026-10-18/910000000123/fake-badge.jpg", "/tmp/fake-badge.jpg"))
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
    response = post_bird(client, number, '', [('https://media.bird.com/fake-badge', 'image/jpeg')])
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


def test_two_cards_create_two_leads_even_with_existing_context(monkeypatch, sent):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": ("2026-10-18/910000000123/fake-two-cards.jpg", "/tmp/fake-two-cards.jpg"))
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
    post_bird(client, number, 'New lead\nName: Existing Context\nOrg: Existing Org')
    response = post_bird(client, number, '', [('https://media.bird.com/fake-two-cards-context', 'image/jpeg')])
    text = sent[-1][1]
    assert response.status_code == 200
    assert "Saved 2 lead record" in text
    leads = server.store.list_leads("from_number=%s", (number,))
    orgs = {lead.org for lead in leads}
    assert {"BIOCRAFT SCIENTIFIC SYSTEMS PVT. LTD.", "xperimentor"}.issubset(orgs)


def test_audio_note_updates_intake_lead(monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": ("2026-10-18/910000000123/fake-audio.ogg", "/tmp/fake-audio.ogg"))
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: "I am school management from Bengaluru interested in TiQer assessment and science lab for 12 schools",
    )
    client = app.test_client()
    number = "whatsapp:+910000000127"
    post_bird(client, number, 'hi')
    lead = server.store.latest_lead_for_number(number)
    post_bird(client, number, '2')
    response = post_bird(client, number, '', [('https://media.bird.com/fake-audio', 'audio/ogg')])
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

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": ("2026-10-18/910000000123/fake-feedback.ogg", "/tmp/fake-feedback.ogg"))
    monkeypatch.setattr(
        server,
        "extract_media_text",
        lambda local_path, content_type: "Feedback from school principal. Interested in demo and proposal for science lab.",
    )
    client = app.test_client()
    number = "whatsapp:+910000000130"
    post_bird(client, number, 'hi')
    post_bird(client, number, '3')
    response = post_bird(client, number, 'Please call tomorrow', [('https://media.bird.com/fake-feedback-audio', 'audio/ogg')])
    assert response.status_code == 200
    lead = server.store.latest_lead_for_number(number)
    assert "Please call tomorrow" in lead.feedback_notes
    assert "school principal" in lead.audio_transcript
    assert "Demo requested" in lead.interest


def test_distributor_note_routes_to_channel_owner(monkeypatch):
    import didac_app.server as server

    client = app.test_client()
    number = "whatsapp:+910000000128"
    post_bird(client, number, 'hi')
    post_bird(client, number, '2')
    response = post_bird(client, number, 'I am a distributor interested in STREAM for 25 schools')
    assert response.status_code == 200
    lead = server.store.latest_lead_for_number(number)
    assert lead.lead_type == "Distributor"
    assert lead.owner == "Channel sales"


def test_media_file_redirects_to_signed_url(store, monkeypatch):
    import didac_app.server as server

    lead = store.create_lead(Lead())
    media_id = store.add_media(lead.id, "https://example.com/m", "image/jpeg", "2026-10-18/91/card.jpg", "")
    monkeypatch.setattr(server.blobstore, "signed_url", lambda config, key: f"https://signed.example/{key}")
    response = app.test_client().get(f"/media/{media_id}")
    assert response.status_code == 302
    assert response.headers["Location"] == "https://signed.example/2026-10-18/91/card.jpg"
