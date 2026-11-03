import os
import tempfile
from pathlib import Path

# Must run before any app module imports settings/db.
_tmp = Path(tempfile.mkdtemp()) / "test.db"
os.environ.update({
    "DB_SOURCE": "sqlite", "DATABASE_PATH": str(_tmp),
    "SECRET_KEY": "test", "IDENTITY_HASH_SECRET": "test-secret", "DEFAULT_TENANT": "demo",
    "ADMIN_EMAIL": "admin@fastsme.com", "ADMIN_PASSWORD": "admin", "ADMIN_API_TOKEN": "tok",
    "REQUIRE_ANALYTICS_CONSENT": "true", "ALLOWED_ORIGINS": "*",
})

import pytest  # noqa: E402

from db import execute  # noqa: E402
from seed import bootstrap  # noqa: E402

TABLES = ["profile", "identity", "signal", "event", "merge_log", "match_candidate", "consent_record",
          "suppression", "audit_log", "consent_drop", "message"]


@pytest.fixture(autouse=True)
def clean():
    for t in TABLES:
        execute(f"delete from {t}")
    bootstrap()
    yield


@pytest.fixture
def web():
    from db import fetchone
    return fetchone("select * from source where kind='js'")


@pytest.fixture
def server():
    from db import fetchone
    return fetchone("select * from source where kind='server'")


FULL = {"analytics": True, "marketing": True, "functional": True}
ANALYTICS_ONLY = {"analytics": True, "marketing": False}
DEVICE = {"userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) Safari/605.1.15",
          "screen": {"width": 1512, "height": 982, "density": 2}, "timezone": "Europe/London", "locale": "en-GB"}


def ctx(cmp=FULL, device=True):
    return {"consent": cmp, **(DEVICE if device else {})}
