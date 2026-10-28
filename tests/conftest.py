import os

# Set before didac_app is imported so tests never touch the real DATABASE_URL from .env.
os.environ["DATABASE_URL"] = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:54329/didac_test"
)

os.environ["BIRD_WEBHOOK_TOKEN"] = "test-token"

import pytest

from didac_app import bird
from didac_app.storage import LeadStore


@pytest.fixture
def store():
    store = LeadStore(os.environ["DATABASE_URL"])
    store.clear()
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
