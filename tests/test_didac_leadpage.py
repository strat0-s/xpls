"""The lead page: new fields, adding a lead by hand, extra photos, the product list and admin access."""
from io import BytesIO

from conftest import csrf, get_or_create_user
from didac_app import blobstore, flows
from didac_app.models import NOT_SCANNED, Lead
from didac_app.server import app


def test_lead_page_saves_products_demo_and_a_tidied_place(store):
    lead = store.create_lead(Lead(name="Asha Rao", phone="+919800000021"))
    client = app.test_client()
    page = client.get(f"/leads/{lead.id}").data.decode()
    assert 'name="products" value="TiQer"' in page and 'name="demo_interest"' in page
    client.post(f"/leads/{lead.id}", data={"csrf": csrf(client), "name": "Asha Rao", "city": "Bangalore Karnataka", "products": ["SIA", "Math Max"], "demo_interest": "Yes, December", "workshop": "Yes", "students": "400", "doc_type": ""})
    saved = store.get_lead(lead.id)
    assert (saved.city, saved.state, saved.products, saved.demo_interest, saved.workshop, saved.students) == ("Bangalore", "Karnataka", "SIA; Math Max", "Yes, December", "Yes", "400")


def test_a_lead_can_be_added_by_hand(store):
    client = app.test_client()
    assert "Add lead" in client.get("/leads/new").data.decode()
    response = client.post("/leads/new", data={"csrf": csrf(client), "name": "Hand Typed", "phone": "+919800000022", "city": "Pune, Maharashtra", "products": ["PlayLab"], "priority": "High"})
    lead = store.list_leads()[0]
    assert response.status_code == 302 and "/leads/new" not in response.headers["Location"] and response.headers["Location"].startswith("/?")
    assert (lead.name, lead.city, lead.state, lead.products, lead.priority, lead.source_channel, lead.event_id) == ("Hand Typed", "Pune", "Maharashtra", "PlayLab", "High", "dashboard", store.live_event()["id"])
    assert lead.event_no == 1 and lead.captured_by == "Test Admin"


def test_adding_by_hand_needs_something_and_opens_an_existing_match(store):
    client = app.test_client()
    assert "Add at least a name" in client.post("/leads/new", data={"csrf": csrf(client), "city": "Pune"}, follow_redirects=True).data.decode() and store.list_leads() == []
    first = store.create_lead(Lead(name="Same Person", phone="+919800000023"))
    again = client.post("/leads/new", data={"csrf": csrf(client), "name": "Same Person", "phone": "+919800000023"})
    assert again.headers["Location"].endswith(f"/leads/{first.id}") and len(store.list_leads()) == 1


def test_extra_photos_fill_blanks_only_and_say_what_changed(store, monkeypatch):
    uploads = []
    monkeypatch.setattr(blobstore, "upload", lambda config, key, data, ct="": uploads.append((key, ct)))
    seen = {}
    monkeypatch.setattr(flows, "read_documents", lambda config, photos, kind, products=(): seen.update(n=len(photos), products=list(products)) or {"fields": {"name": "Changed Name", "email": "new@school.edu", "city": "Pune"}, "check": [], "ms": 1})
    lead = store.create_lead(Lead(name="Keep This", doc_type="form", phone="+919800000024"))
    store.add_media(lead.id, "", "image/jpeg", "k/first.jpg")
    store.add_media(lead.id, "", "image/jpeg", "k/me.jpg", purpose="visitor")  # never read
    client = app.test_client()
    html = client.post(f"/leads/{lead.id}/photos", data={"csrf": csrf(client), "purpose": "scan", "photo": (BytesIO(b"x"), "a.jpg", "image/jpeg")}, content_type="multipart/form-data", follow_redirects=True).data.decode()
    saved = store.get_lead(lead.id)
    assert (saved.name, saved.email, saved.city) == ("Keep This", "new@school.edu", "Pune") and "Filled in: Email, City." in html
    assert seen["n"] == 2 and "SIA" in seen["products"] and uploads[0][0].startswith(f"uploads/{lead.id}/")  # the first photo and the new one, not the visitor's


def test_a_photo_with_the_visitor_is_saved_and_never_read(store, monkeypatch):
    monkeypatch.setattr(blobstore, "upload", lambda *args, **kwargs: None)
    monkeypatch.setattr(flows, "read_documents", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not read")))
    lead = store.create_lead(Lead(name="Asha", phone="+919800000025"))
    client = app.test_client()
    html = client.post(f"/leads/{lead.id}/photos", data={"csrf": csrf(client), "purpose": "visitor", "photo": (BytesIO(b"x"), "me.jpg", "image/jpeg")}, content_type="multipart/form-data", follow_redirects=True).data.decode()
    assert [row["purpose"] for row in store.list_media(lead.id)] == ["visitor"] and "Photo added." in html and "Photo with the visitor" in html
    assert "Choose a photo first." in client.post(f"/leads/{lead.id}/photos", data={"csrf": csrf(client), "purpose": "visitor"}, follow_redirects=True).data.decode()


def test_admin_manages_the_product_list_and_who_is_admin(store):
    client = app.test_client()
    assert "Math Max" in client.get("/admin/products").data.decode()
    client.post("/admin/products", data={"csrf": csrf(client), "label": "Robo Kit"})
    row = next(p for p in store.list_products() if p["label"] == "Robo Kit")
    client.post(f"/admin/products/{row['id']}", data={"csrf": csrf(client), "label": "Robot Kit"})
    assert [p["label"] for p in store.list_products()][-1] == "Robot Kit"
    client.post(f"/admin/products/{row['id']}/delete", data={"csrf": csrf(client)})
    assert len(store.list_products()) == 5
    member = get_or_create_user(store, "Plain Member", "+919800000088", "plain@example.com", "long-enough-pw")
    client.post(f"/team/{member['id']}/admin", data={"csrf": csrf(client)})
    assert store.get_user(member["id"])["is_admin"]
    client.post(f"/team/{member['id']}/admin", data={"csrf": csrf(client)})
    assert not store.get_user(member["id"])["is_admin"]
    me = store.find_user("+919800000001")
    client.post(f"/team/{me['id']}/admin", data={"csrf": csrf(client)})
    assert store.get_user(me["id"])["is_admin"]  # nobody can lock themselves out


def test_the_phone_app_files_are_served_without_signing_in(anon_client):
    client = anon_client
    for name, kind in (("manifest.webmanifest", "application/manifest+json"), ("sw.js", "text/javascript"), ("icon-192.png", "image/png"), ("icon-512.png", "image/png"), ("apple-touch-icon.png", "image/png")):
        response = client.get(f"/{name}")
        assert response.status_code == 200 and response.mimetype == kind, name
    assert client.get("/dashboard").status_code == 302  # the app itself still needs a login
    assert 'rel="manifest"' in app.test_client().get("/dashboard").data.decode()


def test_adding_by_hand_with_photos_reads_the_form_and_keeps_the_visitor_photo(store, monkeypatch):
    monkeypatch.setattr(blobstore, "upload", lambda *args, **kwargs: None)
    seen = {}
    monkeypatch.setattr(flows, "read_documents", lambda config, photos, kind, products=(): seen.update(n=len(photos)) or {"fields": {"name": "From Photo", "phone": "+919800000026", "city": "Pune"}, "check": [], "ms": 1})
    client = app.test_client()
    photo = lambda name: (BytesIO(b"x"), name, "image/jpeg")
    saved = client.post("/leads/new", data={"csrf": csrf(client), "city": "Mumbai", "scan_photo": [photo("a.jpg"), photo("b.jpg")], "visitor_photo": photo("me.jpg")}, content_type="multipart/form-data")
    lead = store.list_leads()[0]
    assert "reading=" in saved.headers["Location"] and lead.review_reason == NOT_SCANNED and seen == {}  # saved first; the read is its own request
    assert f"/leads/{lead.id}/read" in client.get(saved.headers["Location"]).data.decode()
    client.post(f"/leads/{lead.id}/read", data={"csrf": csrf(client), "twin": "1"})
    lead = store.get_lead(lead.id)
    assert (lead.name, lead.phone, lead.city) == ("From Photo", "+919800000026", "Mumbai")  # typed city wins, blanks come from the photo
    assert seen["n"] == 2 and sorted(row["purpose"] for row in store.list_media(lead.id)) == ["", "", "visitor"]
    assert {row["added_by"] for row in store.list_media(lead.id)} == {"Test Admin"}


def test_a_photo_added_in_the_app_names_whoever_added_it(store, monkeypatch):
    monkeypatch.setattr(blobstore, "upload", lambda *args, **kwargs: None)
    lead = store.create_lead(Lead(name="Asha", phone="+919800000027", captured_by="Vijetha Kelamane"))
    client = app.test_client()
    html = client.post(f"/leads/{lead.id}/photos", data={"csrf": csrf(client), "purpose": "visitor", "photo": (BytesIO(b"x"), "me.jpg", "image/jpeg")}, content_type="multipart/form-data", follow_redirects=True).data.decode()
    assert "Added by Test Admin" in html and "Added by Vijetha" not in html
