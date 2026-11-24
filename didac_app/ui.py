from __future__ import annotations

import re
import secrets
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from pathlib import Path

from flask import Response, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for

from didac_app import auth, flows, followup, orbit
from didac_app.dedupe import find_duplicate
from didac_app.media import suffix_for
from didac_app.models import NOT_SCANNED, Lead
from didac_app import blobstore
from didac_app.places import tidy
from didac_app.pager import paginate

IST = ZoneInfo("Asia/Kolkata")
APP_FILES = {"manifest.webmanifest": "application/manifest+json", "sw.js": "text/javascript", "icon-192.png": "image/png", "icon-512.png": "image/png", "apple-touch-icon.png": "image/png"}  # what a phone needs to install the app
OPEN_PATHS = {"/health", "/bird/webhook", *(f"/{name}" for name in APP_FILES)}
PRIORITIES = ("High", "Medium", "Low")
# The lead page's text fields in the order they are shown: (title of the group, [(field, label, width)]); a width of 2 spans the row.
LEAD_SECTIONS = [
    ("Contact", [("name", "Full name", 2), ("org", "Organisation", 2), ("designation", "Designation", 1), ("phone", "Phone", 1), ("email", "Email", 2)]),
    ("Location", [("city", "City", 1), ("state", "State", 1), ("district", "District", 1), ("pincode", "Pincode (6 digits)", 1)]),
    ("Organisation", [("website", "Website", 2), ("google_maps_url", "Google Maps link", 2), ("udise_code", "UDISE code", 1), ("medium_of_instruction", "Medium of instruction", 1)]),
    ("Interest", [("demo_interest", "Demo (yes or no, and when)", 2), ("workshop", "Teacher workshop", 1), ("students", "Students", 1)]),
]
EDITABLE_FIELDS = [(field, label) for _title, rows in LEAD_SECTIONS for field, label, _width in rows]
# Rows the bot writes while processing media; the media itself already shows what was read from it.
DERIVED_FEEDBACK = {"audio", "audio_transcript", "media", "media_text"}


def register(app, get_store, get_config) -> None:
    """Login, the lead gallery/detail pages and the team page. `get_store` is a callable so tests can swap the store."""

    @app.before_request
    def require_login():
        if request.path in OPEN_PATHS:
            return None
        if not app.secret_key:
            return "Dashboard login is not configured: set SECRET_KEY.", 503
        if request.method == "POST" and not auth.same(request.form.get("csrf", ""), csrf_token()):
            abort(400)
        user_id = session.get("uid")
        user = get_store().get_user(user_id) if user_id else None
        # session_version changes when an admin resets the password, which signs out older sessions.
        g.user = user if user and user["is_active"] and user["session_version"] == session.get("sv") else None
        if g.user or request.path == "/login":
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "login required"}), 401
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    @app.context_processor
    def inject_globals():
        return {"user": g.get("user"), "csrf_token": csrf_token, "reader": get_store().reader_status() if g.get("user") else None,  # drives the reading-problem banner
                "live_event": get_store().live_event() if g.get("user") else None}  # shown at the top of the sidebar

    @app.template_global()
    def page_url(number: int, key: str = "page") -> str:
        """This page's address with another page number, keeping every filter."""
        return url_for(request.endpoint, **{**request.args.to_dict(), **(request.view_args or {}), key: number})

    @app.template_filter("local_time")
    def local_time(value: str, fmt: str = "%H:%M") -> str:
        try:
            moment = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return ""
        # Lead timestamps carry a zone; media/feedback ones are naive UTC from the database.
        return moment.replace(tzinfo=moment.tzinfo or UTC).astimezone(IST).strftime(fmt)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(safe_next(request.args.get("next")))
        if request.method == "POST":
            store, login_name = get_store(), request.form.get("login", "")
            ip = request.headers.get("x-vercel-forwarded-for") or request.remote_addr or ""
            if auth.login_throttled(store, login_name, ip):
                flash("Too many failed attempts. Try again in a few minutes.")
                return render_template("login.html"), 429
            found = auth.authenticate(store, login_name, request.form.get("password", ""))
            if found:
                session.clear()
                session["uid"] = found["id"]
                session["sv"] = found["session_version"]
                session.permanent = True
                return redirect(safe_next(request.args.get("next")))
            store.record_failed_login(auth.login_key(login_name), ip)
            flash("That phone, email or password is not right.")
            return render_template("login.html"), 401
        return render_template("login.html")

    for file_name, file_type in APP_FILES.items():
        app.add_url_rule(f"/{file_name}", f"app_file_{file_name}", lambda file_name=file_name, file_type=file_type: send_from_directory(Path(__file__).parent / "static", file_name, mimetype=file_type, max_age=3600))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/dashboard")
    @app.get("/")
    def gallery():
        store = get_store()
        events = {e["id"]: e for e in store.list_events()}
        event_id = chosen_event_id(store, events)
        leads = store.list_leads("event_id = %s", (event_id,)) if event_id else store.list_leads()
        members = members_by_phone(store)
        media = {}
        for row in store.list_media():
            media.setdefault(row["lead_id"], []).append(row)
        labels = {}
        for e in events.values():
            for t in store.list_doc_types(e["id"]):
                labels.setdefault((e["id"], t["key"]), t["label"])
        cards = []
        waiting = store.reply_flags()
        for lead in leads:
            card = lead_card(lead, media.get(lead.id, []), members)
            card["replied_at"] = waiting.get(lead.id, "")
            card["type"] = lead.doc_type or "_none"
            card["type_label"] = labels.get((lead.event_id, lead.doc_type)) or ({"form": "Contact form", "badge": "Event badge / QR"}.get(lead.doc_type) if lead.doc_type else "No scan") or lead.doc_type
            card["event"] = event_line(events.get(lead.event_id), lead)
            cards.append(card)
        who = request.args.get("who") if request.args.get("who") in ("team", "visitors") else ""
        prio = request.args.get("prio") if request.args.get("prio") in (*PRIORITIES, "none") else ""
        check = request.args.get("check") == "1"
        reply_only = request.args.get("reply") == "1"
        replies_waiting = sum(1 for card in cards if card["replied_at"])
        query = request.args.get("q", "").strip().lower()
        if who:
            cards = [card for card in cards if card["is_team"] == (who == "team")]
        if prio:
            cards = [card for card in cards if card["priority"] == ("" if prio == "none" else prio)]
        if check:
            cards = [card for card in cards if card["review_reason"]]
        if reply_only:
            cards = [card for card in cards if card["replied_at"]]
        if query:
            cards = [card for card in cards if query in card["search"]]
        counts = {}
        for card in cards:
            counts[card["type"]] = counts.get(card["type"], 0) + 1
        tabs = [{"key": "all", "label": "All", "count": len(cards)}]
        if event_id:
            tabs += [{"key": t["key"], "label": t["label"], "count": counts.get(t["key"], 0)} for t in store.list_doc_types(event_id) if t["active"] or counts.get(t["key"])]
        known = {tab["key"] for tab in tabs}
        for key, count in counts.items():  # types of other events, old leads, and leads with no scan
            if key not in known:
                tabs.append({"key": key, "label": "No scan" if key == "_none" else next((c["type_label"] for c in cards if c["type"] == key), key), "count": count})
        tab = request.args.get("type") if request.args.get("type") in {t["key"] for t in tabs} else "all"
        if tab != "all":
            cards = [card for card in cards if card["type"] == tab]
        page = paginate(cards, request.args.get("page", 1, type=int), request.args.get("per", 10, type=int))
        return render_template("gallery.html", pg=page, tabs=tabs, tab=tab, q=request.args.get("q", ""), who=who, prio=prio, check=check, reply_only=reply_only, replies_waiting=replies_waiting, priorities=PRIORITIES,
                               events=list(events.values()), event_id=event_id, needs_check=sum(1 for lead in leads if lead.review_reason), reading=request.args.get("reading", type=int))

    @app.route("/leads/<int:lead_id>", methods=["GET", "POST"])
    def lead_detail(lead_id: int):
        store = get_store()
        lead = store.get_lead(lead_id)
        if not lead:
            abort(404)
        if request.method == "POST":
            store.update_lead_fields(lead_id, **lead_form_values(store, lead), review_reason="", updated_by=g.user["name"], updated_at=datetime.now(UTC).isoformat(timespec="seconds"))
            flash("Saved.")
            return redirect(url_for("lead_detail", lead_id=lead_id))
        return render_lead(store, lead)

    @app.route("/leads/new", methods=["GET", "POST"])
    def lead_new():
        """Add a lead by hand: the same form as the lead page; photos can be added once it is saved."""
        store = get_store()
        events = {e["id"]: e for e in store.list_events()}
        event_id = chosen_event_id(store, events, request.values)
        lead = Lead(source_channel="dashboard", captured_by=g.user["name"], from_number=g.user["phone"] or "", staff_number=g.user["phone"] or "", event_id=event_id)
        if request.method == "POST":
            values = lead_form_values(store, lead)
            for name, value in values.items():
                setattr(lead, name, value)
            scans, visitor = image_files("scan_photo", flows.MAX_PHOTOS), image_files("visitor_photo", 1)
            typed = bool(lead.name or lead.org or lead.phone or lead.email)
            if not (typed or scans):
                flash("Add at least a name, organisation, phone or email, or a photo of the form or card to read.")
                return render_lead(store, lead, new=True)
            match = find_duplicate(store, lead) if typed else None
            if match and match[1]:
                flash(f"That is already lead #{match[0].event_no}. Opened it instead.")
                return redirect(url_for("lead_detail", lead_id=match[0].id))
            lead.updated_by = g.user["name"]
            lead.review_reason = NOT_SCANNED if scans and not typed else f"possible duplicate of #{match[0].event_no}" if match else ""  # typed details make it a lead already, even if the photo cannot be read
            lead = store.create_lead(lead)
            save_photos(store, lead.id, visitor, "visitor")
            save_photos(store, lead.id, scans)
            flash("Lead added. Reading the photos..." if scans else "Lead added.")
            # The read takes about 30s, so it runs from the leads page in its own request rather than holding this one past the platform limit.
            return redirect(url_for("gallery", event=lead.event_id or None, reading=lead.id if scans else None))
        return render_lead(store, lead, new=True)

    def lead_form_values(store, lead) -> dict:
        """The lead page's form as lead columns: only what the form shows, so nothing the bot wrote meanwhile is touched."""
        values = {field: request.form.get(field, "").strip() for field, _label in EDITABLE_FIELDS}
        values["city"], values["state"], values["district"] = tidy(values["city"], values["state"], values["district"])
        first, _, last = values["name"].partition(" ")  # the export reads first/last name and designation, so keep them in step with what was edited
        picked = request.form.getlist("curriculum")
        return {
            **values, "first_name": first, "last_name": last.strip(), "role": values["designation"],
            "curriculum": ", ".join(code for code in orbit.CURRICULUM_CHOICES if code in picked),
            "products": "; ".join(dict.fromkeys(item.strip() for item in request.form.getlist("products") if item.strip())),
            "notes": request.form.get("notes", "").strip(),
            "organisation_type": org_type if (org_type := request.form.get("organisation_type", "")) in orbit.ORG_TYPES else "",
            "interest": request.form.get("interest", "").strip(),
            "priority": request.form.get("priority") if request.form.get("priority") in PRIORITIES else "",
            "lead_owner": owner if (owner := request.form.get("lead_owner", "")) in {u["name"] for u in store.list_users()} | {flows.NOT_LISTED} else "",
            "doc_type": kind if (kind := request.form.get("doc_type")) in {row["key"] for row in (store.list_doc_types(lead.event_id) if lead.event_id else [])} else lead.doc_type,
        }

    def render_lead(store, lead, new: bool = False):
        members = members_by_phone(store)
        items = [] if new else lead_items(lead, store, members)
        chats = [] if new else lead_chats(store, lead, members)
        listed = [row["label"] for row in store.list_products()]
        have = [item for item in lead.products.split("; ") if item]
        return render_template(
            "lead.html", lead=lead, new=new, items=items, chats=chats, replied_at="" if new else store.reply_flags().get(lead.id, ""), sections=LEAD_SECTIONS, title="New lead" if new else display_name(lead),
            doc_types=store.list_doc_types(lead.event_id) if lead.event_id else [], org_types=orbit.ORG_TYPES, org_type=orbit.lead_org_type(lead),
            products=listed + [item for item in have if item not in listed], have=have,
            priorities=PRIORITIES, owners=[u["name"] for u in store.list_users() if u["is_active"]] + [flows.NOT_LISTED], curricula=orbit.CURRICULUM_CHOICES, picked=orbit.curriculum_cell(lead.curriculum).split(", "),
        )
    def image_files(field: str, limit: int) -> list:
        return [file for file in request.files.getlist(field) if (file.mimetype or "").startswith("image/")][:limit]

    def save_photos(store, lead_id: int, files: list, purpose: str = "") -> None:
        """Store photos uploaded in the app, remembering who added them. purpose '' = a document to read, 'visitor' = with the visitor."""
        for file in files:
            key = f"uploads/{lead_id}/{secrets.token_hex(6)}{suffix_for(file.mimetype, file.filename or '')}"
            blobstore.upload(get_config(), key, file.read(), file.mimetype)
            store.add_media(lead_id, "", file.mimetype, key, purpose=purpose, added_by=g.user["name"])

    def read_lead_photos(store, lead) -> None:
        """Read the lead's document photos again and fill in only what is still blank; says what was filled."""
        photos = [{"key": row["local_path"], "ct": row["content_type"]} for row in store.list_media(lead.id)
                  if (row["content_type"] or "").startswith("image/") and row["local_path"] and not row["purpose"]][:flows.MAX_PHOTOS]
        if not photos:
            return flash("This lead has no document photos to read.")
        reader = next((row["reader"] for row in store.list_doc_types(lead.event_id) if row["key"] == lead.doc_type), "document") if lead.event_id else "document"
        result = flows.read_documents(get_config(), photos, reader, [row["label"] for row in store.list_products()])
        if result.get("failed"):
            store.record_read_failure(result["reason"])
            flash("Reading is not working just now, so nothing was changed. Please try again in a bit.")
        elif result.get("unclear") or not result["fields"]:
            store.record_read_ok(result.get("ms", 0))
            flash("The photos could not be read. Please fill the details in by hand.")
        else:
            store.record_read_ok(result.get("ms", 0))
            flows.tag_owner(store, result)
            store.merge_into_lead(lead.id, result["fields"], {})  # fills only what is still blank
            after = store.get_lead(lead.id)
            filled = [label for name, label in flows.SUMMARY if getattr(after, name) != getattr(lead, name)]
            store.update_lead_fields(lead.id, review_reason="; ".join(result["check"]), updated_by=g.user["name"], updated_at=datetime.now(UTC).isoformat(timespec="seconds"))
            flash((f"Filled in: {', '.join(filled)}. Nothing that was already there was changed." if filled else "Read the photos: nothing new to fill in, everything found is already on the lead.")
                  + (f" {len(result['more'])} more document(s) were found in the photos; only the first was used." if result.get("more") else ""))

    @app.post("/leads/<int:lead_id>/read")
    def lead_read(lead_id: int):
        store = get_store()
        read_lead_photos(store, store.get_lead(lead_id) or abort(404))
        lead = store.get_lead(lead_id)
        if request.form.get("twin") and (twin := find_duplicate(store, lead, lead_id)):
            store.update_lead_fields(lead_id, review_reason="; ".join(filter(None, (lead.review_reason, f"possible duplicate of #{twin[0].event_no}"))))
            flash(f"This looks like lead #{twin[0].event_no}. It is saved as its own lead and marked Check.")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/leads/<int:lead_id>/photos")
    def lead_photos(lead_id: int):
        """Photos added from the lead page. purpose=scan: more photos of the contact form or card, read straight away to fill what is blank. purpose=visitor: the team member with the visitor, kept as is."""
        store = get_store()
        lead = store.get_lead(lead_id) or abort(404)
        visitor = request.form.get("purpose") == "visitor"
        files = image_files("photo", 1 if visitor else flows.MAX_PHOTOS)
        if not files:
            flash("Choose a photo first.")
            return redirect(url_for("lead_detail", lead_id=lead_id))
        save_photos(store, lead_id, files, "visitor" if visitor else "")
        if visitor:
            flash("Photo added.")
        else:
            read_lead_photos(store, lead)
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/leads/<int:lead_id>/resolve")
    def lead_resolve(lead_id: int):
        store = get_store()
        if not store.get_lead(lead_id):
            abort(404)
        store.update_lead_fields(lead_id, review_reason="", updated_by=g.user["name"], updated_at=datetime.now(UTC).isoformat(timespec="seconds"))
        flash("Marked as checked.")
        target = request.form.get("next", "")
        return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("gallery"))

    @app.post("/leads/<int:lead_id>/replied")
    def lead_replied(lead_id: int):
        store = get_store()
        if not store.get_lead(lead_id):
            abort(404)
        store.clear_reply(lead_id)
        flash("Marked as replied.")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/media/<int:media_id>/rotate")
    def media_rotate(media_id: int):
        if not get_store().get_media(media_id):
            abort(404)
        return jsonify(rotation=get_store().rotate_media(media_id, -90 if request.form.get("dir") == "left" else 90))

    @app.post("/leads/<int:lead_id>/followup")
    def lead_followup(lead_id: int):
        lead = get_store().get_lead(lead_id) or abort(404)
        flash(followup.send_one(get_store(), get_config(), lead))
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/leads/<int:lead_id>/delete")
    def lead_delete(lead_id: int):
        require_admin()
        store = get_store()
        lead = store.get_lead(lead_id)
        if not lead:
            abort(404)
        store.update_lead_fields(lead_id, deleted_at=datetime.now(UTC).isoformat(timespec="seconds"), updated_by=g.user["name"])
        flash("Lead deleted.")
        return redirect(url_for("gallery", event=lead.event_id or None))  # back to the event the lead was on, not the live one

    @app.route("/team", methods=["GET", "POST"])
    def team():
        require_admin()
        store = get_store()
        if request.method == "POST":
            try:
                auth.add_user(
                    store,
                    request.form.get("name", ""),
                    request.form.get("phone", ""),
                    request.form.get("email", ""),
                    request.form.get("password", ""),
                    is_admin=bool(request.form.get("is_admin")),
                )
                flash("Member added.")
            except ValueError as exc:
                flash(str(exc))
            return redirect(url_for("team"))
        return render_template("team.html", members=store.list_users())

    @app.get("/export")
    def export_page():
        leads, people = export_leads(get_store(), request.args)
        rows = [orbit.orbit_row(lead) for lead in leads]
        ids = [lead.id for lead in leads]
        warnings = orbit.export_warnings(rows)
        query = {key: value for key, value in request.args.items() if value and key in EXPORT_FILTERS}
        store = get_store()
        events = store.list_events()
        doc_types = {}
        for e in events:
            for t in store.list_doc_types(e["id"]):
                doc_types.setdefault(t["key"], t["label"])
        return render_template(
            "export.html", rows=rows, ids=ids, columns=orbit.COLUMNS, labels=orbit.LABELS, pg=paginate(list(zip(ids, rows)), request.args.get("page", 1, type=int), request.args.get("per", 10, type=int)), events=events,
            event_id=chosen_event_id(store, {e["id"]: e for e in events}), doc_types=doc_types, people=people, warnings=warnings, org_types=orbit.ORG_TYPES, query=query,
            orgs=orbit.org_count(rows), max_orgs=orbit.MAX_ORGS, args=request.args,
        )

    @app.route("/followup", methods=["GET", "POST"])
    def followup_page():
        store, config = get_store(), get_config()
        if request.method == "POST":
            flash(followup.send_batch(store, config))
            return redirect(url_for("followup_page"))
        followup.refresh_statuses(store, config)
        events = store.list_events()
        event_id = chosen_event_id(store, {e["id"]: e for e in events})
        waiting, skipped = followup.overview(store, config, event_id)
        sent = store.list_followups(event_id=event_id)
        left = max(0, config.followup_daily_cap - store.followups_last_day())
        return render_template("followup.html", tz=IST, events=events, event_id=event_id, left=left, cap=config.followup_daily_cap, ready=bool(config.followup_template_id),
                               batch=min(followup.BATCH, left, len(waiting)),
                               w=paginate(waiting, request.args.get("wpage", 1, type=int), 10), s=paginate(skipped, request.args.get("spage", 1, type=int), 10),
                               d=paginate(sent, request.args.get("dpage", 1, type=int), 10), total_sent=len(sent))

    @app.get("/photos")
    def photos():
        store = get_store()
        events = store.list_events()
        event_id = chosen_event_id(store, {e["id"]: e for e in events})
        return render_template("photos.html", events=events, event_id=event_id, pg=paginate(store.list_event_photos(event_id), request.args.get("page", 1, type=int), 50), tz=IST)

    @app.get("/event-photo/<int:photo_id>")
    def event_photo(photo_id: int):
        row = get_store().get_event_photo(photo_id) or abort(404)
        return redirect(blobstore.signed_url(get_config(), row["key"]))

    @app.get("/export/orbit.<fmt>")
    def export_orbit(fmt: str):
        if fmt not in ("csv", "xlsx"):
            abort(404)
        rows, _people = export_selection(get_store(), request.args)
        if not rows or orbit.org_count(rows) > orbit.MAX_ORGS:
            abort(400)
        body, mimetype = (orbit.to_csv(rows), "text/csv") if fmt == "csv" else (
            orbit.to_xlsx(rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        filename = f"orbit-import-{datetime.now(IST).date().isoformat()}.{fmt}"
        return Response(body, mimetype=mimetype, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/export/everything.xlsx")
    def export_everything():
        store = get_store()
        event_id = chosen_event_id(store, {e["id"]: e for e in store.list_events()}, request.args)
        event = store.get_event(event_id) if event_id else None
        name = re.sub(r"[^a-z0-9]+", "-", (event["name"] if event else "all-events").lower()).strip("-")
        leads = store.list_leads("event_id = %s", (event_id,)) if event_id else store.list_leads()
        members = members_by_phone(store)
        keys = sorted({row["local_path"] for row in store.list_media() if row["local_path"] and row["lead_id"] in {lead.id for lead in leads}})
        links = blobstore.signed_urls(get_config(), keys, EXPORT_LINK_DAYS * 86400) if keys else {}
        return Response(store.export_everything(event_id, links, {lead.id: lead_chats(store, lead, members) for lead in leads}), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}-everything-{datetime.now(IST).date().isoformat()}.xlsx"'})

    @app.post("/team/<int:user_id>/active")
    def team_active(user_id: int):
        require_admin()
        member = get_store().get_user(user_id) or abort(404)
        if member["id"] == g.user["id"]:
            flash("You can't deactivate yourself.")
        else:
            get_store().set_user_active(user_id, not member["is_active"])
        return redirect(url_for("team"))

    @app.post("/team/<int:user_id>/admin")
    def team_admin(user_id: int):
        require_admin()
        member = get_store().get_user(user_id) or abort(404)
        if member["id"] == g.user["id"]:
            flash("You can't change your own access.")  # so there is always an admin left
        else:
            get_store().set_user_admin(user_id, not member["is_admin"])
            flash(f"{member['name']} is now {'staff' if member['is_admin'] else 'an admin'}.")
        return redirect(url_for("team"))

    @app.post("/team/<int:user_id>/edit")
    def team_edit(user_id: int):
        require_admin()
        get_store().get_user(user_id) or abort(404)
        try:
            auth.edit_user(get_store(), user_id, request.form.get("name", ""), request.form.get("phone", ""), request.form.get("email", ""))
            flash("Member saved.")
        except ValueError as exc:
            flash(str(exc))
        return redirect(url_for("team"))

    @app.post("/team/<int:user_id>/password")
    def team_password(user_id: int):
        require_admin()
        get_store().get_user(user_id) or abort(404)
        try:
            auth.change_password(get_store(), user_id, request.form.get("password", ""))
            flash("Password changed.")
        except ValueError as exc:
            flash(str(exc))
        return redirect(url_for("team"))


EXPORT_LINK_DAYS = 90  # how long the photo links in the everything export keep working
EXPORT_FILTERS = ("from", "to", "by", "type", "event", "doc", "skip")


def export_selection(store, args) -> tuple[list[dict], list[str]]:
    leads, people = export_leads(store, args)
    skip = {int(n) for n in args.get("skip", "").split(",") if n.isdigit()}  # leads unticked on the export page
    return [orbit.orbit_row(lead) for lead in leads if lead.id not in skip], people


def export_leads(store, args) -> tuple[list, list[str]]:
    """Leads matching the export filters (capture date in IST, who captured it, organisation type)
    and the capture names to offer in the filter."""
    start, end = parse_day(args.get("from")), parse_day(args.get("to"))
    by, kind, doc = args.get("by", ""), args.get("type", ""), args.get("doc", "")
    members = members_by_phone(store)
    leads, people = [], set()
    event_id = chosen_event_id(store, {e["id"]: e for e in store.list_events()}, args)
    for lead in (store.list_leads("event_id = %s", (event_id,)) if event_id else store.list_leads()):
        name = author(lead, members)[0]
        people.add(name)
        day = created_day(lead)
        if day and ((start and day < start) or (end and day > end)):
            continue
        if (by and name != by) or (kind and orbit.lead_org_type(lead) != kind) or (doc and lead.doc_type != doc) or lead.review_reason == NOT_SCANNED:  # a lead nobody has read yet is left out
            continue
        leads.append(lead)
    return leads, sorted(people)


def chosen_event_id(store, events: dict, args=None) -> int | None:
    """The event a list is about: ?event=<id>, ?event=all (None), or by default the live one."""
    wanted = (args if args is not None else request.args).get("event", "")
    if wanted == "all":
        return None
    if wanted.isdigit() and int(wanted) in events:
        return int(wanted)
    live = store.live_event()
    return live["id"] if live else None


def event_line(event: dict | None, lead) -> dict:
    """{name, when}: the event a lead belongs to and, when the event has dates, which day of it the lead was captured."""
    if not event:
        return {"name": "", "when": ""}
    day = created_day(lead)
    start, end = event["starts_on"], event["ends_on"]
    span = f"{start.strftime('%d')}–{end.strftime('%d %b %Y')}" if start and end and start.month == end.month else " – ".join(d.strftime("%d %b %Y") for d in (start, end) if d)
    number = f"Day {(day - start).days + 1} · " if day and start and day >= start else ""
    return {"name": event["name"], "when": number + span}


def parse_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return None


def created_day(lead) -> date | None:
    try:
        moment = datetime.fromisoformat(lead.created_at)
    except ValueError:
        return None
    return moment.replace(tzinfo=moment.tzinfo or UTC).astimezone(IST).date()


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def safe_next(target: str | None) -> str:
    # Browsers read "\\" as "/" and drop tabs, so "/\\evil.example" or "/<tab>/evil.example" would leave the site.
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target and target.isprintable():
        return target
    return url_for("gallery")


def require_admin() -> None:
    if not g.user["is_admin"]:
        abort(403)


def members_by_phone(store) -> dict[str, str]:
    return {user["phone"]: user["name"] for user in store.list_users() if user["phone"]}


def display_name(lead) -> str:
    return lead.name or f"{lead.first_name} {lead.last_name}".strip() or lead.org or lead.phone or "Unnamed lead"


def author(lead, members: dict[str, str]) -> tuple[str, str, bool]:
    """(name, initials, is_team) of whoever captured the lead; visitors are people not on the team."""
    name = members.get(auth.normalize_phone(lead.from_number)) or lead.captured_by
    if name:
        return name, "".join(word[0] for word in name.split()[:2]).upper(), True
    return "Visitor via WhatsApp", "V", False


def lead_card(lead, media: list[dict], members: dict[str, str]) -> dict:
    images = [row for row in media if (row["content_type"] or "").startswith("image/")]
    has_audio = any((row["content_type"] or "").startswith("audio/") for row in media)
    by, initials, is_team = author(lead, members)
    note = lead.text_notes or lead.audio_transcript or lead.notes or lead.raw_message
    name = display_name(lead)
    org = " · ".join(part for part in (lead.org, lead.city) if part)
    return {
        "id": lead.id,
        "no": lead.event_no,
        "lead_owner": lead.lead_owner,
        "tab": {"form": "forms", "badge": "badges"}.get(lead.doc_type, "other"),
        "name": name,
        "org": org,
        "phone": lead.phone,
        "kind": "image" if images else "audio" if has_audio else "text",
        "image_id": images[0]["id"] if images else None,
        "photo_count": len(images),
        "note": note,
        "review_reason": lead.review_reason,
        "unscanned": lead.review_reason == NOT_SCANNED,
        "priority": lead.priority,
        "by": by,
        "initials": initials,
        "is_team": is_team,
        "created_at": lead.created_at,
        "search": " ".join([f"#{lead.event_no}", name, org, lead.lead_owner, lead.phone, lead.email, note]).lower(),
    }


def same_sitting(rows: list[dict], saved_at: datetime, gap: timedelta = timedelta(hours=1)) -> list[dict]:
    """The rows of one scan: walking back from the moment the lead was saved, stop at the first pause longer than `gap`, so an old chat
    that no lead claimed (a deleted lead, a test) does not lead this one. Rows after the save (a note for this lead) always stay."""
    def at(row):
        moment = datetime.fromisoformat(str(row["created_at"]))
        return moment if moment.tzinfo else moment.replace(tzinfo=UTC)

    keep, edge = [], saved_at
    for row in reversed(rows):
        if at(row) <= saved_at:
            if edge - at(row) > gap:
                break
            edge = at(row)
        keep.append(row)
    return keep[::-1]


def lead_chats(store, lead, members: dict[str, str]) -> list[dict]:
    """The separate WhatsApp chats behind a lead, each with our number on one side: the visitor's own chat, and the team member's scan chat
    (only the messages of the scan conversation it was saved in, so another lead's scan is not mixed in)."""
    chats = []
    visitor = auth.normalize_phone(lead.phone)
    staff = auth.normalize_phone(lead.from_number)
    if staff and staff != visitor:
        if convo := store.lead_convo(lead.id):
            rows = store.chat_with(staff, lead.id, convo=convo)
        else:  # saved before conversations were recorded: guess by time
            since = datetime.fromisoformat(store.previous_lead_time(lead.from_number, lead.id) or "1970-01-01T00:00:00+00:00")
            since = max(since, datetime.fromisoformat(lead.created_at) - timedelta(hours=24))
            rows = same_sitting(store.chat_with(staff, lead.id, since, datetime.fromisoformat(lead.created_at) + timedelta(seconds=1)), datetime.fromisoformat(lead.created_at))
        chats.append({"title": f"Team chat: {members.get(staff) or staff}", "who": members.get(staff) or staff, "rows": rows})
    if visitor:
        chats.append({"title": f"Visitor chat: {lead.name or visitor}", "who": lead.name or visitor, "rows": store.chat_with(visitor, lead.id)})
    return chats


def lead_items(lead, store, members: dict[str, str]) -> list[dict]:
    """Everything received for the lead (photos, voice notes, texts), oldest first."""
    by, _initials, is_team = author(lead, members)
    items = []
    media_rows = store.list_media(lead.id)
    for row in media_rows:
        content_type = row["content_type"] or ""
        kind = "image" if content_type.startswith("image/") else "audio" if content_type.startswith("audio/") else "file"
        items.append({"kind": kind, "id": row["id"], "purpose": row["purpose"], "rotation": row["rotation"], "text": row["extracted_text"], "by": row["added_by"] or by, "is_team": bool(row["added_by"]) or is_team, "created_at": row["created_at"]})
    for row in store.list_feedback(lead.id):
        if row["feedback_type"] in DERIVED_FEEDBACK:
            continue
        name = members.get(auth.normalize_phone(row["from_number"]))
        items.append({"kind": "text", "text": row["body"], "by": name or "Visitor via WhatsApp", "is_team": bool(name), "created_at": row["created_at"]})
    # Text kept only on the lead (older leads, typed "New lead" messages) has no feedback row: show it too, once.
    shown = [item["text"] for item in items if item["kind"] == "text"]
    saved = [part for part in lead.text_notes.split("; ") if part.strip() and not any(part.strip() in text for text in shown)]
    typed = re.sub(r"^new lead\s*", "", lead.raw_message.strip(), flags=re.I) if lead.raw_message and not media_rows else ""
    if typed and not any(typed in text or text in typed for text in shown + saved):
        saved.append(typed)
    if saved:
        items.append({"kind": "text", "text": "; ".join(saved), "by": by, "is_team": is_team, "created_at": lead.created_at})
    return sorted(items, key=lambda item: item["created_at"])
