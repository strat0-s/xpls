from didac_app.models import Lead
from didac_app.server import app


def test_dashboard_routes_load():
    client = app.test_client()
    home = client.get("/")
    dashboard = client.get("/dashboard")
    assert home.status_code == 200
    assert dashboard.status_code == 200
    html = dashboard.data.decode()
    assert "Contact forms" in html
    assert "Other documents" in html
    assert client.get("/brochures").status_code == 200
    assert client.get("/media-library").status_code == 200
    assert client.get("/api/leads").status_code == 200
    assert client.get("/api/feedback").status_code == 200
    assert client.get("/api/media").status_code == 200
    assert client.get("/api/inbound").status_code == 200


def test_report_downloads_xlsx_and_csv():
    client = app.test_client()
    xlsx = client.get("/export/report.xlsx")
    csv_response = client.get("/export/report.csv")
    assert xlsx.status_code == 200
    assert "spreadsheet" in xlsx.content_type or "application/vnd.openxmlformats" in xlsx.content_type
    assert csv_response.status_code == 200
    assert "text/csv" in csv_response.content_type
    assert b"first_name" in csv_response.data


REMOVED_FIELDS = {"score", "owner", "next_action", "follow_up_date", "email_subject", "email_draft", "whatsapp_draft"}


def test_removed_lead_fields_are_absent_from_api_and_csv():
    import didac_app.server as server

    server.store.create_lead(Lead(name="Asha"))
    client = app.test_client()
    api_keys = set(client.get("/api/leads").get_json()[0])
    csv_header = set(client.get("/export/report.csv").data.decode().splitlines()[0].split(","))
    assert "name" in api_keys and "name" in csv_header
    assert not REMOVED_FIELDS & api_keys
    assert not REMOVED_FIELDS & csv_header


def test_media_file_redirects_to_signed_url(store, monkeypatch):
    import didac_app.server as server

    lead = store.create_lead(Lead())
    media_id = store.add_media(lead.id, "https://example.com/m", "image/jpeg", "2026-10-18/91/card.jpg", "")
    monkeypatch.setattr(server.blobstore, "signed_url", lambda config, key: f"https://signed.example/{key}")
    response = app.test_client().get(f"/media/{media_id}")
    assert response.status_code == 302
    assert response.headers["Location"] == "https://signed.example/2026-10-18/91/card.jpg"
