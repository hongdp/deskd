"""The unified agent inbox — THE public ingress for hosts — plus the
capability-addressed router and its unroutable-demand ledger.
"""

from __future__ import annotations

from pathlib import Path

from ..config import CONFIG
from .store import (TASK_PRIORITIES, _agent_role, _clean, _iso,
                    _load_json, _log_event, connect)

# --- unified agent inbox ----------------------------------------------------

def _inbox_insert(conn, target_role: str, source_kind: str, title: str, *,
                  body: str | None = None, ref: str | None = None,
                  priority: str = "normal", dedup_key: str | None = None,
                  expires_at: str | None = None) -> int | None:
    """Same-connection inbox insert (for callers already inside a write txn).
    Returns the new id, or None on a (role, dedup_key) deduped no-op."""
    title = _clean(title, "title")
    if source_kind not in CONFIG.inbox_sources:
        raise ValueError(f"invalid source_kind: {source_kind}")
    if priority not in TASK_PRIORITIES:
        raise ValueError(f"invalid priority: {priority}")
    target_role = _agent_role(conn, target_role)
    cur = conn.execute(
        """INSERT OR IGNORE INTO agent_inbox
               (target_role, source_kind, ref, priority, title, body,
                dedup_key, enqueued_at, expires_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (target_role, source_kind, ref, priority, title,
         _clean(body, "body", required=False), dedup_key, _iso(), expires_at),
    )
    if cur.rowcount:
        _log_event(conn, source_kind, "inbox_enqueue", ref,
                   {"role": target_role, "title": title, "priority": priority})
        return cur.lastrowid
    return None


def inbox_enqueue(target_role: str, source_kind: str, title: str, *,
                  body: str | None = None, ref: str | None = None,
                  priority: str = "normal", dedup_key: str | None = None,
                  expires_at: str | None = None,
                  db_path: Path | str | None = None) -> int | None:
    """Enqueue an agent-directed notification — THE public ingress for hosts.

    This is how a host application injects its own domain events into the engine:
    the engine never reaches into the host to collect them. Returns the new id,
    or None if a same-(role, dedup_key) un-acked item already exists (deduped
    no-op).
    """
    with connect(db_path, write=True) as conn:
        return _inbox_insert(conn, target_role, source_kind, title, body=body,
                             ref=ref, priority=priority, dedup_key=dedup_key,
                             expires_at=expires_at)


# --- capability-addressed ingress -------------------------------------------

def _roles_with_capability(conn, capability: str) -> list[str]:
    """Enabled roles whose REGISTRY row declares `capability`. The registry is
    the source of truth, not CONFIG.roles: runtime changes (enabled=0, a
    capability granted to a live row) must be honoured, and _seed_registry
    never clobbers a live row with config."""
    out = []
    for r in conn.execute(
            "SELECT role, capabilities FROM agent_registry WHERE enabled=1"):
        caps = _load_json(r["capabilities"])
        if isinstance(caps, list) and capability in caps:
            out.append(r["role"])
    return sorted(out)


def _route_role(conn, capability: str) -> str | None:
    """Pick the recipient among qualifying roles: fewest un-acked inbox items,
    then name. Deterministic and presence-INdependent on purpose: who happens
    to be online this second is the wake ladder's axis, and it takes over once
    the item is queued — routing by liveness here would just race it."""
    roles = _roles_with_capability(conn, capability)
    if not roles:
        return None

    def _load(role: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM agent_inbox WHERE target_role=? "
            "AND acked_at IS NULL", (role,)).fetchone()[0]

    return min(roles, key=lambda r: (_load(r), r))


def inbox_route(require_capability: str, source_kind: str, title: str, *,
                body: str | None = None, ref: str | None = None,
                priority: str = "normal", dedup_key: str | None = None,
                expires_at: str | None = None,
                db_path: Path | str | None = None) -> dict:
    """Capability-addressed ingress: enqueue to SOME enabled role that declares
    `require_capability` — the caller names the authority the work needs, not
    who does it. Returns {"routed_to": role, "id": inbox_id}.

    If no enabled role declares the capability the demand is UNROUTABLE — the
    ladder's overdue state on the authority axis. It is recorded durably (never
    dropped), counted red on the board, and plan_wakes re-routes it into a real
    inbox the moment a qualifying role exists. Returns {"unroutable": True,
    "id": row_id} in that case; {"deduped": True, ...} on a dedup no-op of
    either kind."""
    require_capability = _clean(require_capability, "require_capability")
    with connect(db_path, write=True) as conn:
        role = _route_role(conn, require_capability)
        if role is not None:
            iid = _inbox_insert(conn, role, source_kind, title, body=body,
                                ref=ref, priority=priority, dedup_key=dedup_key,
                                expires_at=expires_at)
            if iid is None:
                return {"deduped": True, "routed_to": role}
            return {"routed_to": role, "id": iid}
        # Validate exactly what _inbox_insert would have: the unroutable path
        # must not be a hole through which an invalid row enters the system.
        title = _clean(title, "title")
        if source_kind not in CONFIG.inbox_sources:
            raise ValueError(f"invalid source_kind: {source_kind}")
        if priority not in TASK_PRIORITIES:
            raise ValueError(f"invalid priority: {priority}")
        cur = conn.execute(
            """INSERT OR IGNORE INTO unroutable_demands
                   (require_capability, source_kind, ref, priority, title, body,
                    dedup_key, enqueued_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (require_capability, source_kind, ref, priority, title,
             _clean(body, "body", required=False), dedup_key, _iso(), expires_at))
        if not cur.rowcount:
            return {"deduped": True, "unroutable": True}
        _log_event(conn, source_kind, "unroutable_demand", ref,
                   {"require_capability": require_capability, "title": title})
        return {"unroutable": True, "id": cur.lastrowid}


def _route_unroutable(conn) -> list[dict]:
    """The closing half of inbox_route's guarantee, run every planning tick:
    any recorded unroutable demand whose capability an enabled role NOW
    declares moves into that role's inbox and rides the normal delivery/wake
    ladder. Append-only history: the row is stamped routed_at/routed_to, never
    deleted."""
    out = []
    for r in conn.execute(
            "SELECT * FROM unroutable_demands WHERE routed_at IS NULL").fetchall():
        role = _route_role(conn, r["require_capability"])
        if role is None:
            continue
        try:
            iid = _inbox_insert(conn, role, r["source_kind"], r["title"],
                                body=r["body"], ref=r["ref"],
                                priority=r["priority"], dedup_key=r["dedup_key"],
                                expires_at=r["expires_at"])
        except ValueError:
            # The host's inbox_sources shrank under a stored row. Leave it
            # visible on the board rather than kill every future tick over it.
            continue
        conn.execute(
            "UPDATE unroutable_demands SET routed_at=?, routed_to=? WHERE id=?",
            (_iso(), role, r["id"]))
        _log_event(conn, "orchestrator", "demand_routed", r["ref"],
                   {"require_capability": r["require_capability"], "role": role,
                    "inbox_id": iid, "title": r["title"]})
        out.append({"id": r["id"], "routed_to": role, "inbox_id": iid})
    return out


_INBOX_RANK = {"urgent": 0, "normal": 1, "low": 2}


def _inbox_sort_key(r: dict):
    return (_INBOX_RANK.get(r["priority"], 1), r["enqueued_at"])


def inbox_pending(target_role: str | None = None, *, include_delivered: bool = True,
                  db_path: Path | str | None = None) -> list[dict]:
    """Un-acked inbox items (the live queue). include_delivered=False returns
    only not-yet-delivered items."""
    clauses = ["acked_at IS NULL"]
    params: list = []
    if target_role:
        clauses.append("target_role=?")
        params.append(target_role)
    if not include_delivered:
        clauses.append("delivered_at IS NULL")
    where = " AND ".join(clauses)
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM agent_inbox WHERE {where}", params).fetchall()]
    rows.sort(key=_inbox_sort_key)
    return rows


def inbox_mark_delivered(ids, db_path: Path | str | None = None) -> int:
    """Stamp items delivered. Called by the in-session hook when the session
    actually runs, never speculatively at plan time."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    now = _iso()
    with connect(db_path, write=True) as conn:
        q = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE agent_inbox SET delivered_at=? WHERE id IN ({q}) "
            f"AND delivered_at IS NULL",
            [now, *ids])
        return cur.rowcount


def inbox_ack(target_role: str | None = None, ids=None,
              db_path: Path | str | None = None) -> int:
    """Mark items processed. Pass ids to ack specific items, or target_role to
    ack all of a role's delivered-but-unacked items."""
    now = _iso()
    with connect(db_path, write=True) as conn:
        if ids:
            ids = [int(i) for i in ids]
            q = ",".join("?" * len(ids))
            cur = conn.execute(
                f"UPDATE agent_inbox SET acked_at=? WHERE id IN ({q}) AND acked_at IS NULL",
                [now, *ids])
        elif target_role:
            role = _agent_role(conn, target_role)
            # Only DELIVERED items: an item enqueued after this batch was
            # surfaced (still delivered_at NULL) has never been seen by the
            # agent — a blanket ack must not silently drop it.
            cur = conn.execute(
                "UPDATE agent_inbox SET acked_at=? WHERE target_role=? "
                "AND acked_at IS NULL AND delivered_at IS NOT NULL",
                (now, role))
        else:
            raise ValueError("inbox_ack needs ids or target_role")
        if cur.rowcount:
            _log_event(conn, target_role or "agent", "inbox_ack", None,
                       {"count": cur.rowcount})
        return cur.rowcount


def _inbox_view(rows: list[dict]) -> dict:
    """Group a role's un-acked items into queued (not delivered) vs delivered."""
    queued = [r for r in rows if not r["delivered_at"]]
    delivered = [r for r in rows if r["delivered_at"]]
    return {
        "queued": queued, "delivered": delivered,
        "queued_count": len(queued), "delivered_count": len(delivered),
        "urgent_queued": sum(1 for r in queued if r["priority"] == "urgent"),
    }
