from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request, send_file

from didac_app import auth, bird, blobstore, ui
from didac_app.config import brochure_catalog, load_config
from didac_app.intake import continue_intake, is_start_message, start_intake
from didac_app.media import extract_media_text, save_media
from didac_app.openai_services import split_lead_texts
from didac_app.parser import parse_message, update_lead_from_note, update_lead_from_text
from didac_app.replies import command_reply, lead_created_reply, media_reply
from didac_app.scoring import enrich_lead
from didac_app.storage import LeadStore

app = Flask(__name__)
config = load_config()
store = LeadStore(config.database_url)
app.secret_key = config.secret_key
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.getenv("VERCEL")),
)
ui.register(app, lambda: store, lambda: config)


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(UTC).isoformat(timespec="seconds")}


@app.get("/api/leads")
def api_leads():
    return jsonify([lead.__dict__ for lead in store.list_leads()])


@app.get("/api/feedback")
def api_feedback():
    return jsonify([dict(row) for row in store.list_feedback()])


@app.get("/api/media")
def api_media():
    return jsonify([dict(row) for row in store.list_media()])


@app.get("/api/inbound")
def api_inbound():
    return jsonify([dict(row) for row in store.list_inbound_events()])


@app.get("/brochures")
def brochures():
    return render_template_string(BROCHURES_HTML, brochures=brochure_catalog(config))


@app.get("/brochure/<product_key>")
def brochure_download(product_key: str):
    for item in brochure_catalog(config).values():
        if item["key"] == product_key:
            if item["url"]:
                return {"name": item["name"], "download_url": item["url"]}
            return {"name": item["name"], "error": "Brochure URL not configured"}, 404
    return {"error": "Unknown brochure"}, 404


@app.get("/media/<int:media_id>")
def media_file(media_id: int):
    row = store.get_media(media_id)
    if not row:
        return {"error": "Media not found"}, 404
    if not row["local_path"]:
        return {"error": "No file stored"}, 404
    return redirect(blobstore.signed_url(config, row["local_path"]))


@app.get("/media-library")
def media_library():
    return render_template_string(MEDIA_LIBRARY_HTML, groups=media_library_groups())


@app.get("/export/report")
def export_report():
    path = store.export_report_excel(config.export_dir / "didac_report.xlsx")
    return send_file(Path(path).resolve(), as_attachment=True)


@app.get("/export/report.xlsx")
def export_report_xlsx():
    path = store.export_report_excel(config.export_dir / "didac_report.xlsx")
    return send_file(Path(path).resolve(), as_attachment=True)


@app.get("/export/report.csv")
def export_report_csv():
    path = store.export_report_csv(config.export_dir / "didac_report.csv")
    return send_file(Path(path).resolve(), as_attachment=True, mimetype="text/csv")


@app.post("/bird/webhook")
def bird_webhook():
    # Fail closed: Bird is told to call /bird/webhook?token=<BIRD_WEBHOOK_TOKEN>.
    if not config.bird_webhook_token or not auth.same(request.args.get("token", ""), config.bird_webhook_token):
        return {"error": "unauthorized"}, 401
    event = request.get_json(silent=True) or {}
    message = bird.parse_inbound(event)
    if not message:
        return "ignored", 200
    from_number = message["from_number"]
    body = message["body"]
    media = message["media"]
    num_media = len(media)
    event_id = store.add_inbound_event(from_number, body, num_media, [url for url, _ in media], event, message_id=message["message_id"])
    if event_id is None:
        return "duplicate", 200
    touched_lead_ids = []
    response = []

    if body.startswith("/"):
        response.append(command_reply(body, store, config))
        store.update_inbound_event(event_id, "processed_command")
        return send_replies(event_id, from_number, response, touched_lead_ids)

    latest = store.latest_lead_for_number(from_number)
    if num_media:
        context_lead = store.latest_lead_for_number(from_number)
        is_feedback_media = bool(context_lead and context_lead.status == "Feedback:collect")
        is_intake_media = bool(context_lead and context_lead.status.startswith("Intake:"))
        process_count = min(num_media, config.max_media_per_message)
        skipped_count = max(0, num_media - process_count)
        extracted_texts = []
        created_leads = []
        media_urls = []
        local_paths = []
        applied_extracted_to_lead = False
        for index in range(process_count):
            media_url, content_type = media[index]
            local_path, temp_path = download_media(media_url, content_type, from_number)
            try:
                extracted = extract_media_text(temp_path, content_type)
            finally:
                Path(temp_path).unlink(missing_ok=True)
            media_lead_texts = split_lead_texts(extracted) if extracted else []
            should_create_media_leads = bool(
                media_lead_texts and (content_type.startswith("image/") or len(media_lead_texts) > 1) and (not is_feedback_media or len(media_lead_texts) > 1)
            )
            if should_create_media_leads:
                for lead_text in media_lead_texts:
                    _, new_lead = parse_message("New lead\n" + lead_text, from_number)
                    if body:
                        apply_text_note(new_lead, body)
                    new_lead.media_urls = media_url
                    new_lead.media_local_paths = local_path
                    new_lead = enrich_lead(new_lead, config)
                    new_lead = store.create_lead(new_lead)
                    touched_lead_ids.append(new_lead.id)
                    media_id = store.add_media(new_lead.id, media_url, content_type, local_path, lead_text)
                    if body:
                        store.add_feedback(
                            from_number,
                            body,
                            new_lead.id,
                            feedback_type="text_caption",
                            media_id=media_id,
                            sentiment="",
                            action_required="",
                        )
                    created_leads.append(new_lead)
                    extracted_texts.append(lead_text)
                media_urls.append(media_url)
                local_paths.append(local_path)
                continue

            lead = context_lead
            if not lead:
                lead = enrich_lead(parse_message("New lead\nNotes: Media-only lead", from_number)[1], config)
                lead = store.create_lead(lead)
                touched_lead_ids.append(lead.id)
                context_lead = lead
            media_id = store.add_media(lead.id, media_url, content_type, local_path, extracted)
            if is_feedback_media:
                if body:
                    apply_feedback_note(lead, body)
                    store.add_feedback(
                        from_number,
                        body,
                        lead.id,
                        feedback_type="text_caption",
                        media_id=media_id,
                        sentiment="",
                        action_required="",
                    )
                if extracted and "PENDING" not in extracted:
                    apply_audio_or_media_note(lead, extracted, content_type)
                    applied_extracted_to_lead = True
                store.add_feedback(
                    from_number,
                    extracted or f"Media feedback saved: {content_type}",
                    lead.id,
                    feedback_type="audio" if content_type.startswith("audio/") else "media",
                    media_id=media_id,
                    sentiment="",
                    action_required="review_media",
                )
            elif is_intake_media and extracted and "PENDING" not in extracted:
                if body:
                    apply_text_note(lead, body)
                apply_audio_or_media_note(lead, extracted, content_type)
                applied_extracted_to_lead = True
                store.add_feedback(
                    from_number,
                    extracted,
                    lead.id,
                    feedback_type="audio_transcript" if content_type.startswith("audio/") else "media_text",
                    media_id=media_id,
                    sentiment="",
                    action_required="",
                )
            elif extracted and "PENDING" not in extracted and content_type.startswith("audio/"):
                if body:
                    apply_text_note(lead, body)
                apply_audio_or_media_note(lead, extracted, content_type)
                applied_extracted_to_lead = True
                store.add_feedback(
                    from_number,
                    extracted,
                    lead.id,
                    feedback_type="audio_transcript",
                    media_id=media_id,
                    sentiment="",
                    action_required="",
                )
            media_urls.append(media_url)
            local_paths.append(local_path)
            if extracted:
                extracted_texts.append(extracted)
        if created_leads:
            response.append(multi_card_media_reply(created_leads, process_count, skipped_count))
        else:
            lead = context_lead
            if not lead:
                lead = enrich_lead(parse_message("New lead\nNotes: Media-only lead", from_number)[1], config)
                lead = store.create_lead(lead)
                touched_lead_ids.append(lead.id)
            lead.media_urls = merge_semicolon(lead.media_urls, *media_urls)
            lead.media_local_paths = merge_semicolon(lead.media_local_paths, *local_paths)
            if body:
                apply_text_note(lead, body)
                store.add_feedback(from_number, body, lead.id, feedback_type="text_caption")
            if extracted_texts and not applied_extracted_to_lead and not all("PENDING" in item for item in extracted_texts):
                if is_intake_media:
                    apply_audio_or_media_note(lead, "\n".join(extracted_texts), "")
                else:
                    update_lead_from_text(lead, "\n".join(extracted_texts))
            if lead.status == "Feedback:collect":
                lead.status = "New"
            if lead.status.startswith("Intake:"):
                lead.status = "New"
            lead = enrich_lead(lead, config)
            store.update_lead(lead)
            if lead.id:
                touched_lead_ids.append(lead.id)
            response.append(media_reply(lead, "\n".join(extracted_texts), process_count, skipped_count))
        store.update_inbound_event(event_id, "processed", unique_ids(touched_lead_ids))
    elif latest and latest.status.startswith(("Menu:", "Brochure:", "Feedback:", "Intake:", "Badge:")):
        _, reply = continue_intake(latest, body, store, config)
        response.append(reply)
        if latest.id:
            touched_lead_ids.append(latest.id)
        store.update_inbound_event(event_id, "processed", unique_ids(touched_lead_ids))
        return send_replies(event_id, from_number, response, touched_lead_ids)

    elif is_start_message(body):
        response.append(start_intake(from_number, store))
        latest = store.latest_lead_for_number(from_number)
        if latest and latest.id:
            touched_lead_ids.append(latest.id)
        store.update_inbound_event(event_id, "processed", unique_ids(touched_lead_ids))
        return send_replies(event_id, from_number, response, touched_lead_ids)

    else:
        kind, lead = parse_message(body, from_number)
        if lead:
            lead = enrich_lead(lead, config)
            lead = store.create_lead(lead)
            touched_lead_ids.append(lead.id)
            response.append(lead_created_reply(lead, config))
        elif body:
            lead = store.latest_lead_for_number(from_number)
            if lead:
                apply_text_note(lead, body)
                store.add_feedback(from_number, body, lead.id, feedback_type="text_message")
                lead = enrich_lead(lead, config)
                store.update_lead(lead)
                if lead.id:
                    touched_lead_ids.append(lead.id)
                response.append(lead_created_reply(lead, config))
            else:
                response.append("Send lead details starting with 'New lead', or send /help for the format.")
        else:
            response.append("Message received. Send /help for DIDAC lead commands.")
        store.update_inbound_event(event_id, "processed", unique_ids(touched_lead_ids))
    return send_replies(event_id, from_number, response, touched_lead_ids)


def download_media(media_url: str, content_type: str, from_number: str = "") -> tuple[str, str]:
    return save_media(media_url, content_type, config, bird.media_headers(config), sender=from_number)


def unique_ids(values):
    seen = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def merge_semicolon(current: str, *values: str) -> str:
    parts = [part for part in [current, *values] if part]
    seen = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    return "; ".join(seen)


def apply_text_note(lead, text: str):
    if not text:
        return lead
    lead.text_notes = merge_semicolon(lead.text_notes, text)
    apply_capture_metadata(lead, text)
    update_lead_from_note(lead, text)
    return lead


def apply_feedback_note(lead, text: str):
    if not text:
        return lead
    lead.feedback_notes = merge_semicolon(lead.feedback_notes, text)
    apply_capture_metadata(lead, text)
    update_lead_from_note(lead, text)
    return lead


def apply_audio_or_media_note(lead, text: str, content_type: str):
    if not text:
        return lead
    if content_type.startswith("audio/"):
        lead.audio_transcript = merge_semicolon(lead.audio_transcript, text)
    else:
        lead.text_notes = merge_semicolon(lead.text_notes, text)
    update_lead_from_note(lead, text)
    return lead


def apply_capture_metadata(lead, text: str):
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if normalized in {"by", "captured by", "staff", "team member"}:
            lead.captured_by = merge_semicolon(lead.captured_by, value)
        elif normalized in {"context", "capture context", "event context"}:
            lead.capture_context = merge_semicolon(lead.capture_context, value)
        elif normalized in {"tag", "tags"}:
            lead.tags = merge_semicolon(lead.tags, value)


def send_replies(event_id: int, to_number: str, replies: list[str], lead_ids: list[int]) -> tuple[str, int]:
    # Answer 200 even if sending fails: Bird would retry and the retry is dropped as a duplicate anyway.
    try:
        for text in replies:
            bird.send_message(config, to_number, text)
    except Exception as exc:
        store.update_inbound_event(event_id, "reply_failed", unique_ids(lead_ids), str(exc))
    return "OK", 200


def multi_card_media_reply(leads, processed_count: int, skipped_count: int) -> str:
    lines = [f"Saved {len(leads)} lead record(s) from {processed_count} media file(s)."]
    for lead in leads[:5]:
        lines.append(f"#{lead.id}: {lead.org or lead.name or lead.phone or 'Unnamed lead'}")
    if skipped_count:
        lines.append(f"Skipped {skipped_count} extra file(s). Please send them separately.")
    lines.append("Refresh the dashboard to see all records.")
    return "\n".join(lines)


def media_library_groups():
    groups = {}
    for row in store.list_media():
        parts = (row["local_path"] or "").split("/")
        date = parts[0] if len(parts) == 3 else "legacy"
        sender = parts[1] if len(parts) == 3 else "unknown_sender"
        groups.setdefault(date, {}).setdefault(sender, []).append(row)
    return groups


MEDIA_LIBRARY_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DIDAC Media Library</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; padding: 18px; background: #f5f7fa; color: #17202a; }
    h1 { font-size: 20px; margin: 0 0 12px; }
    h2 { font-size: 16px; margin: 20px 0 8px; }
    h3 { font-size: 14px; margin: 12px 0 8px; color: #475467; }
    .group { background: white; border: 1px solid #d8dee6; border-radius: 8px; padding: 12px; margin-bottom: 14px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(155px, 1fr)); gap: 10px; }
    .tile { border: 1px solid #edf0f3; border-radius: 8px; padding: 8px; background: #fff; }
    .tile img { width: 100%; aspect-ratio: 4 / 3; object-fit: cover; border-radius: 6px; border: 1px solid #edf0f3; display: block; }
    .meta { color: #667085; font-size: 12px; margin-top: 6px; overflow-wrap: anywhere; }
    a { color: #0b6bcb; text-decoration: none; font-weight: 700; }
    .empty { background: white; border: 1px solid #d8dee6; border-radius: 8px; padding: 16px; color: #667085; }
  </style>
</head>
<body>
  <h1>Media Library</h1>
  {% if not groups %}
    <div class="empty">No media uploaded yet.</div>
  {% endif %}
  {% for date, senders in groups.items() %}
  <section class="group">
    <h2>{{ date }}</h2>
    {% for sender, items in senders.items() %}
      <h3>{{ sender }} · {{ items|length }} file(s)</h3>
      <div class="grid">
      {% for item in items %}
        <div class="tile">
          {% if item['content_type'].startswith('image/') %}
            <a href="/media/{{ item['id'] }}" target="_blank"><img src="/media/{{ item['id'] }}" alt="media {{ item['id'] }}"></a>
          {% else %}
            <a href="/media/{{ item['id'] }}" target="_blank">Open {{ item['content_type'] or 'file' }}</a>
          {% endif %}
          <div class="meta">Media #{{ item['id'] }} · Lead #{{ item['lead_id'] or '-' }}</div>
          <div class="meta">{{ item['local_path'] }}</div>
        </div>
      {% endfor %}
      </div>
    {% endfor %}
  </section>
  {% endfor %}
</body>
</html>
"""


BROCHURES_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Xperimentor Brochures</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; margin: 0; padding: 28px; color: #17202a; }
    .card { background: white; border: 1px solid #dde2e8; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
    a { color: #0b6bcb; }
  </style>
</head>
<body>
  <h1>Xperimentor brochures</h1>
  {% for number, item in brochures.items() %}
  <div class="card">
    <strong>{{ number }}. {{ item['name'] }}</strong><br>
    {% if item['url'] %}
      <a href="{{ item['url'] }}">Download / open brochure</a>
    {% else %}
      <span>URL not configured</span>
    {% endif %}
  </div>
  {% endfor %}
  <p><a href="/dashboard">Back to dashboard</a></p>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5001")), debug=os.getenv("FLASK_DEBUG") == "1")
