"""Duplicate review: a reviewer compares the second-pass leads with the originals and records a verdict for each.
The verdicts live in their own table and never touch a lead; an admin reads them off later and fixes the leads."""
from __future__ import annotations

import csv
import io
import re
from difflib import SequenceMatcher

from flask import Response, abort, flash, g, redirect, render_template, request, url_for

from didac_app.dedupe import names_compatible
from didac_app.pager import paginate

REVIEWED = "Sahana"  # whose leads are checked against everyone else's
KINDS = {2: ("clear", "Clear duplicate"), 1: ("check", "Check by eye"), 0: ("new", "No match found")}
FIELDS = [("name", "Name"), ("org", "Organisation"), ("designation", "Designation"), ("phone", "Phone"), ("email", "Email"), ("city", "City")]


def reviewers(store) -> set[int]:
    """Staff an admin has given the page to, kept as ids in one setting."""
    return {int(part) for part in store.get_setting("dup_reviewers").split(",") if part.isdigit()}


def can_review(store, user) -> bool:
    return bool(user and (user["is_admin"] or user["id"] in reviewers(store)))


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\b(dr|mr|mrs|ms|prof|shri|smt|sri)\b\.?", "", (text or "").lower().replace(".", " "))).strip()


def ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0
    m = SequenceMatcher(None, a, b)
    return m.ratio() if m.real_quick_ratio() >= 0.7 and m.quick_ratio() >= 0.7 else 0  # the cheap bounds skip most pairs


def keys(lead) -> dict:
    return {"phone": re.sub(r"\D", "", lead.phone or "")[-10:], "email": (lead.email or "").lower().strip(), "name": clean(lead.name), "org": clean(lead.org), "raw": lead.name}


def score(a: dict, b: dict) -> tuple[int, str, int]:
    """(points, reasons, level) for how alike two leads are: 2 clear, 1 worth a look, 0 nothing."""
    phone = 4 if len(a["phone"]) >= 9 and a["phone"] == b["phone"] else 3 if len(a["phone"]) >= 9 and len(b["phone"]) >= 9 and ratio(a["phone"], b["phone"]) >= 0.88 else 0
    same_domain = a["email"] and b["email"] and a["email"].split("@")[-1] == b["email"].split("@")[-1]
    email = 4 if a["email"] and a["email"] == b["email"] else 2 if same_domain and ratio(a["email"].split("@")[0], b["email"].split("@")[0]) >= 0.85 else 0
    n, o = ratio(a["name"], b["name"]), ratio(a["org"], b["org"])
    name = 2 if n >= 0.85 else 1 if n >= 0.7 or (a["name"] and b["name"] and names_compatible(a["raw"], b["raw"]) and a["name"].split()[0] == b["name"].split()[0]) else 0
    org = 2 if o >= 0.85 else 1 if o >= 0.7 else 0
    why = [t for t in ("same phone" if phone == 4 else "phone differs by a digit" if phone else "", "same email" if email == 4 else "email nearly the same" if email else "",
                       "same name" if name == 2 else "similar name" if name else "", "same organisation" if org == 2 else "similar organisation" if org else "") if t]
    strong = ((phone >= 3 or email >= 4) and (name >= 1 or org >= 1)) or (name == 2 and org == 2)
    weak = phone >= 3 or email >= 4 or name == 2 or org == 2
    return phone + email + name + org, ", ".join(why), 2 if strong else 1 if weak else 0


def cases(leads: list) -> list[dict]:
    """Every lead of the reviewed person with its two likeliest originals, clearest first."""
    known = {lead.id: keys(lead) for lead in leads}
    out = []
    for lead in (lead for lead in leads if lead.captured_by == REVIEWED):
        scored = sorted(((score(known[lead.id], known[other.id]), other) for other in leads if other.id != lead.id), key=lambda pair: -pair[0][0])
        found = [{"points": s[0], "why": s[1], "level": s[2], "lead": other} for s, other in scored if s[2]][:2]
        out.append({"lead": lead, "level": found[0]["level"] if found else 0, "found": found})
    return sorted(out, key=lambda case: (-case["level"], case["lead"].event_no))


def register(app, get_store, get_config) -> None:
    @app.before_request
    def gate():
        if request.path.startswith("/dupes") and g.get("user") and not can_review(get_store(), g.user):
            abort(403)
        if request.path == "/dupes/export.csv" and g.get("user") and not g.user["is_admin"]:
            abort(403)

    def back():
        target = request.form.get("back", "")
        return redirect(target if target.startswith("/dupes") else url_for("dupes"))

    @app.get("/dupes")
    def dupes():
        store = get_store()
        event = store.live_event()
        leads = store.list_leads("event_id = %s", (event["id"],)) if event else []
        saved = store.dup_reviews()
        every = cases(leads)
        for case in every:
            row = saved.get(case["lead"].id) or {}
            case["verdict"], case["note"], case["matches"] = row.get("verdict", ""), row.get("note", ""), [int(i) for i in row.get("matches", "").split(",") if i]
        counts = {"todo": sum(1 for c in every if not c["verdict"]), "done": sum(1 for c in every if c["verdict"]), "all": len(every)}
        show = request.args.get("show") if request.args.get("show") in counts else "todo"
        kind = request.args.get("kind") if request.args.get("kind") in {k for k, _ in KINDS.values()} else ""
        shown = [c for c in every if (show == "all" or bool(c["verdict"]) == (show == "done")) and (not kind or KINDS[c["level"]][0] == kind)]
        pg = paginate(shown, request.args.get("page", 1, type=int), 10)
        by_id = {lead.id: lead for lead in leads}
        media = {}
        for row in store.list_media():
            if (row["content_type"] or "").startswith("image/"):
                media.setdefault(row["lead_id"], []).append(row)
        for case in pg["items"]:
            have = {c["lead"].id for c in case["found"]}
            case["found"] += [{"points": 0, "why": "added by hand", "level": 0, "lead": by_id[i]} for i in case["matches"] if i in by_id and i not in have]
            case["photos"] = {lead.id: media.get(lead.id, []) for lead in [case["lead"]] + [c["lead"] for c in case["found"]]}
            case["kind"] = KINDS[case["level"]]
            case["match_nos"] = [by_id[i].event_no for i in case["matches"] if i in by_id]
        return render_template("dupes.html", pg=pg, show=show, kind=kind, counts=counts, kinds=KINDS, fields=FIELDS, who=REVIEWED, event=event,
                               verdicts={v: sum(1 for c in every if c["verdict"] == v) for v in ("duplicate", "new", "unsure")},
                               by_kind={name: sum(1 for c in every if KINDS[c["level"]][0] == name) for name, _ in KINDS.values()})

    @app.post("/dupes/<int:lead_id>")
    def dupes_save(lead_id: int):
        store = get_store()
        lead = store.get_lead(lead_id) or abort(404)
        row = store.dup_reviews().get(lead_id) or {}
        matches = [int(i) for i in row.get("matches", "").split(",") if i]
        verdict, note = row.get("verdict", ""), request.form.get("note", row.get("note", "")).strip()[:500]
        act, other = request.form.get("act", ""), request.form.get("other", type=int)
        if act == "add":
            found = store.list_leads("event_id = %s AND event_no = %s", (lead.event_id, request.form.get("number", type=int) or 0))
            if not found or found[0].id == lead_id:
                flash("No other lead has that number.")
                return back()
            other, act = found[0].id, "match"
        if act == "match" and other and other != lead_id:
            matches, verdict = list(dict.fromkeys(matches + [other])), "duplicate"
        elif act == "unmatch" and other in matches:
            matches.remove(other)
            verdict = "duplicate" if matches else ""
        elif act in ("new", "unsure"):
            verdict, matches = act, [] if act == "new" else matches
        elif act == "clear":
            verdict, matches, note = "", [], ""
        store.save_dup_review(lead_id, verdict, matches, note, g.user["name"])
        return back()

    @app.get("/dupes/export.csv")
    def dupes_export():
        store = get_store()
        leads = {lead.id: lead for lead in store.list_leads()}
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["lead_no", "lead_name", "verdict", "same_person_as_no", "same_person_as_name", "note", "reviewed_by", "reviewed_at"])
        for lead_id, row in sorted(store.dup_reviews().items()):
            mine = leads.get(lead_id)
            others = [leads[int(i)] for i in row["matches"].split(",") if i and int(i) in leads]
            writer.writerow([mine.event_no if mine else lead_id, mine.name if mine else "", row["verdict"], ", ".join(str(o.event_no) for o in others), "; ".join(o.name for o in others), row["note"], row["reviewed_by"], row["at"]])
        return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=duplicate-review.csv"})

    @app.post("/team/<int:user_id>/dupes")
    def team_dupes(user_id: int):
        store = get_store()
        if not g.user["is_admin"]:
            abort(403)
        member = store.get_user(user_id) or abort(404)
        ids = reviewers(store) ^ {user_id}
        store.set_setting("dup_reviewers", ",".join(str(i) for i in sorted(ids)))
        flash(f"{member['name']} {'can now' if user_id in ids else 'can no longer'} use Duplicate review.")
        return redirect(url_for("team"))
