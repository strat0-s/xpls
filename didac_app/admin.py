"""The admin pages: event control, documents and brochures, and the message wording. Admins only."""
from __future__ import annotations

import re
import secrets
from datetime import date

from flask import abort, flash, g, redirect, render_template, request, url_for

from didac_app import blobstore
from didac_app.flows import DEFAULT_WELCOME

READERS = {"document": "Other document (up to 2 per photo)", "form": "Form (one person, several pages)", "badge": "Badge with a QR code"}


def day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def web_link(value: str) -> str:
    value = (value or "").strip()
    return value if value.lower().startswith(("http://", "https://")) else ""


MAX_PDF = 4 * 1024 * 1024  # Vercel rejects a request body over 4.5 MB, so a bigger file never gets here


def brochure_upload(config, event_id: int, name: str) -> tuple[dict, str]:
    """Store the PDF picked on the form in the bucket. Returns ({file_key, file_name} or {} when no file was picked, an error text or empty)."""
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return {}, ""
    data = upload.read(MAX_PDF + 1)
    if not upload.filename.lower().endswith(".pdf") or not data.startswith(b"%PDF"):
        return {}, "The brochure file has to be a PDF."
    if len(data) > MAX_PDF:
        return {}, "That PDF is over 4 MB. Make it smaller, or paste a link instead."
    key = f"brochures/{event_id}/{secrets.token_hex(6)}.pdf"
    blobstore.upload(config, key, data, "application/pdf")
    return {"file_key": key, "file_name": (re.sub(r"[^\w .-]", "", name).strip() or "Brochure")[:90] + ".pdf"}, ""


def register(app, get_store, get_config) -> None:
    @app.before_request
    def admin_only():
        if request.path.startswith("/admin") and g.get("user") and not g.user["is_admin"]:
            abort(403)

    def chosen_event(store):
        """The event an admin page is about: ?event= or the form's event, else the live one, else the newest."""
        wanted = request.values.get("event", type=int)
        return (store.get_event(wanted) if wanted else None) or store.live_event() or next(iter(store.list_events()), None)

    def back(endpoint: str, event):
        return redirect(url_for(endpoint, event=event["id"] if event else None))

    # ---- event control ---------------------------------------------------------------------------------------------------------
    @app.get("/admin/event")
    def admin_event():
        store = get_store()
        events = store.list_events()
        live = store.live_event()
        shown = chosen_event(store)
        return render_template("admin_event.html", events=events, live=live, shown=shown, photos=len(store.list_event_photos(shown["id"])) if shown else 0)

    @app.post("/admin/event/save")
    def admin_event_save():
        store, event = get_store(), get_store().get_event(request.form.get("event", type=int) or 0) or abort(404)
        name = request.form.get("name", "").strip()
        if not name:
            flash("The event needs a name.")
        else:
            store.update_event(event["id"], name=name, starts_on=day(request.form.get("starts_on")), ends_on=day(request.form.get("ends_on")), venue=request.form.get("venue", "").strip())
            flash("Saved.")
        return back("admin_event", event)

    @app.post("/admin/event/new")
    def admin_event_new():
        store = get_store()
        name = request.form.get("name", "").strip()
        if not name:
            flash("The new event needs a name.")
            return redirect(url_for("admin_event"))
        event_id = store.create_event(name)
        source = request.form.get("copy_from", type=int)
        if source and store.get_event(source):
            store.copy_setup(source, event_id)
        flash(f"Created {name} as a draft. Set up its documents, then make it live.")
        return redirect(url_for("admin_event", event=event_id))

    @app.post("/admin/event/<int:event_id>/start")
    def admin_event_start(event_id: int):
        store = get_store()
        event = store.get_event(event_id) or abort(404)
        previous = store.live_event()
        store.start_event(event_id)
        flash(f"{event['name']} is live." + (f" {previous['name']} was stopped." if previous and previous["id"] != event_id else ""))
        return back("admin_event", event)

    @app.post("/admin/event/<int:event_id>/stop")
    def admin_event_stop(event_id: int):
        store = get_store()
        event = store.get_event(event_id) or abort(404)
        store.stop_event(event_id)
        flash(f"{event['name']} is stopped. Nothing is saved on WhatsApp until an event is live.")
        return back("admin_event", event)

    # ---- documents and brochures ------------------------------------------------------------------------------------------------
    @app.get("/admin/documents")
    def admin_documents():
        store = get_store()
        event = chosen_event(store)
        if not event:
            flash("Create an event first.")
            return redirect(url_for("admin_event"))
        return render_template("admin_documents.html", event=event, events=store.list_events(), doc_types=store.list_doc_types(event["id"]),
                               brochures=store.list_brochures(event["id"], active_only=False), readers=READERS)

    @app.post("/admin/documents/type/add")
    def admin_type_add():
        store, event = get_store(), chosen_event(get_store()) or abort(404)
        label = request.form.get("label", "").strip()
        if not label:
            flash("The document type needs a name.")
        else:
            reader = request.form.get("reader") if request.form.get("reader") in READERS else "document"
            store.add_doc_type(event["id"], label, request.form.get("hint", ""), reader)
            flash(f"Added {label}.")
        return back("admin_documents", event)

    @app.post("/admin/documents/type/<int:type_id>")
    def admin_type_edit(type_id: int):
        store = get_store()
        row = next((r for e in store.list_events() for r in store.list_doc_types(e["id"]) if r["id"] == type_id), None) or abort(404)
        label = request.form.get("label", "").strip()
        if label:
            store.update_doc_type(type_id, label=label, hint=request.form.get("hint", "").strip())
            flash("Saved.")
        else:
            flash("The document type needs a name.")
        return back("admin_documents", store.get_event(row["event_id"]))

    @app.post("/admin/documents/type/<int:type_id>/active")
    def admin_type_active(type_id: int):
        store = get_store()
        row = next((r for e in store.list_events() for r in store.list_doc_types(e["id"]) if r["id"] == type_id), None) or abort(404)
        on = request.form.get("active") == "1"
        store.set_doc_type_active(type_id, on)
        flash(f"{row['label']} is {'on' if on else 'off'} in the WhatsApp menu." + ("" if on else " Leads captured with it are kept."))
        return back("admin_documents", store.get_event(row["event_id"]))

    @app.post("/admin/documents/type/order")
    def admin_type_order():
        store = get_store()
        ids = [int(x) for x in request.form.get("ids", "").split(",") if x.strip().isdigit()]
        for position, type_id in enumerate(ids, 1):
            store.update_doc_type(type_id, position=position)
        return {"ok": True}

    @app.post("/admin/documents/brochure/add")
    def admin_brochure_add():
        store, event = get_store(), chosen_event(get_store()) or abort(404)
        name, link = request.form.get("name", "").strip(), web_link(request.form.get("url", ""))
        file, problem = brochure_upload(get_config(), event["id"], name)
        if problem:
            flash(problem)
        elif not name or not (link or file):
            flash("A brochure needs a name and either a PDF or a link that starts with https://.")
        else:
            store.add_brochure(event["id"], name, link, for_visitors=bool(request.form.get("for_visitors")), for_team=bool(request.form.get("for_team")), **file)
            flash(f"Added {name}.")
        return back("admin_documents", event)

    @app.post("/admin/documents/brochure/<int:brochure_id>")
    def admin_brochure_edit(brochure_id: int):
        store = get_store()
        row = next((r for e in store.list_events() for r in store.list_brochures(e["id"], active_only=False) if r["id"] == brochure_id), None) or abort(404)
        name, link = request.form.get("name", "").strip(), web_link(request.form.get("url", ""))
        file, problem = brochure_upload(get_config(), row["event_id"], name)
        if problem:
            flash(problem)
        elif not name or not (link or file or row["file_key"]):
            flash("A brochure needs a name and either a PDF or a link that starts with https://.")
        else:
            store.update_brochure(brochure_id, name=name, url=link, for_visitors=bool(request.form.get("for_visitors")), for_team=bool(request.form.get("for_team")), **file)
            flash("Saved.")
        return back("admin_documents", store.get_event(row["event_id"]))

    @app.post("/admin/documents/brochure/<int:brochure_id>/active")
    def admin_brochure_active(brochure_id: int):
        store = get_store()
        row = next((r for e in store.list_events() for r in store.list_brochures(e["id"], active_only=False) if r["id"] == brochure_id), None) or abort(404)
        store.set_brochure_active(brochure_id, request.form.get("active") == "1")
        return back("admin_documents", store.get_event(row["event_id"]))

    # ---- message wording -------------------------------------------------------------------------------------------------------
    @app.get("/admin/messages")
    def admin_messages():
        store = get_store()
        event = chosen_event(store)
        return render_template("admin_messages.html", event=event, welcome=(event["welcome_text"] or DEFAULT_WELCOME.format(event=event["name"])) if event else "")

    @app.post("/admin/messages/welcome")
    def admin_welcome_save():
        store, event = get_store(), get_store().get_event(request.form.get("event", type=int) or 0) or abort(404)
        text = request.form.get("text", "").strip()
        default = DEFAULT_WELCOME.format(event=event["name"])
        store.update_event(event["id"], welcome_text="" if text == default else text)  # the standard wording is not stored, so it follows a renamed event
        flash("Welcome message saved.")
        return back("admin_messages", event)
