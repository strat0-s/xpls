from bird_helpers import post_bird
from didac_app.dedupe import fill_blanks, find_duplicate
from didac_app.models import Lead
from didac_app.server import app


def test_email_and_phone_match_are_certain_and_formats_are_normalised(store):
    existing = store.create_lead(Lead(name="Asha Rao", org="Sunrise", phone="98450 11111", email="Asha@Sunrise.edu"))
    by_email = find_duplicate(store, Lead(email="asha@sunrise.edu"))
    by_phone = find_duplicate(store, Lead(phone="+91 9845011111"))
    assert by_email[0].id == existing.id and by_email[1]
    assert by_phone[0].id == existing.id and by_phone[1]


def test_name_and_org_only_is_a_possible_match(store):
    existing = store.create_lead(Lead(name="Meera Iyer", org="Bright School"))
    match = find_duplicate(store, Lead(name="meera iyer", org="BRIGHT SCHOOL", phone="9845022222"))
    assert match[0].id == existing.id and match[1] is False
    assert find_duplicate(store, Lead(name="Meera Iyer", org="Other School")) is None
    assert find_duplicate(store, Lead(name="Meera Iyer")) is None


def test_fill_blanks_never_overwrites():
    existing = Lead(name="Asha Rao", city="Pune")
    filled = fill_blanks(existing, Lead(name="Someone Else", city="Delhi", email="a@b.com", status="Closed"))
    assert filled == ["email"]
    assert (existing.name, existing.city, existing.email, existing.status) == ("Asha Rao", "Pune", "a@b.com", "New")


def scan(monkeypatch, client, number, text, url):
    import didac_app.server as server

    monkeypatch.setattr(server, "download_media", lambda media_url, content_type, from_number="": ("2026-10-18/x/card.jpg", "/tmp/card.jpg"))
    monkeypatch.setattr(server, "extract_media_text", lambda local_path, content_type: text)
    return post_bird(client, number, "", [(url, "image/jpeg")])


def test_repeat_scan_merges_into_existing_lead(monkeypatch, sent):
    import didac_app.server as server

    client = app.test_client()
    number = "whatsapp:+910000000301"
    scan(monkeypatch, client, number, "Name: Kavya Nair\nOrg: Dedupe Academy\nPhone: 9845033333", "https://media.bird.com/dd-1")
    first = server.store.latest_lead_for_number(number)
    scan(monkeypatch, client, number, "Name: Kavya N\nPhone: +91 98450 33333\nEmail: kavya@dedupe.edu", "https://media.bird.com/dd-2")
    assert f"Already saved as #{first.id}" in sent[-1][1]
    assert len(server.store.list_leads("from_number=%s", (number,))) == 1
    merged = server.store.get_lead(first.id)
    assert merged.email == "kavya@dedupe.edu"
    assert merged.name == "Kavya Nair"
    assert len(server.store.list_media(first.id)) == 2


def test_repeat_scan_with_nothing_new_is_skipped(monkeypatch, sent):
    import didac_app.server as server

    client = app.test_client()
    number = "whatsapp:+910000000302"
    card = "Name: Ravi Kumar\nOrg: Skip School\nPhone: 9845044444"
    scan(monkeypatch, client, number, card, "https://media.bird.com/dd-3")
    first = server.store.latest_lead_for_number(number)
    scan(monkeypatch, client, number, card, "https://media.bird.com/dd-4")
    assert f"Already saved as #{first.id}" in sent[-1][1]
    assert len(server.store.list_leads("from_number=%s", (number,))) == 1
    assert len(server.store.list_media(first.id)) == 1


def test_name_org_match_is_saved_and_flagged(monkeypatch, sent):
    import didac_app.server as server

    client = app.test_client()
    number = "whatsapp:+910000000303"
    scan(monkeypatch, client, number, "Name: Tara Menon\nOrg: Flag School\nPhone: 9845055555", "https://media.bird.com/dd-5")
    first = server.store.latest_lead_for_number(number)
    scan(monkeypatch, client, number, "Name: Tara Menon\nOrg: Flag School\nPhone: 9845066666", "https://media.bird.com/dd-6")
    leads = server.store.list_leads("from_number=%s", (number,))
    assert len(leads) == 2
    assert leads[0].review_reason == f"possible duplicate of #{first.id}"
