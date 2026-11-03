"""Append-only audit log. Never put raw PII in ``detail``."""
from db import fetchall, insert_row
from util import dumps, new_id, now_iso


def audit(tenant_id: str, actor: str, action: str, target_type: str = "", target_id: str = "", **detail):
    insert_row(
        "audit_log",
        id=new_id("a_"),
        tenant_id=tenant_id,
        actor=actor or "system",
        action=action,
        target_type=target_type,
        target_id=target_id or "",
        detail_json=dumps(detail),
        ts=now_iso(),
    )


def recent(tenant_id: str, limit: int = 200, target_id: str | None = None):
    if target_id:
        return fetchall(
            "select * from audit_log where tenant_id=? and target_id=? order by ts desc limit ?",
            [tenant_id, target_id, limit],
        )
    return fetchall("select * from audit_log where tenant_id=? order by ts desc limit ?", [tenant_id, limit])
