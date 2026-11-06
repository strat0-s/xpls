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
CURRICULUM_CHOICES = ["CBSE", "ICSE", "STATE", "IGCSE", "CUSTOM"]  # the only boards Orbit keeps
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


def orbit_row(lead) -> dict:
    """One Orbit row per lead, blanks left blank: Orbit lists rows it cannot take in its preview and asks for a default type."""
    first, last = lead.first_name.strip(), lead.last_name.strip()
    if not first and lead.name.strip():
        first, _, rest = lead.name.strip().partition(" ")
        last = last or rest.strip()
    return {
        "name": lead.org.strip(),
        "type": lead_org_type(lead),
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
        "contact_email": normalize_email(lead.email.strip()),  # '' when invalid, because Orbit rejects the whole row for a bad email
        "contact_phone": normalize_phone(lead.phone) or lead.phone.strip(),
    }


def invalid_email(lead) -> bool:
    """True when the lead has an email that is left blank in the file because Orbit rejects the whole row for a bad one."""
    return bool(lead.email.strip()) and not normalize_email(lead.email.strip())


def export_warnings(rows: list[dict], dropped_emails: int = 0) -> list[str]:
    """What Orbit's import will do with this file, in plain words. Nothing here changes the data: a person checks the file and Orbit's preview before importing."""
    named = [row for row in rows if len(row["name"].strip()) >= 2]  # Orbit skips shorter names before it looks at anything else
    pins: dict[str, set[bool]] = {}
    for row in named:
        pins.setdefault(row["name"].strip().lower(), set()).add(bool(row["pincode"].strip()))
    checks = [
        (len(rows) - len(named), "without an organisation name: Orbit skips these and lists them in its preview."),
        (sum(not row["type"] for row in named), "without an organisation type: Orbit files them under the fallback type picked on its import page (Other if none is picked)."),
        (sum(bool(row["udiseCode"].strip()) and not re.fullmatch(r"\d{11}", row["udiseCode"].strip()) for row in named), "with a UDISE code that is not 11 digits: Orbit skips these rows and lists them in its preview."),
        (sum(not row["contact_firstName"] and bool(row["contact_phone"] or row["contact_email"]) for row in named), "with a phone or email but no person's name: Orbit adds the organisation without a contact, so that phone or email is not imported."),
        (dropped_emails, "with an email Orbit would reject: it is left blank in the file so the rest of the row still imports."),
        (sum(len(seen) == 2 for seen in pins.values()), "organisation name(s) appear both with and without a pincode: Orbit imports those as separate organisations."),
    ]
    return [f"{count} {text}" for count, text in checks if count]


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
