"""Read-only aggregates for the console: the board and the per-agent
detail page. Reads every sibling; owns nothing.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from .. import channels
from . import store
from .delivery import _delivery_state, _wake_keys, sync_delivery
from .inbox import _inbox_sort_key, _inbox_view
from .presence import _role_presence, presence
from .store import (LIVE_LIVENESS, TASK_OPEN_STATUSES, TASK_STATUSES,
                    _RECIPIENT_ALL, _agent_role, _iso, _known_roles,
                    _load_json, connect, recent_events)
from .tasks import _queued_tasks, _task_sort_key, _task_view
from .wake import _human_level, _ladder

# --- board aggregate --------------------------------------------------------

def _meeting_load(conn) -> dict:
    """Per-role meeting obligations, from the registry's roles."""
    roles = tuple(sorted(_known_roles(conn)))
    wakes = {r: c for r, c in conn.execute(
        """SELECT role, COUNT(*) FROM meeting_wake_requests
           WHERE status='pending' GROUP BY role""").fetchall()}
    unread = {r: c for r, c in conn.execute(
        """SELECT a.role, COUNT(*) FROM meeting_attendees a
           JOIN meetings m ON m.thread_id=a.thread_id
           JOIN mailbox_messages mm ON mm.thread_id=a.thread_id
                AND mm.recipient IN (a.role, ?) AND mm.sender!=a.role
           LEFT JOIN mailbox_receipts r ON r.message_id=mm.id AND r.role=a.role
           WHERE m.state IN ('active','consensus')
             AND a.checked_in_at IS NOT NULL AND a.stopped_at IS NULL
             AND r.message_id IS NULL
           GROUP BY a.role""", (_RECIPIENT_ALL,)).fetchall()}
    oblig = {}
    for owed, cnt, due in conn.execute(
            """SELECT owed_by, COUNT(*), MIN(due_at) FROM meeting_response_obligations
               WHERE status='pending' GROUP BY owed_by""").fetchall():
        oblig[owed] = {"pending": cnt, "next_due_at": due}
    active_meetings = {}
    for role, thread_id, agenda in conn.execute(
            """SELECT a.role, a.thread_id, m.agenda FROM meeting_attendees a
               JOIN meetings m ON m.thread_id=a.thread_id
               WHERE a.checked_in_at IS NOT NULL AND a.stopped_at IS NULL
                 AND m.state IN ('waiting','active','consensus','termination_pending')""").fetchall():
        active_meetings.setdefault(role, []).append(
            {"thread_id": thread_id, "agenda": agenda})
    out = {}
    for r in roles:
        out[r] = {
            "pending_wakes": wakes.get(r, 0),
            "unread_messages": unread.get(r, 0),
            "response_obligations": oblig.get(r, {"pending": 0, "next_due_at": None}),
            "active_meetings": active_meetings.get(r, []),
        }
    return out


def _delivery_health(conn, now_iso: str) -> dict:
    """Delivery health from the projected ledger. `stuck_deliveries` is the
    invariant-breach surface: past SLA, unread, and NOT yet escalated.

    CLOSED threads are excluded, and for a different reason than the ledger has.
    `delivery_ledger()` keeps reporting a closed thread's unread message as
    `overdue` because that is honest history — it really never was read. This is
    an ALARM, and an alarm may only count what someone can still act on. A closed
    conversation cannot be un-missed: counting it means this number ratchets up
    the first time any thread closes with an unread message and never returns to
    zero. A gauge that can never read zero is not a gauge, and the only thing
    worse than no alarm is one everyone has learned to ignore.

    Record and alarm are different jobs. This does the second, so it scopes to
    the same set the wake path acts on.
    """
    wake = _wake_keys(conn)
    rows = conn.execute(
        """SELECT d.* FROM message_delivery d
           JOIN mailbox_messages mm ON mm.id=d.message_id
           JOIN mailbox_threads t ON t.id=mm.thread_id
           WHERE t.status != 'closed'""").fetchall()
    oldest_unread = None
    unread = stuck = escalated = 0
    for r in rows:
        st = _delivery_state(r, now_iso, wake)
        if st == "read":
            continue
        unread += 1
        if oldest_unread is None or r["queued_at"] < oldest_unread:
            oldest_unread = r["queued_at"]
        if st == "overdue":
            stuck += 1
        elif st == "escalated":
            escalated += 1
    # Same rule as the rows above, for the same reason: a closed thread's failed
    # escalation is history, not a call to action. Nobody can rejoin the
    # conversation to answer it, so counting it pins this gauge above zero
    # forever — 35 of one desk's 39 were exactly that, which is how the number
    # became scenery and the 4 that were about a live, unanswered meeting hid
    # inside it. The rows stay; only the alarm narrows.
    unsent = conn.execute(
        """SELECT COUNT(*) FROM meeting_escalations e
           JOIN mailbox_threads t ON t.id=e.thread_id
           WHERE e.status!='sent' AND t.status != 'closed'""").fetchone()[0]
    age = None
    if oldest_unread:
        age = int((store._now() - dt.datetime.fromisoformat(oldest_unread)).total_seconds())
    return {"oldest_unread_at": oldest_unread, "oldest_unread_age_seconds": age,
            "unread_deliveries": unread, "stuck_deliveries": stuck,
            "escalated_deliveries": escalated, "unsent_escalations": unsent}


def board(db_path: Path | str | None = None) -> dict:
    """Everything the agent board needs, in one aggregate call."""
    now_iso = _iso()
    pres = presence(db_path)
    human_lvl = _human_level(_ladder())
    with connect(db_path, write=True) as conn:
        sync_delivery(conn)
        meeting_load = _meeting_load(conn)
        health = _delivery_health(conn, now_iso)
        all_tasks = [_task_view(dict(r), now_iso) for r in conn.execute(
            "SELECT * FROM agent_tasks").fetchall()]
        todos = {r["role"]: {"snapshot": _load_json(r["snapshot"]),
                             "updated_at": r["updated_at"]}
                 for r in conn.execute("SELECT * FROM session_todos").fetchall()}
        # Wake orchestrator state (READ only — board never records attempts;
        # that is the driver/plan_wakes job).
        wake_pending = {r["role"]: {"pending": r["c"], "max_level": r["ml"]}
                        for r in conn.execute(
                            """SELECT role, COUNT(*) c, MAX(level) ml FROM wake_attempts
                               WHERE outcome='pending' GROUP BY role""").fetchall()}
        # Full aggregate, NOT derived from the limited display window below.
        human_level_wakes = conn.execute(
            "SELECT COUNT(*) FROM wake_attempts WHERE outcome='pending' AND level>=?",
            (human_lvl,)).fetchone()[0]
        wake_activity = [dict(x) for x in conn.execute(
            "SELECT * FROM wake_attempts ORDER BY id DESC LIMIT 12").fetchall()]
        # The two ways an open task can stop being anybody's wake. Neither is a
        # failure of the engine and neither is fixable by waking anyone, which is
        # exactly why they have to be REPORTED: a task that no path will ever
        # raise again, and that nothing says out loud, is the write-only to-do
        # list this change exists to abolish. Someone has to decide about these.
        stalled_tasks = len(_queued_tasks(conn)[1])
        blocked_unspecified = conn.execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE status='blocked' "
            "AND (blocked_on IS NULL OR TRIM(blocked_on)='')").fetchone()[0]
        inbox_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM agent_inbox WHERE acked_at IS NULL").fetchall()]
        hook_rows = [dict(r) for r in conn.execute(
            "SELECT id, owner_role, kind, title, priority, next_fire_at, "
            "fire_count, last_error FROM wake_hooks WHERE status='active' ORDER BY id"
        ).fetchall()]
        # Overdue on the authority axis: demands no enabled role is allowed to
        # take. Anything above zero is a call to action (grant the capability
        # or enable a role) — plan_wakes clears it the tick after.
        unroutable = conn.execute(
            "SELECT COUNT(*) FROM unroutable_demands WHERE routed_at IS NULL"
        ).fetchone()[0]
        # Human-rung escalations whose only delivery is the outbox row itself
        # (status='queued'): each is a "pull a human in" that pulled in nobody
        # unless somebody reads this board.
        undelivered_esc = conn.execute(
            "SELECT COUNT(*) FROM wake_escalations WHERE status='queued'"
        ).fetchone()[0]
    # Which rungs are actually WIRED. The ladder promises a human rung; whether
    # that promise means anything depends on the host having registered a
    # channel that is available right now. unwired = the promise is currently
    # an outbox row only — a state the host must see, not discover.
    ladder_needs_human = any(r.leaves_machine for r in _ladder())
    channel_rows = channels.channel_status()
    all_tasks.sort(key=lambda t: _task_sort_key(t, now_iso))
    open_tasks = [t for t in all_tasks if t["status"] in TASK_OPEN_STATUSES]

    agents = []
    for p in pres:
        role = p["role"]
        role_tasks = [t for t in open_tasks if t["assignee_role"] == role]
        role_inbox = sorted([r for r in inbox_rows if r["target_role"] == role],
                            key=_inbox_sort_key)
        agents.append({
            **p,
            "meeting": meeting_load.get(role, {}),
            "tasks": role_tasks,
            "task_counts": _count_by_status(all_tasks, role),
            "overdue_count": sum(1 for t in role_tasks if t["overdue"]),
            "session_todos": (todos.get(role)
                              if p["liveness"] in LIVE_LIVENESS else None),
            "wake": wake_pending.get(role, {"pending": 0, "max_level": None}),
            "inbox": _inbox_view(role_inbox),
            "hooks": [h for h in hook_rows if h["owner_role"] == role],
        })
    return {
        "generated_at": now_iso,
        "agents": agents,
        "health": {
            **health,
            "total_overdue": sum(1 for t in open_tasks if t["overdue"]),
            "total_open_tasks": len(open_tasks),
            "stalled_tasks": stalled_tasks,
            "blocked_unspecified": blocked_unspecified,
            "pending_wakes": sum(v["pending"] for v in wake_pending.values()),
            "wakes_at_human_level": human_level_wakes,
            "inbox_queued": sum(1 for r in inbox_rows if not r["delivered_at"]),
            "inbox_total": len(inbox_rows),
            "unroutable_demands": unroutable,
            "channels": channel_rows,
            "human_rung_unwired": (ladder_needs_human
                                   and not channels.human_reachable()),
            "undelivered_escalations": undelivered_esc,
        },
        "wake_activity": wake_activity,
        "recent_events": recent_events(20, db_path),
    }


def _count_by_status(all_tasks: list[dict], role: str) -> dict:
    counts = {s: 0 for s in TASK_STATUSES}
    for t in all_tasks:
        if t["assignee_role"] == role:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
    return counts

# --- per-agent detail (RESTful) ---------------------------------------------

def agent_detail(role: str, db_path: Path | str | None = None) -> dict:
    """Everything about ONE agent for its detail page: profile, live session +
    work, meetings, the full inbox, all tasks, and the execution history
    (wake attempts, delivery ledger, orchestration events, hooks)."""
    now = store._now()
    now_iso = _iso(now)
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        sync_delivery(conn)
        reg = conn.execute("SELECT * FROM agent_registry WHERE role=?", (role,)).fetchone()
        pres = _role_presence(conn, role, now)
        live = pres["liveness"] in LIVE_LIVENESS

        td = conn.execute(
            "SELECT snapshot, updated_at FROM session_todos WHERE role=?", (role,)).fetchone()
        session_todos = ({"snapshot": _load_json(td["snapshot"]),
                          "updated_at": td["updated_at"]} if td and live else None)

        tasks_ = [_task_view(dict(r), now_iso) for r in conn.execute(
            "SELECT * FROM agent_tasks WHERE assignee_role=? ORDER BY id DESC",
            (role,)).fetchall()]
        wake_history = [dict(r) for r in conn.execute(
            "SELECT * FROM wake_attempts WHERE role=? ORDER BY id DESC LIMIT 80",
            (role,)).fetchall()]
        inbox = [dict(r) for r in conn.execute(
            "SELECT * FROM agent_inbox WHERE target_role=? ORDER BY id DESC LIMIT 100",
            (role,)).fetchall()]
        hooks_ = []
        for r in conn.execute("SELECT * FROM wake_hooks WHERE owner_role=? ORDER BY id DESC",
                              (role,)):
            d = dict(r)
            d["spec"] = _load_json(d["spec"])
            hooks_.append(d)
        meeting = _meeting_load(conn).get(role, {})
        wake_keys = _wake_keys(conn)
        delivery = []
        for r in conn.execute(
                "SELECT * FROM message_delivery WHERE recipient_role=? "
                "ORDER BY message_id DESC LIMIT 80",
                (role,)):
            d = dict(r)
            d["state"] = _delivery_state(d, now_iso, wake_keys)
            delivery.append(d)
        events = []
        for r in conn.execute(
                "SELECT * FROM orchestration_events WHERE actor=? OR payload LIKE ? "
                "ORDER BY id DESC LIMIT 100", (role, '%"' + role + '"%')):
            d = dict(r)
            if d.get("payload"):
                d["payload"] = _load_json(d["payload"])
            events.append(d)
    return {
        "role": role, "generated_at": now_iso,
        "profile": {
            "display_name": reg["display_name"] if reg else role,
            "capabilities": _load_json(reg["capabilities"]) if reg else [],
            "authority": _load_json(reg["authority"]) if reg else {},
        },
        "presence": pres,
        "session_todos": session_todos,
        "meeting": meeting,
        "inbox": inbox,
        "tasks": tasks_,
        "task_counts": _count_by_status(tasks_, role),
        "wake_history": wake_history,
        "hooks": hooks_,
        "delivery": delivery,
        "events": events,
    }
