from didac_app.config import DidacConfig
from didac_app.intake import continue_intake, start_intake


def test_visitor_intake_flow(store):
    reply = start_intake("whatsapp:+919876543210", store)
    assert "download brochure" in reply.lower()
    lead = store.latest_lead_for_number("whatsapp:+919876543210")
    lead, reply = continue_intake(lead, "2", store, DidacConfig())
    assert "choose your role" in reply.lower()
    lead, reply = continue_intake(lead, "CSR", store, DidacConfig())
    assert lead.lead_type == "CSR"
    assert "city" in reply.lower()
    lead, reply = continue_intake(lead, "Bangalore", store, DidacConfig())
    assert lead.city == "Bangalore"
    lead, reply = continue_intake(lead, "science lab for 20 schools", store, DidacConfig())
    assert lead.status == "New"
    assert lead.priority in {"Hot", "Warm"}


def test_brochure_menu_flow(store):
    config = DidacConfig(tiqer_brochure_url="https://example.com/tiqer.pdf")
    start_intake("whatsapp:+919876543211", store)
    lead = store.latest_lead_for_number("whatsapp:+919876543211")
    lead, reply = continue_intake(lead, "1", store, config)
    assert "tiqer" in reply.lower()
    lead, reply = continue_intake(lead, "2", store, config)
    assert "https://example.com/tiqer.pdf" in reply
    assert "TiQer" in lead.interest


def test_requirement_can_be_captured_as_one_short_note(store):
    config = DidacConfig()
    start_intake("whatsapp:+919876543212", store)
    lead = store.latest_lead_for_number("whatsapp:+919876543212")
    lead, reply = continue_intake(lead, "2", store, config)
    assert "type/record one short note" in reply.lower()
    lead, reply = continue_intake(
        lead,
        "I am a distributor from Karnataka interested in STREAM and science kits for 25 schools",
        store,
        config,
    )
    assert lead.status == "New"
    assert lead.lead_type == "Distributor"
    assert "STREAM" in lead.interest
    assert lead.schools_count == "25"
    assert lead.owner == "Channel sales"
    assert "saved your profile" in reply.lower()


def test_badge_scan_menu_option(store):
    config = DidacConfig()
    start_intake("whatsapp:+919876543213", store)
    lead = store.latest_lead_for_number("whatsapp:+919876543213")
    lead, reply = continue_intake(lead, "6", store, config)
    assert lead.status == "Badge:collect"
    assert "badge" in reply.lower()
