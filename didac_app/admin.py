"""The admin pages: event control, documents and brochures, and the message wording. Admins only."""
from __future__ import annotations

import re
import secrets
from collections import Counter
from datetime import UTC, date, datetime

from flask import abort, flash, g, redirect, render_template, request, url_for

from didac_app import blobstore, followup
from didac_app.auth import normalize_phone
from didac_app.flows import DEFAULT_WELCOME
from didac_app.pager import paginate

READERS = {"document": "Other document (up to 2 per photo)", "form": "Form (one person, several pages)", "badge": "Badge with a QR code"}


def day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


MAX_PDF_MB = 50  # the bucket's own limit; WhatsApp itself allows 100 MB


def brochure_file(event_id: int, name: str) -> dict:
    """The file the browser already put in the bucket (see /admin/documents/upload-url), or {} when none was picked."""
    key = request.form.get("file_key", "")
    if not re.fullmatch(rf"brochures/{event_id}/[0-9a-f]{{12}}\.pdf", key):
        return {}
    return {"file_key": key, "file_name": (re.sub(r"[^\w .-]", "", name).strip() or "Brochure")[:90] + ".pdf"}


def autosaved(error: str = ""):
    """The page saves a field as soon as it changes and asks for JSON back; returns None for an ordinary form post."""
    if request.headers.get("X-Autosave"):
        return ({"error": error}, 400) if error else {"ok": True}


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
                               brochures=store.list_brochures(event["id"], active_only=False), readers=READERS, max_pdf_mb=MAX_PDF_MB)

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
        return autosaved("" if label else "Needs a name") or back("admin_documents", store.get_event(row["event_id"]))

    @app.post("/admin/documents/type/<int:type_id>/active")
    def admin_type_active(type_id: int):
        store = get_store()
        row = next((r for e in store.list_events() for r in store.list_doc_types(e["id"]) if r["id"] == type_id), None) or abort(404)
        on = request.form.get("active") == "1"
        store.set_doc_type_active(type_id, on)
        flash(f"{row['label']} is {'on' if on else 'off'} in the WhatsApp menu." + ("" if on else " Leads captured with it are kept."))
        return autosaved() or back("admin_documents", store.get_event(row["event_id"]))

    @app.post("/admin/documents/type/order")
    def admin_type_order():
        store = get_store()
        ids = [int(x) for x in request.form.get("ids", "").split(",") if x.strip().isdigit()]
        for position, type_id in enumerate(ids, 1):
            store.update_doc_type(type_id, position=position)
        return {"ok": True}

    @app.post("/admin/documents/upload-url")
    def admin_upload_url():
        event = chosen_event(get_store()) or abort(404)
        key = f"brochures/{event['id']}/{secrets.token_hex(6)}.pdf"
        return {"url": blobstore.signed_upload_url(get_config(), key), "key": key}

    @app.post("/admin/documents/brochure/add")
    def admin_brochure_add():
        store, event = get_store(), chosen_event(get_store()) or abort(404)
        name = request.form.get("name", "").strip()
        file = brochure_file(event["id"], name)
        if not name or not file:
            flash("A brochure needs a name and a PDF.")
        else:
            store.add_brochure(event["id"], name, "", for_visitors=bool(request.form.get("for_visitors")), for_team=bool(request.form.get("for_team")), **file)
            flash(f"Added {name}.")
        return back("admin_documents", event)

    @app.post("/admin/documents/brochure/<int:brochure_id>")
    def admin_brochure_edit(brochure_id: int):
        store = get_store()
        row = next((r for e in store.list_events() for r in store.list_brochures(e["id"], active_only=False) if r["id"] == brochure_id), None) or abort(404)
        name = request.form.get("name", "").strip()
        file = brochure_file(row["event_id"], name)
        if not name or not (file or row["file_key"] or row["url"]):
            flash("A brochure needs a name and a PDF.")
        else:
            store.update_brochure(brochure_id, name=name, **({"url": ""} if file else {}), for_visitors=bool(request.form.get("for_visitors")), for_team=bool(request.form.get("for_team")), **file)
            flash("Saved.")
        return autosaved("" if name else "Needs a name") or back("admin_documents", store.get_event(row["event_id"]))

    @app.post("/admin/documents/brochure/<int:brochure_id>/active")
    def admin_brochure_active(brochure_id: int):
        store = get_store()
        row = next((r for e in store.list_events() for r in store.list_brochures(e["id"], active_only=False) if r["id"] == brochure_id), None) or abort(404)
        store.set_brochure_active(brochure_id, request.form.get("active") == "1")
        return autosaved() or back("admin_documents", store.get_event(row["event_id"]))

    # ---- products of interest --------------------------------------------------------------------------------------------------
    @app.route("/admin/products", methods=["GET", "POST"])
    def admin_products():
        store = get_store()
        if request.method == "POST":
            label = " ".join(request.form.get("label", "").split())
            if not label:
                flash("The product needs a name.")
            elif store.add_product(label):
                flash(f"Added {label}.")
            else:
                flash(f"{label} is already on the list.")
            return redirect(url_for("admin_products"))
        return render_template("admin_products.html", products=store.list_products())

    @app.post("/admin/products/<int:product_id>")
    def admin_product_edit(product_id: int):
        label = " ".join(request.form.get("label", "").split())
        ok = bool(label) and get_store().rename_product(product_id, label)
        flash("Saved." if ok else "That name is empty or already on the list.")
        return autosaved("" if ok else "Empty or already listed") or redirect(url_for("admin_products"))

    @app.post("/admin/products/<int:product_id>/delete")
    def admin_product_delete(product_id: int):
        get_store().delete_product(product_id)
        flash("Removed. Leads keep the products they were saved with.")
        return redirect(url_for("admin_products"))

    # ---- follow-up report ------------------------------------------------------------------------------------------------------
    @app.get("/admin/followup")
    def admin_followup():
        store, config = get_store(), get_config()
        followup.refresh_statuses(store, config, 30)
        event = chosen_event(store)
        event_id = event["id"] if event else None
        waiting, skipped = followup.overview(store, config, event_id)
        sent = store.list_followups(event_id=event_id)
        since, live = store.get_setting(followup.AUTO_KEY), (store.live_event() or {}).get("id")
        stopped = followup.blocked(store, config, None)
        used = store.followups_last_day()
        for row in waiting:  # when each waiting number goes out
            lead = row["lead"]
            row["when"] = stopped or ("On the next save of any lead." if since and lead.event_id == live and max(lead.created_at, lead.updated_at) >= since else "Only from the Follow-up page: it was captured before automatic follow-up was switched on.")
        for row in skipped:
            row["when"] = followup.WHEN.get(row["why"], "")
        status = Counter("delivered" if r["status"] in ("delivered", "read") else "failed" if r["status"] in ("delivery_failed", "sending_failed") else "pending" for r in sent)
        failures = Counter(followup.failure_label(r["reason"]) for r in sent if r["status"] in ("delivery_failed", "sending_failed"))
        by_phone = {r["phone"]: r for r in sent}
        waiting_by, skipped_by = {r["lead"].id: r for r in waiting}, {r["lead"].id: r for r in skipped}
        every = []
        for lead in sorted(store.list_leads("event_id = %s", (event_id,)) if event_id else [], key=lambda lead: -lead.id):
            phone, done = normalize_phone(lead.phone), None
            done = by_phone.get(phone) if phone else None
            if done:
                state = "delivered" if done["status"] in ("delivered", "read") else "failed" if done["status"] in ("delivery_failed", "sending_failed") else "pending"
                label = {"delivered": "Delivered", "failed": "Failed", "pending": "On the way"}[state]
                note = done["sent_at"].astimezone(followup.IST).strftime("Sent %d %b, %H:%M") + (f". {followup.failure_label(done['reason'])}." if state == "failed" else "") + ("" if done["lead_id"] == lead.id else " Sent through another lead with the same number.")
            elif lead.id in waiting_by:
                state, label, note = "waiting", "Waiting", waiting_by[lead.id]["when"]
            elif lead.id in skipped_by:
                state, label, note = "notsent", skipped_by[lead.id]["why"], skipped_by[lead.id]["when"]
            else:
                state, label, note = "notsent", "Same number as another lead", followup.WHEN["Same number as another lead"]
            made = datetime.fromisoformat(lead.created_at).astimezone(followup.IST).strftime("Captured %d %b, %H:%M")
            every.append({"lead": lead, "state": state, "label": label, "note": f"{made}. {note}"})
        show = request.args.get("show", "")
        counts = Counter(r["state"] for r in every)
        return render_template("admin_followup.html", event=event, events=store.list_events(), auto_since=since, stopped=stopped, used=used, cap=config.followup_daily_cap, means=followup.MEANS, tz=followup.IST, now=datetime.now(followup.IST),
                               leads=len(store.list_leads("event_id = %s", (event_id,))) if event_id else 0, sent=len(sent), status=status, failures=failures.most_common(),
                               reasons=Counter(r["why"] for r in skipped).most_common(), total_skipped=len(skipped), total_waiting=len(waiting),
                               show=show, counts=counts, e=paginate([r for r in every if not show or r["state"] == show], request.args.get("epage", 1, type=int), 25))

    # ---- message wording -------------------------------------------------------------------------------------------------------
    @app.get("/admin/messages")
    def admin_messages():
        store = get_store()
        event = chosen_event(store)
        since = store.get_setting(followup.AUTO_KEY)
        return render_template("admin_messages.html", event=event, welcome=(event["welcome_text"] or DEFAULT_WELCOME.format(event=event["name"])) if event else "",
                               auto_since=since, auto_ready=bool(get_config().followup_template_id), cap=get_config().followup_daily_cap)

    @app.post("/admin/messages/auto-followup")
    def admin_auto_followup_save():
        store = get_store()
        if request.form.get("on") == "1":
            if not store.get_setting(followup.AUTO_KEY):  # switching on again keeps the original moment, so nothing older is ever sent
                store.set_setting(followup.AUTO_KEY, datetime.now(UTC).isoformat(timespec="seconds"))
            flash("Automatic follow-up is on. Leads captured from now on are messaged as soon as they are saved.")
        else:
            store.set_setting(followup.AUTO_KEY, "")
            flash("Automatic follow-up is off.")
        return redirect(url_for("admin_messages"))

    @app.post("/admin/messages/welcome")
    def admin_welcome_save():
        store, event = get_store(), get_store().get_event(request.form.get("event", type=int) or 0) or abort(404)
        text = request.form.get("text", "").strip()
        default = DEFAULT_WELCOME.format(event=event["name"])
        store.update_event(event["id"], welcome_text="" if text == default else text)  # the standard wording is not stored, so it follows a renamed event
        flash("Welcome message saved.")
        return back("admin_messages", event)
