"""WhatsApp conversation: who gets which menu and what each step does.

Staff (an active person on the Team page, matched by phone) capture contact forms, other documents and notes.
Everyone else is a visitor. The step a person is on lives in the `sessions` table (one row per phone), not on a lead.
Choices go out as tappable buttons or lists; a tap arrives as plain text equal to its label, so typing the label works the same.
"""
from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from didac_app import auth, bird, blobstore, orbit
from didac_app.config import DidacConfig, brochure_catalog
from didac_app.dedupe import FILLABLE, fill_blanks, find_duplicate
from didac_app.media import append_qr_details, extract_media_text, save_media, suffix_for
from didac_app.models import Lead
from didac_app.openai_services import lead_dict_to_text, parse_contacts, vision_read
from didac_app.parser import FIELD_ALIASES, extract_schools_count, normalize_contact_name, normalize_key, parse_message, parse_structured_lead
from didac_app.storage import LeadStore

logger = logging.getLogger(__name__)

SESSION_MINUTES = 30
MAX_PHOTOS = 10
NOTE_WINDOW_MINUTES = 30

FORM, DOC, BADGE, NOTE = "Contact form", "Other document", "Event badge / QR", "Add a note"
# WhatsApp shows a list row's title only (never its description) and cuts it at 24 characters, so the hint lives in the title. The 3-button question
# ("Is this a contact form...") keeps the plain DOC label, since a button label may be 20 characters.
DOC_ROW = "Other docs (cards, etc.)"
SCANS = {FORM.lower(): "form", DOC.lower(): "document", DOC_ROW.lower(): "document", BADGE.lower(): "badge"}
BROCHURE, REQUIREMENT, FEEDBACK = "Download brochure", "Share my requirement", "Feedback / voice note"
SAVE, EDIT, RETAKE = "Save", "Edit", "Retake"
PRIORITIES = ("High", "Medium", "Low")
# The model sometimes lists every empty field as a doubt ("udise_code unknown"), and flags a QR code it cannot decode from pixels; the code decodes QRs itself.
NOT_ON_DOCUMENT = re.compile(r"\bqr\b|\b(unknown|not (printed|present|provided|given|available|mentioned|visible|found)|missing|n/?a)\b", re.I)
WHAT_IS_IT = "Is this a contact form, another document or an event badge?"
MENU_WORDS = {"menu", "hi", "hello", "hey", "start", "info", "brochure"}
# (label shown in the list, Orbit type code, one-line description)
TYPES = [
    ("School", "SCHOOL", "A single school"),
    ("School chain / group", "SCHOOL_CHAIN", "Several schools under one group"),
    ("NGO", "NGO", "NGO or foundation"),
    ("CSR / corporate", "CSR", "CSR team or company"),
    ("Government", "GOVERNMENT", "Government department or body"),
    ("Distributor / reseller", "DISTRIBUTOR", "Dealer or reseller"),
    ("College / institution", "INSTITUTION", "College, university, training centre"),
    ("Another company", "VENDOR", "Edtech, hardware or content company"),
    ("Other", "OTHER", "Individual, media, investor"),
]
# Lead fields shown in the scan summary: (field, label). Name, Organisation, Phone and Email always show, even blank.
SUMMARY = [("name", "Name"), ("org", "Organisation"), ("role", "Role"), ("phone", "Phone"), ("email", "Email"), ("organisation_type", "Type"), ("priority", "Priority"),
           ("city", "City"), ("state", "State"), ("curriculum", "Curriculum"), ("interest", "Interest"), ("notes", "Notes"),
           ("qr_url", "QR link"), ("qr_page_text", "QR text")]
ALWAYS = {"name", "org", "phone", "email"}


@dataclass
class Reply:
    text: str
    options: list[tuple[str, str]] = field(default_factory=list)  # (label, description); description is only shown in lists
    as_list: bool = False


def buttons(text: str, *labels: str) -> Reply:
    return Reply(text, [(label, "") for label in labels])


def staff_user(store: LeadStore, number: str) -> dict | None:
    user = store.find_user(auth.normalize_phone(number)) if auth.normalize_phone(number) else None
    return user if user and user["is_active"] else None


def handle(store: LeadStore, config: DidacConfig, number: str, body: str, media: list[tuple[str, str]]) -> tuple[list[Reply], list[int]]:
    chat = Chat(store, config, number)
    chat.run(body.strip(), media)
    return chat.replies, chat.lead_ids


# Seams the tests replace: both talk to Bird, the bucket and OpenAI.
def fetch_media(config: DidacConfig, url: str, content_type: str, number: str) -> tuple[str, str]:
    """Store the file in the bucket. Returns (bucket key, transcript); only voice notes are transcribed, photos are read later, all together."""
    key, temp_path = save_media(url, content_type, config, bird.media_headers(config), sender=number)
    try:
        return key, extract_media_text(temp_path, content_type) if content_type.startswith("audio/") else ""
    finally:
        Path(temp_path).unlink(missing_ok=True)


def read_documents(config: DidacConfig, photos: list[dict], kind: str) -> dict:
    """One read over every photo of a document (kind is form, document or badge). Returns {"fields": {lead field: value}, "check": [doubtful things]}; never raises."""
    paths, qr_text = [], ""
    try:
        for photo in photos:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix_for(photo["ct"], photo["key"])) as tmp:
                tmp.write(blobstore.download(config, photo["key"]))
            paths.append((tmp.name, photo["ct"]))
        raw = vision_read(paths, kind)
        qr_text = "\n".join(filter(None, (append_qr_details(path, "") for path, _ in paths)))
    except Exception:
        logger.exception("Reading the document failed")
        return {"fields": {}, "check": ["could not read the photos"]}
    finally:
        for path, _ in paths:
            Path(path).unlink(missing_ok=True)
    no_qr = ["QR code could not be scanned, retake with the QR flat and in focus"] if kind == "badge" and not qr_text else []
    contacts = parse_contacts(raw)
    if not contacts:
        return {"fields": {}, "check": ["could not read the photos", *no_qr]}
    contact = dict(contacts[0])
    check = [item for item in (str(item).strip() for item in contact.pop("check", None) or []) if item and not NOT_ON_DOCUMENT.search(item)] + no_qr
    if len(contacts) > 1:
        check.append(f"{len(contacts)} people found, only the first is kept")
    demo, workshop, students, remarks = (str(contact.pop(key, "") or "").strip() for key in ("demo", "workshop", "students", "remarks"))
    interest = [f"{label}: {value}" for label, value in (("Demo", demo), ("Teacher workshop", workshop)) if value]
    notes = [f"{label}: {value}" for label, value in (("Students", students), ("Remarks", remarks)) if value]
    lead = parse_structured_lead("New lead\n" + lead_dict_to_text(contact) + ("\n" + qr_text if qr_text else ""))
    lead.organisation_type = orbit.lead_org_type(lead)
    lead.interest = "; ".join(filter(None, [lead.interest, *interest]))
    lead.notes = "; ".join(filter(None, [lead.notes, *notes]))
    fields = {name: getattr(lead, name) for name in FILLABLE if getattr(lead, name)}
    if not fields.get("name") and not fields.get("phone"):
        check.append("no name or phone found")
    return {"fields": fields, "check": check}


class Chat:
    def __init__(self, store: LeadStore, config: DidacConfig, number: str):
        self.store, self.config, self.number = store, config, number
        self.user = staff_user(store, number)
        self.replies: list[Reply] = []
        self.lead_ids: list[int] = []

    # ---- routing -------------------------------------------------------------------------------------------------
    def run(self, text: str, media: list[tuple[str, str]]) -> None:
        low = text.lower().strip(" .!")
        session = self.store.get_session(self.number)
        expired = bool(session and session["age_minutes"] > SESSION_MINUTES)
        if expired:
            self.store.delete_session(self.number)
            session = None
        if low == "cancel":
            self.store.delete_session(self.number)
            self.say("Cancelled. Type menu to start again.")
        elif low in MENU_WORDS:
            self.menu(session)
        elif expired and not media:
            self.say("That scan timed out and was not saved. Type menu to start again.")
        elif session and session["flow"] in (*SCANS.values(), "note", "choose") and not self.user:
            self.menu(session)  # deactivated mid-scan: back to the visitor menu
        elif session and session["flow"] in ("note", "requirement", "brochure", "feedback") and low in self.menu_choices():
            self.store.delete_session(self.number)
            self.no_flow(text, media)
        elif session:
            self.in_flow(session, text, media)
        else:
            self.no_flow(text, media)

    def menu_choices(self) -> set[str]:
        return {label.lower() for label in ((FORM, DOC, DOC_ROW, BADGE, NOTE) if self.user else (BROCHURE, REQUIREMENT, FEEDBACK))}

    def say(self, text: str) -> None:
        self.replies.append(Reply(text))

    def menu(self, session: dict | None = None) -> None:
        lost = len((session or {}).get("data", {}).get("photos", []))
        if session:
            self.store.delete_session(self.number)
        if lost:
            self.say(f"Discarded {lost} photo(s) that were not saved.")
        if self.user:
            first = self.user["name"].split()[0]
            rows = [(FORM, "The printed Xperimentor form"), (DOC_ROW, "Visiting cards, brochures, flyers and all that"), (BADGE, "Badge or ID card with a QR code"), (NOTE, "Voice or text note on your last lead")]
            self.replies.append(Reply(f"Hi {first}. What would you like to do?\n\nType menu any time. Type cancel to stop.", rows, as_list=True))
        else:
            rows = [(BROCHURE, "SIA, TiQer, Play Lab and more"), (REQUIREMENT, "Tell us what you need"), (FEEDBACK, "Type, speak or send a photo")]
            self.replies.append(Reply("Welcome to Xperimentor at DIDAC.\nPlease choose an option. Type menu at any time to see this again.", rows, as_list=True))

    def no_flow(self, text: str, media: list[tuple[str, str]]) -> None:
        choice = text.lower()
        if self.user:
            if choice in SCANS:
                return self.start_scan(SCANS[choice])
            if choice == NOTE.lower():
                return self.start_note()
            if any(ct.startswith("image/") for _, ct in media):
                self.store.put_session(self.number, "choose", "kind", {"photos": []}, keep_existing=True)
                if self.take_photos(media, text) == 1:  # photos of one batch arrive as parallel messages: only the first asks
                    self.replies.append(buttons(WHAT_IS_IT, FORM, DOC, BADGE))
                return
            if media:  # a voice note with no menu choice goes to the last lead, like Add a note
                data = {"lead_id": self.recent_lead_id()}
                self.store.put_session(self.number, "note", "collect", data)
                return self.note_step(data, text, media)
            kind, lead = parse_message(text, self.number)
            if lead and kind == "lead":
                lead.captured_by = lead.captured_by or self.user["name"]
                self.store.create_lead(lead)
                self.store.add_feedback(self.number, text, lead.id, feedback_type="text_message")
                self.lead_ids.append(lead.id)
                return self.say(f"Lead saved: {lead.org or lead.name or 'Unnamed lead'}")
            return self.menu()
        if choice == BROCHURE.lower():
            self.store.put_session(self.number, "brochure", "pick", {})
            return self.replies.append(Reply("Which brochure would you like?", [(item["name"], "Open the brochure") for item in brochure_catalog(self.config).values()], as_list=True))
        if choice == REQUIREMENT.lower():
            self.store.put_session(self.number, "requirement", "name", {})
            return self.say("Happy to help. What is your name?")
        if choice == FEEDBACK.lower():
            self.store.put_session(self.number, "feedback", "collect", {})
            return self.say("Please type your feedback, or send a voice note or photo now. We will save it for the team. Type menu when you are done.")
        if media:
            self.collect_feedback(text, media)
            return self.say("Saved for the team. Type menu for options.")
        self.menu()

    def in_flow(self, session: dict, text: str, media: list[tuple[str, str]]) -> None:
        flow, step, data = session["flow"], session["step"], session["data"]
        if flow == "choose":
            choice = text.lower()
            if choice in SCANS:
                flow = SCANS[choice]
                self.store.put_session(self.number, flow, "photos", data)
                return self.say(f"Got {len(data['photos'])} photo(s). Send more or reply DONE.")
            if any(ct.startswith("image/") for _, ct in media):
                self.take_photos(media, text)
                return
            return self.replies.append(buttons(WHAT_IS_IT, FORM, DOC, BADGE))
        if flow in SCANS.values():
            return self.scan_step(flow, step, data, text, media)
        if flow == "note":
            return self.note_step(data, text, media)
        if flow == "requirement":
            return self.requirement_step(step, data, text)
        if flow == "brochure":
            return self.brochure_step(text)
        if flow == "feedback":
            self.collect_feedback(text, media)
            return self.say("Thank you. We saved that for the team. Send more, or type menu.")
        self.menu(session)

    # ---- staff: contact form / other document --------------------------------------------------------------------
    def start_scan(self, flow: str) -> None:
        self.store.put_session(self.number, flow, "photos", {"photos": []})
        if flow == "form":
            self.say("Send a photo of the filled contact form. Reply DONE when all pages are sent.")
        elif flow == "badge":
            self.say("Send a photo of the event badge or ID card with the QR code in view, flat and in focus. Reply DONE when all photos are sent.")
        else:
            self.say("Send photos of the document, front and back if it has two sides. Reply DONE when all photos are sent.")

    def take_photos(self, media: list[tuple[str, str]], caption: str = "") -> int | None:
        """Store each image in the bucket and append it to the session. Returns the photo count after the last one, None if nothing was added."""
        count = None
        for url, content_type in media:
            if not content_type.startswith("image/"):
                continue
            key, _ = fetch_media(self.config, url, content_type, self.number)
            added = self.store.add_session_photo(self.number, {"key": key, "url": url, "ct": content_type, "caption": caption}, MAX_PHOTOS)
            if added is None:
                self.say(f"That is the limit of {MAX_PHOTOS} photos for one document. Reply DONE.")
                break
            count = added
        return count

    def scan_step(self, flow: str, step: str, data: dict, text: str, media: list[tuple[str, str]]) -> None:
        low = text.lower().strip(" .!")
        if step == "photos":
            if media:
                if any(not ct.startswith("image/") for _, ct in media):
                    return self.say("Please send photos only. Reply DONE when all photos are sent.")
                count = self.take_photos(media, text)
                if count:
                    self.say(f"Photo {count} received. Send the next one or reply DONE.")
                return
            if low != "done":
                return self.say("Send a photo, or reply DONE when all photos are sent. Type cancel to stop.")
            if not data.get("photos"):
                return self.say("No photos yet. Send a photo first.")
            result = read_documents(self.config, data["photos"], flow)
            data |= {"draft": result["fields"], "check": result["check"], "original": summary(result["fields"], [])}
            self.store.put_session(self.number, flow, "confirm", data)
            return self.confirm(data)
        if step == "edit":  # the Save / Edit / Retake taps from the summary stay tappable, and "back" ends the edit
            if low in ("back", "done", "stop"):
                self.store.put_session(self.number, flow, "confirm", data)
                return self.confirm(data)
            if low in (SAVE.lower(), EDIT.lower(), RETAKE.lower()):
                step = "confirm"
        if step == "priority":  # a lead is only saved once staff have tagged it
            pick = next((name for name in PRIORITIES if name.lower() == low), "")
            if not pick:
                return self.replies.append(buttons("Please tap High, Medium or Low.", *PRIORITIES))
            data["draft"]["priority"] = pick
            return self.save_scan(flow, data)
        if step == "confirm":
            choice = low
            if choice == SAVE.lower():
                if data["draft"].get("priority"):
                    return self.save_scan(flow, data)
                self.store.put_session(self.number, flow, "priority", data)
                return self.replies.append(buttons("How important is this lead?", *PRIORITIES))
            if choice == EDIT.lower():
                self.store.put_session(self.number, flow, "edit", data)
                return self.say("Reply with corrections, one per line.\nExample:\nPhone: 98450 11112\nPriority: High\nType BACK to stop editing.")
            if choice == RETAKE.lower():
                self.store.put_session(self.number, flow, "photos", {"photos": []})
                return self.say("Photos cleared. Send the new photos, then reply DONE.")
            return self.replies.append(buttons("Please choose Save, Edit or Retake.", SAVE, EDIT, RETAKE))
        if step == "edit":
            applied, unknown = apply_corrections(data["draft"], text)
            if not applied:
                return self.say("I could not read a correction. Use one per line, like:\nPhone: 98450 11112\nType BACK to stop editing.")
            data["check"] = [item for item in data["check"] if not any(word in item.lower() for word in applied)]
            self.store.put_session(self.number, flow, "confirm", data)
            return self.confirm(data, f"I could not use: {', '.join(unknown)}\n\n" if unknown else "")

    def confirm(self, data: dict, prefix: str = "") -> None:
        """The Save / Edit / Retake step, with nothing shortened. A button message holds about 1000 characters; a longer summary goes out as one plain
        message (WhatsApp allows 4096) with the buttons right after it."""
        text = prefix + summary(data["draft"], data["check"], with_check=True)
        if len(text) <= 1000:
            return self.replies.append(buttons(text, SAVE, EDIT, RETAKE))
        self.say(text[:4000])
        self.replies.append(buttons("Save this lead, edit it, or retake the photos?", SAVE, EDIT, RETAKE))

    def save_scan(self, flow: str, data: dict) -> None:
        lead = Lead(from_number=self.number, staff_number=self.number, captured_by=self.user["name"], doc_type=flow if flow in ("form", "badge") else "other",
                    raw_message=data["original"], review_reason="; ".join(data["check"]))
        for name, value in data["draft"].items():
            setattr(lead, name, value)
        normalize_contact_name(lead)
        photos = data["photos"]
        lead.media_urls = "; ".join(photo["url"] for photo in photos)
        lead.media_local_paths = "; ".join(photo["key"] for photo in photos)
        saved, outcome = self.store_lead(lead, photos, data["original"])
        label = saved.org or saved.name or saved.phone or "Unnamed lead"
        self.store.put_session(self.number, flow, "photos", {"photos": []})
        if outcome == "created":
            badge = " It shows a Check badge on the dashboard." if saved.review_reason else ""
            self.say(f"Saved as #{saved.id}, {label}{f' ({saved.priority} priority)' if saved.priority else ''}.{badge}\nSend the next {flow}, or type menu.")
        else:
            added = "I added the new details to it." if outcome == "merged" else "There was nothing new to add."
            self.say(f"Already saved as #{saved.id}, {saved.name or label}. {added}\nSend the next {flow}, or type menu.")

    def store_lead(self, lead: Lead, photos: list[dict], original_text: str = "") -> tuple[Lead, str]:
        """Create the lead, or merge into the matching one. Outcome is 'created', 'merged' (new details added) or 'unchanged'.
        An email or phone match fills blanks only; a name+organisation match is saved as its own lead with a Check badge.
        ponytail: find_duplicate then create_lead is not atomic, so two simultaneous scans of one new person can both create a lead; take an advisory lock on the normalised email/phone if that shows up."""
        match = find_duplicate(self.store, lead)
        if match and match[1]:
            existing = match[0]
            filled = fill_blanks(existing, lead)
            append = {name: getattr(lead, name) for name in ("tags", "captured_by", "capture_context") if getattr(lead, name)}
            if filled:
                append |= {"media_urls": lead.media_urls, "media_local_paths": lead.media_local_paths}
            if filled or append:
                self.store.merge_into_lead(existing.id, {name: getattr(existing, name) for name in filled}, append)
            if filled:
                self.attach_photos(existing.id, photos, original_text)
            self.lead_ids.append(existing.id)
            return existing, "merged" if filled else "unchanged"
        if match:
            lead.review_reason = "; ".join(filter(None, [lead.review_reason, f"possible duplicate of #{match[0].id}"]))
        lead = self.store.create_lead(lead)
        self.attach_photos(lead.id, photos, original_text)
        self.lead_ids.append(lead.id)
        return lead, "created"

    def attach_photos(self, lead_id: int, photos: list[dict], original_text: str) -> None:
        for index, photo in enumerate(photos):
            media_id = self.store.add_media(lead_id, photo["url"], photo["ct"], photo["key"], original_text if index == 0 else "")
            if photo.get("caption"):
                self.store.add_feedback(self.number, photo["caption"], lead_id, feedback_type="text_caption", media_id=media_id)

    # ---- staff: notes ----------------------------------------------------------------------------------------------
    def recent_lead_id(self) -> int | None:
        lead = self.store.latest_lead_for_number(self.number)
        age = minutes_since(lead.updated_at) if lead else None
        return lead.id if lead and age is not None and age <= NOTE_WINDOW_MINUTES else None

    def start_note(self) -> None:
        lead = self.store.latest_lead_for_number(self.number)
        age = minutes_since(lead.updated_at) if lead else None
        if lead and age is not None and age <= NOTE_WINDOW_MINUTES:
            self.store.put_session(self.number, "note", "collect", {"lead_id": lead.id})
            return self.say(f"Send a voice note or text. It will be added to #{lead.id}, {lead.org or lead.name or 'Unnamed lead'} (saved {int(age)} min ago).")
        self.store.put_session(self.number, "note", "collect", {"lead_id": None})
        self.say(f"You have no lead from the last {NOTE_WINDOW_MINUTES} minutes. Send a note with a name or phone number and I will save it as a new lead.")

    def note_step(self, data: dict, text: str, media: list[tuple[str, str]]) -> None:
        if not text and not media:
            return self.say("Send a voice note or text, or type menu.")
        stored = [(url, ct, *fetch_media(self.config, url, ct, self.number)) for url, ct in media]
        transcripts = [t for *_, t in stored if t and "PENDING" not in t]
        note = "\n".join(filter(None, [text, *transcripts]))
        lead = self.store.get_lead(data["lead_id"]) if data.get("lead_id") else None
        if not lead:
            lead = parse_structured_lead("New lead\n" + note)
            if not (lead.name or lead.phone or lead.email):
                return self.say("I need a name or a phone number to start a new lead. Send one, or type menu.")
            lead.from_number = lead.staff_number = self.number
            lead.captured_by = self.user["name"]
            lead.review_reason = "note only"
            lead.text_notes = text
            lead.audio_transcript = "; ".join(transcripts)
            lead = self.store.create_lead(lead)
            self.store.put_session(self.number, "note", "collect", {"lead_id": lead.id})
        else:
            self.store.merge_into_lead(lead.id, {}, {name: value for name, value in (("text_notes", text), ("audio_transcript", "; ".join(transcripts))) if value})
        for url, content_type, key, transcript in stored:
            self.store.add_media(lead.id, url, content_type, key, transcript)
        if text:
            self.store.add_feedback(self.number, text, lead.id, feedback_type="text_message")
        self.lead_ids.append(lead.id)
        self.say(f"Added to #{lead.id}. Send another, or type menu.")

    # ---- visitors ----------------------------------------------------------------------------------------------------
    def visitor_lead(self, interest: str = "") -> Lead:
        lead = self.store.latest_lead_for_number(self.number) or self.store.create_lead(Lead(from_number=self.number, phone=self.number.replace("whatsapp:", "")))
        if interest and interest.lower() not in lead.interest.lower():
            self.store.update_lead_fields(lead.id, interest="; ".join(filter(None, [lead.interest, interest])))
        self.lead_ids.append(lead.id)
        return lead

    def brochure_step(self, text: str) -> None:
        choice = text.lower()
        selected = next((item for item in brochure_catalog(self.config).values() if item["name"].lower() == choice or item["key"] == choice), None)
        if not selected:
            return self.replies.append(Reply("Please pick a brochure from the list.", [(item["name"], "Open the brochure") for item in brochure_catalog(self.config).values()], as_list=True))
        self.visitor_lead(interest=f"Downloaded brochure: {selected['name']}")
        self.store.delete_session(self.number)
        if selected["url"]:
            return self.say(f"Here is the {selected['name']} brochure:\n{selected['url']}\n\nType menu to share your requirement or see other options.")
        self.say(f"The {selected['name']} brochure link is not set up yet. Our team will send it to you. Type menu for other options.")

    def requirement_step(self, step: str, data: dict, text: str) -> None:
        if not text:
            return self.say("Please type your answer, or type menu to stop.")
        if step == "name":
            self.store.put_session(self.number, "requirement", "org", data | {"name": text})
            return self.say("Which organisation are you with?")
        if step == "org":
            self.store.put_session(self.number, "requirement", "type", data | {"org": text})
            return self.replies.append(Reply("What best describes it?", [(label, description) for label, _, description in TYPES], as_list=True))
        if step == "type":
            code = next((code for label, code, _ in TYPES if label.lower() == text.lower()), "") or orbit.normalize_org_type(text)
            if not code:
                return self.replies.append(Reply("Please pick one from the list.", [(label, description) for label, _, description in TYPES], as_list=True))
            self.store.put_session(self.number, "requirement", "place", data | {"type": code})
            return self.say("Which city and state?")
        if step == "place":
            self.store.put_session(self.number, "requirement", "need", data | {"place": text})
            return self.say("What are you looking for? Example: science lab for 20 schools, teacher training, CSR pilot.")
        city, _, state = data["place"].partition(",")
        lead = Lead(from_number=self.number, phone=self.number.replace("whatsapp:", ""), name=data["name"], org=data["org"], organisation_type=data["type"],
                    city=city.strip(), state=state.strip(), interest=text, schools_count=extract_schools_count(text))
        normalize_contact_name(lead)
        saved, outcome = self.store_lead(lead, [])
        if outcome != "created":
            self.store.merge_into_lead(saved.id, {}, {"interest": text})
        self.store.add_feedback(self.number, text, saved.id, feedback_type="text_message")
        self.store.delete_session(self.number)
        self.say(f"Thanks {data['name'].split()[0]}. We saved your details. Our team will be in touch.\nType menu for brochures.")

    def collect_feedback(self, text: str, media: list[tuple[str, str]]) -> None:
        lead = self.visitor_lead()
        for url, content_type in media:
            key, transcript = fetch_media(self.config, url, content_type, self.number)
            transcript = "" if "PENDING" in transcript else transcript
            media_id = self.store.add_media(lead.id, url, content_type, key, transcript)
            if transcript:
                self.store.merge_into_lead(lead.id, {}, {"audio_transcript": transcript})
                self.store.add_feedback(self.number, transcript, lead.id, feedback_type="audio_transcript", media_id=media_id)
        if text:
            self.store.merge_into_lead(lead.id, {}, {"feedback_notes": text})
            self.store.add_feedback(self.number, text, lead.id, feedback_type="text", sentiment=feedback_sentiment(text), action_required=feedback_action(text))


# ---- helpers ------------------------------------------------------------------------------------------------------------
def summary(fields: dict, check: list[str], with_check: bool = False) -> str:
    lines = []
    for name, label in SUMMARY:
        value = fields.get(name, "")
        if name == "organisation_type" and value:
            value = orbit.ORG_TYPES.get(value, value)
        if value or name in ALWAYS or (name == "priority" and with_check):  # the saved "original reading" leaves the empty priority out
            lines.append(f"{label}: {value or ('(not set, asked on Save)' if name == 'priority' else '(blank)')}")
    text = "\n".join(lines)
    if not with_check:
        return text
    return f"I read this as:\n{text}" + (f"\n\nCheck: {', '.join(check)}" if check else "")


def apply_corrections(draft: dict, text: str) -> tuple[list[str], list[str]]:
    """Apply `Field: value` lines onto the draft. Returns (field words changed, lines that were not understood)."""
    applied, unknown = [], []
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key, value = normalize_key(key), value.strip()
        if key in ("type", "organisation type", "organization type"):
            name, value = "organisation_type", orbit.normalize_org_type(value)
        elif key == "priority":
            name, value = "priority", next((p for p in PRIORITIES if p.lower() == value.lower()), "")
        else:
            name = FIELD_ALIASES.get(key)
        if name in FILLABLE and value:
            if name == "name":
                draft.pop("first_name", None)
                draft.pop("last_name", None)
            draft[name] = value
            applied += [key, name.replace("_", " ")]
        elif line.strip():
            unknown.append(line.strip())
    return applied, unknown


def minutes_since(stamp: str) -> float | None:
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return (datetime.now(UTC) - (then if then.tzinfo else then.replace(tzinfo=UTC))).total_seconds() / 60


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
