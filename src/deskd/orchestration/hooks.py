"""Self-service wake hooks: at / interval / cron / probe. Firing
enqueues an inbox item, which rides the normal delivery/wake ladder.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from . import store
from ..config import CONFIG
from .inbox import _inbox_insert
from .store import (TASK_PRIORITIES, _agent_role, _clean, _iso,
                    _load_json, _log_event, _normalize_due, connect)

# --- agent wake hooks (self-service wake API) --------------------------------

WAKE_HOOK_KINDS = {"at", "interval", "probe", "cron"}

#: Shape of a probe path: 'dotted.module:function'. The allowlist decides which
#: dotted prefixes are importable — this only validates the syntax.
_PROBE_SHAPE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")


def _cron_field(field: str, lo: int, hi: int) -> set:
    """Expand one 5-field-cron field: supports *, a, a-b, */n, a-b/n, and commas."""
    out = set()
    for part in field.split(","):
        rng, step = part, 1
        if "/" in part:
            rng, s = part.split("/", 1)
            step = int(s)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            aa, bb = rng.split("-", 1)
            a, b = int(aa), int(bb)
        else:
            a = b = int(rng)
        v = a
        while v <= b:
            if lo <= v <= hi:
                out.add(v)
            v += step
    return out


def _next_cron_fire(expr: str, tzname: str, after: dt.datetime) -> str | None:
    """Next UTC firing time at/after `after` for a 5-field cron in `tzname`
    (min hour dom month dow; dow 0=Sun). AND semantics for dom/dow. Scans
    minute-by-minute up to ~8 days — trivial cost, DST-correct via zoneinfo."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron must have 5 fields (got {expr!r})")
    mins = _cron_field(parts[0], 0, 59)
    hrs = _cron_field(parts[1], 0, 23)
    doms = _cron_field(parts[2], 1, 31)
    months = _cron_field(parts[3], 1, 12)
    dows = _cron_field(parts[4], 0, 6)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname)
    except Exception:
        tz = CONFIG.tzinfo()
    t = after.astimezone(tz).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(8 * 24 * 60):
        if (t.minute in mins and t.hour in hrs and t.day in doms
                and t.month in months and ((t.weekday() + 1) % 7) in dows):
            return _iso(t.astimezone(dt.timezone.utc))
        t += dt.timedelta(minutes=1)
    return None


def _probe_path_ok(path: str) -> bool:
    """True if `path` is syntactically a probe AND inside CONFIG.probe_allowlist.

    An EMPTY allowlist denies everything: the engine only ever imports code the
    host has explicitly opted in. The prefix match is dotted-boundary-aware, so
    an allowlist of 'myapp.watch' never admits 'myapp.watchdog_evil'.
    """
    if not _PROBE_SHAPE_RE.match(path or ""):
        return False
    module = path.partition(":")[0]
    for prefix in CONFIG.probe_allowlist:
        if module == prefix or module.startswith(prefix + "."):
            return True
    return False


def _resolve_probe(path: str):
    """Import a 'module:function' probe, restricted to CONFIG.probe_allowlist.

    A probe may only OBSERVE and NOTIFY: its return value is turned into inbox
    items and nothing else. It is called with no arguments and no engine handle.
    """
    if not _probe_path_ok(path):
        if not CONFIG.probe_allowlist:
            raise ValueError(
                f"probe {path!r} rejected: probes are disabled "
                f"(CONFIG.probe_allowlist is empty)")
        allowed = ", ".join(CONFIG.probe_allowlist)
        raise ValueError(
            f"probe {path!r} is not allowed: expected '<module>:<function>' "
            f"under one of [{allowed}]")
    import importlib
    mod_name, _, func_name = path.partition(":")
    fn = getattr(importlib.import_module(mod_name), func_name, None)
    if not callable(fn):
        raise ValueError(f"probe {path!r} does not resolve to a callable")
    return fn


def hook_add(owner_role: str, title: str, *, at: str | None = None,
             every: int | None = None, callable_path: str | None = None,
             cron: str | None = None, tz: str | None = None,
             until: str | None = None, body: str | None = None,
             priority: str = "normal",
             db_path: Path | str | None = None) -> dict:
    """Register a wake hook. Exactly one shape:

    - at=ISO ts                      -> one-shot timer
    - every=N [until=ISO]            -> recurring timer
    - cron="m h dom mon dow" [tz]    -> calendar schedule (tz defaults to CONFIG.timezone)
    - callable_path=mod:fn [every N] -> custom watcher probe (fires when the
      function returns a truthy dict / list of dicts)

    Validation is fail-fast at registration: a probe outside the allowlist, a
    missing function, or a cron that never matches is rejected here rather than
    silently failing on some future tick.
    """
    title = _clean(title, "title")
    if priority not in TASK_PRIORITIES:
        raise ValueError(f"invalid priority: {priority}")
    now = store._now()
    spec: dict = {}
    if callable_path:
        _resolve_probe(callable_path)  # fail fast on disallowed / missing probe
        kind = "probe"
        every = int(every or CONFIG.default_probe_every)
        if every < CONFIG.min_hook_every:
            raise ValueError(f"every must be >= {CONFIG.min_hook_every}s")
        spec = {"callable": callable_path, "every": every}
        next_fire = _iso(now)                       # evaluate on the next tick
    elif cron:
        kind = "cron"
        tzname = tz or CONFIG.timezone
        next_fire = _next_cron_fire(cron, tzname, now)  # validates + schedules
        if next_fire is None:
            raise ValueError(f"cron never matches within 8 days: {cron!r}")
        spec = {"cron": cron, "tz": tzname}
    elif at:
        kind = "at"
        next_fire = _normalize_due(at)
        spec = {"at": next_fire}
    elif every:
        kind = "interval"
        every = int(every)
        if every < CONFIG.min_hook_every:
            raise ValueError(f"every must be >= {CONFIG.min_hook_every}s")
        spec = {"every": every}
        next_fire = _iso(now + dt.timedelta(seconds=every))
    else:
        raise ValueError("hook needs one of: at / every / cron / callable_path")
    if until:
        spec["until"] = _normalize_due(until)
    now_iso = _iso(now)
    with connect(db_path, write=True) as conn:
        owner_role = _agent_role(conn, owner_role)
        cur = conn.execute(
            """INSERT INTO wake_hooks (owner_role, kind, title, body, priority,
                                       spec, status, next_fire_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?, 'active', ?,?,?)""",
            (owner_role, kind, title, _clean(body, "body", required=False),
             priority, json.dumps(spec), next_fire, now_iso, now_iso))
        _log_event(conn, owner_role, "hook_add", str(cur.lastrowid),
                   {"kind": kind, "title": title, "spec": spec})
        return {"hook": cur.lastrowid, "kind": kind, "next_fire_at": next_fire}


def hooks(owner_role: str | None = None, *, include_closed: bool = False,
          db_path: Path | str | None = None) -> list[dict]:
    clauses, params = [], []
    if owner_role:
        clauses.append("owner_role=?")
        params.append(owner_role)
    if not include_closed:
        clauses.append("status='active'")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect(db_path) as conn:
        out = []
        for r in conn.execute(f"SELECT * FROM wake_hooks {where} ORDER BY id", params):
            d = dict(r)
            d["spec"] = _load_json(d["spec"])
            out.append(d)
        return out


def hook_cancel(hook_id: int, *, actor: str | None = None,
                db_path: Path | str | None = None) -> bool:
    with connect(db_path, write=True) as conn:
        cur = conn.execute(
            "UPDATE wake_hooks SET status='cancelled', updated_at=? "
            "WHERE id=? AND status='active'", (_iso(), int(hook_id)))
        if cur.rowcount:
            _log_event(conn, actor or "agent", "hook_cancel", str(hook_id), None)
        return bool(cur.rowcount)


def _eval_wake_hooks(conn, now: dt.datetime) -> list[dict]:
    """Evaluate due hooks inside the caller's write txn; fire -> inbox items.

    A probe that raises CONFIG.max_error_streak times in a row is auto-disabled
    and its owner is notified through the inbox (so a broken watcher can't fail
    silently forever). A probe exception NEVER breaks the tick. Timer hooks
    cannot raise.
    """
    now_iso = _iso(now)
    fired = []
    rows = conn.execute(
        "SELECT * FROM wake_hooks WHERE status='active' "
        "AND next_fire_at IS NOT NULL AND next_fire_at<=?", (now_iso,)).fetchall()
    for h in rows:
        spec = _load_json(h["spec"]) or {}
        until = spec.get("until")
        if until and now_iso > until:
            conn.execute("UPDATE wake_hooks SET status='done', updated_at=? WHERE id=?",
                         (now_iso, h["id"]))
            continue
        items, err = [], None
        if h["kind"] == "probe":
            try:
                res = _resolve_probe(spec["callable"])()
                if res:
                    items = res if isinstance(res, list) else [res]
                    items = [i for i in items if isinstance(i, dict)] or [{}]
            except Exception as exc:  # never let a probe break the tick
                err = f"{type(exc).__name__}: {exc}"[:300]
        else:
            items = [{}]  # timers fire with the hook's own title/body

        if err:
            streak = (h["error_streak"] or 0) + 1
            if streak >= CONFIG.max_error_streak:
                conn.execute(
                    "UPDATE wake_hooks SET status='error', error_streak=?, "
                    "last_error=?, updated_at=? WHERE id=?",
                    (streak, err, now_iso, h["id"]))
                _inbox_insert(conn, h["owner_role"], "system",
                              f"Wake hook #{h['id']} ({h['title']}) disabled "
                              f"after repeated errors",
                              body=err, ref=f"hook:{h['id']}", priority="normal",
                              dedup_key=f"hook-error:{h['id']}")
            else:
                nxt = _iso(now + dt.timedelta(
                    seconds=int(spec.get("every", CONFIG.default_probe_every))))
                conn.execute(
                    "UPDATE wake_hooks SET error_streak=?, last_error=?, "
                    "next_fire_at=?, updated_at=? WHERE id=?",
                    (streak, err, nxt, now_iso, h["id"]))
            continue

        n_enqueued = 0
        for item in items:
            prio = item.get("priority") if item.get("priority") in TASK_PRIORITIES \
                else h["priority"]
            try:
                iid = _inbox_insert(
                    conn, h["owner_role"], "system",
                    item.get("title") or h["title"], body=item.get("body") or h["body"],
                    ref=item.get("ref") or f"hook:{h['id']}", priority=prio,
                    dedup_key=item.get("dedup_key")
                    or f"hook:{h['id']}:{(item.get('title') or h['title'])[:80]}")
            except Exception:
                iid = None
            if iid:
                n_enqueued += 1
        # bookkeeping + reschedule
        if h["kind"] == "at":
            conn.execute(
                "UPDATE wake_hooks SET status='done', last_fired_at=?, "
                "fire_count=fire_count+1, error_streak=0, next_fire_at=NULL, "
                "updated_at=? WHERE id=?",
                (now_iso, now_iso, h["id"]))
        else:
            nxt = (_next_cron_fire(spec["cron"], spec.get("tz") or CONFIG.timezone, now)
                   if h["kind"] == "cron"
                   else _iso(now + dt.timedelta(
                       seconds=int(spec.get("every", CONFIG.default_probe_every)))))
            done = bool(nxt is None or (until and nxt > until))
            conn.execute(
                "UPDATE wake_hooks SET status=?, last_fired_at=?, "
                "fire_count=fire_count + ?, error_streak=0, next_fire_at=?, "
                "updated_at=? WHERE id=?",
                ("done" if done else "active",
                 now_iso if n_enqueued else h["last_fired_at"],
                 1 if n_enqueued else 0,
                 None if done else nxt, now_iso, h["id"]))
        if n_enqueued:
            _log_event(conn, "orchestrator", "hook_fire", str(h["id"]),
                       {"role": h["owner_role"], "title": h["title"], "items": n_enqueued})
            fired.append({"hook": h["id"], "role": h["owner_role"],
                          "title": h["title"], "items": n_enqueued})
    return fired
