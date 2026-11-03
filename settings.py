"""Environment-driven configuration for FastIdentity."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# --- app ---------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
PORT = _int("FASTIDENTITY_PORT", 5023)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:5023").rstrip("/")
DEFAULT_TENANT = os.getenv("DEFAULT_TENANT", "demo")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@fastsme.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
# Server-to-server token for the privacy API (/v1/erasure, /v1/export) and the SDK.
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN", "")

# Google SSO (optional). Shared FastSME OAuth client; redirect = <PUBLIC_BASE_URL>/auth/google/callback.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")
# Google accounts not yet in staff_user are auto-provisioned only if allow-listed.
GOOGLE_ALLOWED_DOMAINS = {d.strip().lower() for d in os.getenv("GOOGLE_ALLOWED_DOMAINS", "").split(",") if d.strip()}
GOOGLE_ALLOWED_EMAILS = {e.strip().lower() for e in os.getenv("GOOGLE_ALLOWED_EMAILS", "").split(",") if e.strip()}

# --- data layer ----------------------------------------------------------------
# DB_SOURCE picks the backend explicitly: "sqlite" (default, local file) or "psql".
DB_SOURCE = os.getenv("DB_SOURCE", "sqlite").strip().lower()
if DB_SOURCE in ("postgres", "postgresql", "pg"):
    DB_SOURCE = "psql"
if DB_SOURCE not in ("sqlite", "psql"):
    raise RuntimeError(f"DB_SOURCE must be 'sqlite' or 'psql', got {DB_SOURCE!r}")
DATABASE_PATH = os.getenv("DATABASE_PATH", str(DATA_DIR / "fastidentity.db"))
# Postgres DSN: app-specific name first, then the family-wide names.
DATABASE_URL = (os.getenv("FASTIDENTITY_DATABASE_URL") or os.getenv("DATABASE_URL")
                or os.getenv("DB_URL") or "").strip().strip("'\"")
DB_SCHEMA = os.getenv("DB_SCHEMA") or os.getenv("FASTIDENTITY_DB_SCHEMA") or "fast_identity"
if DB_SOURCE == "psql" and not DATABASE_URL:
    raise RuntimeError("DB_SOURCE=psql but no DATABASE_URL / FASTIDENTITY_DATABASE_URL is set")

# --- privacy / compliance ----------------------------------------------------------
# HMAC key used to hash device + network signals. Never store raw fingerprints.
# Rotating it breaks probabilistic linkage to older hashes (which is a feature).
IDENTITY_HASH_SECRET = os.getenv("IDENTITY_HASH_SECRET", "dev-only-hash-secret")
DATA_REGION = os.getenv("DATA_REGION", "uk")  # uk | eu — shown in UI, enforced at deploy
EVENT_RETENTION_DAYS = _int("EVENT_RETENTION_DAYS", 395)  # ~13 months
ANON_PROFILE_RETENTION_DAYS = _int("ANON_PROFILE_RETENTION_DAYS", 90)
# When true, events that arrive without analytics consent are dropped (counted only).
REQUIRE_ANALYTICS_CONSENT = _bool("REQUIRE_ANALYTICS_CONSENT", True)
# Comma-separated origins allowed to call /collect from the browser ("*" for dev).
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# --- matching --------------------------------------------------------------------
MATCH_ENGINE = os.getenv("MATCH_ENGINE", "builtin")  # builtin | splink
AUTO_MERGE_THRESHOLD = _float("AUTO_MERGE_THRESHOLD", 0.97)
REVIEW_THRESHOLD = _float("REVIEW_THRESHOLD", 0.70)
# Event property keys that become "interests" on a profile (domain models).
INTEREST_KEYS = [k.strip() for k in os.getenv("INTEREST_KEYS", "treatment,hospital,category,product").split(",") if k.strip()]

# --- activation ------------------------------------------------------------------
DEFAULT_FREQUENCY_CAP = _int("DEFAULT_FREQUENCY_CAP", 2)  # messages per profile ...
DEFAULT_FREQUENCY_WINDOW_DAYS = _int("DEFAULT_FREQUENCY_WINDOW_DAYS", 7)  # ... per window
FLOW_WORKER_INTERVAL_SECONDS = _int("FLOW_WORKER_INTERVAL_SECONDS", 60)

KLAVIYO_API_KEY = os.getenv("KLAVIYO_API_KEY", "")
CUSTOMERIO_SITE_ID = os.getenv("CUSTOMERIO_SITE_ID", "")
CUSTOMERIO_API_KEY = os.getenv("CUSTOMERIO_API_KEY", "")
CUSTOMERIO_REGION = os.getenv("CUSTOMERIO_REGION", "eu")  # eu | us
BRAZE_API_KEY = os.getenv("BRAZE_API_KEY", "")
BRAZE_REST_ENDPOINT = os.getenv("BRAZE_REST_ENDPOINT", "https://rest.fra-02.braze.eu")
WEBHOOK_DEFAULT_URL = os.getenv("WEBHOOK_DEFAULT_URL", "")
WEBHOOK_SIGNING_SECRET = os.getenv("WEBHOOK_SIGNING_SECRET", "")
