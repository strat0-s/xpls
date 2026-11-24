from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path


def openai_enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


LEAD_PROMPT = """
Extract lead details from this event stall image. It may be a visiting card, event ID badge, QR badge, handwritten note, brochure, or visitor form.
If multiple visiting cards or contacts are visible in one image, return JSON with a top-level "contacts" array, one object per card/contact.
If only one card/contact is visible, you may return a single object.
Photos are often sideways or upside down: turn the image upright in your head before reading, and read every card on it, however small or at whatever angle.
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
city is the town or city only, state is the Indian state written out in full (Karnataka, not KA), and district is the district only when it is printed. Never put the state or the district inside city, and never put a city in state.
"""

SINGLE_TEXT = """
The images are the sides or pages of ONE document: read them together and return a single object, not a "contacts" array.
"""
MULTI_TEXT = """
An image can show up to 2 separate documents side by side, and different images can show different documents. Return a top-level "contacts" array with ONE object per separate document.
A printed Xperimentor CONTACT FORM with a visiting card stapled or placed on it is ONE contact, never two: the person is on the card and in the form's own fields (role, curriculum, remarks, students, demo, workshop); merge them into one object, the form's filled fields winning. A name handwritten at the top right, just above the CONTACT FORM heading (written as "LO: Name", "Lead owner: Name" or just the name), is the team member who met the visitor: return just that name as "lead_owner" and never use it as the contact's name. The contact's name always comes from the visiting card or the form's own Name field. Leave lead_owner empty when nothing is written there.
Sides or pages of the same document (the same person) are ONE object: merge them. A side that shows only the organisation (logo, address, an office phone) and no person's name is the back of the other card, even when its phone differs: merge it and put the extra phone in notes. Add a key "photos" to each object: the list of image numbers (the first image is 1) that the document appears in.
"""
CHECK_TEXT = """
Add a key "check": a list of short phrases naming each field you could not read with confidence (for example "phone digits unclear"). List only values that ARE on the document but are hard to read or doubtful. Never list a field that is simply empty, not printed or unknown. QR codes are decoded separately by software: never read or flag a QR code.
"""
DOCUMENT_PROMPT = LEAD_PROMPT + SINGLE_TEXT + CHECK_TEXT  # one document: the base of the form and badge prompts
MULTI_PROMPT = LEAD_PROMPT + MULTI_TEXT + CHECK_TEXT  # any other document type: one or two separate documents per image

FORM_PROMPT = DOCUMENT_PROMPT + """
This is the printed Xperimentor contact form. Fields: Full Name, Organization, City, Phone, Email; Your Role (Management, Principal, Teacher/Coordinator, Other);
Curriculum (CBSE, ICSE, State, IB, IGCSE, Other); Interested in demo (At School, Online); Interested to attend teacher workshops (Yes, No); Number of Students; Remarks.
Use the extra keys: demo (whatever is ticked or written at "Interested in demo": Yes, No, At school or Online, followed by any date or time frame written next to it, exactly as written, for example "Yes, 2nd week of December" or "Online - December"), workshop ("Yes" or "No"), students (exactly as written), remarks (exactly as written), products (a list, see below).
Put a role or curriculum that was only written in (not ticked) in role/curriculum as written and add it to "check". A crossed-out printed value is not an answer.
Handwritten or doubtful values (student count, phone digits) go in "check".
Add a key "lead_owner": the name handwritten at the top right of the form, just above the CONTACT FORM heading (written as "LO: Name", "Lead owner: Name" or just the name). LO means Lead Owner. It is the team member who met the visitor, never the visitor: do not use it for name, which comes from the card or the form's Name field. Return just the name; leave it empty when nothing is written there.
The photos can also include the person's visiting card, stapled or sent with the form. It is the same person: the form wins, and the card fills anything the form leaves blank (designation, organisation, email, phone, website, address).
"""


BADGE_PROMPT = DOCUMENT_PROMPT + """
This is an event badge or ID card. Read the person's name, designation, organisation and city. Printed codes, stall or exhibitor numbers that are not a person's details go in notes.
If a URL or ID is printed next to the QR code, put it in qr_url (a URL) or qr_page_text (an ID). Anything doubtful goes in "check".
"""


def products_text(products: list[str]) -> str:
    return ("\nAdd a key \"products\": the list of products the person is interested in, whether ticked or written in the remarks. Use only these exact names: " + ", ".join(products)
            + ". A product written in a different spelling (for example Tiqer for TiQer) gets the listed name. Any product that is not on this list goes in \"other_products\" as written.\n") if products else ""


def vision_read(images: list[tuple[str, str]], kind: str = "document", products: list[str] = ()) -> str:
    """One model call over every photo of a document (kind: form, badge or document); returns the raw JSON text. images = [(local_path, content_type)]."""
    if not openai_enabled():
        return ""
    from openai import OpenAI

    content = [{"type": "input_text", "text": {"form": FORM_PROMPT + products_text(products), "badge": BADGE_PROMPT}.get(kind, MULTI_PROMPT)}]
    content += [{"type": "input_image", "image_url": image_data_url(path, content_type)} for path, content_type in images]
    # OPENAI_VISION_EFFORT (minimal, low, medium, high) trims the model's thinking time; unset keeps the model's default.
    effort = os.getenv("OPENAI_VISION_EFFORT", "").strip()
    extra = {"reasoning": {"effort": effort}} if effort else {}
    # A call that hangs must end before the platform kills the whole request: one retry, and no single try longer than OPENAI_TIMEOUT seconds.
    client = OpenAI(timeout=float(os.getenv("OPENAI_TIMEOUT", "40")), max_retries=1)
    response = client.responses.create(model=os.getenv("OPENAI_VISION_MODEL", "gpt-5-mini"), input=[{"role": "user", "content": content}], **extra)
    return response.output_text


READER_HINTS = {"form": "a printed filled-in form, possibly with a visiting card pinned or stapled to it", "badge": "an event badge or ID card, usually with a QR code",
                "document": "a visiting card, flyer, brochure or any other document"}


def vision_classify(image: tuple[str, str], options: list[dict]) -> str:
    """Which document type a photo is: the key of the one that fits best, "none" when none fits, "" when the reading service is off.
    options = document-type rows (key, label, hint, reader). Raises when the call fails."""
    if not openai_enabled():
        return ""
    from openai import OpenAI

    lines = "\n".join(f"{row['key']}: {row['label']} ({READER_HINTS.get(row['reader'], 'a document')}{'; ' + row['hint'] if row['hint'] else ''})" for row in options)
    text = f"What kind of document is in this photo? Answer with exactly one key from this list, or none if it fits none of them. Answer with the key only.\n{lines}"
    content = [{"type": "input_text", "text": text}, {"type": "input_image", "image_url": image_data_url(*image)}]
    client = OpenAI(timeout=float(os.getenv("OPENAI_CLASSIFY_TIMEOUT", "20")), max_retries=0)
    return client.responses.create(model=os.getenv("OPENAI_VISION_MODEL", "gpt-5-mini"), input=[{"role": "user", "content": content}], reasoning={"effort": "minimal"}).output_text.strip().lower()


REQUIREMENT_PROMPT = """A visitor at an education event typed ONE WhatsApp message giving their details and what they need. Return a single JSON object with the keys
name, email, org, organisation_type, city, state, interest. interest is what they are looking for, in their own words. organisation_type must be exactly one of
SCHOOL, SCHOOL_CHAIN, CSR, NGO, GOVERNMENT, DISTRIBUTOR, INSTITUTION, VENDOR, OTHER, or empty when unclear. Use empty strings for anything not written. Do not guess."""


def requirement_read(text: str) -> str:
    """The raw JSON the model returns for a visitor's one-message requirement; empty when the reading service is off. Raises when it fails."""
    if not openai_enabled():
        return ""
    from openai import OpenAI

    client = OpenAI(timeout=float(os.getenv("OPENAI_TIMEOUT", "40")), max_retries=0)
    # Pulling fields out of one short message needs no deep thinking, so the model is kept quick: the visitor is waiting for the next message.
    return client.responses.create(model=os.getenv("OPENAI_VISION_MODEL", "gpt-5-mini"), input=f"{REQUIREMENT_PROMPT}\n\nMessage:\n{text}", reasoning={"effort": "minimal"}).output_text


def classify_failure(exc: Exception) -> tuple[str, str]:
    """(kind, reason for admins) for an error from the reading service. kind is timeout, quota, key, outage or error; the team never sees the reason."""
    name, code, status = type(exc).__name__, str(getattr(exc, "code", "") or ""), getattr(exc, "status_code", None)
    if "Timeout" in name:
        return "timeout", "reading service did not answer in time"
    if code == "insufficient_quota" or "quota" in str(exc).lower():
        return "quota", "reading service quota used up"
    if status in (401, 403) or "Authentication" in name or "PermissionDenied" in name:
        return "key", "reading service key rejected"
    if status == 429:
        return "outage", "reading service is rate limiting us"
    if "Connection" in name or (isinstance(status, int) and status >= 500):
        return "outage", "reading service is not answering"
    return "error", f"unexpected error: {name}"


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


def image_data_url(local_path: str, content_type: str = "") -> str:
    path = Path(local_path)
    mime = content_type or mimetypes.guess_type(path.name)[0] or "image/jpeg"
    shrunk = shrunk_jpeg(local_path)
    if shrunk:
        mime = "image/jpeg"
    encoded = base64.b64encode(shrunk or path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def shrunk_jpeg(local_path: str, max_side: int = 2000) -> bytes | None:
    """A phone photo is 3-10 MB; the model reads at most about 2000 px anyway, so a smaller upload is the same read, sooner.
    Upright (EXIF turn applied) and re-encoded, only when the photo is bigger than max_side. None means send the original."""
    try:
        import cv2

        image = cv2.imread(local_path)
        if image is None or max(image.shape[:2]) <= max_side:
            return None
        scale = max_side / max(image.shape[:2])
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return buffer.tobytes() if ok else None
    except Exception:
        return None


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
