"""WhatsApp conversation: who gets which menu and what each step does.

Staff (an active person on the Team page, matched by phone) capture contact forms, other documents and notes.
Everyone else is a visitor. The step a person is on lives in the `sessions` table (one row per phone), not on a lead.
Choices go out as tappable buttons or lists; a tap arrives as plain text equal to its label, so typing the label works the same.
"""
from __future__ import annotations

import logging
import re
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from datetime import UTC, date, datetime
from pathlib import Path

from didac_app import auth, bird, blobstore, followup, orbit
from didac_app.config import DidacConfig
from didac_app.dedupe import FILLABLE, fill_blanks, find_duplicate
from didac_app.media import append_qr_details, extract_media_text, save_media, suffix_for
from didac_app.models import NOT_SCANNED, Lead
from didac_app.openai_services import classify_failure, lead_dict_to_text, parse_contacts, parse_json, requirement_read, vision_classify, vision_read
from didac_app.places import tidy
from didac_app.products import match_products
from didac_app.parser import FIELD_ALIASES, extract_schools_count, normalize_contact_name, normalize_key, parse_message, parse_structured_lead
from didac_app.storage import LeadStore

logger = logging.getLogger(__name__)
timing = logging.getLogger("didac.timing")  # same logger the webhook uses, so these lines show up in the Vercel logs too

SESSION_MINUTES = 24 * 60  # matches the WhatsApp 24-hour reply window
MAX_PHOTOS = 10
MAX_PER_PHOTO = 2  # documents laid side by side in one photo; photos sent one by one are not limited by this
NOTE_WINDOW_MINUTES = 30  # how recent a lead must be for a voice note sent with no menu choice to go onto it
NOTE_LIST_CHARS = 3600  # the lead list for a note must fit in one WhatsApp message (4096) with its heading
NEW_LEAD = "New lead"

NOTE = "Add a note"
EVENT_PHOTOS, TEAM_BROCHURES = "Send event photos", "Brochures"
# The document types in the menu are not fixed: they come from the live event (see Chat.doc_types).
BROCHURE, REQUIREMENT, DEMO, FEEDBACK = "Download brochure", "Share my requirement", "Schedule a demo", "Feedback / voice note"
ONLINE, OFFLINE = "Online", "Offline"
DATE_ASK = "Please type the date as DD/MM/YYYY. Example: 05/12/2026"
AUTO_CONTEXT = "Sent without a menu choice"  # capture_context of a lead saved straight from photos nobody introduced
PAIR_MINUTES = 10  # a form and its visiting card, sent one after the other, are one lead if they arrive this close together
FORM_ONLY = ("curriculum", "students", "demo_interest", "workshop")  # answers that are on the printed form and never on a card
NOT_SCANS = {"note", "choose", "requirement", "demo", "brochure", "feedback", "eventphotos"}  # session flows that are not a document scan
NO_EVENT_TEAM = "Hi {first}. No event is live right now, so I cannot save leads or photos. Please ask an admin to start the next event."
NO_PHOTOS = "This channel does not accept photos. Type menu to see what I can do for you."
NO_EVENT_VISITOR = "Thanks for getting in touch. The event has finished. Our team will reach out soon."
DETAILS_ASK = "Happy to help. Please send your name, organisation, city and state in one message.\n\nExample: Meera Nair, Greenfield School, Pune Maharashtra"
NEED_ASK = "What are you looking for? Example: science lab for 20 schools, teacher training, CSR pilot."
NOT_LISTED = "Not on the team list"  # lead_owner when the name written on the form is nobody on the Team page
SAVE, EDIT, RETAKE, CANCEL = "Save", "Edit", "Retake", "Cancel"
TRY_AGAIN, SAVE_LATER, TYPE_DETAILS = "Try again", "Save to read later", "Type the details"
UNCLEAR = "I could not make out this document. Try again in better light with it flat, or type the details instead."
MANUAL_HELP = "Send the details one per line, like:\nName: Meera Nair\nOrganisation: Greenfield School\nPhone: 98450 11112"
PRIORITIES = ("High", "Medium", "Low")
# The model sometimes lists every empty field as a doubt ("udise_code unknown"), and flags a QR code it cannot decode from pixels; the code decodes QRs itself.
NOT_ON_DOCUMENT = re.compile(r"\bqr\b|\b(unknown|not (printed|present|provided|given|available|mentioned|visible|found)|missing|n/?a)\b", re.I)
WHAT_IS_IT = "What kind of document is this?"
DEFAULT_WELCOME = (
    "Welcome to Xperimentor! 👋\nThank you for connecting with us at {event}.\n"
    "Explore our learning solutions, product catalogues and brochures below. Find the resources you're looking for and discover how we can transform learning in your school.\n\n"
    "📚 Explore our products\n🔬 Science in Action — Grades 6–10\n🧮 MathMax — Mathematics Learning Solutions\n🎨 PlayLab — Experiential Learning for Grades 1–5\n\n"
    "Want to see how these work in your school?\nType menu at any time to see this again."
)
NOT_AVAILABLE = "That option is not available any more. Here is what is available now:"
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
SUMMARY = [("name", "Name"), ("org", "Organisation"), ("role", "Role"), ("phone", "Phone"), ("email", "Email"), ("organisation_type", "Type"), ("priority", "Priority"), ("lead_owner", "Lead owner"),
           ("city", "City"), ("state", "State"), ("curriculum", "Curriculum"), ("products", "Products"), ("demo_interest", "Demo"), ("workshop", "Teacher workshop"), ("students", "Students"), ("interest", "Interest"), ("notes", "Notes"),
           ("qr_url", "QR link"), ("qr_page_text", "QR text")]
ALWAYS = {"name", "org", "phone", "email", "lead_owner"}


@dataclass
class Reply:
    text: str
    options: list[tuple[str, str]] = field(default_factory=list)  # (label, description); description is only shown in lists
    as_list: bool = False
    document: tuple[str, str] | None = None  # (public url, file name): the text goes with it as the caption


def buttons(text: str, *labels: str) -> Reply:
    return Reply(text, [(label, "") for label in labels])


def save_choices(text: str) -> Reply:
    """Save / Edit / Retake / Cancel. WhatsApp allows 3 reply buttons, so four choices go out as a tap-list. Cancel is handled for every step by Chat.run."""
    rows = [(SAVE, "Save it as a lead"), (EDIT, "Correct something first"), (RETAKE, "Clear the photos and start again"), (CANCEL, "Throw this scan away")]
    return Reply(text, rows, as_list=True)


def team_owner(store: LeadStore, written: str) -> str:
    """The team member a handwritten first name or full name points to, or NOT_LISTED. Close spellings count ("Magesh" is Magesh Kumar AP).
    ponytail: the closest first name wins, so two teammates with the same first name pick the first one."""
    word = re.sub(r"[^a-z ]", "", written.lower()).split()
    if not word:
        return ""
    def score(user: dict) -> float:
        parts = re.sub(r"[^a-z ]", "", user["name"].lower()).split()
        return max(SequenceMatcher(None, word[0], parts[0]).ratio(), SequenceMatcher(None, " ".join(word), " ".join(parts)).ratio()) if parts else 0
    best = max((user for user in store.list_users() if user["is_active"]), key=score, default=None)
    return best["name"] if best and score(best) >= 0.8 else NOT_LISTED


def tag_owner(store: LeadStore, result: dict) -> None:
    """Turn the lead owner the reader copied off the form into a team member (or NOT_LISTED, with the written name in the Check line)."""
    written = result.get("fields", {}).get("lead_owner", "")
    if written:
        owner = result["fields"]["lead_owner"] = team_owner(store, written)
        if owner == NOT_LISTED:
            result["check"] = [*result["check"], f"lead owner written as {written}, not on the team list"]


def staff_user(store: LeadStore, number: str) -> dict | None:
    user = store.find_user(auth.normalize_phone(number)) if auth.normalize_phone(number) else None
    return user if user and user["is_active"] else None


def handle(store: LeadStore, config: DidacConfig, number: str, body: str, media: list[tuple[str, str]]) -> tuple[list[Reply], list[int]]:
    chat = Chat(store, config, number)
    chat.run(body.strip(), media)
    followup.auto_send(store, config)  # whatever the message did (a scan, a note, a requirement), a lead with a new number gets the thank-you
    return chat.replies, chat.lead_ids


# Seams the tests replace: both talk to Bird, the bucket and OpenAI.
def fetch_media(config: DidacConfig, url: str, content_type: str, number: str) -> tuple[str, str]:
    """Store the file in the bucket. Returns (bucket key, transcript); only voice notes are transcribed, photos are read later, all together."""
    key, temp_path = save_media(url, content_type, config, bird.media_headers(config), sender=number)
    try:
        return key, extract_media_text(temp_path, content_type) if content_type.startswith("audio/") else ""
    finally:
        Path(temp_path).unlink(missing_ok=True)


def read_documents(config: DidacConfig, photos: list[dict], kind: str, products: list[str] = ()) -> dict:
    """One read over every photo of a scan (kind is form, document or badge). Returns {"fields": {lead field: value}, "check": [doubtful things]} for the first document,
    "photos" (which photo numbers it is on), "more" (the same for any further documents) and "too_many" (photo number, count) when a photo holds more documents than allowed; never raises."""
    paths, qr_text = [], ""
    started = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(photos))) as pool:  # all photos download at once, not one after another
            blobs = list(pool.map(lambda photo: blobstore.download(config, photo["key"]), photos))
        for photo, blob in zip(photos, blobs):
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix_for(photo["ct"], photo["key"])) as tmp:
                tmp.write(blob)
            paths.append((tmp.name, photo["ct"]))
        downloaded = time.perf_counter()
        # The model call is mostly waiting on OpenAI and the QR decode is CPU work, so they run side by side instead of one after the other.
        with ThreadPoolExecutor(max_workers=1) as pool:
            reading = pool.submit(lambda: (vision_read(paths, kind, products), time.perf_counter()))
            qr_text = "\n".join(filter(None, (append_qr_details(path, "") for path, _ in paths)))
            qr_done = time.perf_counter()
            raw, vision_done = reading.result()
        timing.info("read photos=%d download_ms=%d vision_ms=%d qr_ms=%d total_ms=%d", len(photos), (downloaded - started) * 1000,
                    (vision_done - downloaded) * 1000, (qr_done - downloaded) * 1000, (time.perf_counter() - started) * 1000)
    except Exception as exc:
        logger.exception("Reading the document failed")
        kind_of_failure, reason = classify_failure(exc)
        return {"fields": {}, "check": ["could not read the photos"], "failed": kind_of_failure, "reason": reason}
    finally:
        for path, _ in paths:
            Path(path).unlink(missing_ok=True)
    no_qr = ["QR code could not be scanned, retake with the QR flat and in focus"] if kind == "badge" and not qr_text else []
    contacts = parse_contacts(raw)
    ms = int((time.perf_counter() - started) * 1000)
    if not contacts:  # the reader answered but found nothing: a photo problem, not an outage
        return {"fields": {}, "check": ["could not read the photos", *no_qr], "unclear": True, "ms": ms}
    # Only a visiting-card style document may hold several documents; a form or a badge is one person (a badge's QR belongs to one).
    docs = [lead_fields(contact, qr_text if index == 0 else "", no_qr if index == 0 else [], products) for index, contact in enumerate(contacts if kind == "document" else contacts[:1])]
    if kind != "document" and len(contacts) > 1:
        docs[0]["check"].append(f"{len(contacts)} people found, only the first is kept")
    if kind == "document":
        docs = merge_org_backs(docs)
    result = {**docs[0], "more": docs[1:], "ms": ms}
    per_photo = Counter(number for doc in docs for number in doc["photos"])
    if per_photo and max(per_photo.values()) > MAX_PER_PHOTO:
        number, count = per_photo.most_common(1)[0]
        result["too_many"] = (number, count)
    return result


def classify_document(config: DidacConfig, photo: dict, types: list[dict]) -> str:
    """Which of the event's document types this photo is: its key, "none" when none fits, "" when it could not be told (never raises)."""
    path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix_for(photo["ct"], photo["key"])) as tmp:
            tmp.write(blobstore.download(config, photo["key"]))
        path = tmp.name
        answer = vision_classify((path, photo["ct"]), types)
        return answer if answer in {row["key"] for row in types} | {"none"} else ""
    except Exception:
        logger.exception("Telling what kind of document this is failed")
        return ""
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


def merge_org_backs(docs: list[dict]) -> list[dict]:
    """The back of a visiting card often shows only the organisation and an office phone, and the reader can return it as a second document.
    A document with no person's name (blank, or just the organisation) on other photos than a named document of the same organisation is folded into it:
    blanks are filled, and a different phone is kept in the notes."""
    def org(doc: dict) -> str:
        return re.sub(r"[^a-z0-9 ]", "", doc["fields"].get("org", "").lower()).strip()
    def named(doc: dict) -> bool:
        name = doc["fields"].get("name", "").strip().lower()
        return bool(name) and name != doc["fields"].get("org", "").strip().lower()
    kept: list[dict] = []
    for doc in docs:
        front = next((other for other in kept if named(other) and not named(doc) and org(doc) and org(other)
                      and (org(doc) in org(other) or org(other) in org(doc)) and not set(doc["photos"]) & set(other["photos"])), None)
        if not front:
            kept.append(doc)
            continue
        fields, extra = front["fields"], doc["fields"]
        for name, value in extra.items():
            if name not in ("name", "phone") and not fields.get(name):
                fields[name] = value
        if extra.get("phone") and extra["phone"] != fields.get("phone"):
            if fields.get("phone"):
                fields["notes"] = "; ".join(filter(None, [fields.get("notes", ""), f"Other phone: {extra['phone']}"]))
            else:
                fields["phone"] = extra["phone"]
        front["photos"] = sorted({*front["photos"], *doc["photos"]})
        front["check"] += [item for item in doc["check"] if item not in front["check"]]
    return kept


def read_requirement(text: str) -> dict:
    """{name, email, org, organisation_type, city, state, interest} from the visitor's one message. The reading service understands plain sentences; labelled lines ("Name: ...")
    and a typed email work without it. Never raises: when nothing can be told apart the whole message is kept as what they need."""
    found: dict = {}
    try:
        found = parse_json(requirement_read(text))
    except Exception:
        logger.exception("Reading the visitor's details failed")
    if not found.get("name"):  # labelled lines, which the reading service may be off for
        lead = parse_structured_lead("New lead\n" + text)
        found = {**{key: getattr(lead, key) for key in ("name", "org", "city", "state", "interest")}, **{key: value for key, value in found.items() if value}}
    email = auth.normalize_email(found.get("email", "")) or next((auth.normalize_email(word.strip(".,;:()<>")) for word in text.split() if "@" in word), "")
    kind = found.get("organisation_type", "")
    found = {key: str(found.get(key) or "").strip() for key in ("name", "org", "city", "state", "interest")} | {"email": email, "organisation_type": kind if kind in orbit.ORG_TYPES else ""}
    found["city"], found["state"], _ = tidy(found.get("city", ""), found.get("state", ""))
    found = {key: value for key, value in found.items() if value}
    return found if found.get("name") or found.get("interest") else {**found, "interest": text.strip()}


def one_phone(value: str) -> tuple[str, list[str]]:
    """A card with two numbers: the first mobile becomes the lead's phone (a follow-up needs exactly one), the rest are returned for the notes."""
    parts = [part.strip() for part in re.split(r"[,;/\n]|\s&\s|\s(?:and|or)\s", value or "") if part.strip()]
    if len(parts) < 2:
        return value, []
    valid = [part for part in parts if (phone := auth.normalize_phone(part))]
    keep = next((part for part in valid if followup.is_mobile(auth.normalize_phone(part))), valid[0] if valid else parts[0])
    return keep, [part for part in parts if part != keep]


def lead_fields(contact: dict, qr_text: str = "", no_qr: list[str] | None = None, products: list[str] = ()) -> dict:
    """One document the reader found, as {"fields": lead fields, "check": doubts, "photos": the photo numbers it appears on}."""
    contact = dict(contact)
    contact["phone"], spare = one_phone(str(contact.get("phone") or ""))
    photos = [number for number in (contact.pop("photos", None) or []) if isinstance(number, int)]
    check = [item for item in (str(item).strip() for item in contact.pop("check", None) or []) if item and not NOT_ON_DOCUMENT.search(item)] + (no_qr or [])
    owner = re.sub(r"^\s*(?:lead\s*owner\s*[:\-]*|l\.?\s*o\.?\s*[:\-]+)\s*", "", str(contact.pop("lead_owner", "") or ""), flags=re.I).strip()  # "Lead owner: Magesh" as written on the form
    demo, workshop, students, remarks = (str(contact.pop(key, "") or "").strip() for key in ("demo", "workshop", "students", "remarks"))
    contact["city"], contact["state"], contact["district"] = tidy(*(str(contact.get(key) or "") for key in ("city", "state", "district")))  # before the text form, where a line break would split the value
    liked, other = match_products(contact.pop("products", None), list(products))
    other += match_products(contact.pop("other_products", None), [])[1]
    liked += [name for name in products if name not in liked and re.search(rf"\b{re.escape(name)}\b", remarks, re.I)]  # a product named in the remarks that the reader did not list
    notes = [f"Remarks: {remarks}"] if remarks else []
    if spare:
        notes.append("Other phone: " + ", ".join(spare))
    if other:
        notes.append("Other products: " + ", ".join(other))
    lead = parse_structured_lead("New lead\n" + lead_dict_to_text(contact) + ("\n" + qr_text if qr_text else ""))
    lead.organisation_type = orbit.lead_org_type(lead)
    lead.notes = "; ".join(filter(None, [lead.notes, *notes]))
    lead.products, lead.demo_interest, lead.workshop, lead.students = "; ".join(liked), demo, workshop, students
    fields = {name: getattr(lead, name) for name in FILLABLE if getattr(lead, name)}
    if owner:
        fields["lead_owner"] = owner  # as written: Chat.read_scan matches it to the team
        if fields.get("name", "").strip().lower() == owner.lower():  # the reader took the staff name for the visitor: better blank than wrong
            del fields["name"]
            check.append("the visitor's name was not read from the card, please add it")
    if not fields.get("name") and not fields.get("phone"):
        check.append("no name or phone found")
    return {"fields": fields, "check": check, "photos": photos}


class Chat:
    def __init__(self, store: LeadStore, config: DidacConfig, number: str):
        self.store, self.config, self.number = store, config, number
        self.user = staff_user(store, number)
        self.replies: list[Reply] = []
        self.lead_ids: list[int] = []
        # The live event decides which document types and brochures exist right now. One query each, per message.
        self.event = store.live_event()
        self.doc_types = {row["key"]: row for row in store.list_doc_types(self.event["id"], active_only=True)} if self.event else {}
        self.by_label = {row["label"].lower(): row["key"] for row in self.doc_types.values()}

    # ---- routing -------------------------------------------------------------------------------------------------
    def run(self, text: str, media: list[tuple[str, str]]) -> None:
        low = text.lower().strip(" .!")
        session = self.store.get_session(self.number)
        expired = bool(session and session["age_minutes"] > SESSION_MINUTES)
        if expired:
            self.store.delete_session(self.number)
            session = None
        if not self.event:  # between events nothing is saved, to anyone
            self.store.delete_session(self.number)
            return self.say(NO_EVENT_TEAM.format(first=self.user["name"].split()[0]) if self.user else NO_EVENT_VISITOR)
        if low == "cancel":
            self.store.delete_session(self.number)
            self.say("Cancelled. Type menu to start again.")
        elif low in MENU_WORDS:
            self.menu(session)
        elif expired and not media:
            self.say("That scan timed out and was not saved. Type menu to start again.")
        elif session and session["flow"] not in NOT_SCANS and session["flow"] not in self.doc_types:
            self.say(NOT_AVAILABLE)  # the admin switched that document type off, or the event changed, while this chat was open
            self.menu(session)
        elif session and session["flow"] in (*self.doc_types, "note", "choose", "eventphotos") and not self.user:
            self.menu(session)  # deactivated mid-scan: back to the visitor menu
        elif session and session["flow"] in ("note", "requirement", "demo", "brochure", "feedback", "eventphotos") and low in self.menu_choices():
            self.store.delete_session(self.number)
            self.no_flow(text, media)
        elif session:
            self.in_flow(session, text, media)
        else:
            self.no_flow(text, media)

    def menu_choices(self) -> set[str]:
        if self.user:
            return {*self.by_label, NOTE.lower(), EVENT_PHOTOS.lower(), TEAM_BROCHURES.lower()}
        return {label.lower() for label in (BROCHURE, REQUIREMENT, DEMO, FEEDBACK)}

    def brochures(self, audience: str = "visitors") -> list[dict]:
        """The active brochures of the live event that this audience ('visitors' or 'team') may see, as {key, name, url, for_visitors}."""
        return [{"key": row["key"], "name": row["name"], "url": row["url"], "file_key": row["file_key"], "file_name": row["file_name"], "for_visitors": row["for_visitors"]} for row in self.store.list_brochures(self.event["id"], audience)]

    def say(self, text: str) -> None:
        self.replies.append(Reply(text))

    def say_now(self, text: str) -> None:
        """Send right away, before the slow work that follows. A failure is ignored: the normal replies still go out afterwards."""
        try:
            bird.send_message(self.config, self.number, text)
            self.store.log_outbound(self.number, text, convo=self.store.convo_for(self.number))
        except Exception:
            logger.exception("Could not send the progress message")

    def menu(self, session: dict | None = None) -> None:
        lost = len((session or {}).get("data", {}).get("photos", []))
        if session:
            self.store.delete_session(self.number)
        if lost:
            self.say(f"Discarded {lost} photo(s) that were not saved.")
        if self.user:
            first = self.user["name"].split()[0]
            rows = [(row["label"], row["hint"]) for row in self.doc_types.values()][:7] + [(EVENT_PHOTOS, "Photos of the event, saved but not read")]  # a WhatsApp list holds 10 rows
            if self.brochures("team"):
                rows.append((TEAM_BROCHURES, "Brochures and price lists"))
            rows.append((NOTE, "Voice or text note on a lead"))
            self.replies.append(Reply(f"Hi {first}. You are at {self.event['name']}. What would you like to do?\n\nType menu any time. Type cancel to stop.", rows, as_list=True))
        else:
            brochures = self.brochures()
            rows = ([(BROCHURE, ", ".join(item["name"] for item in brochures[:3]) + (" and more" if len(brochures) > 3 else ""))] if brochures else []) \
                + [(REQUIREMENT, "Tell us what you need"), (DEMO, "Online or in person"), (FEEDBACK, "Type, speak or send a photo")]
            welcome = (self.event["welcome_text"] or DEFAULT_WELCOME.format(event=self.event["name"])).replace("[event]", self.event["name"])
            self.replies.append(Reply(welcome, rows, as_list=True))

    def ask_kind(self) -> Reply:
        """What kind of document is this? Up to three types fit on buttons, more go in a list."""
        options = [(row["label"], row["hint"]) for row in self.doc_types.values()][:10]
        return Reply(WHAT_IS_IT, options if len(options) > 3 else [(label, "") for label, _ in options], as_list=len(options) > 3)

    def no_flow(self, text: str, media: list[tuple[str, str]]) -> None:
        choice = text.lower()
        if self.user:
            if choice in self.by_label:
                return self.start_scan(self.by_label[choice])
            if choice == NOTE.lower():
                return self.start_note()
            if choice == EVENT_PHOTOS.lower():
                self.store.put_session(self.number, "eventphotos", "photos", {})
                return self.say(f"Send as many event photos as you like. They are saved to {self.event['name']} and are not read. Type menu when you are done.")
            if choice == TEAM_BROCHURES.lower():
                return self.pick_brochure("team")
            if any(ct.startswith("image/") for _, ct in media):
                if not self.doc_types:
                    return self.say("No document types are switched on for this event. Please ask an admin.")
                if choice.startswith("visitor meet"):  # the team's photo with a visitor, captioned as the guidelines say: kept with the event photos, never read
                    return self.event_photos_step(media, text)
                return self.auto_scan(media, text)
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
            return self.pick_brochure("visitors")
        if choice == REQUIREMENT.lower():
            return self.start_requirement()
        if choice == DEMO.lower():
            self.store.put_session(self.number, "demo", "mode", {})
            return self.replies.append(buttons("Would you like the demo online or offline?", ONLINE, OFFLINE))
        if choice == FEEDBACK.lower():
            self.store.put_session(self.number, "feedback", "collect", {})
            return self.say("Please type your feedback, or send a voice note or photo now. We will save it for the team. Type menu when you are done.")
        if any(ct.startswith("image/") for _, ct in media):
            return self.say(NO_PHOTOS)
        if media:
            self.collect_feedback(text, media)
            return self.say("Saved for the team. Type menu for options.")
        self.menu()

    def pick_brochure(self, audience: str) -> None:
        items = self.brochures(audience)
        if not items:
            return self.say("Our team will send you the brochures. Type menu for other options." if audience == "visitors" else "There are no brochures yet. Please ask an admin to add one.")
        self.store.put_session(self.number, "brochure", "pick", {})
        self.replies.append(Reply("Which brochure would you like?", [(item["name"], "Open the brochure") for item in items], as_list=True))

    def in_flow(self, session: dict, text: str, media: list[tuple[str, str]]) -> None:
        flow, step, data = session["flow"], session["step"], session["data"]
        if flow == "choose":
            choice = text.lower()
            if choice in self.by_label:
                flow = self.by_label[choice]
                self.store.put_session(self.number, flow, "photos", data)
                return self.say(f"Got {len(data['photos'])} photo(s). Send more or reply DONE.")
            if any(ct.startswith("image/") for _, ct in media):
                self.take_photos(media, text)
                return
            return self.replies.append(self.ask_kind())
        if flow in self.doc_types:
            return self.scan_step(flow, step, data, text, media)
        if flow == "note":
            return self.pick_note_lead(data, text) if step == "pick" else self.note_step(data, text, media)
        if flow == "eventphotos":
            return self.event_photos_step(media)
        if flow == "requirement":
            return self.requirement_step(step, data, text)
        if flow == "demo":
            return self.demo_step(step, data, text)
        if flow == "brochure":
            return self.brochure_step(text)
        if flow == "feedback":
            self.collect_feedback(text, media)
            return self.say("Thank you. We saved that for the team. Send more, or type menu.")
        self.menu(session)

    # ---- staff: contact form / other document --------------------------------------------------------------------
    def start_scan(self, flow: str) -> None:
        self.store.put_session(self.number, flow, "photos", {"photos": []})
        label = item_noun(self.doc_types[flow])
        reader = self.doc_types[flow]["reader"]
        if reader == "form":
            self.say(f"Send a photo of the filled {label}. Reply DONE when all pages are sent.")
        elif reader == "badge":
            self.say(f"Send a photo of the {label} with the QR code in view, flat and in focus. Reply DONE when all photos are sent.")
        else:
            self.say(f"Send photos of the {label}, front and back if it has two sides. Reply DONE when all photos are sent.")

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
            return self.read_scan(flow, data)
        if step == "failed":
            return self.failed_step(flow, data, low)
        if step == "unclear":
            return self.unclear_step(flow, data, low)
        if step == "manual":
            return self.manual_step(flow, data, text)
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
            for doc in scan_docs(data):
                doc["draft"].setdefault("priority", pick)
            return self.ask_photo(flow, data)
        if step == "photo":  # the last question before saving
            if low in ("yes", "y"):
                self.store.put_session(self.number, flow, "visitorphoto", data)
                return self.say("Send the photo of you with the visitor. Type SKIP to save without it.")
            if low in ("no", "n", "skip"):
                return self.save_scan(flow, data)
            return self.replies.append(buttons("Please tap Yes or No.", "Yes", "No"))
        if step == "visitorphoto":
            image = next(((url, ct) for url, ct in media if ct.startswith("image/")), None)
            if image:
                key, _ = fetch_media(self.config, image[0], image[1], self.number)
                data["visitor_photo"] = {"key": key, "url": image[0], "ct": image[1]}
                return self.save_scan(flow, data)
            if low == "skip":
                return self.save_scan(flow, data)
            return self.say("Send the photo, or type SKIP to save without it.")
        if step == "confirm":
            choice = low
            if choice == SAVE.lower():
                docs = scan_docs(data)
                if all(doc["draft"].get("priority") for doc in docs):
                    return self.ask_photo(flow, data)
                self.store.put_session(self.number, flow, "priority", data)
                return self.replies.append(buttons("How important is this lead?" if len(docs) == 1 else "How important are these leads?", *PRIORITIES))
            if choice == EDIT.lower():
                self.store.put_session(self.number, flow, "edit", data)
                if len(scan_docs(data)) > 1:
                    return self.say("Reply with corrections, one per line. Start a line with 1 or 2 to say which document.\nExample:\n2 Phone: 98450 11112\n1 Lead owner: Magesh\nType BACK to stop editing.")
                return self.say("Reply with corrections, one per line.\nExample:\nPhone: 98450 11112\nLead owner: Magesh\nType BACK to stop editing.")
            if choice == RETAKE.lower():
                self.store.put_session(self.number, flow, "photos", {"photos": []})
                return self.say("Photos cleared. Send the new photos, then reply DONE.")
            return self.replies.append(save_choices("Please choose Save, Edit, Retake or Cancel."))
        if step == "edit":
            applied, unknown = apply_to_documents(data, text)
            for doc in scan_docs(data):
                if doc["draft"].get("lead_owner") and "lead owner" in applied:  # a typed name is matched to the team too
                    doc["draft"]["lead_owner"] = team_owner(self.store, doc["draft"]["lead_owner"])
            if not applied:
                return self.say("I could not read a correction. Use one per line, like:\nPhone: 98450 11112\nType BACK to stop editing.")
            self.store.put_session(self.number, flow, "confirm", data)
            return self.confirm(data, f"I could not use: {', '.join(unknown)}\n\n" if unknown else "")

    def ask_photo(self, flow: str, data: dict) -> None:
        self.store.put_session(self.number, flow, "photo", data)
        self.replies.append(buttons("Add a photo with the visitor?", "Yes", "No"))

    def read_scan(self, flow: str, data: dict, retry: bool = False) -> None:
        """Read the photos of a scan and move to the summary, or to the calm failure step when the reading service is not working."""
        slow = " This is taking a little longer than usual, so hang tight. Your photos are safe." if self.store.reader_status()["state"] == "slow" else ""
        self.say_now(f"Reading your {len(data['photos'])} photo(s)... this takes a few seconds.{slow}")
        result = read_documents(self.config, data["photos"], self.doc_types[flow]["reader"], [row["label"] for row in self.store.list_products()])
        if result.get("failed"):
            self.store.record_read_failure(result["reason"])
            return self.read_failed(flow, data, result["failed"], retry)
        self.store.record_read_ok(result.get("ms", 0))
        tag_owner(self.store, result)
        if result.get("unclear"):
            self.store.put_session(self.number, flow, "unclear", data)
            return self.replies.append(buttons(UNCLEAR, RETAKE, TYPE_DETAILS))
        if result.get("too_many"):
            number, count = result["too_many"]
            self.store.put_session(self.number, flow, "photos", {"photos": []})
            return self.say(f"I can see {count} documents in photo {number}. Two at most per photo, please retake with 2 or fewer. Photos cleared, send them again.")
        data |= scan_draft(result)
        self.store.put_session(self.number, flow, "confirm", data)
        self.confirm(data)

    def read_failed(self, flow: str, data: dict, kind: str, retry: bool = False) -> None:
        """Nothing is lost and nothing technical is said: the photos are already saved, and the team picks what to do next."""
        paused = self.store.reader_status()["state"] == "down"
        count = len(data["photos"])
        if retry and paused:
            text = "Still not working. Your photos are safe. You can try once more in a bit, save them to read later, or type the details."
        elif paused or kind != "timeout":
            text = f"I cannot read {'these' if count > 1 else 'this'} just now, but your {count} photo(s) {'are' if count > 1 else 'is'} saved safely to {self.event['name']}. Nothing is lost.\n\nWhat would you like to do?"
        else:
            text = "That one took longer than I like, and I could not finish it. Your photo is safe."
        self.store.put_session(self.number, flow, "failed", data)
        self.replies.append(buttons(text, TRY_AGAIN, SAVE_LATER, TYPE_DETAILS))

    def failed_step(self, flow: str, data: dict, low: str) -> None:
        if low == TRY_AGAIN.lower():
            return self.read_scan(flow, data, retry=True)
        if low == SAVE_LATER.lower():
            return self.save_unscanned(flow, data)
        if low == TYPE_DETAILS.lower():
            return self.start_manual(flow, data)
        self.replies.append(buttons("Please tap Try again, Save to read later or Type the details.", TRY_AGAIN, SAVE_LATER, TYPE_DETAILS))

    def unclear_step(self, flow: str, data: dict, low: str) -> None:
        if low == RETAKE.lower():
            self.store.put_session(self.number, flow, "photos", {"photos": []})
            return self.say("Photos cleared. Send the new photos, then reply DONE.")
        if low == TYPE_DETAILS.lower():
            return self.start_manual(flow, data)
        self.replies.append(buttons("Please tap Retake or Type the details.", RETAKE, TYPE_DETAILS))

    def start_manual(self, flow: str, data: dict) -> None:
        data |= {"draft": {}, "check": [], "original": "", "more": [], "photo_ids": []}
        self.store.put_session(self.number, flow, "manual", data)
        self.say("No problem. " + MANUAL_HELP)

    def manual_step(self, flow: str, data: dict, text: str) -> None:
        applied, unknown = apply_corrections(data["draft"], text)
        if not applied:
            return self.say("I could not read that. " + MANUAL_HELP)
        data["original"] = summary(data["draft"], [])
        self.store.put_session(self.number, flow, "confirm", data)
        self.confirm(data, f"I could not use: {', '.join(unknown)}\n\n" if unknown else "")

    def save_unscanned(self, flow: str, data: dict) -> None:
        """Keep the lead with its photos and nothing read. It shows as Not scanned on the dashboard, where it can be read again or filled in."""
        photos = data["photos"]
        lead = self.store.create_lead(Lead(from_number=self.number, staff_number=self.number, captured_by=self.user["name"], doc_type=flow, review_reason=NOT_SCANNED,
                                          media_urls="; ".join(photo["url"] for photo in photos), media_local_paths="; ".join(photo["key"] for photo in photos)))
        self.attach_photos(lead.id, photos, "")
        self.lead_ids.append(lead.id)
        self.store.put_session(self.number, flow, "photos", {"photos": []})
        self.store.close_convo(self.number)
        status = self.store.reader_status()
        tail = f"\n\nThere is an error reading documents right now. Please tell someone to check. Reference {status['ref']}." if status["state"] == "down" else ""
        self.say(f"Saved as lead #{lead.event_no} with {len(photos)} photo(s). Find it under Leads, marked \"Not scanned\". Open it to read it again later, or fill in the details yourself.{tail}")

    def confirm(self, data: dict, prefix: str = "") -> None:
        """The Save / Edit / Retake step, with nothing shortened. A button message holds about 1000 characters; a longer summary goes out as one plain
        message (WhatsApp allows 4096) with the buttons right after it."""
        docs = scan_docs(data)
        if len(docs) == 1:
            text = prefix + summary(data["draft"], data["check"], with_check=True) + self.duplicate_note(data)
        else:
            parts = [f"{number}.\n" + summary(doc["draft"], doc["check"], with_check=True).removeprefix("I read this as:\n") + self.duplicate_note(doc) for number, doc in enumerate(docs, 1)]
            text = prefix + f"I read {len(docs)} documents:\n\n" + "\n\n".join(parts)
        if len(text) <= 1000:
            return self.replies.append(save_choices(text))
        self.say(text[:4000])
        self.replies.append(save_choices("Save this lead, edit it, retake the photos, or cancel?" if len(docs) == 1 else "Save these leads, edit them, retake the photos, or cancel?"))

    def doc_lead(self, doc: dict, flow: str = "", photos: list[dict] | None = None) -> Lead:
        """The lead for one document; `photos` are the ones it is on, whose links are kept on the lead."""
        lead = Lead(from_number=self.number, staff_number=self.number, captured_by=self.user["name"], doc_type=flow,
                    raw_message=doc["original"], review_reason="; ".join(doc["check"]))
        for name, value in doc["draft"].items():
            setattr(lead, name, value)
        normalize_contact_name(lead)
        if photos is not None:
            lead.media_urls = "; ".join(photo["url"] for photo in photos)
            lead.media_local_paths = "; ".join(photo["key"] for photo in photos)
        return lead

    def duplicate_note(self, doc: dict) -> str:
        """Said right under the summary, so the person knows before tapping Save. Mirrors what store_lead will do."""
        match = find_duplicate(self.store, self.doc_lead(doc))
        if not match:
            return ""
        existing, certain = match
        who = f"#{existing.event_no}, {existing.name or existing.org or existing.phone or 'unnamed'}"
        return f"\n\nAlready saved as {who}. Saving adds any new details to it." if certain else f"\n\nLooks like {who}, which is already saved. Saving keeps this as its own lead marked Check."

    def save_scan(self, flow: str, data: dict) -> None:
        docs, outcomes = scan_docs(data), []
        for doc in docs:
            photos = photos_of(doc, data["photos"])
            saved, outcome = self.store_lead(self.doc_lead(doc, flow, photos), photos, doc["original"])
            if data.get("visitor_photo"):
                with_visitor = data["visitor_photo"]
                self.store.add_media(saved.id, with_visitor["url"], with_visitor["ct"], with_visitor["key"], purpose="visitor")
            outcomes.append((saved, outcome))
        self.store.put_session(self.number, flow, "photos", {"photos": []})
        self.store.close_convo(self.number)
        self.say(saved_text(outcomes) + f"\nSend the next {item_noun(self.doc_types[flow])}, or type menu.")

    def auto_scan(self, media: list[tuple[str, str]], caption: str = "") -> None:
        """Photos sent with no menu choice are read and saved with no questions, so a venue with no internet can just queue them to the number.
        Nothing is lost when reading fails: the lead is kept with its photos as Not scanned."""
        flow = next((row["key"] for row in self.doc_types.values() if row["reader"] == "form"), next(iter(self.doc_types), ""))  # when the kind cannot be told: the contact form
        photos = []
        for url, content_type in media:
            if content_type.startswith("image/"):
                key, _ = fetch_media(self.config, url, content_type, self.number)
                photos.append({"key": key, "url": url, "ct": content_type, "caption": caption})
        if len(self.doc_types) > 1:  # the first photo says what kind of document it is: a badge, a form with its card, or something else
            kind = classify_document(self.config, photos[0], list(self.doc_types.values()))
            flow = kind if kind in self.doc_types else "other" if kind == "none" and "other" in self.doc_types else flow
        result = read_documents(self.config, photos, self.doc_types[flow]["reader"], [row["label"] for row in self.store.list_products()])
        if result.get("failed"):
            self.store.record_read_failure(result["reason"])
        else:
            self.store.record_read_ok(result.get("ms", 0))
        if result.get("failed") or result.get("unclear"):
            lead = self.store.create_lead(Lead(from_number=self.number, staff_number=self.number, captured_by=self.user["name"], doc_type=flow, review_reason=NOT_SCANNED, capture_context=AUTO_CONTEXT,
                                              media_urls="; ".join(photo["url"] for photo in photos), media_local_paths="; ".join(photo["key"] for photo in photos)))
            self.attach_photos(lead.id, photos, "")
            self.lead_ids.append(lead.id)
            return self.say(f"Saved as #{lead.event_no}, but I could not read the photo. Open it under Leads, marked \"Not scanned\", to read it again or fill it in.")
        tag_owner(self.store, result)
        outcomes = []
        for doc in scan_docs(scan_draft(result)):
            doc_photos = photos_of(doc, photos)
            lead = self.doc_lead(doc, flow, doc_photos)
            lead.capture_context = AUTO_CONTEXT
            saved, outcome = self.store_lead(lead, doc_photos, doc["original"])
            outcomes.append(self.pair_up(saved) if outcome == "created" else (saved, outcome))
        self.store.close_convo(self.number)
        self.say(saved_text(outcomes) + "\nPriority is not set yet.")

    def recent_autos(self, skip_id: int = 0) -> list[Lead]:
        """The leads this person's photos made in the last few minutes (photos sent one after another belong together).
        ponytail: scans every live lead, like find_duplicate."""
        return [item for item in self.store.list_leads() if item.id != skip_id and item.event_id == self.event["id"] and item.staff_number == self.number
                and item.capture_context == AUTO_CONTEXT and (minutes_since(item.created_at) or 0) <= PAIR_MINUTES]

    def pair_up(self, lead: Lead) -> tuple[Lead, str]:
        """Join the lead to the one made just before it when they are the same visitor: a form with no contact details and a card with no form answers
        (either order), or the back of a card (nothing on it that names a person) right after its front. Several possible partners, as when a burst of
        photos arrives at once, are not guessed at: the lead is marked Check instead. A wrong guess is fixed by hand on the lead page."""
        def has_contact(item: Lead) -> bool:
            return bool(item.name or item.phone or item.email)
        def has_form(item: Lead) -> bool:
            return any(getattr(item, name) for name in FORM_ONLY)
        others = self.recent_autos(lead.id)
        partners = [other for other in others if (has_contact(other) and not has_form(other) and has_form(lead) and not has_contact(lead)) or (has_form(other) and not has_contact(other) and has_contact(lead) and not has_form(lead))]
        if not partners and not has_contact(lead) and not has_form(lead):  # a back side
            partners = [max(earlier, key=lambda item: item.id)] if (earlier := [other for other in others if other.id < lead.id]) else []
        if len(partners) > 1:
            self.store.update_lead_fields(lead.id, review_reason="; ".join(filter(None, [lead.review_reason, "may go with " + " or ".join(f"#{other.event_no}" for other in partners)])))
            return self.store.get_lead(lead.id), "created"
        if not partners:
            return lead, "created"
        keep, drop = sorted((partners[0], lead), key=lambda item: item.id)
        filled = fill_blanks(keep, drop)
        self.store.merge_into_lead(keep.id, {name: getattr(keep, name) for name in filled},
                                   {name: getattr(drop, name) for name in ("media_urls", "media_local_paths", "review_reason") if getattr(drop, name)})
        self.store.absorb_lead(keep.id, drop.id)
        self.lead_ids = [number for number in self.lead_ids if number != drop.id] + [keep.id]
        return self.store.get_lead(keep.id), "paired"

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
            lead.review_reason = "; ".join(filter(None, [lead.review_reason, f"possible duplicate of #{match[0].event_no}"]))
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
        """The team picks which lead the note is for from one plain message listing the live event's leads, newest first. They reply with the lead's number (or NEW)."""
        leads = self.store.list_leads("event_id = %s", (self.event["id"],))
        if not leads:
            return self.ask_new_note_lead()
        lines, size = [], 0
        for lead in leads:
            line = f"#{lead.event_no} " + " · ".join(filter(None, [lead.name, lead.org]))[:60] if lead.name or lead.org else f"#{lead.event_no} Unnamed lead"
            if size + len(line) > NOTE_LIST_CHARS:  # WhatsApp holds 4096 characters; any lead's number can still be typed
                lines.append(f"...and {len(leads) - len(lines)} older. Type any lead number.")
                break
            lines.append(line)
            size += len(line) + 1
        self.store.put_session(self.number, "note", "pick", {})
        self.say("Which lead is this note for? Reply with its number, or NEW for a new lead.\n\n" + "\n".join(lines))

    def ask_new_note_lead(self) -> None:
        self.store.put_session(self.number, "note", "collect", {"lead_id": None})
        self.say("Send a note with a name or phone number and I will save it as a new lead.")

    def pick_note_lead(self, data: dict, text: str) -> None:
        low = text.lower().strip()
        if low in ("new", NEW_LEAD.lower()):
            return self.ask_new_note_lead()
        found = re.match(r"#?(\d{1,6})\b", low)
        lead = next(iter(self.store.list_leads("event_id = %s AND event_no = %s", (self.event["id"], int(found[1])))), None) if found else None
        if not lead:
            return self.say("I could not find that lead. Reply with a number from the list, NEW for a new lead, or type menu.")
        self.store.put_session(self.number, "note", "collect", {"lead_id": lead.id})
        self.say(f"Send a voice note or text. It will be added to #{lead.event_no}, {lead.org or lead.name or 'Unnamed lead'}.")

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
        self.say(f"Added to #{lead.event_no}. Send another, or type menu.")

    # ---- visitors ----------------------------------------------------------------------------------------------------
    def known_lead(self) -> Lead | None:
        return lead_for_visitor(self.store, self.number)

    def visitor_lead(self, interest: str = "", demo: str = "") -> Lead:
        lead = self.known_lead() or self.store.create_lead(Lead(from_number=self.number, phone=self.number.replace("whatsapp:", "")))
        if interest and interest.lower() not in lead.interest.lower():
            self.store.update_lead_fields(lead.id, interest="; ".join(filter(None, [lead.interest, interest])))
        if demo:  # the latest request wins: one line, replaced
            self.store.update_lead_fields(lead.id, demo_interest=demo)
        self.lead_ids.append(lead.id)
        return lead

    def event_photos_step(self, media: list[tuple[str, str]], caption: str = "") -> None:
        images = [(url, content_type) for url, content_type in media if content_type.startswith("image/")]
        if not images:
            return self.say("Please send photos only. Type menu when you are done." if media else "Send photos, or type menu when you are done.")
        for url, content_type in images:
            key, _ = fetch_media(self.config, url, content_type, self.number)
            self.store.add_event_photo(self.event["id"], self.number, self.user["name"], key, content_type, url, caption.strip())
        self.say(f"{len(images)} photo{'' if len(images) == 1 else 's'} saved to {self.event['name']}.")

    def brochure_step(self, text: str) -> None:
        choice = text.lower()
        audience = "team" if self.user else "visitors"
        selected = next((item for item in self.brochures(audience) if item["name"].lower() == choice or item["key"] == choice), None)
        if not selected:
            return self.replies.append(Reply("Please pick a brochure from the list.", [(item["name"], "Open the brochure") for item in self.brochures(audience)], as_list=True))
        if not self.user:  # a team member looking something up is not a lead
            self.visitor_lead(interest=f"Downloaded brochure: {selected['name']}")
        self.store.delete_session(self.number)
        team_only = "\n\nThis one is for the team only." if self.user and not selected["for_visitors"] else ""
        more = "Type menu for more." if self.user else "Type menu to share your requirement or see other options."
        if selected["file_key"]:
            try:
                link = blobstore.signed_url(self.config, selected["file_key"], 3600)
            except Exception:
                logger.exception("Could not sign the brochure file")
                return self.say(f"We could not fetch the {selected['name']} brochure just now. Our team will send it to you. Type menu for other options.")
            return self.replies.append(Reply(f"Here is the {selected['name']} brochure.{team_only}\n\n{more}", document=(link, selected["file_name"] or f"{selected['name']}.pdf")))
        if selected["url"]:
            return self.say(f"Here is the {selected['name']} brochure:\n{selected['url']}{team_only}\n\n{more}")
        self.say(f"The {selected['name']} brochure link is not set up yet. Our team will send it to you. Type menu for other options.")

    def start_requirement(self) -> None:
        """A visitor we already hold (they replied to the follow-up) is only asked what they need. Anyone else sends name, organisation, city and state in one message, then says what they need."""
        known = self.known_lead()
        if known and known.name:
            self.store.put_session(self.number, "requirement", "need", {"name": known.name})
            return self.say(f"Thanks {known.name.split()[0]}. {NEED_ASK}")
        self.store.put_session(self.number, "requirement", "details", {})
        self.say(DETAILS_ASK)

    def requirement_step(self, step: str, data: dict, text: str) -> None:
        if not text:
            return self.say("Please type your answer, or type menu to stop.")
        if step == "details":
            data = data | read_requirement(text)
        else:
            data = data | {"name" if step == "name" else "interest": text}
        for missing, ask in (("name", "What is your name?"), ("interest", NEED_ASK)):  # only these two are needed; the rest is kept when it was given
            if not data.get(missing):
                self.store.put_session(self.number, "requirement", "name" if missing == "name" else "need", data)
                return self.say(ask)
        lead = Lead(from_number=self.number, phone=self.number.replace("whatsapp:", ""), name=data["name"], email=data.get("email", ""), org=data.get("org", ""), organisation_type=data.get("organisation_type", ""),
                    city=data.get("city", ""), state=data.get("state", ""), interest=data["interest"], schools_count=extract_schools_count(data["interest"]))
        normalize_contact_name(lead)
        if saved := self.known_lead():  # a follow-up reply goes on the lead we scanned, even if they spell their name differently
            filled = [name for name in fill_blanks(saved, lead) if name != "interest"]  # the need is added below, never both filled and appended
            self.store.merge_into_lead(saved.id, {name: getattr(saved, name) for name in filled}, {"interest": data["interest"]})
            self.lead_ids.append(saved.id)
        else:
            saved, outcome = self.store_lead(lead, [])
            if outcome != "created":
                self.store.merge_into_lead(saved.id, {}, {"interest": data["interest"]})
        self.store.add_feedback(self.number, data["interest"], saved.id, feedback_type="text_message")
        self.store.delete_session(self.number)
        self.say(f"Thanks {data['name'].split()[0]}. We saved your details. Our team will be in touch.\nType menu for brochures.")

    def demo_step(self, step: str, data: dict, text: str) -> None:
        """Online: a date. Offline: a date and a place, in one message or two. Saved on the visitor's lead as a line in Interest."""
        low = text.lower().strip()
        if step == "mode":
            if low not in (ONLINE.lower(), OFFLINE.lower()):
                return self.replies.append(buttons("Please tap Online or Offline.", ONLINE, OFFLINE))
            data = {"mode": low}
            self.store.put_session(self.number, "demo", "when", data)
            return self.say(DATE_ASK if low == "online" else f"{DATE_ASK}, then the location.\nExample: 05/12/2026, Greenfield School, Pune")
        if step == "when":
            match = re.search(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", text)
            try:
                day = date(int(match[3]), int(match[2]), int(match[1])) if match else None
            except ValueError:
                day = None
            if not day:
                return self.say(f"I could not read that date. {DATE_ASK}")
            if day < datetime.now(followup.IST).date():
                return self.say(f"That date has passed. {DATE_ASK}")
            data = data | {"date": day.strftime("%d/%m/%Y")}
            place = re.sub(r"^[\s,;:\-]+|[\s,;:\-]+$", "", text.replace(match[0], "", 1))
            if data["mode"] == "offline" and not place:
                self.store.put_session(self.number, "demo", "where", data)
                return self.say("Where should the demo be? Type the location.")
            data["place"] = place if data["mode"] == "offline" else ""
        elif step == "where":
            data = data | {"place": text}
        what = f"{data['mode'].capitalize()}, {data['date']}" + (f", {data['place']}" if data["place"] else "")
        lead = self.visitor_lead(demo=what)
        self.store.add_feedback(self.number, f"Demo requested: {what}", lead.id, feedback_type="text_message")
        self.store.delete_session(self.number)
        self.say(f"Thank you. We have noted your {data['mode']} demo for {data['date']}" + (f" at {data['place']}" if data["place"] else "") + ". Our team will confirm with you.\nType menu for more.")

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
def lead_for_visitor(store: LeadStore, number: str) -> Lead | None:
    """The lead this visitor was captured as: the one we sent the follow-up for, else any lead with their number, else one they started themselves."""
    phone = auth.normalize_phone(number)
    lead = store.get_lead(store.followup_lead_id(phone) or 0) if phone else None
    if not lead and phone:
        lead = next((item for item in store.list_leads() if auth.normalize_phone(item.phone) == phone), None)  # ponytail: scans every live lead, like find_duplicate
    return lead or store.latest_lead_for_number(number)


def scan_draft(result: dict) -> dict:
    """A read result as scan data: the first document's draft, check, original text and photo numbers, and the rest under "more"."""
    return {"draft": result["fields"], "check": result["check"], "original": summary(result["fields"], []), "photo_ids": result.get("photos", []),
            "more": [{"draft": doc["fields"], "check": doc["check"], "original": summary(doc["fields"], []), "photo_ids": doc["photos"]} for doc in result.get("more", [])]}


def photos_of(doc: dict, every_photo: list[dict]) -> list[dict]:
    """The photos a document is on; photo numbers the reader did not give mean all of them."""
    return [every_photo[number - 1] for number in doc.get("photo_ids", []) if 1 <= number <= len(every_photo)] or every_photo


def saved_text(outcomes: list[tuple[Lead, str]]) -> str:
    """What staff are told after a save: one line for one lead, a list for several. Outcome is created, merged, unchanged or paired."""
    if len(outcomes) == 1:
        saved, outcome = outcomes[0]
        label = saved.org or saved.name or saved.phone or "Unnamed lead"
        if outcome == "created":
            badge = " It shows a Check badge on the dashboard." if saved.review_reason else ""
            return f"Saved as #{saved.event_no}, {label}{f' ({saved.priority} priority)' if saved.priority else ''}.{badge}"
        if outcome == "paired":
            return f"Added to #{saved.event_no}, {label}: joined with the photo you sent just before."
        added = "I added the new details to it." if outcome == "merged" else "There was nothing new to add."
        return f"Already saved as #{saved.event_no}, {saved.name or label}. {added}"
    lines = []
    for saved, outcome in outcomes:
        label = saved.org or saved.name or saved.phone or "Unnamed lead"
        note = {"created": f" ({saved.priority} priority)" if saved.priority else "", "merged": ", already saved, new details added", "unchanged": ", already saved", "paired": ", joined to the earlier photo"}[outcome]
        lines.append(f"- #{saved.event_no}, {label}{note}")
    return f"Saved {len(outcomes)} documents:\n" + "\n".join(lines)


def scan_docs(data: dict) -> list[dict]:
    """The documents of a scan as dicts with draft/check/original/photo_ids: the first one is the session data itself, the rest are in data["more"]."""
    return [data, *data.get("more", [])]


def apply_to_documents(data: dict, text: str) -> tuple[list[str], list[str]]:
    """Corrections for a scan. With several documents a line may start with 1 or 2 to pick one; any other line goes to the first."""
    docs = scan_docs(data)
    lines: list[list[str]] = [[] for _ in docs]
    for line in text.splitlines():
        match = re.match(r"\s*(\d)[\s.:)-]+(\S.*)", line) if len(docs) > 1 else None
        target = int(match[1]) - 1 if match and 1 <= int(match[1]) <= len(docs) else 0
        lines[target].append(match[2] if match and 1 <= int(match[1]) <= len(docs) else line)
    applied, unknown = [], []
    for doc, doc_lines in zip(docs, lines):
        done, left = apply_corrections(doc["draft"], "\n".join(doc_lines))
        doc["check"] = [item for item in doc["check"] if not any(word in item.lower() for word in done)]
        applied += done
        unknown += left
    return applied, unknown


def item_noun(doc_type: dict) -> str:
    """What to call one scan in a sentence: 'contact form', 'event badge / QR', or just 'document' for the generic type, whatever its menu name."""
    return "document" if doc_type["reader"] == "document" else noun(doc_type["label"])


def noun(label: str) -> str:
    """'Contact form' -> 'contact form', 'Event badge / QR' -> 'event badge / QR': a label used in the middle of a sentence."""
    return " ".join(word if word.isupper() else word.lower() for word in label.split())


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
    if {"city", "state", "district"} & {key for key in draft}:
        for key, value in zip(("city", "state", "district"), tidy(*(draft.get(key, "") for key in ("city", "state", "district")))):
            draft[key] = value
            if not value:
                del draft[key]
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
