from __future__ import annotations

import re
import secrets
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from flask import Response, abort, flash, g, jsonify, redirect, render_template, request, session, url_for

from didac_app import auth, orbit

IST = ZoneInfo("Asia/Kolkata")
OPEN_PATHS = {"/health", "/bird/webhook"}
EDITABLE_FIELDS = [
    ("name", "Full name"),
    ("org", "Organization"),
    ("designation", "Designation"),
    ("phone", "Phone"),
    ("email", "Email"),
    ("city", "City"),
    ("state", "State"),
    ("district", "District"),
    ("pincode", "Pincode (6 digits)"),
    ("website", "Website"),
    ("google_maps_url", "Google Maps link"),
    ("udise_code", "UDISE code"),
    ("medium_of_instruction", "Medium of instruction (comma separated, e.g. English, Kannada)"),
]
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
        return {"user": g.get("user"), "csrf_token": csrf_token}

    @app.template_filter("local_time")
    def local_time(value: str) -> str:
        try:
            moment = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return ""
        # Lead timestamps carry a zone; media/feedback ones are naive UTC from the database.
        return moment.replace(tzinfo=moment.tzinfo or UTC).astimezone(IST).strftime("%H:%M")

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

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/dashboard")
    @app.get("/")
    def gallery():
        store = get_store()
        leads = store.list_leads()
        members = members_by_phone(store)
        media = {}
        for row in store.list_media():
            media.setdefault(row["lead_id"], []).append(row)
        cards = [lead_card(lead, media.get(lead.id, []), members) for lead in leads]
        tab = request.args.get("type")
        who = request.args.get("who") if request.args.get("who") in ("team", "visitors") else ""
        if who:
            cards = [card for card in cards if card["is_team"] == (who == "team")]
        if tab not in ("forms", "badges", "other"):
            tab = next((name for name in ("forms", "badges") if any(card["tab"] == name for card in cards)), "other")
        query = request.args.get("q", "").strip().lower()
        shown = [card for card in cards if card["tab"] == tab and (not query or query in card["search"])]
        return render_template("gallery.html", cards=shown, tab=tab, q=request.args.get("q", ""), who=who)

    @app.route("/leads/<int:lead_id>", methods=["GET", "POST"])
    def lead_detail(lead_id: int):
        store = get_store()
        lead = store.get_lead(lead_id)
        if not lead:
            abort(404)
        if request.method == "POST":
            values = {field: request.form.get(field, "").strip() for field, _label in EDITABLE_FIELDS}
            first, _, last = values["name"].partition(" ")  # the export reads first/last name and designation, so keep them in step with what was edited
            picked = request.form.getlist("curriculum")
            store.update_lead_fields(
                lead_id,
                **values,
                first_name=first, last_name=last.strip(), role=values["designation"],
                curriculum=", ".join(code for code in orbit.CURRICULUM_CHOICES if code in picked),
                notes=request.form.get("notes", "").strip(),
                organisation_type=org_type if (org_type := request.form.get("organisation_type", "")) in orbit.ORG_TYPES else "",
                interest=request.form.get("interest", "").strip(),
                doc_type=kind if (kind := request.form.get("doc_type")) in ("form", "badge") else "other",
                updated_by=g.user["name"],
                updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
                review_reason="",
            )
            flash("Saved.")
            return redirect(url_for("lead_detail", lead_id=lead_id))
        items = lead_items(lead, store, members_by_phone(store))
        return render_template(
            "lead.html", lead=lead, items=items, fields=EDITABLE_FIELDS, title=display_name(lead),
            org_types=orbit.ORG_TYPES, org_type=orbit.lead_org_type(lead),
            curricula=orbit.CURRICULUM_CHOICES, picked=orbit.curriculum_cell(lead.curriculum).split(", "),
        )

    @app.post("/leads/<int:lead_id>/resolve")
    def lead_resolve(lead_id: int):
        store = get_store()
        if not store.get_lead(lead_id):
            abort(404)
        store.update_lead_fields(lead_id, review_reason="", updated_by=g.user["name"], updated_at=datetime.now(UTC).isoformat(timespec="seconds"))
        flash("Marked as checked.")
        target = request.form.get("next", "")
        return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("gallery"))

    @app.post("/leads/<int:lead_id>/delete")
    def lead_delete(lead_id: int):
        require_admin()
        store = get_store()
        lead = store.get_lead(lead_id)
        if not lead:
            abort(404)
        store.update_lead_fields(lead_id, deleted_at=datetime.now(UTC).isoformat(timespec="seconds"), updated_by=g.user["name"])
        flash("Lead deleted.")
        return redirect(url_for("gallery"))

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
        rows, people = export_selection(get_store(), request.args)
        query = {key: value for key, value in request.args.items() if value and key in EXPORT_FILTERS}
        return render_template(
            "export.html", rows=rows, people=people, org_types=orbit.ORG_TYPES, query=query,
            orgs=orbit.org_count(rows), max_orgs=orbit.MAX_ORGS, args=request.args,
        )

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

    @app.post("/team/<int:user_id>/active")
    def team_active(user_id: int):
        require_admin()
        member = get_store().get_user(user_id) or abort(404)
        if member["id"] == g.user["id"]:
            flash("You can't deactivate yourself.")
        else:
            get_store().set_user_active(user_id, not member["is_active"])
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


EXPORT_FILTERS = ("from", "to", "by", "type")


def export_selection(store, args) -> tuple[list[dict], list[str]]:
    """Leads matching the export filters (capture date in IST, who captured it, organisation type) as Orbit rows
    and the capture names to offer in the filter."""
    start, end = parse_day(args.get("from")), parse_day(args.get("to"))
    by, kind = args.get("by", ""), args.get("type", "")
    members = members_by_phone(store)
    leads, people = [], set()
    for lead in store.list_leads():
        name = author(lead, members)[0]
        people.add(name)
        day = created_day(lead)
        if day and ((start and day < start) or (end and day > end)):
            continue
        if (by and name != by) or (kind and orbit.lead_org_type(lead) != kind):
            continue
        leads.append(lead)
    return [orbit.orbit_row(lead) for lead in leads], sorted(people)


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
        "tab": {"form": "forms", "badge": "badges"}.get(lead.doc_type, "other"),
        "name": name,
        "org": org,
        "phone": lead.phone,
        "kind": "image" if images else "audio" if has_audio else "text",
        "image_id": images[0]["id"] if images else None,
        "photo_count": len(images),
        "note": note,
        "review_reason": lead.review_reason,
        "by": by,
        "initials": initials,
        "is_team": is_team,
        "created_at": lead.created_at,
        "search": " ".join([name, org, lead.phone, lead.email, note]).lower(),
    }


def lead_items(lead, store, members: dict[str, str]) -> list[dict]:
    """Everything received for the lead (photos, voice notes, texts), oldest first."""
    by, _initials, is_team = author(lead, members)
    items = []
    media_rows = store.list_media(lead.id)
    for row in media_rows:
        content_type = row["content_type"] or ""
        kind = "image" if content_type.startswith("image/") else "audio" if content_type.startswith("audio/") else "file"
        items.append({"kind": kind, "id": row["id"], "text": row["extracted_text"], "by": by, "is_team": is_team, "created_at": row["created_at"]})
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
