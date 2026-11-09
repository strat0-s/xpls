from __future__ import annotations

from didac_app.config import DidacConfig
from didac_app.storage import LeadStore


HELP_TEXT = """Xperimentor DIDAC WhatsApp bot (staff):
Type menu to scan a contact form, a document or an event badge, or to add a note.
Type cancel to stop what you are doing.

/summary
/export excel
/send brochure <phone>"""


def command_reply(body: str, store: LeadStore, config: DidacConfig) -> str:
    command = body.strip().lower()
    if command in {"/help", "help"}:
        return HELP_TEXT
    if command.startswith("/summary"):
        return f"DIDAC summary\nTotal leads: {len(store.list_leads())}"
    if command.startswith("/export"):
        return f"Download the report: {config.public_base_url.rstrip('/')}/export/report.xlsx"
    if command.startswith("/send brochure"):
        return asset_links(config) or "Brochure links are not configured yet. Set BROCHURE_URL/CASE_STUDY_URL/CALENDAR_URL in .env."
    return "Unknown command. Send /help."


def asset_links(config: DidacConfig) -> str:
    links = []
    if config.brochure_url:
        links.append(f"Brochure: {config.brochure_url}")
    if config.case_study_url:
        links.append(f"Case study: {config.case_study_url}")
    if config.demo_video_url:
        links.append(f"Demo video: {config.demo_video_url}")
    if config.calendar_url:
        links.append(f"Book demo: {config.calendar_url}")
    return "\n".join(links)


def body_after_command(body: str, command: str) -> str:
    return body[len(command) :].strip()
