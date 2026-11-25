from __future__ import annotations

import re

from didac_app.models import Lead

FIELD_ALIASES = {
    "first name": "first_name",
    "firstname": "first_name",
    "last name": "last_name",
    "lastname": "last_name",
    "name": "name",
    "person": "name",
    "contact": "name",
    "by": "captured_by",
    "captured by": "captured_by",
    "staff": "captured_by",
    "team member": "captured_by",
    "context": "capture_context",
    "capture context": "capture_context",
    "event context": "capture_context",
    "org": "org",
    "organisation": "org",
    "organisation name": "org",
    "organization": "org",
    "organization name": "org",
    "company": "org",
    "school": "org",
    "role": "role",
    "designation": "role",
    "title": "designation",
    "job title": "designation",
    "organisation type": "organisation_type",
    "organization type": "organisation_type",
    "type of organisation": "organisation_type",
    "type of organization": "organisation_type",
    "curriculum": "curriculum",
    "medium": "medium_of_instruction",
    "medium of instruction": "medium_of_instruction",
    "city": "city",
    "state": "state",
    "district": "district",
    "address": "address",
    "street address": "address",
    "postal address": "address",
    "pincode": "pincode",
    "pin code": "pincode",
    "postal code": "pincode",
    "phone": "phone",
    "mobile": "phone",
    "email": "email",
    "website": "website",
    "google maps url": "google_maps_url",
    "google map": "google_maps_url",
    "maps url": "google_maps_url",
    "qr url": "qr_url",
    "qr link": "qr_url",
    "qr page text": "qr_page_text",
    "event profile": "qr_page_text",
    "udise": "udise_code",
    "udise code": "udise_code",
    "parent organisation id": "parent_organisation_id",
    "parent organization id": "parent_organisation_id",
    "type": "lead_type",
    "lead type": "lead_type",
    "interest": "interest",
    "requirement": "interest",
    "need": "interest",
    "products": "products",
    "product": "products",
    "products interested": "products",
    "demo": "demo_interest",
    "demo interest": "demo_interest",
    "interested in demo": "demo_interest",
    "workshop": "workshop",
    "teacher workshop": "workshop",
    "students": "students",
    "number of students": "students",
    "budget": "budget",
    "timeline": "timeline",
    "schools": "schools_count",
    "no of schools": "schools_count",
    "number of schools": "schools_count",
    "grades": "grades",
    "notes": "notes",
    "note": "notes",
    "lead owner": "lead_owner",
    "owner": "lead_owner",
    "tag": "tags",
    "tags": "tags",
}


def parse_message(body: str, from_number: str = "") -> tuple[str, Lead | None]:
    text = (body or "").strip()
    lowered = text.lower()
    if lowered.startswith("new lead"):
        return "lead", parse_structured_lead(text, from_number)
    if lowered.startswith("met "):
        return "lead", parse_voice_style_note(text, from_number)
    if lowered.startswith("tag:") or lowered.startswith("tags:"):
        lead = Lead(from_number=from_number, tags=text.split(":", 1)[-1].strip(), raw_message=text)
        return "tag", lead
    return "message", None


def parse_structured_lead(text: str, from_number: str = "") -> Lead:
    lead = Lead(from_number=from_number, staff_number=from_number, raw_message=text)
    for line in text.splitlines()[1:]:
        if ":" not in line:
            append_note(lead, line)
            continue
        key, value = line.split(":", 1)
        field = FIELD_ALIASES.get(normalize_key(key))
        if field and hasattr(lead, field):
            current = getattr(lead, field)
            setattr(lead, field, merge_value(current, value.strip()))
        else:
            append_note(lead, line)
    infer_contact_from_text(lead, text)
    normalize_contact_name(lead)
    return lead


def parse_voice_style_note(text: str, from_number: str = "") -> Lead:
    lead = Lead(from_number=from_number, staff_number=from_number, notes=text, raw_message=text)
    match = re.search(r"met\s+([A-Z][A-Za-z.\s]{1,45}?)(?:\s+from\s+|\s+at\s+)", text, re.I)
    if match:
        lead.name = clean_person(match.group(1))
        split_name_into_parts(lead)
    org_match = re.search(r"(?:from|at)\s+([^.,\n]+)", text, re.I)
    if org_match:
        lead.org = org_match.group(1).strip()
    schools = re.search(r"(\d+)\s*(?:government\s*)?schools?", text, re.I)
    if schools:
        lead.schools_count = schools.group(1)
    city_state = re.search(r"\bin\s+([A-Z][A-Za-z ]{2,30})(?:[.,]|$)", text)
    if city_state:
        lead.city = city_state.group(1).strip()
    if re.search(r"\bcsr\b", text, re.I):
        lead.lead_type = "CSR"
    elif re.search(r"\bngo|foundation|trust\b", text, re.I):
        lead.lead_type = "NGO"
    elif re.search(r"\bschool|principal\b", text, re.I):
        lead.lead_type = "School"
    lead.interest = interest_from_text(text)
    lead.timeline = timeline_from_text(text)
    infer_contact_from_text(lead, text)
    normalize_contact_name(lead)
    return lead


def update_lead_from_text(lead: Lead, text: str) -> Lead:
    update = parse_structured_lead("New lead\n" + text, lead.from_number)
    for field in FIELD_ALIASES.values():
        value = getattr(update, field, "")
        if value:
            current = getattr(lead, field)
            setattr(lead, field, merge_value(current, value))
    if update.notes:
        append_note(lead, update.notes)
    normalize_contact_name(lead)
    return lead


def update_lead_from_note(lead: Lead, text: str) -> Lead:
    update_lead_from_text(lead, text)
    note_update = parse_qualification_note(text, lead.from_number)
    for field in (
        "lead_type",
        "organisation_type",
        "role",
        "interest",
        "budget",
        "timeline",
        "schools_count",
        "city",
        "state",
        "grades",
    ):
        value = getattr(note_update, field, "")
        if value:
            current = getattr(lead, field)
            setattr(lead, field, merge_value(current, value))
    append_note(lead, f"Qualification note: {text}")
    normalize_contact_name(lead)
    return lead


def parse_qualification_note(text: str, from_number: str = "") -> Lead:
    lead = Lead(from_number=from_number, staff_number=from_number, raw_message=text)
    lowered = text.lower()
    role = role_from_text(lowered)
    if role:
        lead.lead_type = role
        lead.organisation_type = role
        lead.role = role
    lead.interest = interest_from_text(text)
    lead.schools_count = extract_schools_count(text)
    lead.timeline = timeline_from_text(text)
    lead.budget = budget_from_text(text)
    lead.grades = grades_from_text(text)
    place = place_from_text(text)
    if place:
        lead.city = place
        lead.state = place
    return lead


def infer_contact_from_text(lead: Lead, text: str) -> None:
    phone = re.search(r"(?:(?:\+91[\s-]?)?[6-9]\d{9})", text)
    if phone and not lead.phone:
        lead.phone = phone.group(0)
    email = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I)
    if email and not lead.email:
        lead.email = email.group(0).lower()


def normalize_contact_name(lead: Lead) -> None:
    if not lead.name and (lead.first_name or lead.last_name):
        lead.name = " ".join(part for part in (lead.first_name, lead.last_name) if part).strip()
    if lead.name and (not lead.first_name or not lead.last_name):
        split_name_into_parts(lead)
    if lead.role and not lead.designation:
        lead.designation = lead.role
    if lead.designation and not lead.role:
        lead.role = lead.designation


def split_name_into_parts(lead: Lead) -> None:
    parts = [part for part in re.split(r"\s+", lead.name.strip()) if part]
    if parts and not lead.first_name:
        lead.first_name = parts[0]
    if len(parts) > 1 and not lead.last_name:
        lead.last_name = " ".join(parts[1:])


def interest_from_text(text: str) -> str:
    lowered = text.lower()
    interests = []
    product_terms = {
        "sia": "SIA",
        "tiqer": "TiQer",
        "play lab": "Play Lab",
        "stream": "STREAM",
        "lab-in-classroom": "Lab-in-Classroom",
        "lab in classroom": "Lab-in-Classroom",
    }
    for term, label in product_terms.items():
        if term in lowered:
            interests.append(label)
    if "proposal" in lowered:
        interests.append("Proposal requested")
    if "demo" in lowered:
        interests.append("Demo requested")
    if "stem" in lowered or "science" in lowered or "lab" in lowered:
        interests.append("STEM lab / science kits")
    if "teacher" in lowered or "training" in lowered:
        interests.append("Teacher training")
    if "assessment" in lowered or "analytics" in lowered or "report" in lowered:
        interests.append("Assessment / analytics")
    if "distributor" in lowered or "dealership" in lowered or "channel" in lowered:
        interests.append("Channel partnership")
    return "; ".join(dict.fromkeys(interests))


def timeline_from_text(text: str) -> str:
    lowered = text.lower()
    if "next week" in lowered:
        return "Next week"
    if "immediate" in lowered or "as soon as possible" in lowered or "asap" in lowered:
        return "Immediate"
    if "q1" in lowered:
        return "Q1"
    if "q2" in lowered:
        return "Q2"
    if "q3" in lowered:
        return "Q3"
    if "q4" in lowered:
        return "Q4"
    if "month" in lowered:
        return "This month"
    return ""


def role_from_text(lowered: str) -> str:
    if any(term in lowered for term in ("distributor", "dealer", "channel partner", "reseller")):
        return "Distributor"
    if any(term in lowered for term in ("csr", "corporate", "company foundation")):
        return "CSR"
    if any(term in lowered for term in ("ngo", "foundation", "trust")):
        return "NGO"
    if any(term in lowered for term in ("school management", "school owner", "principal", "school")):
        return "School"
    if any(term in lowered for term in ("government", "department", "ministry")):
        return "Government"
    if "investor" in lowered:
        return "Investor"
    return ""


def extract_schools_count(text: str) -> str:
    match = re.search(r"(\d+)\s*(?:government\s*|govt\s*)?schools?", text, re.I)
    return match.group(1) if match else ""


def budget_from_text(text: str) -> str:
    match = re.search(r"(?:budget\s*(?:is|of|:)?\s*)?((?:rs\.?|₹|inr)\s*[\d,.]+\s*(?:lakh|lakhs|cr|crore|crores|k)?)", text, re.I)
    return match.group(1).strip() if match else ""


def grades_from_text(text: str) -> str:
    match = re.search(r"\b(?:grade|grades|class|classes)\s*([0-9ivx,\-\s]+)", text, re.I)
    return match.group(1).strip() if match else ""


def place_from_text(text: str) -> str:
    match = re.search(
        r"\b(?:in|from|across)\s+([A-Z][A-Za-z ]{2,30}?)(?:\s+(?:interested|looking|need|want|require|for)\b|[.,]|$)",
        text,
    )
    return match.group(1).strip() if match else ""


def normalize_key(key: str) -> str:
    return re.sub(r"\s+", " ", key.strip().lower())


def clean_person(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .,")


def merge_value(current: str, new: str) -> str:
    if not current:
        return new
    if not new or new.lower() in current.lower():
        return current
    return f"{current}; {new}"


def append_note(lead: Lead, note: str) -> None:
    note = note.strip()
    if not note:
        return
    lead.notes = merge_value(lead.notes, note)
