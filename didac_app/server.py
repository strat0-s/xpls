from __future__ import annotations

import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request, send_file

from didac_app import auth, bird, blobstore, flows, followup, ui
from didac_app.config import brochure_catalog, load_config
from didac_app.flows import Reply
from didac_app.replies import command_reply
from didac_app.storage import LeadStore

app = Flask(__name__)
timing = logging.getLogger("didac.timing")  # one line per webhook in the Vercel logs, to see where the time goes
timing.setLevel(logging.INFO)
timing.addHandler(logging.StreamHandler(sys.stdout))
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
    started = time.perf_counter()
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
    if body.startswith("/") and flows.staff_user(store, from_number):
        return send_replies(event_id, from_number, [Reply(command_reply(body, store, config))], [], "processed_command", started)
    if body.strip(" .!").lower() in followup.STOP_WORDS and not flows.staff_user(store, from_number):
        store.opt_out(auth.normalize_phone(from_number) or from_number)
        return send_replies(event_id, from_number, [Reply("You will not get any more follow-up messages from us. Type menu if you want our brochures.")], [], "opted_out", started)
    replies, lead_ids = flows.handle(store, config, from_number, body, media)
    return send_replies(event_id, from_number, replies, lead_ids, "processed", started)


def unique_ids(values):
    seen = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def send_replies(event_id: int, to_number: str, replies: list[Reply], lead_ids: list[int], status: str, started: float) -> tuple[str, int]:
    # Reply first, then record the outcome, so the visitor does not wait for the status write.
    # Answer 200 even if sending fails: Bird would retry and the retry is dropped as a duplicate anyway.
    worked = time.perf_counter()
    error = ""
    try:
        for reply in replies:
            bird.send_message(config, to_number, reply.text, reply.options, reply.as_list)
    except Exception as exc:
        status, error = "reply_failed", str(exc)
    sent = time.perf_counter()
    store.update_inbound_event(event_id, status, unique_ids(lead_ids), error)
    timing.info("webhook work_ms=%d send_ms=%d log_ms=%d total_ms=%d status=%s", (worked - started) * 1000, (sent - worked) * 1000, (time.perf_counter() - sent) * 1000, (time.perf_counter() - started) * 1000, status)
    return "OK", 200


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
