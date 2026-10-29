from __future__ import annotations

import csv
import json
from dataclasses import asdict, fields
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from didac_app.models import Lead


LEAD_FIELDS = [field.name for field in fields(Lead)]
INTEGER_FIELDS = {"score"}
NOT_DELETED = "(deleted_at IS NULL OR deleted_at = '')"


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
            f"{field} {'INTEGER' if field in INTEGER_FIELDS else 'TEXT'}" for field in LEAD_FIELDS if field != "id"
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
                    column_type = "INTEGER" if field in INTEGER_FIELDS else "TEXT"
                    conn.execute(f"ALTER TABLE leads ADD COLUMN IF NOT EXISTS {field} {column_type}")
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
                    role TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_phone_key ON users (phone) WHERE phone IS NOT NULL")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_key ON users (email) WHERE email IS NOT NULL")
            conn.execute("ALTER TABLE inbound_events ADD COLUMN IF NOT EXISTS message_id TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS inbound_events_message_id_key ON inbound_events (message_id)")

    def clear(self) -> None:
        with self.connect() as conn:
            conn.execute("TRUNCATE inbound_events, feedback, media, leads RESTART IDENTITY")

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
            conn.execute("UPDATE users SET password_hash=%s WHERE id=%s", (password_hash, user_id))

    def list_leads(self, where: str = "", params: tuple = ()) -> list[Lead]:
        query = f"SELECT * FROM leads WHERE {NOT_DELETED}"
        if where:
            query += f" AND ({where})"
        query += " ORDER BY id DESC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row_to_lead(row) for row in rows]

    def count_by_priority(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT priority, COUNT(*) AS count FROM leads GROUP BY priority").fetchall()
        return {row["priority"] or "Unknown": row["count"] for row in rows}

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

    def list_feedback(self) -> list[dict]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM feedback ORDER BY id DESC").fetchall()

    def list_media(self) -> list[dict]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM media ORDER BY id DESC").fetchall()

    def get_media(self, media_id: int) -> dict | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM media WHERE id=%s", (media_id,)).fetchone()

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
