"""The contact list for a Bird campaign: leads someone has marked "Checked for export", one row per mobile number."""
from __future__ import annotations

import csv
import io

from didac_app import followup
from didac_app.auth import normalize_phone

COLUMNS = ["display name", "first name", "last name", "phone", "email"]  # what Bird's CSV import maps; no name means a blank first name, so the template's fallback ("Educator") shows


def campaign_rows(store, event_id: int | None) -> tuple[list[list[str]], dict[str, int]]:
    """(rows, counts). counts: leads (all in the event), checked, included, and why checked leads were left out."""
    leads = store.list_leads("event_id = %s", (event_id,)) if event_id else store.list_leads()
    team = {normalize_phone(user["phone"]) for user in store.list_users() if user["phone"]}
    stopped = store.opted_out_phones()
    counts = {"leads": len(leads), "checked": 0, "included": 0, "no mobile number": 0, "team number": 0, "replied STOP": 0, "same number as an earlier lead": 0}
    rows, seen = [], set()
    for lead in sorted(leads, key=lambda lead: lead.event_no or lead.id):
        if lead.export_checked != "Yes":
            continue
        counts["checked"] += 1
        phone = normalize_phone(lead.phone)
        if not phone or not followup.is_mobile(phone):
            counts["no mobile number"] += 1
        elif phone in team:
            counts["team number"] += 1
        elif phone in stopped:
            counts["replied STOP"] += 1
        elif phone in seen:
            counts["same number as an earlier lead"] += 1
        else:
            seen.add(phone)
            name = " ".join(lead.name.split())
            rows.append([name or "Educator", name, "", phone, lead.email.strip().lower()])
            counts["included"] += 1
    return rows, counts


def to_csv(rows: list[list[str]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    writer.writerows(rows)
    return out.getvalue()
