from __future__ import annotations

import hmac
import secrets
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from flask import abort, flash, g, jsonify, redirect, render_template, request, session, url_for

from didac_app import auth

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
        if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf", ""), csrf_token()):
            abort(400)
        user_id = session.get("uid")
        user = get_store().get_user(user_id) if user_id else None
        g.user = user if user and user["is_active"] else None
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
            found = auth.authenticate(get_store(), request.form.get("login", ""), request.form.get("password", ""))
            if found:
                session.clear()
                session["uid"] = found["id"]
                session.permanent = True
                return redirect(safe_next(request.args.get("next")))
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
        if tab not in ("forms", "other"):
            tab = "forms" if any(card["is_form"] for card in cards) else "other"
        query = request.args.get("q", "").strip().lower()
        shown = [card for card in cards if card["is_form"] == (tab == "forms") and (not query or query in card["search"])]
        return render_template("gallery.html", cards=shown, tab=tab, q=request.args.get("q", ""))

    @app.route("/leads/<int:lead_id>", methods=["GET", "POST"])
    def lead_detail(lead_id: int):
        store = get_store()
        lead = store.get_lead(lead_id)
        if not lead:
            abort(404)
        if request.method == "POST":
            for field, _label in EDITABLE_FIELDS:
                setattr(lead, field, request.form.get(field, "").strip())
            lead.interest = request.form.get("interest", "").strip()
            lead.doc_type = "form" if request.form.get("doc_type") == "form" else "other"
            lead.updated_by = g.user["name"]
            lead.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
            lead.review_reason = ""
            store.update_lead(lead)
            flash("Saved.")
            return redirect(url_for("lead_detail", lead_id=lead_id))
        items = lead_items(lead, store, members_by_phone(store))
        return render_template("lead.html", lead=lead, items=items, fields=EDITABLE_FIELDS, title=display_name(lead))

    @app.post("/leads/<int:lead_id>/delete")
    def lead_delete(lead_id: int):
        require_admin()
        store = get_store()
        lead = store.get_lead(lead_id)
        if not lead:
            abort(404)
        lead.deleted_at = datetime.now(UTC).isoformat(timespec="seconds")
        lead.updated_by = g.user["name"]
        store.update_lead(lead)
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


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def safe_next(target: str | None) -> str:
    return target if target and target.startswith("/") and not target.startswith("//") else url_for("gallery")


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
        "is_form": lead.doc_type == "form",
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
    for row in store.list_media():
        if row["lead_id"] != lead.id:
            continue
        content_type = row["content_type"] or ""
        kind = "image" if content_type.startswith("image/") else "audio" if content_type.startswith("audio/") else "file"
        items.append({"kind": kind, "id": row["id"], "text": row["extracted_text"], "by": by, "is_team": is_team, "created_at": row["created_at"]})
    for row in store.list_feedback():
        if row["lead_id"] != lead.id or row["feedback_type"] in DERIVED_FEEDBACK:
            continue
        name = members.get(auth.normalize_phone(row["from_number"]))
        items.append({"kind": "text", "text": row["body"], "by": name or "Visitor via WhatsApp", "is_team": bool(name), "created_at": row["created_at"]})
    return sorted(items, key=lambda item: item["created_at"])
