from __future__ import annotations

import atexit
import csv
import json
import re
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from didac_app.models import NOT_SCANNED, Lead


DOWN_AFTER = 5  # failed reads in a row before reading counts as paused
SLOW_MS = 20_000  # a read longer than this makes the next ones show the "a little slower" line
LEAD_FIELDS = [field.name for field in fields(Lead)]
NOT_DELETED = "(deleted_at IS NULL OR deleted_at = '')"
# For media/feedback joined to their lead; rows without a lead stay visible (LEFT JOIN gives NULL).
LEAD_NOT_DELETED = "(l.deleted_at IS NULL OR l.deleted_at = '')"


class LeadStore:
    def __init__(self, dsn: str):
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set")
        self.dsn = dsn
        # A warm instance keeps these connections, so most queries skip the TCP/TLS/login handshake. A connection Supabase dropped while idle is replaced on checkout.
        # prepare_threshold=None: Supabase's transaction pooler (pgbouncer) rejects prepared statements.
        self.pool = ConnectionPool(
            dsn, min_size=1, max_size=4, timeout=10, open=False, check=ConnectionPool.check_connection,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
        )
        self.pool.open()
        atexit.register(self.pool.close)  # else Python complains about the pool's worker threads at exit

    def connect(self):
        return self.pool.connection()  # same `with ... as conn` use as before: commits on success, rolls back on error, then hands the connection back

    def close(self) -> None:
        self.pool.close()

    def init_db(self) -> None:
        columns = ", ".join(
            f"{field} {column_type(field)}" for field in LEAD_FIELDS if field != "id"
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
                    conn.execute(f"ALTER TABLE leads ADD COLUMN IF NOT EXISTS {field} {column_type(field)}")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS events (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                "name TEXT NOT NULL, starts_on DATE, ends_on DATE, venue TEXT NOT NULL DEFAULT '', welcome_text TEXT NOT NULL DEFAULT '', "
                "status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'live', 'ended')))"
            )
            # At most one row can be 'live': the database refuses a second one, so no code path can break the rule.
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_one_live ON events (status) WHERE status = 'live'")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS doc_types (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE, "
                "key TEXT NOT NULL, label TEXT NOT NULL, hint TEXT NOT NULL DEFAULT '', reader TEXT NOT NULL DEFAULT 'document', "
                "position INTEGER NOT NULL DEFAULT 0, active BOOLEAN NOT NULL DEFAULT TRUE, UNIQUE (event_id, key))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS brochures (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE, "
                "key TEXT NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', for_visitors BOOLEAN NOT NULL DEFAULT TRUE, for_team BOOLEAN NOT NULL DEFAULT TRUE, "
                "position INTEGER NOT NULL DEFAULT 0, active BOOLEAN NOT NULL DEFAULT TRUE, UNIQUE (event_id, key))"
            )
            for column in ("file_key", "file_name"):  # a brochure is either a pasted link or a PDF kept in the bucket and sent as a document
                conn.execute(f"ALTER TABLE brochures ADD COLUMN IF NOT EXISTS {column} TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS event_photos (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), from_number TEXT NOT NULL DEFAULT '', uploaded_by TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, content_type TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '')"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS reader_status (id INTEGER PRIMARY KEY CHECK (id = 1), failures INTEGER NOT NULL DEFAULT 0, last_ms INTEGER NOT NULL DEFAULT 0, "
                "last_ok_at TIMESTAMPTZ, last_failure_at TIMESTAMPTZ, down_since TIMESTAMPTZ, last_reason TEXT NOT NULL DEFAULT '')"
            )
            conn.execute("INSERT INTO reader_status (id) VALUES (1) ON CONFLICT DO NOTHING")
            conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')")
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
                f"CREATE TABLE IF NOT EXISTS outbound_messages (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, created_at TEXT DEFAULT {now}, "
                "to_number TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', lead_ids TEXT NOT NULL DEFAULT '')"
            )
            conn.execute(f"CREATE TABLE IF NOT EXISTS reply_flags (lead_id BIGINT PRIMARY KEY, at TEXT NOT NULL DEFAULT {now})")  # a visitor texted this lead and nobody has answered yet
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
            conn.execute("ALTER TABLE media ADD COLUMN IF NOT EXISTS rotation INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE inbound_events ADD COLUMN IF NOT EXISTS message_id TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS inbound_events_message_id_key ON inbound_events (message_id)")

    # One row per phone ever: sent, or opted out. The primary key is what keeps it to one message per number.
    def followup_phones(self) -> set[str]:
        with self.connect() as conn:
            return {row["phone"] for row in conn.execute("SELECT phone FROM followups")}

    def followup_lead_id(self, phone: str) -> int | None:
        with self.connect() as conn:
            row = conn.execute("SELECT lead_id FROM followups WHERE phone = %s", (phone,)).fetchone()
        return row["lead_id"] if row else None

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

    def list_followups(self, limit: int = 1000, event_id: int | None = None) -> list[dict]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT f.phone, f.lead_id, f.sent_at, f.status, f.reason, l.name, l.org, l.captured_by FROM followups f LEFT JOIN leads l ON l.id = f.lead_id "
                "WHERE NOT f.opted_out AND (%s::bigint IS NULL OR l.event_id = %s) ORDER BY f.sent_at DESC LIMIT %s", (event_id, event_id, limit)).fetchall()

    def opted_out_phones(self) -> set[str]:
        with self.connect() as conn:
            return {row["phone"] for row in conn.execute("SELECT phone FROM followups WHERE opted_out")}

    def opt_out(self, phone: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO followups (phone, opted_out) VALUES (%s, TRUE) ON CONFLICT (phone) DO UPDATE SET opted_out = TRUE", (phone,))

    def clear(self) -> None:
        with self.connect() as conn:
            conn.execute("TRUNCATE inbound_events, outbound_messages, reply_flags, feedback, media, leads, sessions, event_photos RESTART IDENTITY")
            conn.execute("UPDATE reader_status SET failures=0, last_ms=0, last_ok_at=NULL, last_failure_at=NULL, down_since=NULL, last_reason=''")

    # ---- events: one is live at a time and every lead captured over WhatsApp goes into it -----------------------------
    def create_event(self, name: str, starts_on=None, ends_on=None, venue: str = "", welcome_text: str = "") -> int:
        with self.connect() as conn:
            return conn.execute("INSERT INTO events (name, starts_on, ends_on, venue, welcome_text) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                                (name.strip(), starts_on, ends_on, venue.strip(), welcome_text.strip())).fetchone()["id"]

    def get_event(self, event_id: int) -> dict | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM events WHERE id=%s", (event_id,)).fetchone()

    def live_event(self) -> dict | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM events WHERE status='live'").fetchone()

    def list_events(self) -> list[dict]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT e.*, (SELECT count(*) FROM leads l WHERE l.event_id = e.id AND (l.deleted_at IS NULL OR l.deleted_at = '')) AS lead_count FROM events e ORDER BY e.id DESC").fetchall()

    def update_event(self, event_id: int, **values) -> None:
        """name, starts_on, ends_on, venue and welcome_text only. Keys are code-owned, never request data."""
        assignments = ", ".join(f"{column}=%s" for column in values)
        with self.connect() as conn:
            conn.execute(f"UPDATE events SET {assignments} WHERE id=%s", [*values.values(), event_id])

    def start_event(self, event_id: int) -> None:
        """Make this event the live one. A live event is ended first, in the same transaction, so there is never a moment with two."""
        with self.connect() as conn:
            conn.execute("UPDATE events SET status='ended' WHERE status='live' AND id<>%s", (event_id,))
            conn.execute("UPDATE events SET status='live' WHERE id=%s", (event_id,))

    def stop_event(self, event_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE events SET status='ended' WHERE id=%s AND status='live'", (event_id,))

    # ---- document types and brochures belong to an event; deactivating hides them from WhatsApp but never deletes the data --------
    def list_doc_types(self, event_id: int, active_only: bool = False) -> list[dict]:
        with self.connect() as conn:
            return conn.execute(f"SELECT * FROM doc_types WHERE event_id=%s{' AND active' if active_only else ''} ORDER BY position, id", (event_id,)).fetchall()

    def add_doc_type(self, event_id: int, label: str, hint: str = "", reader: str = "document", key: str = "") -> int:
        key = key or unique_key(label, {row["key"] for row in self.list_doc_types(event_id)})
        with self.connect() as conn:
            position = conn.execute("SELECT COALESCE(max(position), 0) + 1 AS n FROM doc_types WHERE event_id=%s", (event_id,)).fetchone()["n"]
            return conn.execute("INSERT INTO doc_types (event_id, key, label, hint, reader, position) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                                (event_id, key, label.strip(), hint.strip(), reader, position)).fetchone()["id"]

    def update_doc_type(self, doc_type_id: int, **values) -> None:
        """label, hint, position only. Keys are code-owned, never request data."""
        assignments = ", ".join(f"{column}=%s" for column in values)
        with self.connect() as conn:
            conn.execute(f"UPDATE doc_types SET {assignments} WHERE id=%s", [*values.values(), doc_type_id])

    def set_doc_type_active(self, doc_type_id: int, active: bool) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE doc_types SET active=%s WHERE id=%s", (active, doc_type_id))

    def list_brochures(self, event_id: int, audience: str = "", active_only: bool = True) -> list[dict]:
        """audience is 'visitors', 'team' or '' for both."""
        where = "event_id=%s" + (" AND active" if active_only else "") + {"visitors": " AND for_visitors", "team": " AND for_team"}.get(audience, "")
        with self.connect() as conn:
            return conn.execute(f"SELECT * FROM brochures WHERE {where} ORDER BY position, id", (event_id,)).fetchall()

    def add_brochure(self, event_id: int, name: str, url: str = "", for_visitors: bool = True, for_team: bool = True, key: str = "", file_key: str = "", file_name: str = "") -> int:
        key = key or unique_key(name, {row["key"] for row in self.list_brochures(event_id, active_only=False)})
        with self.connect() as conn:
            position = conn.execute("SELECT COALESCE(max(position), 0) + 1 AS n FROM brochures WHERE event_id=%s", (event_id,)).fetchone()["n"]
            return conn.execute("INSERT INTO brochures (event_id, key, name, url, for_visitors, for_team, position, file_key, file_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                                (event_id, key, name.strip(), url.strip(), for_visitors, for_team, position, file_key, file_name)).fetchone()["id"]

    def update_brochure(self, brochure_id: int, **values) -> None:
        """name, url, file_key, file_name, for_visitors and for_team only. Keys are code-owned, never request data."""
        assignments = ", ".join(f"{column}=%s" for column in values)
        with self.connect() as conn:
            conn.execute(f"UPDATE brochures SET {assignments} WHERE id=%s", [*values.values(), brochure_id])

    def set_brochure_active(self, brochure_id: int, active: bool) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE brochures SET active=%s WHERE id=%s", (active, brochure_id))

    def copy_setup(self, from_event_id: int, to_event_id: int) -> None:
        """Copy the document types and brochures of one event into another (a new event starts from last time's setup)."""
        with self.connect() as conn:
            conn.execute("INSERT INTO doc_types (event_id, key, label, hint, reader, position, active) SELECT %s, key, label, hint, reader, position, active FROM doc_types WHERE event_id=%s",
                         (to_event_id, from_event_id))
            conn.execute("INSERT INTO brochures (event_id, key, name, url, for_visitors, for_team, position, active, file_key, file_name) SELECT %s, key, name, url, for_visitors, for_team, position, active, file_key, file_name FROM brochures WHERE event_id=%s",
                         (to_event_id, from_event_id))

    def ensure_default_event(self, brochures: list[tuple[str, str, str]]) -> int | None:
        """First run only: create the live DIDAC event with today's three document types and the given (key, name, url) brochures.
        Does nothing once any event exists."""
        if self.list_events():
            return None
        event_id = self.create_event("DIDAC 2026")
        self.start_event(event_id)
        for key, label, hint, reader in DEFAULT_DOC_TYPES:
            self.add_doc_type(event_id, label, hint, reader, key=key)
        for key, name, url in brochures:
            self.add_brochure(event_id, name, url, key=key)
        return event_id

    def add_event_photo(self, event_id: int, from_number: str, uploaded_by: str, key: str, content_type: str = "", url: str = "") -> int:
        with self.connect() as conn:
            return conn.execute("INSERT INTO event_photos (event_id, from_number, uploaded_by, key, content_type, url) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                                (event_id, from_number, uploaded_by, key, content_type, url)).fetchone()["id"]

    def list_event_photos(self, event_id: int | None = None) -> list[dict]:
        """Newest first; every event when event_id is None."""
        with self.connect() as conn:
            if event_id is None:
                return conn.execute("SELECT p.*, e.name AS event_name FROM event_photos p JOIN events e ON e.id = p.event_id ORDER BY p.id DESC").fetchall()
            return conn.execute("SELECT p.*, e.name AS event_name FROM event_photos p JOIN events e ON e.id = p.event_id WHERE p.event_id=%s ORDER BY p.id DESC", (event_id,)).fetchall()

    def get_event_photo(self, photo_id: int) -> dict | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM event_photos WHERE id=%s", (photo_id,)).fetchone()

    # ---- the reading service's health: paused after DOWN_AFTER failures in a row, back on the first success ------------------------
    def record_read_ok(self, ms: int = 0) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE reader_status SET failures=0, last_ms=%s, last_ok_at=now(), down_since=NULL, last_reason='' WHERE id=1", (int(ms),))

    def record_read_failure(self, reason: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE reader_status SET failures=failures+1, last_failure_at=now(), last_reason=%s, "
                         "down_since=CASE WHEN failures+1 >= %s AND down_since IS NULL THEN now() ELSE down_since END WHERE id=1", (reason, DOWN_AFTER))

    def reader_status(self) -> dict:
        """{state: ok|slow|down, reason, since, ref, waiting (leads saved unread)}. Slow means the last read took over SLOW_MS."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM reader_status WHERE id=1").fetchone()
            waiting = conn.execute("SELECT count(*) AS n FROM leads WHERE review_reason=%s AND (deleted_at IS NULL OR deleted_at = '')", (NOT_SCANNED,)).fetchone()["n"]
        state = "down" if row["failures"] >= DOWN_AFTER else "slow" if row["last_ms"] > SLOW_MS else "ok"
        since = row["down_since"] or row["last_failure_at"]
        return {"state": state, "reason": row["last_reason"], "since": row["down_since"], "waiting": waiting, "failures": row["failures"],
                "ref": f"RD-{int(since.timestamp()) % 65536:04X}" if since else ""}

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))

    def create_lead(self, lead: Lead) -> Lead:
        if lead.event_id is None and (live := self.live_event()):
            lead.event_id = live["id"]
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

    def update_user(self, user_id: int, name: str, phone: str, email: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE users SET name=%s, phone=%s, email=%s WHERE id=%s", (name, phone, email or None, user_id))

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

    def rotate_media(self, media_id: int, degrees: int) -> int:
        """Turn the photo's saved view by `degrees` (+90 clockwise, -90 anticlockwise); returns the new angle. The stored file itself is never changed."""
        with self.connect() as conn:
            return conn.execute("UPDATE media SET rotation = ((rotation + %s) %% 360 + 360) %% 360 WHERE id = %s RETURNING rotation", (degrees, media_id)).fetchone()["rotation"]

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

    def flag_reply(self, lead_id: int) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO reply_flags (lead_id) VALUES (%s) ON CONFLICT (lead_id) DO UPDATE SET at = to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')", (lead_id,))

    def clear_reply(self, lead_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM reply_flags WHERE lead_id = %s", (lead_id,))

    def reply_flags(self) -> dict[int, str]:
        """{lead id: when the visitor last texted} for the leads still waiting for an answer."""
        with self.connect() as conn:
            return {row["lead_id"]: row["at"] for row in conn.execute("SELECT f.lead_id, f.at FROM reply_flags f JOIN leads l ON l.id = f.lead_id WHERE (l.deleted_at IS NULL OR l.deleted_at = '')")}

    def log_outbound(self, to_number: str, body: str, lead_ids: list[int] | None = None) -> None:
        """What we sent, kept next to inbound_events so a lead can show the whole chat."""
        with self.connect() as conn:
            conn.execute("INSERT INTO outbound_messages (to_number, body, lead_ids) VALUES (%s, %s, %s)", (to_number, body, ",".join(str(item) for item in lead_ids or [])))

    def chat_with(self, number: str, lead_id: int, since: datetime | None = None, until: datetime | None = None) -> list[dict]:
        """One WhatsApp chat, both directions, oldest first: everything to or from `number`. With since/until only that stretch of it,
        plus any exchange tagged with the lead (a team member's chat covers many leads). Inside the stretch, an exchange tagged with some other lead
        (a note, photo or voice note sent for the previous lead) is left out. Rows are {direction 'in'|'out', created_at, body, num_media}."""
        numbers = [number, f"whatsapp:{number}"]
        window = "(created_at::timestamptz > %s AND created_at::timestamptz <= %s AND COALESCE(lead_ids, '') = '' OR %s = ANY(string_to_array(lead_ids, ',')))" if since else "TRUE"
        extra = [since, until, str(lead_id)] if since else []
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM ("
                f"SELECT 'in' AS direction, id, created_at, body, num_media FROM inbound_events WHERE from_number = ANY(%s) AND {window} "
                "UNION ALL "
                f"SELECT 'out', id, created_at, body, 0 FROM outbound_messages WHERE to_number = ANY(%s) AND {window}"
                ") t ORDER BY created_at, direction, id",
                (numbers, *extra, numbers, *extra),
            ).fetchall()

    def previous_lead_time(self, from_number: str, lead_id: int) -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT max(created_at) AS at FROM leads WHERE from_number = %s AND id < %s", (from_number, lead_id)).fetchone()
        return row["at"] or ""

    def list_inbound_events(self) -> list[dict]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM inbound_events ORDER BY id DESC").fetchall()

    def export_excel(self, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        leads = list(reversed(self.list_leads()))
        wb = Workbook()
        ws = wb.active
        ws.title = "Leads"
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


def column_type(field: str) -> str:
    return "BIGINT" if field == "event_id" else "TEXT"


# (key, label, hint, reader). The key is what a lead stores in doc_type; reader says how the photo is read: form, document or badge (QR).
DEFAULT_DOC_TYPES = [
    ("form", "Contact form", "The printed Xperimentor form", "form"),
    ("other", "Other docs", "Visiting cards, flyers, brochures and all that", "document"),
    ("badge", "Event badge / QR", "Badge or ID card with a QR code", "badge"),
]
RESERVED_KEYS = {"note", "choose", "requirement", "brochure", "feedback"}  # session flow names a document type key must never collide with


def unique_key(label: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "type"
    key, n = base, 2
    while key in taken or key in RESERVED_KEYS:
        key, n = f"{base}_{n}", n + 1
    return key


def row_to_lead(row) -> Lead:
    data = {field: (row[field] if row[field] is not None else ("" if field != "event_id" else None)) for field in LEAD_FIELDS if field in row.keys()}
    data["id"] = row["id"]
    return Lead(**data)
