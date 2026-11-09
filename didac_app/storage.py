from __future__ import annotations

import csv
import json
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from didac_app.models import Lead


LEAD_FIELDS = [field.name for field in fields(Lead)]
NOT_DELETED = "(deleted_at IS NULL OR deleted_at = '')"
# For media/feedback joined to their lead; rows without a lead stay visible (LEFT JOIN gives NULL).
LEAD_NOT_DELETED = "(l.deleted_at IS NULL OR l.deleted_at = '')"


class LeadStore:
    def __init__(self, dsn: str):
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set")
        self.dsn = dsn
        self.init_db()

    def connect(self):
        # prepare_threshold=None: Supabase's transaction pooler (pgbouncer) rejects prepared statements.
        return psycopg.connect(self.dsn, row_factory=dict_row, prepare_threshold=None)

    def init_db(self) -> None:
        columns = ", ".join(
            f"{field} TEXT" for field in LEAD_FIELDS if field != "id"
        )
        now = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
        with self.connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS leads (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    {columns}
                )
                """
            )
            for field in LEAD_FIELDS:
                if field != "id":
                    conn.execute(f"ALTER TABLE leads ADD COLUMN IF NOT EXISTS {field} TEXT")
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS media (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    lead_id BIGINT REFERENCES leads(id),
                    created_at TEXT DEFAULT {now},
                    url TEXT,
                    content_type TEXT,
                    local_path TEXT,
                    extracted_text TEXT
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS feedback (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    created_at TEXT DEFAULT {now},
                    from_number TEXT,
                    lead_id BIGINT REFERENCES leads(id),
                    feedback_type TEXT,
                    body TEXT,
                    media_id BIGINT REFERENCES media(id),
                    sentiment TEXT,
                    action_required TEXT
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS inbound_events (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    created_at TEXT DEFAULT {now},
                    from_number TEXT,
                    body TEXT,
                    num_media INTEGER,
                    media_urls TEXT,
                    status TEXT,
                    lead_ids TEXT,
                    error TEXT,
                    raw_payload TEXT
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    created_at TEXT DEFAULT {now},
                    name TEXT NOT NULL,
                    phone TEXT,
                    email TEXT,
                    password_hash TEXT NOT NULL,
                    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    session_version INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS role")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS login_attempts (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                "at TIMESTAMPTZ NOT NULL DEFAULT now(), login TEXT NOT NULL, ip TEXT NOT NULL)"
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_phone_key ON users (phone) WHERE phone IS NOT NULL")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_key ON users (email) WHERE email IS NOT NULL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions (phone TEXT PRIMARY KEY, flow TEXT NOT NULL, step TEXT NOT NULL, "
                "data JSONB NOT NULL DEFAULT '{}', updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS followups (phone TEXT PRIMARY KEY, lead_id BIGINT, sent_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                "opted_out BOOLEAN NOT NULL DEFAULT FALSE)"
            )
            for column in ("message_id", "status", "reason"):
                conn.execute(f"ALTER TABLE followups ADD COLUMN IF NOT EXISTS {column} TEXT")
            # Leads left on the old text-status conversation steps go back to plain New.
            conn.execute("UPDATE leads SET status='New' WHERE status ~ '^(Menu|Intake|Brochure|Feedback|Badge):'")
            conn.execute("ALTER TABLE inbound_events ADD COLUMN IF NOT EXISTS message_id TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS inbound_events_message_id_key ON inbound_events (message_id)")

    # One row per phone ever: sent, or opted out. The primary key is what keeps it to one message per number.
    def followup_phones(self) -> set[str]:
        with self.connect() as conn:
            return {row["phone"] for row in conn.execute("SELECT phone FROM followups")}

    def followups_last_day(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT count(*) AS n FROM followups WHERE NOT opted_out AND sent_at > now() - interval '24 hours'").fetchone()["n"]

    def claim_followup(self, phone: str, lead_id: int) -> bool:
        with self.connect() as conn:
            return conn.execute("INSERT INTO followups (phone, lead_id) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING phone", (phone, lead_id)).fetchone() is not None

    def release_followup(self, phone: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM followups WHERE phone = %s AND NOT opted_out", (phone,))

    def set_followup_message(self, phone: str, message_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE followups SET message_id = %s, status = 'accepted' WHERE phone = %s", (message_id, phone))

    def update_followup_status(self, phone: str, status: str, reason: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE followups SET status = %s, reason = %s WHERE phone = %s", (status, reason, phone))

    def pending_followups(self, limit: int) -> list[dict]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT phone, message_id FROM followups WHERE message_id IS NOT NULL AND status NOT IN ('delivered', 'read', 'delivery_failed', 'sending_failed') "
                "ORDER BY sent_at LIMIT %s", (limit,)).fetchall()

    def list_followups(self, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT f.phone, f.lead_id, f.sent_at, f.status, f.reason, l.name, l.org FROM followups f LEFT JOIN leads l ON l.id = f.lead_id "
                "WHERE NOT f.opted_out ORDER BY f.sent_at DESC LIMIT %s", (limit,)).fetchall()

    def opt_out(self, phone: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO followups (phone, opted_out) VALUES (%s, TRUE) ON CONFLICT (phone) DO UPDATE SET opted_out = TRUE", (phone,))

    def clear(self) -> None:
        with self.connect() as conn:
            conn.execute("TRUNCATE inbound_events, feedback, media, leads, sessions RESTART IDENTITY")

    def create_lead(self, lead: Lead) -> Lead:
        data = asdict(lead)
        data.pop("id", None)
        columns = list(data.keys())
        placeholders = ", ".join("%s" for _ in columns)
        with self.connect() as conn:
            lead.id = conn.execute(
                f"INSERT INTO leads ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id",
                [data[column] for column in columns],
            ).fetchone()["id"]
        return lead

    def update_lead(self, lead: Lead) -> Lead:
        if not lead.id:
            return self.create_lead(lead)
        data = asdict(lead)
        data.pop("id", None)
        assignments = ", ".join(f"{column}=%s" for column in data)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE leads SET {assignments} WHERE id=%s",
                [*data.values(), lead.id],
            )
        return lead

    def update_lead_fields(self, lead_id: int, **values) -> None:
        """UPDATE only the given columns so a UI save cannot overwrite what the bot wrote meanwhile. Keys are code-owned, never request data."""
        assignments = ", ".join(f"{column}=%s" for column in values)
        with self.connect() as conn:
            conn.execute(f"UPDATE leads SET {assignments} WHERE id=%s", [*values.values(), lead_id])

    def merge_into_lead(self, lead_id: int, fill: dict[str, str], append: dict[str, str]) -> None:
        """One UPDATE that fills only still-blank columns and appends to '; '-lists, so a dashboard edit or a parallel scan is not overwritten.
        Keys are code-owned, never request data."""
        sets, params = [], []
        for column, value in fill.items():
            sets.append(f"{column}=COALESCE(NULLIF({column}, ''), %s)")
            params.append(value)
        for column, value in append.items():
            sets.append(
                f"{column}=CASE WHEN COALESCE({column}, '')='' THEN %s WHEN %s = ANY(string_to_array({column}, '; ')) THEN {column} ELSE {column} || '; ' || %s END"
            )
            params += [value, value, value]
        sets.append("updated_at=%s")
        params.append(datetime.now(UTC).isoformat(timespec="seconds"))
        with self.connect() as conn:
            conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=%s", [*params, lead_id])

    def latest_lead_for_number(self, from_number: str) -> Lead | None:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM leads WHERE from_number=%s AND {NOT_DELETED} ORDER BY id DESC LIMIT 1",
                (from_number,),
            ).fetchone()
        return row_to_lead(row) if row else None

    def get_lead(self, lead_id: int) -> Lead | None:
        with self.connect() as conn:
            row = conn.execute(f"SELECT * FROM leads WHERE id=%s AND {NOT_DELETED}", (lead_id,)).fetchone()
        return row_to_lead(row) if row else None

    def create_user(self, name: str, phone: str, email: str, password_hash: str, is_admin: bool = False) -> int:
        """phone/email are normalised by the caller; empty values are stored as NULL so they can stay unique."""
        with self.connect() as conn:
            return conn.execute(
                "INSERT INTO users (name, phone, email, password_hash, is_admin) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (name, phone or None, email or None, password_hash, is_admin),
            ).fetchone()["id"]

    def get_user(self, user_id: int) -> dict | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM users WHERE id=%s", (user_id,)).fetchone()

    def find_user(self, phone: str = "", email: str = "") -> dict | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE (phone IS NOT NULL AND phone=%s) OR (email IS NOT NULL AND email=%s) LIMIT 1",
                (phone, email),
            ).fetchone()

    def list_users(self) -> list[dict]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM users ORDER BY id").fetchall()

    def set_user_active(self, user_id: int, active: bool) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE users SET is_active=%s WHERE id=%s", (active, user_id))

    def set_user_password(self, user_id: int, password_hash: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE users SET password_hash=%s, session_version=session_version+1 WHERE id=%s", (password_hash, user_id))

    def record_failed_login(self, login: str, ip: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM login_attempts WHERE at < now() - interval '1 day'")
            conn.execute("INSERT INTO login_attempts (login, ip) VALUES (%s, %s)", (login, ip))

    def count_failed_logins(self, login: str, ip: str, minutes: int) -> tuple[int, int]:
        """(failures for this login, failures from this ip) in the last `minutes`."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FILTER (WHERE login=%s) AS by_login, COUNT(*) FILTER (WHERE ip=%s) AS by_ip FROM login_attempts "
                "WHERE at >= now() - make_interval(mins => %s)",
                (login, ip, minutes),
            ).fetchone()
        return row["by_login"], row["by_ip"]

    def get_session(self, phone: str) -> dict | None:
        """The phone's session with `age_minutes`; the caller decides whether it is too old."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT *, EXTRACT(EPOCH FROM (now() - updated_at)) / 60 AS age_minutes FROM sessions WHERE phone=%s", (phone,)
            ).fetchone()

    def put_session(self, phone: str, flow: str, step: str, data: dict, keep_existing: bool = False) -> None:
        """Create or replace the phone's session; keep_existing leaves a session that is already there (a parallel message created it first)."""
        conflict = "DO NOTHING" if keep_existing else "DO UPDATE SET flow=EXCLUDED.flow, step=EXCLUDED.step, data=EXCLUDED.data, updated_at=now()"
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO sessions (phone, flow, step, data) VALUES (%s, %s, %s, %s) ON CONFLICT (phone) {conflict}",
                (phone, flow, step, json.dumps(data)),
            )

    def delete_session(self, phone: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE phone=%s", (phone,))

    def add_session_photo(self, phone: str, photo: dict, limit: int) -> int | None:
        """Append one photo in a single UPDATE (photos of one batch arrive as parallel webhooks). Returns the photo count, or None when full or no longer collecting."""
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE sessions SET data = jsonb_set(data, '{photos}', COALESCE(data->'photos', '[]'::jsonb) || %s::jsonb), updated_at=now() "
                "WHERE phone=%s AND step IN ('photos', 'kind') AND jsonb_array_length(COALESCE(data->'photos', '[]'::jsonb)) < %s "
                "RETURNING jsonb_array_length(data->'photos') AS n",
                (json.dumps([photo]), phone, limit),
            ).fetchone()
        return row["n"] if row else None

    def list_leads(self, where: str = "", params: tuple = ()) -> list[Lead]:
        query = f"SELECT * FROM leads WHERE {NOT_DELETED}"
        if where:
            query += f" AND ({where})"
        query += " ORDER BY id DESC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row_to_lead(row) for row in rows]

    def add_media(self, lead_id: int, url: str, content_type: str = "", local_path: str = "", extracted_text: str = "") -> None:
        with self.connect() as conn:
            return conn.execute(
                "INSERT INTO media (lead_id, url, content_type, local_path, extracted_text) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (lead_id, url, content_type, local_path, extracted_text),
            ).fetchone()["id"]

    def add_feedback(
        self,
        from_number: str,
        body: str,
        lead_id: int | None = None,
        feedback_type: str = "general",
        media_id: int | None = None,
        sentiment: str = "",
        action_required: str = "",
    ) -> int:
        with self.connect() as conn:
            return conn.execute(
                """
                INSERT INTO feedback
                (from_number, lead_id, feedback_type, body, media_id, sentiment, action_required)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (from_number, lead_id, feedback_type, body, media_id, sentiment, action_required),
            ).fetchone()["id"]

    def list_feedback(self, lead_id: int | None = None) -> list[dict]:
        return self._of_live_leads("feedback", lead_id)

    def list_media(self, lead_id: int | None = None) -> list[dict]:
        return self._of_live_leads("media", lead_id)

    def get_media(self, media_id: int) -> dict | None:
        with self.connect() as conn:
            return conn.execute(
                f"SELECT m.* FROM media m LEFT JOIN leads l ON l.id = m.lead_id WHERE m.id=%s AND {LEAD_NOT_DELETED}", (media_id,)
            ).fetchone()

    def _of_live_leads(self, table: str, lead_id: int | None) -> list[dict]:
        query = f"SELECT t.* FROM {table} t LEFT JOIN leads l ON l.id = t.lead_id WHERE {LEAD_NOT_DELETED}"
        with self.connect() as conn:
            if lead_id is None:
                return conn.execute(f"{query} ORDER BY t.id DESC").fetchall()
            return conn.execute(f"{query} AND t.lead_id=%s ORDER BY t.id DESC", (lead_id,)).fetchall()

    def add_inbound_event(
        self,
        from_number: str,
        body: str,
        num_media: int,
        media_urls: list[str],
        raw_payload: dict,
        status: str = "received",
        message_id: str = "",
    ) -> int | None:
        """Returns the new event id, or None if this message_id was already recorded (a webhook retry)."""
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO inbound_events
                (from_number, body, num_media, media_urls, status, lead_ids, error, raw_payload, message_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (message_id) DO NOTHING
                RETURNING id
                """,
                (
                    from_number,
                    body,
                    num_media,
                    json.dumps(media_urls),
                    status,
                    "",
                    "",
                    json.dumps(raw_payload, ensure_ascii=False),
                    message_id or None,
                ),
            ).fetchone()
            return row["id"] if row else None

    def update_inbound_event(self, event_id: int | None, status: str, lead_ids: list[int] | None = None, error: str = "") -> None:
        if not event_id:
            return
        with self.connect() as conn:
            conn.execute(
                "UPDATE inbound_events SET status=%s, lead_ids=%s, error=%s WHERE id=%s",
                (status, ",".join(str(item) for item in lead_ids or []), error, event_id),
            )

    def list_inbound_events(self) -> list[dict]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM inbound_events ORDER BY id DESC").fetchall()

    def export_excel(self, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        leads = list(reversed(self.list_leads()))
        wb = Workbook()
        ws = wb.active
        ws.title = "DIDAC Leads"
        headers = [field for field in LEAD_FIELDS if field != "raw_message"]
        ws.append(headers)
        for lead in leads:
            data = asdict(lead)
            ws.append([data.get(header, "") for header in headers])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        header_fill = PatternFill("solid", fgColor="D9EAF7")
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = header_fill
        for column_cells in ws.columns:
            width = 12
            for cell in column_cells[:30]:
                if cell.value:
                    width = min(max(width, len(str(cell.value)) + 2), 55)
            ws.column_dimensions[get_column_letter(column_cells[0].column)].width = width
        wb.save(output_path)
        return output_path

    def export_report_excel(self, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        wb = Workbook()
        leads = list(reversed(self.list_leads()))
        ws = wb.active
        ws.title = "Leads"
        headers = [field for field in LEAD_FIELDS if field != "raw_message"]
        ws.append(headers)
        for lead in leads:
            data = asdict(lead)
            ws.append([data.get(header, "") for header in headers])
        feedback_ws = wb.create_sheet("Feedback")
        feedback_headers = ["id", "created_at", "from_number", "lead_id", "feedback_type", "body", "media_id", "sentiment", "action_required"]
        feedback_ws.append(feedback_headers)
        for row in reversed(self.list_feedback()):
            feedback_ws.append([row[header] for header in feedback_headers])
        media_ws = wb.create_sheet("Media")
        media_headers = ["id", "lead_id", "created_at", "url", "content_type", "local_path", "extracted_text"]
        media_ws.append(media_headers)
        for row in reversed(self.list_media()):
            media_ws.append([row[header] for header in media_headers])
        inbound_ws = wb.create_sheet("Inbound Events")
        inbound_headers = ["id", "created_at", "from_number", "body", "num_media", "media_urls", "status", "lead_ids", "error"]
        inbound_ws.append(inbound_headers)
        for row in reversed(self.list_inbound_events()):
            inbound_ws.append([row[header] for header in inbound_headers])
        for sheet in wb.worksheets:
            format_sheet(sheet)
        wb.save(output_path)
        return output_path

    def export_report_csv(self, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        headers = [field for field in LEAD_FIELDS if field != "raw_message"]
        leads = list(reversed(self.list_leads()))
        with output_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(headers)
            for lead in leads:
                data = asdict(lead)
                writer.writerow([data.get(header, "") for header in headers])
        return output_path


def format_sheet(ws) -> None:
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    header_fill = PatternFill("solid", fgColor="D9EAF7")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
    for column_cells in ws.columns:
        width = 12
        for cell in column_cells[:30]:
            if cell.value:
                width = min(max(width, len(str(cell.value)) + 2), 60)
        ws.column_dimensions[get_column_letter(column_cells[0].column)].width = width


def row_to_lead(row) -> Lead:
    data = {field: (row[field] if row[field] is not None else "") for field in LEAD_FIELDS if field in row.keys()}
    data["id"] = row["id"]
    return Lead(**data)
