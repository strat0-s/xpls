import csv
import io

import pytest
from openpyxl import load_workbook

from conftest import csrf
from didac_app import orbit
from didac_app.models import Lead
from didac_app.server import app


@pytest.mark.parametrize(
    "text, code",
    [
        ("School", "SCHOOL"), ("SCHOOL_CHAIN", "SCHOOL_CHAIN"), ("School Chain", "SCHOOL_CHAIN"), ("Govt", "GOVERNMENT"),
        ("Dealer", "DISTRIBUTOR"), ("Distributor", "DISTRIBUTOR"), ("Reseller", "DISTRIBUTOR"), ("University", "INSTITUTION"),
        ("Edtech company", "VENDOR"), ("CSR", "CSR"), ("ABC Foundation", "NGO"), ("Other", "OTHER"),
        ("Principal", ""), ("", ""),
    ],
)
def test_normalize_org_type(text, code):
    assert orbit.normalize_org_type(text) == code


def test_row_maps_lead_to_orbit_columns():
    lead = Lead(
        org="Sunrise School", organisation_type="School", name="Asha Rao", designation="Principal", phone="98450 11111",
        email="Asha@Sunrise.edu", pincode="560001", curriculum="CBSE; State Board; IB", medium_of_instruction="English; Hindi",
        google_maps_url="https://maps.example/x", udise_code="29010100101",
    )
    assert orbit.orbit_row(lead) == {
        "name": "Sunrise School", "type": "SCHOOL", "state": "", "city": "", "district": "", "pincode": "560001",
        "website": "", "googleMapsUrl": "https://maps.example/x", "udiseCode": "29010100101", "curriculum": "CBSE, STATE",
        "mediumOfInstruction": "English, Hindi", "contact_firstName": "Asha", "contact_lastName": "Rao",
        "contact_designation": "Principal", "contact_email": "asha@sunrise.edu", "contact_phone": "+919845011111",
    }


def test_type_falls_back_to_lead_type_and_incomplete_leads_still_get_a_row():
    assert orbit.orbit_row(Lead(org="Hope NGO", lead_type="NGO"))["type"] == "NGO"
    assert orbit.orbit_row(Lead(org="", organisation_type="School"))["name"] == ""
    assert orbit.orbit_row(Lead(org="Hope", lead_type="Principal"))["type"] == ""
    assert orbit.orbit_row(Lead(org="Hope", organisation_type="NGO", email="not-an-email"))["contact_email"] == ""


def test_files_keep_text_and_have_no_bom():
    rows = [orbit.orbit_row(l) for l in [Lead(org="Sunrise", organisation_type="School", first_name="Asha", pincode="056001", phone="+919845011111")]]
    raw = orbit.to_csv(rows)
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert next(csv.DictReader(io.StringIO(raw.decode())))["pincode"] == "056001"
    sheet = load_workbook(io.BytesIO(orbit.to_xlsx(rows))).active
    assert [c.value for c in sheet[1]] == orbit.COLUMNS
    assert sheet["F2"].value == "056001" and sheet["P2"].value == "+919845011111"


def test_org_count_merges_same_name_and_pincode():
    rows = [orbit.orbit_row(l) for l in [
        Lead(org="Sunrise", organisation_type="School", pincode="560001"),
        Lead(org="sunrise", organisation_type="School", pincode="560001"),
        Lead(org="Sunrise", organisation_type="School", pincode="560002"),
    ]]
    assert orbit.org_count(rows) == 2


def make(store, **fields):
    return store.create_lead(Lead(**fields))


def test_export_page_filters_and_download(store):
    make(store, org="Sunrise School", organisation_type="School", first_name="Asha", captured_by="Ravi", created_at="2026-11-01T10:00:00+00:00")
    make(store, org="Bright Dealers", lead_type="Dealer", first_name="Dev", captured_by="Meera", created_at="2026-11-03T10:00:00+00:00")
    make(store, org="Mystery", first_name="Who", captured_by="Ravi", created_at="2026-11-03T11:00:00+00:00")
    client = app.test_client()

    page = client.get("/export").data.decode()
    assert "<b>3</b> leads in the file" in page and "1 without an organisation type" in page

    assert "<b>2</b> leads in the file" in client.get("/export?from=2026-11-02").data.decode()
    assert "<b>2</b> leads in the file" in client.get("/export?by=Ravi").data.decode()
    assert "<b>1</b> lead in the file" in client.get("/export?type=DISTRIBUTOR").data.decode()

    download = client.get("/export/orbit.csv?type=DISTRIBUTOR")
    assert download.status_code == 200 and "attachment" in download.headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(download.data.decode())))
    assert [(r["name"], r["type"]) for r in rows] == [("Bright Dealers", "DISTRIBUTOR")]
    assert client.get("/export/orbit.xlsx").status_code == 200
    assert client.get("/export/orbit.csv?by=Nobody").status_code == 400
    assert client.get("/export/orbit.pdf").status_code == 404


def test_date_filter_uses_india_time(store):
    # 20:00 UTC on the 1st is already 01:30 on the 2nd in India.
    make(store, org="Late Night School", organisation_type="School", created_at="2026-11-01T20:00:00+00:00")
    client = app.test_client()
    assert "<b>1</b> lead in the file" in client.get("/export?from=2026-11-02").data.decode()
    assert "<b>0</b> leads in the file" in client.get("/export?to=2026-11-01").data.decode()


def test_export_needs_login(anon_client):
    assert anon_client.get("/export").status_code == 302
    assert anon_client.get("/export/orbit.csv").status_code == 302


def test_lead_page_sets_org_type_from_the_fixed_list(store):
    lead = make(store, org="Hope", lead_type="Dealer")
    client = app.test_client()
    assert 'value="DISTRIBUTOR" selected' in client.get(f"/leads/{lead.id}").data.decode()
    form = {"csrf": csrf(client), "name": "A", "org": "Hope", "organisation_type": "NGO", "doc_type": "other"}
    client.post(f"/leads/{lead.id}", data=form)
    assert store.get_lead(lead.id).organisation_type == "NGO"
    client.post(f"/leads/{lead.id}", data=form | {"organisation_type": "made-up"})
    assert store.get_lead(lead.id).organisation_type == ""
