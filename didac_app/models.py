from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class Lead:
    id: int | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    source_channel: str = "whatsapp"
    from_number: str = ""
    staff_number: str = ""
    captured_by: str = ""
    capture_context: str = ""
    first_name: str = ""
    last_name: str = ""
    name: str = ""
    org: str = ""
    organisation_type: str = ""
    role: str = ""
    designation: str = ""
    curriculum: str = ""
    medium_of_instruction: str = ""
    city: str = ""
    district: str = ""
    pincode: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    google_maps_url: str = ""
    qr_url: str = ""
    qr_page_text: str = ""
    udise_code: str = ""
    parent_organisation_id: str = ""
    lead_type: str = ""
    interest: str = ""
    budget: str = ""
    timeline: str = ""
    schools_count: str = ""
    grades: str = ""
    state: str = ""
    notes: str = ""
    text_notes: str = ""
    audio_transcript: str = ""
    feedback_notes: str = ""
    tags: str = ""
    score: int = 0
    priority: str = "Nurture"
    owner: str = "Sales team"
    status: str = "New"
    next_action: str = ""
    follow_up_date: str = ""
    email_subject: str = ""
    email_draft: str = ""
    whatsapp_draft: str = ""
    media_urls: str = ""
    media_local_paths: str = ""
    raw_message: str = ""
    doc_type: str = ""
    review_reason: str = ""
    updated_by: str = ""
    deleted_at: str = ""


@dataclass(frozen=True)
class MediaAttachment:
    url: str
    content_type: str = ""
    local_path: str = ""
