from __future__ import annotations

from didac_app.config import DidacConfig
from didac_app.storage import LeadStore


HELP_TEXT = """Xperimentor DIDAC WhatsApp bot (staff):
Type menu to scan a contact form, a document or an event badge, or to add a note.
Type cancel to stop what you are doing.

/summary
/export excel"""


def command_reply(body: str, store: LeadStore, config: DidacConfig) -> str:
    command = body.strip().lower()
    if command in {"/help", "help"}:
        return HELP_TEXT
    if command.startswith("/summary"):
        return f"DIDAC summary\nTotal leads: {len(store.list_leads())}"
    if command.startswith("/export"):
        return f"Download the report: {config.public_base_url.rstrip('/')}/export/report.xlsx"
    return "Unknown command. Send /help."

