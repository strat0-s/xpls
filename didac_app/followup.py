"""Thank-you WhatsApp to every captured lead with a mobile number: once per number ever, daily cap, daytime only, never to staff or anyone who said STOP."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import phonenumbers

from didac_app import bird
from didac_app.auth import normalize_phone
from didac_app.config import DidacConfig
from didac_app.models import NOT_SCANNED
from didac_app.storage import LeadStore

IST = ZoneInfo("Asia/Kolkata")
SEND_HOURS = range(9, 21)  # 09:00-20:59 IST
STOP_WORDS = {"stop", "unsubscribe"}
BATCH = 40  # one dashboard click must finish inside the 60s Vercel limit
AUTO_KEY = "auto_followup_since"  # setting: when automatic follow-up was switched on (blank = off); only leads captured after that are sent
AUTO_BATCH = 5  # most numbers one captured lead can trigger, so a save never waits long
logger = logging.getLogger(__name__)


def is_mobile(e164: str) -> bool:
    return phonenumbers.number_type(phonenumbers.parse(e164)) in (phonenumbers.PhoneNumberType.MOBILE, phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE)


def eligible(store: LeadStore, since: str = "") -> list[tuple[str, int]]:
    """(E.164 number, lead id) for each distinct mobile number not yet messaged, oldest lead first. With `since`, only leads captured after that moment."""
    skip = store.followup_phones() | {normalize_phone(user["phone"]) for user in store.list_users() if user["phone"]}
    out = {}
    for lead in store.list_leads("created_at >= %s", (since,)) if since else store.list_leads():
        phone = normalize_phone(lead.phone)
        if phone and phone not in skip and phone not in out and is_mobile(phone):
            out[phone] = lead.id
    return list(out.items())


def overview(store: LeadStore, config: DidacConfig, event_id: int | None = None) -> tuple[list[dict], list[dict]]:
    """(waiting, skipped) for the leads of one event (every event when None). Waiting rows say whether they are in the next batch; skipped rows say why."""
    pairs = eligible(store)
    batch = {lead_id for _, lead_id in pairs[:max(0, min(BATCH, config.followup_daily_cap - store.followups_last_day()))]}
    leads = {lead.id: lead for lead in (store.list_leads("event_id = %s", (event_id,)) if event_id else store.list_leads())}
    waiting_ids = {lead_id for _, lead_id in pairs}
    waiting = [{"lead": leads[lead_id], "phone": phone, "next": lead_id in batch} for phone, lead_id in pairs if lead_id in leads]
    messaged, opted = store.followup_phones(), store.opted_out_phones()
    team = {normalize_phone(user["phone"]) for user in store.list_users() if user["phone"]}
    skipped, seen = [], set()
    for lead in leads.values():
        phone = normalize_phone(lead.phone)
        if lead.id in waiting_ids or (phone and phone in messaged and phone not in opted and phone not in team):
            continue  # waiting, or already messaged (it is in the Sent list)
        if lead.review_reason == NOT_SCANNED:
            why = "Not scanned"
        elif not phone:
            why = "No phone number"
        elif phone in team:
            why = "Team member"
        elif phone in opted:
            why = "Replied STOP"
        elif not is_mobile(phone):
            why = "Landline"
        elif phone in seen:
            continue  # the same number on another lead
        else:
            why = "Same number as another lead"
        seen.add(phone)
        skipped.append({"lead": lead, "phone": lead.phone, "why": why})
    return waiting, skipped


def blocked(store: LeadStore, config: DidacConfig, now: datetime | None) -> str:
    """Why nothing may go out right now, or an empty string."""
    if not config.followup_template_id:
        return "Follow-up is not set up: BIRD_FOLLOWUP_TEMPLATE_ID is missing."
    if (now or datetime.now(IST)).astimezone(IST).hour not in SEND_HOURS:
        return "Quiet hours: messages go out between 9:00 and 21:00 IST."
    if config.followup_daily_cap - store.followups_last_day() <= 0:
        return f"Daily cap of {config.followup_daily_cap} reached. Try again tomorrow."
    return ""


def send_to(store: LeadStore, config: DidacConfig, phone: str, lead_id: int) -> str:
    """Claim the number, send, and give the claim back if the send fails. Returns an error text, or empty."""
    if not store.claim_followup(phone, lead_id):
        return ""
    try:
        message_id = bird.send_template(config, phone)
    except Exception as exc:
        store.release_followup(phone)
        return str(exc)
    if message_id:
        store.set_followup_message(phone, message_id)
    store.log_outbound(phone, "Follow-up message sent (the Bird thank-you template).\n[Share my requirement | Download brochure]", [lead_id])
    return "sent"


def send_batch(store: LeadStore, config: DidacConfig, now: datetime | None = None) -> str:
    """Send to the next eligible numbers and say what happened. Stops at the first failure so a rejected template is not hammered."""
    if reason := blocked(store, config, now):
        return reason
    sent = 0
    for phone, lead_id in eligible(store)[:min(BATCH, config.followup_daily_cap - store.followups_last_day())]:
        result = send_to(store, config, phone, lead_id)
        if result not in ("", "sent"):
            return f"Sent {sent}, then stopped: {result}"
        sent += result == "sent"
    return f"Sent {sent} message(s)."


def auto_send(store: LeadStore, config: DidacConfig, now: datetime | None = None) -> None:
    """Automatic follow-up: right after a lead is saved, send the thank-you to the leads captured since the admin switched it on. That is the new lead and any that waited out
    quiet hours or the daily cap, never older ones. Never raises: saving a lead must not fail because of this."""
    since = store.get_setting(AUTO_KEY)
    if not since or blocked(store, config, now):
        return
    try:
        for phone, lead_id in eligible(store, since)[:AUTO_BATCH]:
            if send_to(store, config, phone, lead_id) not in ("", "sent"):
                return
    except Exception:
        logger.exception("Automatic follow-up failed")


def send_one(store: LeadStore, config: DidacConfig, lead, now: datetime | None = None) -> str:
    """The same message to one lead's number, with the same rules (mobile only, once per number, not staff, not opted out)."""
    if reason := blocked(store, config, now):
        return reason
    phone = normalize_phone(lead.phone)
    if not phone or not is_mobile(phone):
        return "This lead has no mobile number to message."
    if phone in store.followup_phones():
        return "This number was already messaged, or asked us to stop."
    if any(normalize_phone(user["phone"]) == phone for user in store.list_users() if user["phone"]):
        return "This is a team member's number."
    result = send_to(store, config, phone, lead.id)
    return "Message handed to WhatsApp. Check delivery on the Follow-up page." if result == "sent" else f"Could not send: {result}"


def refresh_statuses(store: LeadStore, config: DidacConfig, limit: int = 20) -> None:
    """Ask Bird how the undecided messages ended up (Meta can still refuse after Bird accepted). Bounded so the page stays quick."""
    for row in store.pending_followups(limit):
        try:
            status, reason = bird.message_status(config, row["message_id"])
        except Exception:
            return  # try again on the next page load
        store.update_followup_status(row["phone"], status, reason)
