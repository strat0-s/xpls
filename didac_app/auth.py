from __future__ import annotations

import phonenumbers
import psycopg
from werkzeug.security import check_password_hash, generate_password_hash

from didac_app.storage import LeadStore

MIN_PASSWORD_LENGTH = 8
# Checked against when the login is unknown so a wrong user and a wrong password take the same time.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def normalize_phone(value: str) -> str:
    """E.164 (+91...) for a valid number, else an empty string. Bare numbers are read as Indian."""
    try:
        parsed = phonenumbers.parse(value.replace("whatsapp:", "").strip(), "IN")
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(parsed):
        return ""
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    local, _, domain = email.partition("@")
    return email if local and "." in domain else ""


def authenticate(store: LeadStore, login: str, password: str) -> dict | None:
    """Log in with a phone number or an email. Deactivated users never authenticate."""
    login = login.strip()
    phone, email = ("", normalize_email(login)) if "@" in login else (normalize_phone(login), "")
    user = store.find_user(phone, email) if phone or email else None
    password_ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
    return user if user and password_ok and user["is_active"] else None


def add_user(store: LeadStore, name: str, phone: str, email: str, password: str, is_admin: bool = False) -> int:
    """Validate and create a user. Raises ValueError with a message that is safe to show."""
    name = name.strip()
    phone_e164 = normalize_phone(phone) if phone.strip() else ""
    email_clean = normalize_email(email) if email.strip() else ""
    if not name:
        raise ValueError("Name is required.")
    if not phone_e164:
        raise ValueError("Enter a valid phone number.")
    if email.strip() and not email_clean:
        raise ValueError("Enter a valid email or leave it empty.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    try:
        return store.create_user(name, phone_e164, email_clean, generate_password_hash(password), is_admin)
    except psycopg.errors.UniqueViolation:
        raise ValueError("Someone with that phone number or email already exists.") from None


def change_password(store: LeadStore, user_id: int, password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    store.set_user_password(user_id, generate_password_hash(password))
