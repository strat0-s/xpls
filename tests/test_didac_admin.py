"""The admin pages: event control, documents and brochures, messages. Admins only."""
from conftest import csrf, get_or_create_user
from didac_app import flows
from didac_app.server import app

STAFF = "whatsapp:+919800000001"


def post(client, path, **data):
    return client.post(path, data={"csrf": csrf(client), **data}, follow_redirects=True)


def test_every_admin_page_renders_with_the_sidebar(store):
    client = app.test_client()
    for url in ("/admin/event", "/admin/documents", "/admin/messages", "/dashboard", "/team", "/export"):
        page = client.get(url)
        html = page.data.decode()
        assert page.status_code == 200 and "Footfall" in html and "DIDAC 2026" in html and "Documents and brochures" in html, url
    assert "Event control" in client.get("/admin/event").data.decode()


def test_only_admins_can_open_or_use_the_admin_pages(store):
    member = get_or_create_user(store, "Plain Member", "+919800000077", "member@example.com", "long-enough-pw")
    client = app.test_client_class(app, app.response_class)
    with client.session_transaction() as sess:
        sess["uid"], sess["sv"] = member["id"], member["session_version"]
    assert client.get("/admin/event").status_code == 403
    assert client.post("/admin/event/new", data={"csrf": csrf(client), "name": "Sneaky"}).status_code == 403
    assert "Event control" not in client.get("/dashboard").data.decode()  # the admin links are not even shown


def test_new_event_copies_a_setup_and_going_live_stops_the_old_one(store, sent):
    client = app.test_client()
    first = store.live_event()
    post(client, "/admin/event/new", name="Bett India 2027", copy_from=first["id"])
    second = next(e for e in store.list_events() if e["name"] == "Bett India 2027")
    assert second["status"] == "draft" and len(store.list_doc_types(second["id"])) == 3
    html = post(client, f"/admin/event/{second['id']}/start").data.decode()
    assert "Bett India 2027 is live. DIDAC 2026 was stopped." in html
    assert store.live_event()["id"] == second["id"] and store.get_event(first["id"])["status"] == "ended"
    post(client, f"/admin/event/{second['id']}/stop")
    assert store.live_event() is None and "No event is live" in client.get("/admin/event").data.decode()


def test_event_details_can_be_renamed_and_need_a_name(store):
    client = app.test_client()
    event = store.live_event()
    post(client, "/admin/event/save", event=event["id"], name="DIDAC 2027", starts_on="2027-03-01", ends_on="2027-03-03", venue="Hall 4")
    saved = store.get_event(event["id"])
    assert (saved["name"], str(saved["starts_on"]), saved["venue"]) == ("DIDAC 2027", "2027-03-01", "Hall 4")
    assert "The event needs a name." in post(client, "/admin/event/save", event=event["id"], name="  ").data.decode()
    assert store.get_event(event["id"])["name"] == "DIDAC 2027"


def test_a_document_type_can_be_added_renamed_ordered_and_switched_off(store, sent):
    client = app.test_client()
    event = store.live_event()
    post(client, "/admin/documents/type/add", event=event["id"], label="Raffle slip", hint="Entry slip", reader="document")
    types = store.list_doc_types(event["id"])
    slip = types[-1]
    assert slip["label"] == "Raffle slip" and slip["key"] == "raffle_slip"
    post(client, f"/admin/documents/type/{slip['id']}", label="Raffle ticket", hint="Entry ticket")
    assert store.list_doc_types(event["id"])[-1]["label"] == "Raffle ticket"
    client.post("/admin/documents/type/order", data={"csrf": csrf(client), "ids": ",".join(str(t["id"]) for t in reversed(store.list_doc_types(event["id"])))})
    assert store.list_doc_types(event["id"])[0]["label"] == "Raffle ticket"
    post(client, f"/admin/documents/type/{slip['id']}/active", active="0")
    post(client, "/admin/documents/type/add", event=event["id"], label="")
    flows_menu = [t["label"] for t in store.list_doc_types(event["id"], active_only=True)]
    assert "Raffle ticket" not in flows_menu and "Contact form" in flows_menu


def test_a_brochure_needs_a_real_link(store):
    client = app.test_client()
    event = store.live_event()
    html = post(client, "/admin/documents/brochure/add", event=event["id"], name="Bad", url="javascript:alert(1)", for_visitors="1").data.decode()
    assert "starts with https://" in html and not any(b["name"] == "Bad" for b in store.list_brochures(event["id"], active_only=False))
    post(client, "/admin/documents/brochure/add", event=event["id"], name="Flyer", url="https://drive.google.com/file/d/abc/view", for_team="1")
    flyer = next(b for b in store.list_brochures(event["id"]) if b["name"] == "Flyer")
    assert flyer["for_team"] and not flyer["for_visitors"]
    post(client, f"/admin/documents/brochure/{flyer['id']}/active", active="0")
    assert all(b["name"] != "Flyer" for b in store.list_brochures(event["id"]))


def test_the_welcome_message_is_per_event_and_the_default_is_not_stored(store, sent):
    client = app.test_client()
    event = store.live_event()
    post(client, "/admin/messages/welcome", event=event["id"], text="Hello! Welcome to [event].\nPick one:")
    assert store.get_event(event["id"])["welcome_text"] == "Hello! Welcome to [event].\nPick one:"
    post_bird_hi = __import__("bird_helpers").post_bird
    post_bird_hi(client, "whatsapp:+919811111111", "hi")
    assert sent[-1][1] == "Hello! Welcome to DIDAC 2026.\nPick one:"
    post(client, "/admin/messages/welcome", event=event["id"], text=flows.DEFAULT_WELCOME.format(event="DIDAC 2026"))
    assert store.get_event(event["id"])["welcome_text"] == ""
    post(client, "/admin/messages/followup", text="Thanks [name]!")
    assert store.get_setting("followup_message") == "Thanks [name]!"
