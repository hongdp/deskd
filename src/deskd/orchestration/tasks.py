"""Cross-session agent tasks: CRUD, the actionable/stalled queue split,
and the meeting -> task projection.
"""

from __future__ import annotations

from pathlib import Path

from ..config import CONFIG
from .store import (TASK_OPEN_STATUSES, TASK_PRIORITIES, TASK_STATUSES,
                    _agent_role, _clean, _iso, _log_event,
                    _normalize_due, _task_sources, connect)

_PRIO_RANK = {"urgent": 0, "normal": 1, "low": 2}


def _task_sort_key(t: dict, now_iso: str):
    closed = t["status"] in ("done", "cancelled")
    overdue = (not closed) and bool(t["due_at"]) and t["due_at"] < now_iso
    return (
        1 if closed else 0,                       # open before closed
        0 if overdue else 1,                      # overdue before on-time
        t["due_at"] if overdue else "",           # most-overdue (earliest due) first
        _PRIO_RANK.get(t["priority"], 1),         # urgent before normal before low
        0 if t["due_at"] else 1,                  # has-due before no-due
        t["due_at"] or "9999-12-31",              # soonest due first
        t["created_at"],                          # stable oldest-first
    )


def _task_view(row: dict, now_iso: str) -> dict:
    out = dict(row)
    out["overdue"] = (out["status"] in TASK_OPEN_STATUSES
                      and bool(out["due_at"]) and out["due_at"] < now_iso)
    return out


def task_add(title: str, *, assignee_role: str, detail: str | None = None,
             priority: str = "normal", source_kind: str = "self",
             source_ref: str | None = None, due_at: str | None = None,
             created_by: str | None = None,
             db_path: Path | str | None = None) -> int:
    """Create a cross-session work item.

    ``due_at`` is a SOFT deadline: it drives ordering and the overdue flag, and
    never wakes anyone. Only priority='urgent' generates a wake demand.
    """
    now = _iso()
    title = _clean(title, "title")
    if priority not in TASK_PRIORITIES:
        raise ValueError(f"invalid priority: {priority}")
    if source_kind not in _task_sources():
        raise ValueError(f"invalid source_kind: {source_kind}")
    due_at = _normalize_due(due_at)
    with connect(db_path, write=True) as conn:
        assignee_role = _agent_role(conn, assignee_role)
        created_by = _clean(created_by, "created_by", required=False) or assignee_role
        cur = conn.execute(
            """INSERT INTO agent_tasks
                   (title, detail, assignee_role, status, priority, source_kind,
                    source_ref, due_at, created_by, created_at, updated_at)
               VALUES (?,?,?,'pending',?,?,?,?,?,?,?)""",
            (title, _clean(detail, "detail", required=False), assignee_role,
             priority, source_kind, source_ref, due_at, created_by, now, now),
        )
        task_id = cur.lastrowid
        _log_event(conn, created_by, "task_add", str(task_id),
                   {"assignee": assignee_role, "priority": priority, "title": title})
        return task_id


def tasks(*, assignee_role: str | None = None, status: str | None = None,
          include_closed: bool = False,
          db_path: Path | str | None = None) -> list[dict]:
    now_iso = _iso()
    clauses, params = [], []
    if assignee_role:
        clauses.append("assignee_role=?")
        params.append(assignee_role)
    if status:
        clauses.append("status=?")
        params.append(status)
    elif not include_closed:
        clauses.append("status NOT IN ('done','cancelled')")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect(db_path) as conn:
        rows = [_task_view(dict(r), now_iso) for r in conn.execute(
            f"SELECT * FROM agent_tasks {where}", params).fetchall()]
    rows.sort(key=lambda t: _task_sort_key(t, now_iso))
    return rows


def _apply_blocked_on(updates: dict, current) -> None:
    """Hold the resulting row to: blocked IFF it names what it is blocked ON.

    'blocked' was the one status with no live obligation attached to it. Nothing
    recorded the dependency, so nothing could ever tell whether it had been met —
    a task marked blocked left the wake path (waking cannot help someone who is
    waiting on you) and entered a state no path could return it from. The live
    desk has one, blocked since 07:02, and the engine cannot say what it waits
    for. Naming the dependency is what makes 'blocked' a claim rather than a
    place to put things.

    The invariant is on the RESULTING row, not on the call: a caller may name the
    dependency now or already have named it, and one that leaves blocked need not
    mention the column at all — a stale `blocked_on` on an actionable task would
    say it waits on something while its status says it does not.
    """
    new_status = updates.get("status", current["status"])
    if new_status == "blocked":
        dep = _clean(updates.get("blocked_on", current["blocked_on"]),
                     "blocked_on", required=False)
        if not dep:
            raise ValueError(
                "status='blocked' requires blocked_on: name the dependency you "
                "are waiting on. If nothing names it, it is not blocked — do it, "
                "transfer it, or escalate it.")
        updates["blocked_on"] = dep
    elif "blocked_on" in updates:
        raise ValueError(
            f"blocked_on is only meaningful for status='blocked' (this task "
            f"would be {new_status!r})")
    elif current["blocked_on"] is not None:
        updates["blocked_on"] = None      # leaving blocked: the wait is over


def task_update(task_id: int, *, actor: str | None = None,
                db_path: Path | str | None = None, **fields) -> bool:
    """Update mutable task fields. Only status/priority/due_at/detail/title/
    result_note/assignee_role/blocked_on are settable; unknown or None fields are
    ignored.

    ``status='blocked'`` REQUIRES a ``blocked_on``, and leaving blocked clears it
    — see _apply_blocked_on.
    """
    allowed = {"status", "priority", "due_at", "detail", "title",
               "result_note", "assignee_role", "blocked_on"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    if "status" in updates and updates["status"] not in TASK_STATUSES:
        raise ValueError(f"invalid status: {updates['status']}")
    if "priority" in updates and updates["priority"] not in TASK_PRIORITIES:
        raise ValueError(f"invalid priority: {updates['priority']}")
    if "due_at" in updates:
        updates["due_at"] = _normalize_due(updates["due_at"])
    now = _iso()
    with connect(db_path, write=True) as conn:
        if "assignee_role" in updates:
            updates["assignee_role"] = _agent_role(conn, updates["assignee_role"])
        current = conn.execute(
            "SELECT status, blocked_on FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        if current is None:
            return False
        _apply_blocked_on(updates, current)
        sets = ", ".join(f"{k}=?" for k in updates) + ", updated_at=?"
        params = list(updates.values()) + [now, task_id]
        cur = conn.execute(f"UPDATE agent_tasks SET {sets} WHERE id=?", params)
        if cur.rowcount:
            _log_event(conn, actor or "system", "task_update", str(task_id), updates)
        return bool(cur.rowcount)


def task_close(task_id: int, *, status: str = "done", note: str | None = None,
               actor: str | None = None, db_path: Path | str | None = None) -> bool:
    if status not in ("done", "cancelled"):
        raise ValueError("close status must be done or cancelled")
    return task_update(task_id, status=status, result_note=note, actor=actor,
                       db_path=db_path)

def sync_meeting_close_tasks(conn) -> int:
    """Project every live work meeting into its attendees' task lists.

    An open meeting is unfinished work and closing it is the agents' job — but a
    meeting lives in its own tables, not in anyone's queue. "I still owe this a
    close" therefore existed nowhere an agent looks between wakes, and the only
    thing that ever noticed was the idle deadline retiring it an hour later.
    This is the durable, poll-free version of remembering.

    Soft on purpose. priority='normal' never INTERRUPTS: a conversation that is
    still going does not need an attendee cut off mid-turn to be told to end it.
    An attendee that is idle gets woken for it like any other queued work
    (idle_task — there is no turn to interrupt, and two parked agents holding a
    meeting open is precisely the case nothing else was noticing). It turns
    urgent, and interrupts, exactly when the thread goes idle: nobody is talking,
    nobody closed it, and a reminder in a list an agent is not currently reading
    has already failed. Then it is demand like any other and climbs the ladder.

    DMs are excluded. A one-to-one with the supervisor is theirs to end
    (meetings._propose_end refuses the agent), so a close task there would be one
    the agent cannot discharge: it would sit pending forever, and once urgent it
    would climb the ladder to the very human it was told not to bother.

    Idempotent and self-healing, like sync_delivery: the meeting rows are the
    truth and this only mirrors them. Requires a write transaction.
    """
    now = _iso()
    supervisor = CONFIG.supervisor_role
    live: dict[str, dict] = {}
    for m in conn.execute(
            """SELECT m.thread_id, m.agenda, t.stop_reason, t.status AS thread_status
               FROM meetings m JOIN mailbox_threads t ON t.id=m.thread_id
               WHERE m.state IN ('active','consensus')"""):
        agents = [r["role"] for r in conn.execute(
            """SELECT role FROM meeting_attendees
               WHERE thread_id=? AND checked_in_at IS NOT NULL
                 AND stopped_at IS NULL AND role!=?""",
            (m["thread_id"], supervisor))]
        if len(agents) < 2:
            continue  # a DM, or nobody to hold to it
        # Idle means the conversation is over in every sense except the ledger's:
        # it stopped, nobody ended it, and it is now stale work only a wake will
        # clear. `meetings.state` stays 'active' through an idle timeout, so the
        # meeting is still closeable — the task remains dischargeable.
        idle = m["thread_status"] == "paused" and m["stop_reason"] == "idle timeout"
        live[m["thread_id"]] = {"agenda": m["agenda"], "agents": agents,
                                "priority": "urgent" if idle else "normal"}

    touched = 0
    for thread_id, info in live.items():
        for role in info["agents"]:
            row = conn.execute(
                """SELECT id, status, priority FROM agent_tasks
                   WHERE source_kind='meeting' AND source_ref=? AND assignee_role=?""",
                (thread_id, role)).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO agent_tasks
                       (title,detail,assignee_role,status,priority,source_kind,
                        source_ref,created_by,created_at,updated_at)
                       VALUES (?,?,?,'pending',?,'meeting',?,'orchestrator',?,?)""",
                    (f"close the meeting: {info['agenda']}"[:200],
                     "Agree an end with the other attendees (propose-end / "
                     "confirm-end). Leaving it open is the work not finished.",
                     role, info["priority"], thread_id, now, now))
                touched += 1
            elif row["status"] == "pending" and row["priority"] != info["priority"]:
                conn.execute(
                    "UPDATE agent_tasks SET priority=?,updated_at=? WHERE id=?",
                    (info["priority"], now, row["id"]))
                touched += 1

    # Retire what is no longer owed: the meeting closed, or this role left it.
    # Without this the queue fills with closes nobody can perform, and any that
    # had gone urgent would climb the ladder over a finished conversation.
    for row in conn.execute(
            "SELECT id, source_ref, assignee_role FROM agent_tasks "
            "WHERE source_kind='meeting' AND status='pending'").fetchall():
        info = live.get(row["source_ref"])
        if info is None or row["assignee_role"] not in info["agents"]:
            conn.execute(
                """UPDATE agent_tasks SET status='done',updated_at=?,
                   result_note='meeting is no longer open to this role' WHERE id=?""",
                (now, row["id"]))
            touched += 1
    return touched

#: The exact agent_tasks rows that raise an `urgent_task` demand. ONE string,
#: used both by the query that raises that demand and by the query that excludes
#: those rows from idle_task's actionable set. The two must partition the queue —
#: every open task raises exactly one kind of demand or is a reported fact — and
#: a second spelling of "already being woken for this" is how they would stop.
#: Note it is not "priority != urgent": an urgent task that has gone in_progress
#: raises NO urgent_task demand (that clause is `status='pending'`), so excluding
#: it by priority alone would drop it out of every wake path — the exact hole
#: this whole change exists to close, re-opened at its most expensive task.
_URGENT_TASK_WHERE = "priority='urgent' AND status='pending'"

def _queued_tasks(conn, role: str | None = None) -> tuple[list[dict], list[dict]]:
    """(actionable, stalled) — open work whose assignee nothing else is waking.

    ACTIONABLE is what a wake could still move: assigned, open, not already
    carried by an urgent_task demand, not blocked (it waits on a named
    dependency; waking its assignee cannot make that dependency happen), and not
    stalled.

    STALLED is DERIVED, never stored, from the ledger that already exists: the
    number of idle_task wakes this role has had since the task last moved. Time-
    dependent state is computed at read time here (see _delivery_state,
    _presence_row) precisely so it cannot go stale, and a `stalled_at` column
    would be a second, staler copy of a fact the wake_attempts rows already hold.

    A stalled task LEAVES the actionable set, and that is the whole loop breaker:
    we woke the agent for its queue, it looked, it did not move this — so this
    stops being a reason to wake anyone and becomes a fact someone must decide
    about (board()'s health.stalled_tasks). No cooldown, no timer, no new state:
    the demand stops because the reason for it stopped being true. A cooldown
    would have been a patch over the missing rule, which is the mistake this
    module keeps making.

    The count is role-scoped, not task-scoped, on purpose: wake_sources() shows a
    woken agent its WHOLE queue, so every idle_task wake is an occasion on which
    this task was in front of it and did not move.
    """
    where = ["t.status IN ('pending','in_progress')", f"NOT ({_URGENT_TASK_WHERE})"]
    params: list = []
    if role is not None:
        where.append("t.assignee_role=?")
        params.append(role)
    rows = conn.execute(
        f"""SELECT t.*, (SELECT COUNT(*) FROM wake_attempts w
                         WHERE w.role=t.assignee_role AND w.reason_kind='idle_task'
                           AND w.attempted_at > t.updated_at) AS idle_wakes_since_move
            FROM agent_tasks t WHERE {" AND ".join(where)} ORDER BY t.id""",
        params).fetchall()
    threshold = CONFIG.idle_task_stall_wakes
    actionable, stalled = [], []
    for r in rows:
        (stalled if r["idle_wakes_since_move"] >= threshold
         else actionable).append(dict(r))
    return actionable, stalled
