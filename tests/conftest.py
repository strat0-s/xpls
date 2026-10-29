import os

# Set before didac_app is imported so tests never touch the real DATABASE_URL from .env.
os.environ["DATABASE_URL"] = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:54329/didac_test"
)

os.environ["BIRD_WEBHOOK_TOKEN"] = "test-token"
os.environ["SECRET_KEY"] = "test-secret-key"

import pytest
from flask.testing import FlaskClient

from didac_app import auth, bird
from didac_app.storage import LeadStore


@pytest.fixture
def store():
    store = LeadStore(os.environ["DATABASE_URL"])
    store.clear()
    with store.connect() as conn:
        conn.execute("TRUNCATE users RESTART IDENTITY")  # clear() leaves users alone so reset-sample can't lock everyone out
    return store


@pytest.fixture(autouse=True)
def isolated_store(store, monkeypatch):
    import didac_app.server as server

    monkeypatch.setattr(server, "store", store)


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """Capture replies the app sends through Bird instead of calling the API."""
    messages = []
    monkeypatch.setattr(bird, "send_message", lambda config, to_number, text: messages.append((to_number, text)))
    return messages


ADMIN = {"name": "Test Admin", "phone": "+919800000001", "email": "admin@example.com", "password": "correct-horse-battery"}


def get_or_create_user(store, name, phone, email, password, is_admin=False):
    existing = store.find_user(auth.normalize_phone(phone), email)
    return existing or store.get_user(auth.add_user(store, name, phone, email, password, is_admin))


def csrf(client) -> str:
    with client.session_transaction() as sess:
        return sess.setdefault("csrf", "test-csrf")


@pytest.fixture
def admin(store):
    return get_or_create_user(store, **ADMIN, is_admin=True)


@pytest.fixture(autouse=True)
def signed_in(store, admin, monkeypatch):
    """app.test_client() returns a client already signed in as an admin; use anon_client for logged-out checks."""
    import didac_app.server as server

    class SignedIn(FlaskClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            with self.session_transaction() as sess:
                sess["uid"] = admin["id"]

    monkeypatch.setattr(server.app, "test_client_class", SignedIn)


@pytest.fixture
def anon_client():
    import didac_app.server as server

    return FlaskClient(server.app, server.app.response_class)
