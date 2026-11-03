import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def now_iso_us() -> str:
    """Microsecond UTC timestamp for ledgers where order within a second matters."""
    return datetime.now(timezone.utc).isoformat()


def to_iso(value: Any) -> str:
    """Normalise a client-supplied timestamp to ISO-8601 UTC.

    Accepts ISO strings (with or without a trailing ``Z``) and epoch numbers in
    seconds or milliseconds. Anything unparseable falls back to ``now``. Keeping
    every ``event.ts`` in the same lexicographically-sortable UTC format is what
    makes the segment time-window queries (``ts >= since``) correct.
    """
    if value in (None, ""):
        return now_iso()
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=0).isoformat()
        except (OverflowError, OSError, ValueError):
            return now_iso()
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except ValueError:
        return now_iso()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4()}" if prefix else str(uuid.uuid4())


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def loads(text: str | None, default=None):
    if not text:
        return {} if default is None else default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {} if default is None else default


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(check, digest)


def new_write_key() -> str:
    return "wk_" + secrets.token_urlsafe(24)


def keyed_hash(value: str, purpose: str = "signal") -> str:
    """HMAC-SHA256 of a signal with the deployment secret, scoped by purpose.

    Used for device fingerprints, network prefixes and suppressed addresses so
    the raw value is never persisted. Truncated to 32 hex chars (128 bits).
    """
    from settings import IDENTITY_HASH_SECRET

    msg = f"{purpose}:{value}".encode()
    return hmac.new(IDENTITY_HASH_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:32]


def normalize_email(value) -> str | None:
    if not value or not isinstance(value, str) or "@" not in value:
        return None
    return value.strip().lower()


def normalize_phone(value) -> str | None:
    """Very small E.164-ish normaliser: keep digits and a leading +; UK 07… -> +447…"""
    if not value:
        return None
    s = str(value).strip()
    digits = "".join(c for c in s if c.isdigit())
    if not digits:
        return None
    if s.startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        return "+44" + digits[1:]
    return "+" + digits


def days_ago_iso(days: float) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
