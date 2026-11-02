from __future__ import annotations

from didac_app.auth import normalize_email, normalize_phone
from didac_app.models import Lead
from didac_app.storage import LeadStore

# Fields a merge may fill when blank on the existing lead; everything else (ids, timestamps, sender, status, media, flags) is left alone.
FILLABLE = [
    "first_name", "last_name", "name", "org", "organisation_type", "role", "designation", "curriculum",
    "medium_of_instruction", "city", "district", "pincode", "phone", "email", "website", "google_maps_url",
    "qr_url", "qr_page_text", "udise_code", "lead_type", "interest", "budget", "timeline", "schools_count",
    "grades", "state", "notes",
]


def find_duplicate(store: LeadStore, new: Lead) -> tuple[Lead, bool] | None:
    """(existing lead, certain). Email or phone match is certain; name+organisation alone is only a possible match.
    ponytail: scans every live lead in Python (phones are stored un-normalised); index a normalised column if volume grows past a few thousand."""
    email, phone = normalize_email(new.email), normalize_phone(new.phone)
    name, org = new.name.strip().lower(), new.org.strip().lower()
    possible = None
    for lead in store.list_leads():
        if email and normalize_email(lead.email) == email:
            return lead, True
        if phone and normalize_phone(lead.phone) == phone:
            return lead, True
        if possible is None and name and org and lead.name.strip().lower() == name and lead.org.strip().lower() == org:
            possible = lead
    return (possible, False) if possible else None


def fill_blanks(existing: Lead, new: Lead) -> list[str]:
    """Copy values from `new` into blank fields of `existing`; returns the names of the fields filled."""
    filled = []
    for name in FILLABLE:
        if not getattr(existing, name) and getattr(new, name):
            setattr(existing, name, getattr(new, name))
            filled.append(name)
    return filled
