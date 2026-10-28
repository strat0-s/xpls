from __future__ import annotations

from pathlib import Path

from didac_app.config import DidacConfig
from didac_app.models import Lead
from didac_app.openai_services import openai_enabled, text_to_speech
from didac_app.storage import LeadStore


HELP_TEXT = """Xperimentor DIDAC WhatsApp bot:
Visitors can type: hi or menu

Staff lead format:
New lead
Name:
Org:
Role:
City:
Interest:
Budget:
Timeline:

/summary
/hot
/followups
/export excel
/send brochure <phone>
/tts <text>
You can also send a visiting-card photo or a voice note."""


def lead_created_reply(lead: Lead, config: DidacConfig) -> str:
    assets = asset_links(config)
    return (
        f"Lead saved: {lead.org or lead.name or 'Unnamed lead'}\n"
        f"Priority: {lead.priority} | Score: {lead.score} | Owner: {lead.owner}\n"
        f"Next: {lead.next_action}\n\n"
        f"Draft WhatsApp:\n{lead.whatsapp_draft}\n\n"
        f"{assets}"
    ).strip()


def media_reply(lead: Lead, extracted_text: str, processed_count: int = 1, skipped_count: int = 0) -> str:
    if extracted_text and "PENDING" not in extracted_text:
        extra = f"\nExtracted text: {extracted_text[:300]}"
    elif extracted_text:
        extra = "\nMedia saved. OCR/transcription is pending or unavailable."
    else:
        extra = "\nMedia saved. No readable text was extracted."
    skipped = f"\nSkipped {skipped_count} extra file(s). Please send them in a separate message." if skipped_count else ""
    return (
        f"Saved {processed_count} media file(s) for lead #{lead.id}: {lead.org or lead.name or lead.from_number}."
        f"{extra}{skipped}\nRefresh the dashboard to see the record. Send missing details as Name/Org/Role/Interest if needed."
    )


def command_reply(body: str, store: LeadStore, config: DidacConfig) -> str:
    command = body.strip().lower()
    if command in {"/help", "help"}:
        return HELP_TEXT
    if command.startswith("/summary"):
        leads = store.list_leads()
        counts = store.count_by_priority()
        return (
            f"DIDAC summary\nTotal leads: {len(leads)}\n"
            f"Hot: {counts.get('Hot', 0)}\n"
            f"Warm: {counts.get('Warm', 0)}\n"
            f"Nurture: {counts.get('Nurture', 0)}\n"
            f"Low priority: {counts.get('Low priority', 0)}"
        )
    if command.startswith("/hot"):
        leads = store.list_leads("priority=%s", ("Hot",))[:10]
        return format_lead_list("Hot leads", leads)
    if command.startswith("/followups"):
        leads = store.list_leads("status != %s", ("Closed",))[:10]
        return format_lead_list("Pending follow-ups", leads)
    if command.startswith("/export"):
        path = store.export_report_excel(config.export_dir / "didac_report.xlsx")
        return f"Excel exported: {Path(path).resolve()}"
    if command.startswith("/send brochure"):
        return asset_links(config) or "Brochure links are not configured yet. Set BROCHURE_URL/CASE_STUDY_URL/CALENDAR_URL in .env."
    if command.startswith("/tts"):
        text = body_after_command(body, "/tts")
        if not text:
            return "Usage: /tts text to convert into speech"
        if not openai_enabled():
            return "OpenAI TTS is not enabled. Set OPENAI_API_KEY in .env."
        output = config.media_dir / "tts" / "whatsapp_reply.mp3"
        path = text_to_speech(text, output)
        return f"TTS audio created: {Path(path).resolve()}" if path else "TTS generation failed."
    return "Unknown command. Send /help."


def format_lead_list(title: str, leads: list[Lead]) -> str:
    if not leads:
        return f"{title}: none yet."
    lines = [title]
    for lead in leads:
        lines.append(
            f"#{lead.id} {lead.priority} | {lead.org or lead.name or lead.from_number} | "
            f"{lead.interest or 'No interest noted'} | Owner: {lead.owner}"
        )
    return "\n".join(lines)


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
