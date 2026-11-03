from didac_app.dedupe import fill_blanks, find_duplicate, names_compatible
from didac_app.models import Lead


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


def test_shared_phone_with_a_different_name_is_only_possible(store):
    existing = store.create_lead(Lead(name="Asha Rao", org="Sunrise", phone="080 2345 6789"))
    match = find_duplicate(store, Lead(name="Vikram Shah", org="Sunrise", phone="+91 80 2345 6789"))
    assert match[0].id == existing.id and match[1] is False
    assert find_duplicate(store, Lead(phone="+91 80 2345 6789"))[1] is True


def test_names_compatible():
    assert names_compatible("Kavya Nair", "Kavya N")
    assert names_compatible("Dr. Asha Rao", "Asha Rao")
    assert names_compatible("", "Asha Rao")
    assert not names_compatible("Asha Rao", "Vikram Shah")
    assert not names_compatible("Anil Kumar", "Sunil Kumar")


def test_soft_deleted_lead_is_not_matched(store):
    lead = store.create_lead(Lead(name="Gone Person", email="gone@x.edu"))
    store.update_lead_fields(lead.id, deleted_at="2026-11-02T10:00:00")
    assert find_duplicate(store, Lead(email="gone@x.edu")) is None


def test_merge_into_lead_keeps_a_value_set_since_the_read(store):
    lead = store.create_lead(Lead(name="Asha Rao", tags="Expo"))
    store.update_lead_fields(lead.id, city="Pune")  # dashboard edit after the scan read the lead
    store.merge_into_lead(lead.id, {"city": "Delhi", "email": "a@b.com"}, {"tags": "Expo", "captured_by": "Ravi"})
    merged = store.get_lead(lead.id)
    assert (merged.city, merged.email, merged.tags, merged.captured_by) == ("Pune", "a@b.com", "Expo", "Ravi")
