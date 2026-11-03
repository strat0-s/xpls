from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path


def openai_enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


LEAD_PROMPT = """
Extract lead details from this DIDAC stall image. It may be a visiting card, event ID badge, QR badge, handwritten note, brochure, or visitor form.
If multiple visiting cards or contacts are visible in one image, return JSON with a top-level "contacts" array, one object per card/contact.
If only one card/contact is visible, you may return a single object.
For event badges and handwritten forms, carefully read printed and handwritten text. Preserve uncertain handwritten words in notes instead of guessing.
If a QR code or URL is visibly printed and readable, include it as qr_url. If badge/profile details are visible around the QR code, include them in notes.
Each contact object should use these keys:
first_name, last_name, name, designation, role, email, phone,
org, organisation_type, curriculum, medium_of_instruction, state, city, district, pincode,
website, google_maps_url, qr_url, qr_page_text, udise_code, parent_organisation_id,
lead_type, interest, budget, timeline, schools_count, notes.
organisation_type must be exactly one of SCHOOL, SCHOOL_CHAIN, CSR, NGO, GOVERNMENT, DISTRIBUTOR, INSTITUTION, VENDOR, OTHER.
(DISTRIBUTOR = reseller or dealer; INSTITUTION = college, university or training centre; VENDOR = another edtech, hardware or content company; OTHER = individual, media, investor.)
Leave organisation_type empty if the organisation is unclear.
Use empty strings when unknown. Do not guess.
"""

DOCUMENT_PROMPT = LEAD_PROMPT + """
The images are the sides or pages of ONE document: read them together and return a single object, not a "contacts" array.
Add a key "check": a list of short phrases naming each field you could not read with confidence (for example "phone digits unclear").
"""

FORM_PROMPT = DOCUMENT_PROMPT + """
This is the printed Xperimentor contact form. Fields: Full Name, Organization, City, Phone, Email; Your Role (Management, Principal, Teacher/Coordinator, Other);
Curriculum (CBSE, ICSE, State, IB, IGCSE, Other); Interested in demo (At School, Online); Interested to attend teacher workshops (Yes, No); Number of Students; Remarks.
Use the extra keys: demo ("At school" or "Online"), workshop ("Yes" or "No"), students (exactly as written), remarks (exactly as written).
Put a role or curriculum that was only written in (not ticked) in role/curriculum as written and add it to "check". A crossed-out printed value is not an answer.
Handwritten or doubtful values (student count, phone digits) go in "check".
"""


BADGE_PROMPT = DOCUMENT_PROMPT + """
This is an event badge or ID card. Read the person's name, designation, organisation and city. Printed codes, stall or exhibitor numbers that are not a person's details go in notes.
If a URL or ID is printed next to the QR code, put it in qr_url (a URL) or qr_page_text (an ID). Anything doubtful goes in "check".
"""


def vision_read(images: list[tuple[str, str]], kind: str = "document") -> str:
    """One model call over every photo of a document (kind: form, badge or document); returns the raw JSON text. images = [(local_path, content_type)]."""
    if not openai_enabled():
        return ""
    from openai import OpenAI

    content = [{"type": "input_text", "text": {"form": FORM_PROMPT, "badge": BADGE_PROMPT}.get(kind, DOCUMENT_PROMPT)}]
    content += [{"type": "input_image", "image_url": image_data_url(path, content_type)} for path, content_type in images]
    response = OpenAI().responses.create(model=os.getenv("OPENAI_VISION_MODEL", "gpt-5-mini"), input=[{"role": "user", "content": content}])
    return response.output_text


def vision_ocr_to_lead_text(local_path: str, content_type: str = "") -> str:
    if not openai_enabled():
        return ""
    from openai import OpenAI

    response = OpenAI().responses.create(
        model=os.getenv("OPENAI_VISION_MODEL", "gpt-5-mini"),
        input=[{"role": "user", "content": [{"type": "input_text", "text": LEAD_PROMPT}, {"type": "input_image", "image_url": image_data_url(local_path, content_type)}]}],
    )
    return json_to_lead_text(response.output_text)


def split_lead_texts(value: str) -> list[str]:
    if "---CONTACT---" in (value or ""):
        return [part.strip() for part in value.split("---CONTACT---") if part.strip()]
    contacts = parse_contacts(value)
    if not contacts:
        text = json_to_lead_text(value)
        return [text] if text else []
    return [text for text in (lead_dict_to_text(contact) for contact in contacts) if text]


def transcribe_audio(local_path: str) -> str:
    if not openai_enabled():
        return ""
    from openai import OpenAI

    client = OpenAI()
    prompt = os.getenv(
        "OPENAI_TRANSCRIBE_PROMPT",
        (
            "This is a WhatsApp voice note from an education event stall in India. "
            "Expect names of schools, NGOs, CSR teams, distributors, cities, budgets, "
            "number of schools, Xperimentor, Scikraft, SIA, TiQer, Play Lab, STREAM, "
            "Lab-in-Classroom, STEM, science kits, math kits, teacher training, proposal, and demo."
        ),
    )
    language = os.getenv("OPENAI_TRANSCRIBE_LANGUAGE", "en").strip()
    request = {
        "model": os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe"),
        "file": None,
        "prompt": prompt,
        "temperature": 0,
    }
    if language:
        request["language"] = language
    with open(local_path, "rb") as audio_file:
        request["file"] = audio_file
        transcript = client.audio.transcriptions.create(**request)
    return getattr(transcript, "text", "") or str(transcript)


def text_to_speech(text: str, output_path: str | Path) -> str:
    if not openai_enabled():
        return ""
    from openai import OpenAI

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    client = OpenAI()
    speech = client.audio.speech.create(
        model=os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
        voice=os.getenv("OPENAI_TTS_VOICE", "alloy"),
        input=text[:4096],
    )
    speech.write_to_file(output)
    return str(output)


def image_data_url(local_path: str, content_type: str = "") -> str:
    path = Path(local_path)
    mime = content_type or mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def json_to_lead_text(value: str) -> str:
    data = parse_json(value)
    if not data:
        return value.strip()
    if isinstance(data.get("contacts"), list):
        return "\n\n---CONTACT---\n\n".join(
            text for text in (lead_dict_to_text(item) for item in data["contacts"] if isinstance(item, dict)) if text
        )
    return lead_dict_to_text(data)


def lead_dict_to_text(data: dict) -> str:
    mapping = {
        "first_name": "First Name",
        "last_name": "Last Name",
        "name": "Name",
        "designation": "Designation",
        "org": "Org",
        "organisation_type": "Organisation Type",
        "role": "Role",
        "curriculum": "Curriculum",
        "medium_of_instruction": "Medium of Instruction",
        "city": "City",
        "state": "State",
        "district": "District",
        "pincode": "Pincode",
        "phone": "Phone",
        "email": "Email",
        "website": "Website",
        "google_maps_url": "Google Maps URL",
        "qr_url": "QR URL",
        "qr_page_text": "QR Page Text",
        "udise_code": "UDISE Code",
        "parent_organisation_id": "Parent Organisation ID",
        "lead_type": "Type",
        "interest": "Interest",
        "budget": "Budget",
        "timeline": "Timeline",
        "schools_count": "Schools",
        "notes": "Notes",
    }
    lines = []
    for key, label in mapping.items():
        item = str(data.get(key, "") or "").strip()
        if item:
            lines.append(f"{label}: {item}")
    return "\n".join(lines)


def parse_contacts(value: str) -> list[dict]:
    data = parse_json(value)
    if not data:
        return []
    contacts = data.get("contacts")
    if isinstance(contacts, list):
        return [item for item in contacts if isinstance(item, dict)]
    if any(data.get(key) for key in ("name", "first_name", "org", "email", "phone")):
        return [data]
    return []


def parse_json(value: str) -> dict:
    text = (value or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
