from __future__ import annotations

import re

from didac_app.config import DidacConfig, brochure_catalog
from didac_app.models import Lead
from didac_app.parser import update_lead_from_note
from didac_app.storage import LeadStore

MAIN_MENU = (
    "Welcome to Xperimentor at DIDAC.\n"
    "Please reply with a number:\n\n"
    "1. Download brochure\n"
    "2. Share my requirement\n"
    "3. Give feedback / send voice note or photo\n"
    "4. Book a demo\n"
    "5. Talk to sales\n"
    "6. Scan event badge / ID card / QR\n\n"
    "At any time, type menu to see this again."
)

ROLE_PROMPT = (
    "Please choose your role, or type/record one short note about yourself and your requirement.\n\n"
    "1. School\n2. NGO / Foundation\n3. CSR / Corporate\n4. Distributor\n5. Government\n6. Other\n\n"
    "Example: I am a distributor from Karnataka interested in STREAM and science kits for 25 schools."
)


def is_start_message(body: str) -> bool:
    return body.strip().lower() in {"hi", "hello", "hey", "start", "brochure", "info", "menu"}


def start_intake(from_number: str, store: LeadStore) -> str:
    lead = Lead(from_number=from_number, staff_number="", phone=clean_whatsapp_number(from_number), status="Menu:await")
    store.create_lead(lead)
    return MAIN_MENU


def continue_intake(lead: Lead, body: str, store: LeadStore, config: DidacConfig) -> tuple[Lead, str]:
    state = lead.status
    text = body.strip()
    if text.lower() == "menu":
        lead.status = "Menu:await"
        store.update_lead(lead)
        return lead, MAIN_MENU
    if state == "Menu:await":
        return handle_menu_choice(lead, text, store, config)
    if state == "Brochure:choose":
        return handle_brochure_choice(lead, text, store, config)
    if state == "Feedback:collect":
        store.add_feedback(lead.from_number, text, lead.id, feedback_type="text", sentiment=feedback_sentiment(text), action_required=feedback_action(text))
        lead.feedback_notes = merge_note(lead.feedback_notes, text)
        lead.notes = merge_note(lead.notes, f"Feedback: {text}")
        lead.status = "New"
        update_lead_from_note(lead, text)
        store.update_lead(lead)
        return lead, "Thank you. We saved your feedback. If you want anything else, reply menu."
    if state == "Badge:collect":
        return lead, "Please send the badge / ID card photo now. Type menu to go back."
    if state == "Intake:role":
        if is_qualification_note(text):
            update_lead_from_note(lead, text)
            lead.status = "New"
            store.update_lead(lead)
            return lead, qualification_saved_reply(lead)
        lead.lead_type = normalize_role(text)
        lead.status = "Intake:city"
        store.update_lead(lead)
        return lead, "Great. Which city/state are you from?"
    if state == "Intake:city":
        lead.city = text
        lead.state = text
        lead.status = "Intake:requirement"
        store.update_lead(lead)
        return lead, "What are you looking for? Example: science lab for 20 schools, teacher training, CSR pilot, distributor enquiry."
    if state == "Intake:requirement":
        lead.interest = text
        lead.schools_count = extract_schools_count(text)
        lead.status = "New"
        store.update_lead(lead)
        return lead, "Thanks. We saved your details. Our team will be in touch."
    return lead, ROLE_PROMPT


def handle_menu_choice(lead: Lead, text: str, store: LeadStore, config: DidacConfig) -> tuple[Lead, str]:
    lowered = text.lower()
    if lowered.startswith("1") or "brochure" in lowered:
        lead.status = "Brochure:choose"
        store.update_lead(lead)
        return lead, brochure_menu(config)
    if lowered.startswith("2") or "requirement" in lowered or "lead" in lowered:
        lead.status = "Intake:role"
        store.update_lead(lead)
        return lead, ROLE_PROMPT
    if lowered.startswith("3") or "feedback" in lowered or "photo" in lowered or "voice" in lowered:
        lead.status = "Feedback:collect"
        store.update_lead(lead)
        return lead, "Please type your feedback, or send a voice note/photo now. We will save it for the team."
    if lowered.startswith("4") or "demo" in lowered:
        lead.interest = merge_note(lead.interest, "Demo requested")
        lead.status = "New"
        store.update_lead(lead)
        calendar = f"\nBook here: {config.calendar_url}" if config.calendar_url else ""
        return lead, f"Thanks. We marked this as a demo request. Our team will follow up.{calendar}"
    if lowered.startswith("5") or "sales" in lowered or "talk" in lowered:
        lead.interest = merge_note(lead.interest, "Talk to sales requested")
        lead.status = "New"
        store.update_lead(lead)
        return lead, "Thanks. We marked this for sales follow-up. Please share your name and organisation if not already shared."
    if lowered.startswith("6") or "badge" in lowered or "id card" in lowered or "qr" in lowered:
        lead.status = "Badge:collect"
        store.update_lead(lead)
        return lead, "Please send a photo of the event badge / ID card. If possible, keep the QR code clear and not tilted."
    return lead, "I didn't catch that. Please reply with 1, 2, 3, 4, 5, or 6.\n\n" + MAIN_MENU


def brochure_menu(config: DidacConfig) -> str:
    lines = ["Which brochure would you like?", ""]
    for number, item in brochure_catalog(config).items():
        lines.append(f"{number}. {item['name']}")
    lines.append("\nReply with the number. Type menu to go back.")
    return "\n".join(lines)


def handle_brochure_choice(lead: Lead, text: str, store: LeadStore, config: DidacConfig) -> tuple[Lead, str]:
    catalog = brochure_catalog(config)
    choice = text.strip().lower()
    selected = catalog.get(choice)
    if not selected:
        for item in catalog.values():
            if choice in item["name"].lower():
                selected = item
                break
    if not selected:
        return lead, "Please choose a brochure number.\n\n" + brochure_menu(config)
    lead.interest = merge_note(lead.interest, f"Downloaded brochure: {selected['name']}")
    lead.tags = merge_note(lead.tags, selected["key"])
    lead.status = "New"
    store.update_lead(lead)
    if selected["url"]:
        return lead, f"Here is the {selected['name']} brochure:\n{selected['url']}\n\nWould you also like to share your requirement? Reply 2 or type menu."
    return lead, f"{selected['name']} brochure link is not configured yet. Our team will send it manually. Type menu for other options."


def normalize_role(text: str) -> str:
    lowered = text.lower()
    if lowered.startswith("1") or "school" in lowered:
        return "School"
    if lowered.startswith("2") or "ngo" in lowered or "foundation" in lowered:
        return "NGO"
    if lowered.startswith("3") or "csr" in lowered or "corporate" in lowered:
        return "CSR"
    if lowered.startswith("4") or "distributor" in lowered or "dealer" in lowered:
        return "Distributor"
    if lowered.startswith("5") or "government" in lowered:
        return "Government"
    return text.title() if text else "Other"


def extract_schools_count(text: str) -> str:
    match = re.search(r"(\d+)\s*(?:government\s*)?schools?", text, re.I)
    return match.group(1) if match else ""


def is_qualification_note(text: str) -> bool:
    lowered = text.lower()
    if len(text.split()) >= 7:
        return True
    return any(term in lowered for term in ("interested", "looking", "need", "want", "require", "schools", "budget", "demo", "proposal"))


def qualification_saved_reply(lead: Lead) -> str:
    return (
        "Thanks. We saved your profile and requirement.\n"
        f"Role: {lead.lead_type or lead.organisation_type or 'Not specified'}\n"
        f"Interest: {lead.interest or 'Not specified'}\n\n"
        "You can send a visiting card photo also, or type menu for brochures/demo."
    )


def clean_whatsapp_number(value: str) -> str:
    return value.replace("whatsapp:", "")


def merge_note(current: str, value: str) -> str:
    if not current:
        return value
    if value.lower() in current.lower():
        return current
    return f"{current}; {value}"


def feedback_sentiment(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("bad", "issue", "problem", "not good", "confusing", "expensive")):
        return "negative"
    if any(word in lowered for word in ("good", "great", "excellent", "interested", "useful", "nice")):
        return "positive"
    return "neutral"


def feedback_action(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("call", "proposal", "demo", "price", "pricing", "quote")):
        return "sales_follow_up"
    if any(word in lowered for word in ("issue", "problem", "complaint")):
        return "manager_review"
    return ""
