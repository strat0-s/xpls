import os

# Set before didac_app is imported so tests never touch the real DATABASE_URL from .env.
os.environ["DATABASE_URL"] = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:54329/didac_test"
)

import pytest

from didac_app.storage import LeadStore


@pytest.fixture
def store():
    store = LeadStore(os.environ["DATABASE_URL"])
    store.clear()
    return store
