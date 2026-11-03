"""Data layer for FastIdentity.

Dual-mode like FastCDP/FastERP, selected by ``DB_SOURCE``: ``sqlite`` (default,
local file) or ``psql`` (shared PostgreSQL, per-app schema ``DB_SCHEMA``). Domain and UI code
only use the helpers below, written with SQLite ``?`` placeholders; ``_pg_sql``
rewrites them for psycopg. All primary keys are prefixed text ids (``p_…``,
``e_…``) so both backends behave identically. Tables are created at import.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from settings import DATABASE_PATH, DATABASE_URL, DB_SCHEMA, DB_SOURCE

USE_POSTGRES = DB_SOURCE == "psql"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenant (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS staff_user (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT,
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin'
);
CREATE TABLE IF NOT EXISTS source (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT,
    name             TEXT NOT NULL,
    write_key        TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'js',
    allowed_origins  TEXT DEFAULT '',
    created_at       TEXT DEFAULT ''
);

-- Identity graph -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS profile (
    id             TEXT PRIMARY KEY,
    tenant_id      TEXT,
    status         TEXT DEFAULT 'anonymous',
    email          TEXT,
    phone          TEXT,
    user_id        TEXT,
    traits_json    TEXT DEFAULT '{}',
    interests_json TEXT DEFAULT '{}',
    first_seen     TEXT DEFAULT '',
    last_seen      TEXT DEFAULT '',
    merged_into    TEXT,
    erased_at      TEXT
);
-- Deterministic identifiers. origin_profile_id never changes, so a merge can be
-- undone by moving rows whose origin is the loser back to it.
CREATE TABLE IF NOT EXISTS identity (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT,
    profile_id         TEXT NOT NULL,
    origin_profile_id  TEXT NOT NULL,
    id_type            TEXT NOT NULL,
    id_value           TEXT NOT NULL,
    created_at         TEXT DEFAULT ''
);
-- Hashed probabilistic signals (device, network prefix, UA family, tz…).
CREATE TABLE IF NOT EXISTS signal (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT,
    profile_id         TEXT NOT NULL,
    origin_profile_id  TEXT NOT NULL,
    signal_type        TEXT NOT NULL,
    value_hash         TEXT NOT NULL,
    first_seen         TEXT DEFAULT '',
    last_seen          TEXT DEFAULT '',
    seen_count         INTEGER DEFAULT 1
);
-- Append-only event log (profile_id is re-pointed on merge; origin is not).
CREATE TABLE IF NOT EXISTS event (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT,
    event              TEXT NOT NULL,
    ts                 TEXT NOT NULL,
    profile_id         TEXT,
    origin_profile_id  TEXT,
    anonymous_id       TEXT,
    source_id          TEXT,
    properties_json    TEXT DEFAULT '{}',
    context_json       TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS merge_log (
    id                     TEXT PRIMARY KEY,
    tenant_id              TEXT,
    survivor_id            TEXT NOT NULL,
    loser_id               TEXT NOT NULL,
    method                 TEXT NOT NULL,
    score                  REAL,
    reason                 TEXT DEFAULT '',
    survivor_snapshot_json TEXT DEFAULT '{}',
    actor                  TEXT DEFAULT 'system',
    ts                     TEXT NOT NULL,
    undone_at              TEXT,
    undone_by              TEXT
);
CREATE TABLE IF NOT EXISTS match_candidate (
    id             TEXT PRIMARY KEY,
    tenant_id      TEXT,
    left_id        TEXT NOT NULL,
    right_id       TEXT NOT NULL,
    score          REAL NOT NULL,
    features_json  TEXT DEFAULT '{}',
    engine         TEXT DEFAULT 'builtin',
    status         TEXT DEFAULT 'pending',
    created_at     TEXT NOT NULL,
    reviewed_at    TEXT,
    reviewed_by    TEXT
);

-- Compliance -----------------------------------------------------------------
-- Append-only consent ledger; the current state is the latest row per purpose.
CREATE TABLE IF NOT EXISTS consent_record (
    id             TEXT PRIMARY KEY,
    tenant_id      TEXT,
    profile_id     TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    granted        INTEGER NOT NULL,
    legal_basis    TEXT NOT NULL DEFAULT 'consent',
    source         TEXT DEFAULT '',
    evidence_json  TEXT DEFAULT '{}',
    ts             TEXT NOT NULL
);
-- Addresses are stored hashed so suppression survives erasure without PII.
CREATE TABLE IF NOT EXISTS suppression (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT,
    channel       TEXT NOT NULL,
    address_hash  TEXT NOT NULL,
    reason        TEXT DEFAULT 'unsubscribe',
    created_at    TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_log (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    target_type  TEXT DEFAULT '',
    target_id    TEXT DEFAULT '',
    detail_json  TEXT DEFAULT '{}',
    ts           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consent_drop (
    tenant_id  TEXT NOT NULL,
    day        TEXT NOT NULL,
    dropped    INTEGER DEFAULT 0,
    PRIMARY KEY (tenant_id, day)
);

-- Activation -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS segment (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT,
    name               TEXT NOT NULL,
    definition_json    TEXT DEFAULT '{"all":[]}',
    materialized_count INTEGER DEFAULT 0,
    last_computed_at   TEXT
);
CREATE TABLE IF NOT EXISTS destination (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    config_json  TEXT DEFAULT '{}',
    enabled      INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS flow (
    id                     TEXT PRIMARY KEY,
    tenant_id              TEXT,
    name                   TEXT NOT NULL,
    trigger_event          TEXT NOT NULL,
    exit_event             TEXT NOT NULL,
    delay_minutes          INTEGER DEFAULT 60,
    lookback_hours         INTEGER DEFAULT 72,
    channel                TEXT DEFAULT 'email',
    destination_id         TEXT,
    template               TEXT DEFAULT '',
    frequency_cap          INTEGER,
    frequency_window_days  INTEGER,
    enabled                INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS message (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT,
    profile_id        TEXT NOT NULL,
    flow_id           TEXT,
    trigger_event_id  TEXT,
    channel           TEXT,
    destination_id    TEXT,
    status            TEXT NOT NULL,
    reason            TEXT DEFAULT '',
    payload_json      TEXT DEFAULT '{}',
    ts                TEXT NOT NULL
)
"""

_INDEXES = [
    "create unique index if not exists ux_identity on identity(tenant_id, id_type, id_value)",
    "create index if not exists ix_identity_profile on identity(profile_id)",
    "create unique index if not exists ux_signal on signal(tenant_id, origin_profile_id, signal_type, value_hash)",
    "create index if not exists ix_signal_value on signal(tenant_id, signal_type, value_hash)",
    "create index if not exists ix_event_profile on event(tenant_id, profile_id, ts)",
    "create index if not exists ix_event_name on event(tenant_id, event, ts)",
    "create index if not exists ix_event_origin on event(origin_profile_id)",
    "create index if not exists ix_profile_email on profile(tenant_id, email)",
    "create unique index if not exists ux_source_key on source(write_key)",
    "create index if not exists ix_consent_profile on consent_record(profile_id, purpose, ts)",
    "create unique index if not exists ux_suppression on suppression(tenant_id, channel, address_hash)",
    "create unique index if not exists ux_candidate on match_candidate(tenant_id, left_id, right_id)",
    "create index if not exists ix_message_profile on message(profile_id, ts)",
    "create unique index if not exists ux_message_trigger on message(flow_id, trigger_event_id)",
    "create index if not exists ix_audit_ts on audit_log(tenant_id, ts)",
]

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=0,
            max_size=4,
            open=True,
            kwargs={
                "row_factory": dict_row,
                "autocommit": True,
                "options": f"-c search_path={DB_SCHEMA},public",
                "application_name": "fastidentity",
            },
        )
        import atexit

        atexit.register(close_pool)
    return _pool


def _pg_sql(sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s")


@contextmanager
def _sqlite_conn():
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def fetchall(sql: str, args=()):
    if USE_POSTGRES:
        with _get_pool().connection() as conn:
            return list(conn.execute(_pg_sql(sql), list(args)))
    with _sqlite_conn() as conn:
        return [dict(r) for r in conn.execute(sql, list(args)).fetchall()]


def fetchone(sql: str, args=()):
    rows = fetchall(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args=()):
    if USE_POSTGRES:
        with _get_pool().connection() as conn:
            conn.execute(_pg_sql(sql), list(args))
        return
    with _sqlite_conn() as conn:
        conn.execute(sql, list(args))


def scalar(sql: str, args=()):
    row = fetchone(sql, args)
    if not row:
        return None
    return next(iter(row.values()))


def insert_row(table: str, **record):
    cols = list(record)
    sql = f"insert into {table} ({','.join(cols)}) values ({','.join('?' for _ in cols)})"
    execute(sql, [record[c] for c in cols])


def update_row(table: str, pk, **updates):
    if not updates:
        return
    sets = ",".join(f"{k}=?" for k in updates)
    execute(f"update {table} set {sets} where id=?", [*updates.values(), pk])


def init_db():
    if USE_POSTGRES:
        with _get_pool().connection() as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"')
    # Strip SQL comment lines first (they may contain ';'), then split statements.
    sql = "\n".join(l for l in SCHEMA.splitlines() if not l.strip().startswith("--"))
    for stmt in sql.split(";"):
        if stmt.strip():
            execute(stmt.strip())
    for stmt in _INDEXES:
        execute(stmt)


def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def db_exists() -> bool:
    if USE_POSTGRES:
        return True
    p = Path(DATABASE_PATH)
    return p.exists() and p.stat().st_size > 0


init_db()
