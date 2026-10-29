from conftest import csrf, get_or_create_user
from didac_app import auth
from didac_app.models import Lead
from didac_app.server import app

STAFF = {"name": "Sam Staff", "phone": "+919800000002", "email": "sam@example.com", "password": "staff-password-1"}


def sign_in(client, login, password):
    return client.post("/login", data={"login": login, "password": password, "csrf": csrf(client)})


def test_logged_out_visitors_are_sent_to_login(anon_client):
    page = anon_client.get("/?type=other")
    assert page.status_code == 302
    assert page.headers["Location"].startswith("/login?next=")
    assert anon_client.get("/api/leads").status_code == 401
    assert anon_client.get("/export/report.csv").status_code == 302
    assert anon_client.get("/media/1").status_code == 302


def test_health_and_webhook_stay_open(anon_client):
    assert anon_client.get("/health").status_code == 200
    assert anon_client.post("/bird/webhook?token=wrong", json={}).status_code == 401


def test_login_by_phone_or_email(anon_client, admin):
    for login in ("+919800000001", "98 0000 0001", "ADMIN@example.com"):
        client = app.test_client_class(app, app.response_class)
        response = sign_in(client, login, "correct-horse-battery")
        assert response.status_code == 302, login
        assert client.get("/").status_code == 200


def test_wrong_password_and_unknown_user_are_rejected(anon_client, admin):
    assert sign_in(anon_client, "+919800000001", "nope").status_code == 401
    assert sign_in(anon_client, "+919811111111", "whatever").status_code == 401
    assert anon_client.get("/").status_code == 302


def test_deactivated_user_cannot_log_in_or_keep_session(store, anon_client):
    user = get_or_create_user(store, **STAFF)
    assert sign_in(anon_client, STAFF["email"], STAFF["password"]).status_code == 302
    assert anon_client.get("/").status_code == 200
    store.set_user_active(user["id"], False)
    assert anon_client.get("/").status_code == 302
    assert sign_in(anon_client, STAFF["email"], STAFF["password"]).status_code == 401
    store.set_user_active(user["id"], True)


def test_login_next_cannot_leave_the_site(anon_client, admin):
    response = anon_client.post(
        "/login?next=//evil.example",
        data={"login": ADMIN_EMAIL, "password": "correct-horse-battery", "csrf": csrf(anon_client)},
    )
    assert response.headers["Location"] == "/"


ADMIN_EMAIL = "admin@example.com"


def test_login_next_rejects_backslash_and_control_characters(anon_client, admin):
    for target in ("/\\evil.example", "/%5Cevil.example", "/\t/evil.example", "https://evil.example", "evil.example"):
        client = app.test_client_class(app, app.response_class)
        response = client.post(
            "/login", query_string={"next": target}, data={"login": ADMIN_EMAIL, "password": "correct-horse-battery", "csrf": csrf(client)}
        )
        location = response.headers["Location"]
        assert response.status_code == 302 and "\\" not in location and not location.startswith("//"), target
        assert target == "/%5Cevil.example" or location == "/", target


def test_non_ascii_csrf_and_webhook_token_are_refused_not_500(anon_client):
    assert anon_client.post("/login", data={"login": "x", "password": "y", "csrf": "é"}).status_code == 400
    assert anon_client.post("/bird/webhook?token=é", json={}).status_code == 401


def test_login_is_throttled_after_repeated_failures(anon_client, admin):
    for _ in range(auth.MAX_FAILURES_PER_LOGIN):
        assert sign_in(anon_client, "98 0000 0001", "wrong-password").status_code == 401
    # Even the right password is refused, whichever way the phone is written.
    assert sign_in(anon_client, "+919800000001", "correct-horse-battery").status_code == 429
    assert anon_client.get("/").status_code == 302
    assert sign_in(anon_client, "someone-else@example.com", "x").status_code == 401


def test_password_reset_signs_out_existing_sessions(store, anon_client):
    user = get_or_create_user(store, **STAFF)
    assert sign_in(anon_client, STAFF["email"], STAFF["password"]).status_code == 302
    assert anon_client.get("/").status_code == 200
    auth.change_password(store, user["id"], "a-brand-new-password")
    assert anon_client.get("/").status_code == 302
    assert sign_in(anon_client, STAFF["email"], "a-brand-new-password").status_code == 302


def test_ui_save_keeps_what_the_bot_wrote_meanwhile(store):
    lead = store.create_lead(Lead(name="Old", text_notes="first"))
    client = app.test_client()
    client.get(f"/leads/{lead.id}")  # staff opens the page...
    bot = store.get_lead(lead.id)
    bot.text_notes = "first | second note from the bot"
    store.update_lead(bot)  # ...the bot appends a note...
    client.post(f"/leads/{lead.id}", data={"name": "New", "doc_type": "other", "csrf": csrf(client)})  # ...then staff saves
    saved = store.get_lead(lead.id)
    assert saved.name == "New" and saved.text_notes == "first | second note from the bot"


def test_deleted_leads_hide_their_media_and_feedback(store, monkeypatch):
    monkeypatch.setattr("didac_app.blobstore.signed_url", lambda config, path: "https://signed.example/" + path)
    lead = store.create_lead(Lead(name="Gone", from_number="whatsapp:+919811111114"))
    media_id = store.add_media(lead.id, "u", "image/jpeg", "k/gone.jpg", "text")
    store.add_feedback("whatsapp:+919811111114", "typed", lead.id, feedback_type="text_message")
    orphan_id = store.add_media(None, "u2", "image/jpeg", "k/orphan.jpg", "")
    client = app.test_client()
    assert client.get(f"/media/{media_id}").status_code == 302
    client.post(f"/leads/{lead.id}/delete", data={"csrf": csrf(client)})
    assert client.get(f"/media/{media_id}").status_code == 404
    assert media_id not in [row["id"] for row in client.get("/api/media").json]
    assert client.get("/api/feedback").json == []
    assert client.get(f"/media/{orphan_id}").status_code == 302


def test_post_without_csrf_token_is_refused():
    client = app.test_client()
    assert client.post("/logout").status_code == 400
    assert client.post("/logout", data={"csrf": csrf(client)}).status_code == 302


def test_staff_cannot_open_team_page(store, anon_client):
    get_or_create_user(store, **STAFF)
    sign_in(anon_client, STAFF["email"], STAFF["password"])
    assert anon_client.get("/team").status_code == 403
    assert b"/team" not in anon_client.get("/").data


def test_admin_adds_member_who_can_then_log_in(store):
    client = app.test_client()
    data = {"name": "New Person", "phone": "98450 11111", "email": "", "password": "longenough1", "csrf": csrf(client)}
    assert client.post("/team", data=data).status_code == 302
    created = store.find_user("+919845011111")
    assert created["name"] == "New Person" and not created["is_admin"]
    assert auth.authenticate(store, "+919845011111", "longenough1")


def test_team_rejects_bad_input(store):
    client = app.test_client()
    token = csrf(client)
    base = {"name": "X", "phone": "+919845022222", "email": "", "password": "longenough1", "csrf": token}
    for change in ({"phone": "123"}, {"password": "short"}, {"name": " "}, {"email": "not-an-email"}, {"phone": "+919800000001"}):
        client.post("/team", data={**base, **change})
    assert store.find_user("+919845022222") is None
    assert [u["name"] for u in store.list_users()].count("X") == 0


def test_admin_resets_password_and_deactivates_others_but_not_self(store, admin):
    staff = get_or_create_user(store, **STAFF)
    client = app.test_client()
    token = csrf(client)
    client.post(f"/team/{staff['id']}/password", data={"password": "brand-new-pass", "csrf": token})
    assert auth.authenticate(store, STAFF["email"], "brand-new-pass")
    client.post(f"/team/{admin['id']}/active", data={"csrf": token})
    assert store.get_user(admin["id"])["is_active"]
    client.post(f"/team/{staff['id']}/active", data={"csrf": token})
    assert not store.get_user(staff["id"])["is_active"]
    store.set_user_active(staff["id"], True)


def test_gallery_splits_forms_from_other_documents(store):
    store.create_lead(Lead(name="Form Person", doc_type="form", phone="+919800000010"))
    store.create_lead(Lead(name="Card Person", phone="+919800000011"))
    client = app.test_client()
    forms = client.get("/?type=forms").data.decode()
    other = client.get("/?type=other").data.decode()
    assert "Form Person" in forms and "Card Person" not in forms
    assert "Card Person" in other and "Form Person" not in other
    assert "Card Person" in client.get("/?type=other&q=person").data.decode()
    assert "Card Person" not in client.get("/?type=other&q=zzz").data.decode()


def test_visitor_and_team_leads_are_labelled(store, admin):
    store.create_lead(Lead(name="Visitor Lead", from_number="whatsapp:+919811111112"))
    store.create_lead(Lead(name="Team Lead", from_number="whatsapp:" + admin["phone"]))
    html = app.test_client().get("/?type=other").data.decode()
    assert "Visitor via WhatsApp" in html
    assert "Test Admin" in html


def test_editing_a_lead_records_who_and_clears_the_check_badge(store):
    lead = store.create_lead(Lead(name="Old Name", review_reason="phone", phone="+919800000012"))
    client = app.test_client()
    page = client.get(f"/leads/{lead.id}").data.decode()
    assert "Needs a quick check" in page
    response = client.post(
        f"/leads/{lead.id}", data={"name": "New Name", "org": "Org", "doc_type": "form", "interest": "Labs", "csrf": csrf(client)}
    )
    assert response.status_code == 302
    saved = store.get_lead(lead.id)
    assert (saved.name, saved.org, saved.doc_type, saved.interest) == ("New Name", "Org", "form", "Labs")
    assert saved.updated_by == "Test Admin" and saved.review_reason == ""


def test_lead_detail_shows_all_received_items(store):
    lead = store.create_lead(Lead(name="Many Things", from_number="whatsapp:+919811111113"))
    store.add_media(lead.id, "u1", "image/jpeg", "k/1.jpg", "Name: Many")
    store.add_media(lead.id, "u2", "audio/ogg", "k/2.ogg", "spoken words here")
    store.add_feedback("whatsapp:+919811111113", "raw text as typed", lead.id, feedback_type="text_message")
    store.add_feedback("whatsapp:+919811111113", "derived transcript", lead.id, feedback_type="audio_transcript")
    html = app.test_client().get(f"/leads/{lead.id}").data.decode()
    assert "spoken words here" in html and "raw text as typed" in html
    assert "derived transcript" not in html


def test_only_admins_delete_and_deleted_leads_disappear(store, anon_client):
    lead = store.create_lead(Lead(name="Doomed", phone="+919800000013"))
    get_or_create_user(store, **STAFF)
    sign_in(anon_client, STAFF["email"], STAFF["password"])
    assert anon_client.post(f"/leads/{lead.id}/delete", data={"csrf": csrf(anon_client)}).status_code == 403
    client = app.test_client()
    assert client.post(f"/leads/{lead.id}/delete", data={"csrf": csrf(client)}).status_code == 302
    assert store.get_lead(lead.id) is None
    assert "Doomed" not in client.get("/?type=other").data.decode()
    assert client.get(f"/leads/{lead.id}").status_code == 404


def test_phone_normalisation():
    assert auth.normalize_phone("98450 12345") == "+919845012345"
    assert auth.normalize_phone("whatsapp:+919845012345") == "+919845012345"
    assert auth.normalize_phone("12345") == ""


def test_cli_creates_an_admin(store, monkeypatch):
    from didac_app import cli

    monkeypatch.setattr("sys.argv", ["cli", "create-admin", "--name", "Cli Admin", "--phone", "98450 33333"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: "cli-password-1")
    cli.main()
    created = store.find_user("+919845033333")
    assert created["is_admin"] and auth.authenticate(store, "+919845033333", "cli-password-1")
