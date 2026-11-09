"""Thank-you WhatsApp to every captured lead with a mobile number: once per number ever, daily cap, daytime only, never to staff or anyone who said STOP."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import phonenumbers

from didac_app import bird
from didac_app.auth import normalize_phone
from didac_app.config import DidacConfig
from didac_app.storage import LeadStore

IST = ZoneInfo("Asia/Kolkata")
SEND_HOURS = range(9, 21)  # 09:00-20:59 IST
STOP_WORDS = {"stop", "unsubscribe"}
BATCH = 40  # one dashboard click must finish inside the 60s Vercel limit


def is_mobile(e164: str) -> bool:
    return phonenumbers.number_type(phonenumbers.parse(e164)) in (phonenumbers.PhoneNumberType.MOBILE, phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE)


def eligible(store: LeadStore) -> list[tuple[str, int]]:
    """(E.164 number, lead id) for each distinct mobile number not yet messaged, oldest lead first."""
    skip = store.followup_phones() | {normalize_phone(user["phone"]) for user in store.list_users() if user["phone"]}
    out = {}
    for lead in store.list_leads():
        phone = normalize_phone(lead.phone)
        if phone and phone not in skip and phone not in out and is_mobile(phone):
            out[phone] = lead.id
    return list(out.items())


def send_batch(store: LeadStore, config: DidacConfig, now: datetime | None = None) -> str:
    """Send to the next eligible numbers and say what happened. Stops at the first failure so a rejected template is not hammered."""
    if not config.followup_template_id:
        return "Follow-up is not set up: BIRD_FOLLOWUP_TEMPLATE_ID is missing."
    if (now or datetime.now(IST)).astimezone(IST).hour not in SEND_HOURS:
        return "Quiet hours: messages go out between 9:00 and 21:00 IST."
    room = min(BATCH, config.followup_daily_cap - store.followups_last_day())
    if room <= 0:
        return f"Daily cap of {config.followup_daily_cap} reached. Try again tomorrow."
    sent = 0
    for phone, lead_id in eligible(store)[:room]:
        if not store.claim_followup(phone, lead_id):
            continue
        try:
            bird.send_template(config, phone)
        except Exception as exc:
            store.release_followup(phone)
            return f"Sent {sent}, then stopped: {exc}"
        sent += 1
    return f"Sent {sent} message(s)."
