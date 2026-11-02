"""Export leads as an Orbit CRM organisation import (see ORBIT_IMPORT_SPEC.md): one row per contact, `type` from a fixed list."""
from __future__ import annotations

import csv
import io
import re

from openpyxl import Workbook

from didac_app.auth import normalize_email, normalize_phone

# Same values in the dashboard dropdown, the scanner prompt and Orbit's `type` enum, so no mapping table is needed.
ORG_TYPES = {
    "SCHOOL": "School",
    "SCHOOL_CHAIN": "School chain / group",
    "CSR": "CSR",
    "NGO": "NGO",
    "GOVERNMENT": "Government",
    "DISTRIBUTOR": "Distributor / reseller",
    "INSTITUTION": "College, university, training centre",
    "VENDOR": "Other edtech / hardware / content company",
    "OTHER": "Other (individual, media, investor)",
}
MAX_ORGS = 500
COLUMNS = [
    "name", "type", "state", "city", "district", "pincode", "website", "googleMapsUrl", "udiseCode",
    "curriculum", "mediumOfInstruction", "contact_firstName", "contact_lastName", "contact_designation",
    "contact_email", "contact_phone",
]
# Checked in order, so "school chain" wins over "school".
TYPE_WORDS = [
    ("SCHOOL_CHAIN", ("chain", "group of schools", "franchise")),
    ("CSR", ("csr", "corporate")),
    ("NGO", ("ngo", "foundation", "nonprofit", "non-profit")),
    ("GOVERNMENT", ("govt", "government", "department", "ministry")),
    ("DISTRIBUTOR", ("distributor", "dealer", "reseller", "channel partner")),
    ("INSTITUTION", ("college", "university", "institute", "coaching", "training", "iti")),
    ("VENDOR", ("vendor", "supplier", "edtech", "company", "publisher")),
    ("SCHOOL", ("school",)),
    ("OTHER", ("other",)),
]
CURRICULA = {"CBSE", "ICSE", "IGCSE", "CUSTOM"}


def normalize_org_type(value: str) -> str:
    """Free text ("Govt", "School Chain", "Dealer") -> one of ORG_TYPES, or '' if it is not recognisable."""
    text = re.sub(r"[_\-\s]+", " ", (value or "").strip().lower())
    if not text:
        return ""
    if text.upper().replace(" ", "_") in ORG_TYPES:
        return text.upper().replace(" ", "_")
    for code, words in TYPE_WORDS:
        if any(re.search(rf"\b{re.escape(word)}\b", text) for word in words):
            return code
    return ""


def lead_org_type(lead) -> str:
    return normalize_org_type(lead.organisation_type) or normalize_org_type(lead.lead_type)


def split_list(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[;,]", value or "") if part.strip()]


def curriculum_cell(value: str) -> str:
    """Orbit keeps only CBSE, ICSE, STATE, IGCSE, CUSTOM; map the obvious spellings and drop the rest."""
    found = []
    for part in split_list(value):
        code = "STATE" if part.upper().startswith("STATE") else part.upper()
        if (code == "STATE" or code in CURRICULA) and code not in found:
            found.append(code)
    return ", ".join(found)


def orbit_row(lead) -> tuple[dict | None, str]:
    """(row, '') for an importable lead, or (None, reason) when Orbit would reject it."""
    org = lead.org.strip()
    if len(org) < 2:
        return None, "no organisation name"
    org_type = lead_org_type(lead)
    if not org_type:
        return None, "no organisation type"
    email = lead.email.strip()
    if email and not normalize_email(email):
        return None, "email is not valid"
    first, last = lead.first_name.strip(), lead.last_name.strip()
    if not first and lead.name.strip():
        first, _, rest = lead.name.strip().partition(" ")
        last = last or rest.strip()
    return {
        "name": org,
        "type": org_type,
        "state": lead.state,
        "city": lead.city,
        "district": lead.district,
        "pincode": lead.pincode,
        "website": lead.website,
        "googleMapsUrl": lead.google_maps_url,
        "udiseCode": lead.udise_code,
        "curriculum": curriculum_cell(lead.curriculum),
        "mediumOfInstruction": ", ".join(split_list(lead.medium_of_instruction)),
        "contact_firstName": first,
        "contact_lastName": last,
        "contact_designation": lead.designation or lead.role,
        "contact_email": normalize_email(email),
        "contact_phone": normalize_phone(lead.phone) or lead.phone.strip(),
    }, ""


def build_export(leads) -> tuple[list[dict], list[tuple]]:
    """(rows, left_out) where left_out is [(lead, reason)]."""
    rows, left_out = [], []
    for lead in leads:
        row, reason = orbit_row(lead)
        if row:
            rows.append(row)
        else:
            left_out.append((lead, reason))
    return rows, left_out


def org_count(rows: list[dict]) -> int:
    """Orbit merges rows with the same name + pincode into one organisation and limits the file to MAX_ORGS of them."""
    return len({(row["name"].lower(), row["pincode"].lower()) for row in rows})


def to_csv(rows: list[dict]) -> bytes:
    # No BOM: Orbit matches headers by name and a BOM would stick to the first one.
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def to_xlsx(rows: list[dict]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Organisations"
    sheet.append(COLUMNS)
    for row in rows:
        sheet.append([str(row[column]) for column in COLUMNS])  # strings keep leading zeros and +91
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()
