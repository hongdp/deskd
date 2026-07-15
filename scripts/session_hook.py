#!/usr/bin/env python3
"""PostToolUse hook: push pending deskd work into a live agent's context.

Install this as a PostToolUse hook in the harness that runs your agents. It is
the mid-session *listening guarantee*: even while an agent is deep in unrelated
work, pending wake requests, unread meeting messages, un-attended invitations
and queued inbox items surface in its context within one tool call plus the
rate-limit window (45 s). Without it an agent is only reachable between turns,
and the wake ladder would have to resume/spawn sessions for work an already
online agent could have handled itself (rung L0 "hook" exists precisely so it
does not have to).

Design constraints, in order of importance:

1. It MUST NEVER break the calling session. Every failure path — missing DB,
   missing table, busy writer, malformed payload, no registry — exits 0 with no
   output. A coordination hiccup must never surface as a tool error inside the
   host agent's turn.
2. Stdlib only, no deskd import. The hook runs inside the *host agent's*
   process tree, which may not have deskd's venv on sys.path. It therefore
   talks to the coordination DB directly and self-locates it (DESKD_DB).
   The queries below duplicate a little engine SQL; that is the deliberate
   price of not coupling the hook to an importable package.
3. No hardcoded roles. ``agent_registry`` is the source of truth. Role literals
   only ever reach SQL as bound placeholders built from the registry.

Writes are deliberately minimal and bounded: an UPDATE-only presence heartbeat,
the TodoWrite mirror, and stamping inbox items delivered. Everything else is a
read-only query.
"""
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import time

#: Notices are recomputed at most this often per session. One tool call plus
#: this window is the worst-case latency for an online agent to see new work.
RATE_SECONDS = 45

#: The role this session declared. Exported by the wake driver when it
#: spawns/resumes, and by any host runner that starts a session for a role.
ROLE_ENV = "DESKD_ROLE"

#: Broadcast recipient tokens understood by the mailbox layer. The engine's
#: mailbox module owns the canonical value; both spellings are accepted here so
#: the hook cannot silently miss broadcast messages if that value is renamed.
#: An unused token matches nothing, so accepting both costs nothing.
BROADCAST_RECIPIENTS = ("all", "both")

#: Notice lines that start with "<role>:" are wake-worthy demand. Anything a
#: role-startswith counter must NOT treat as a reason to wake gets one of these
#: prefixes instead (see _pending: soft deadlines surface but never wake).
TASK_PREFIX = "[deskd-task]"
INBOX_PREFIX = "[deskd-inbox]"


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _db_path() -> str:
    """Locate the coordination DB without importing deskd.

    Mirrors deskd.config: DESKD_DB wins; otherwise <base>/data/deskd.db. The
    base is DESKD_HOME, else the harness's project dir, else this script's
    repository root (scripts/session_hook.py -> ../). Never cwd: a hook runs
    with the host session's cwd, which is not ours to assume.
    """
    explicit = os.environ.get("DESKD_DB")
    if explicit:
        return explicit
    base = (os.environ.get("DESKD_HOME")
            or os.environ.get("CLAUDE_PROJECT_DIR")
            or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "data", "deskd.db")


def _known_roles(conn) -> tuple[str, ...]:
    """The registered agent roles. deskd has NO hardcoded roles: the registry
    is the source of truth, and the supervisor is by construction NOT in it.
    Registry membership is therefore exactly the agent/supervisor boundary —
    which is how this hook enforces "agent-facing surfaces reject the
    supervisor" without ever naming the supervisor role."""
    try:
        rows = conn.execute(
            "SELECT role FROM agent_registry WHERE enabled=1 ORDER BY role").fetchall()
    except sqlite3.Error:
        return ()
    return tuple(r[0] for r in rows)


def _heartbeat(session_id: str) -> None:
    """Refresh this session's presence heartbeat (UPDATE-only, best-effort).

    We never CREATE the row: registration is an explicit `deskd status set` at
    session start; the hook only keeps an existing, non-ended row warm. That
    asymmetry matters — a hook that could create presence would let a session
    that never registered masquerade as an online agent. Any failure (table
    missing, no row, busy DB) is swallowed so the host session is safe.
    """
    role = os.environ.get(ROLE_ENV)
    if not role:
        return
    db = _db_path()
    if not os.path.exists(db):
        return
    conn = sqlite3.connect(db, timeout=1)
    try:
        conn.execute("PRAGMA busy_timeout = 1500")
        if role not in _known_roles(conn):
            return
        conn.execute(
            """UPDATE agent_sessions
               SET last_heartbeat_at=?, session_id=COALESCE(session_id, ?)
               WHERE role=? AND ended_at IS NULL""",
            (_iso_now(), session_id or None, role),
        )
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def _capture_todos(payload) -> None:
    """Mirror this session's live TodoWrite list into session_todos so the board
    can show what the agent is executing now vs queued next.

    Fires on EVERY TodoWrite and runs BEFORE the rate limit: the todo list is
    the agent's own statement of intent, it changes rarely, and a stale mirror
    is worse than a slightly chattier write. Best-effort; never raises.
    """
    if payload.get("tool_name") != "TodoWrite":
        return
    role = os.environ.get(ROLE_ENV)
    if not role:
        return
    todos = (payload.get("tool_input") or {}).get("todos")
    if todos is None:
        return
    db = _db_path()
    if not os.path.exists(db):
        return
    conn = sqlite3.connect(db, timeout=1)
    try:
        conn.execute("PRAGMA busy_timeout = 1500")
        # Registry check before an INSERT (unlike the UPDATE-only heartbeat,
        # this statement can create a row — an unregistered role must not).
        if role not in _known_roles(conn):
            return
        conn.execute(
            """INSERT INTO session_todos (role, snapshot, updated_at) VALUES (?,?,?)
               ON CONFLICT(role) DO UPDATE SET snapshot=excluded.snapshot,
                   updated_at=excluded.updated_at""",
            (role, json.dumps(todos), _iso_now()))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def _inbox_deliver(role: str) -> list[str]:
    """Surface the role's un-acked inbox items and mark the not-yet-delivered
    ones delivered.

    This hook IS the in-session delivery path for an online agent, and one of
    only two places that may stamp delivered_at (the other being an explicit
    ack). The wake planner must never stamp it speculatively: it does not know
    whether the driver will actually launch the session. Delivery is recorded
    here because the item has now provably entered an agent's context.

    Best-effort/writable; any failure returns [] (the item stays undelivered,
    the demand stays alive, and the ladder escalates — the safe direction).
    """
    if not role:
        return []
    db = _db_path()
    if not os.path.exists(db):
        return []
    conn = sqlite3.connect(db, timeout=1)
    lines = []
    try:
        conn.execute("PRAGMA busy_timeout = 1500")
        if role not in _known_roles(conn):
            return []
        rows = conn.execute(
            """SELECT id, priority, source_kind, title, delivered_at
               FROM agent_inbox WHERE target_role=? AND acked_at IS NULL
               ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id""",
            (role,),
        ).fetchall()
        undelivered = [r[0] for r in rows if r[4] is None]
        for r in rows:
            mark = "[!]" if r[1] == "urgent" else ""
            lines.append(f"{INBOX_PREFIX} {role}: {mark}{r[2]}: {r[3]}")
        if undelivered:
            q = ",".join("?" * len(undelivered))
            conn.execute(
                f"UPDATE agent_inbox SET delivered_at=? WHERE id IN ({q})",
                [_iso_now(), *undelivered])
            conn.commit()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    if lines:
        lines.append(f"{INBOX_PREFIX} {role}: when handled, run "
                     f"`deskd inbox ack --for {role}`")
    return lines


def _rate_limited(session_id: str) -> bool:
    """True if this session was notified less than RATE_SECONDS ago. The stamp
    is per session id, so concurrent sessions never rate-limit each other."""
    stamp_dir = os.path.join(tempfile.gettempdir(), "deskd-session-hook")
    os.makedirs(stamp_dir, exist_ok=True)
    stamp = os.path.join(stamp_dir, f"{session_id or 'unknown'}.stamp")
    now = time.time()
    try:
        if now - os.path.getmtime(stamp) < RATE_SECONDS:
            return True
    except OSError:
        pass
    with open(stamp, "w") as fh:
        fh.write(str(now))
    return False


def _pending(conn, roles: tuple[str, ...]) -> list[str]:
    """Read-only notice lines for all registered roles.

    Deliberately NOT filtered to this session's role: a session may be running
    without DESKD_ROLE set, and the notice text tells the reader to ignore rows
    that are not its own. `roles` comes from the registry and only ever enters
    SQL as bound placeholders.
    """
    if not roles:
        return []
    lines = []
    ph = ",".join("?" * len(roles))
    recip = ",".join("?" * len(BROADCAST_RECIPIENTS))

    wakes = conn.execute(
        """SELECT w.role, w.thread_id, m.agenda FROM meeting_wake_requests w
           JOIN meetings m ON m.thread_id=w.thread_id
           WHERE w.status='pending'""",
    ).fetchall()
    for role, thread_id, agenda in wakes:
        lines.append(
            f"{role}: pending wake for {thread_id} ({agenda}) — "
            f"run `deskd meeting wake-ack` + `check-in`"
        )

    invited = conn.execute(
        f"""SELECT a.role, a.thread_id, m.agenda FROM meeting_attendees a
            JOIN meetings m ON m.thread_id=a.thread_id
            WHERE m.state='waiting' AND a.required=1 AND a.role IN ({ph})
              AND a.checked_in_at IS NULL AND a.stopped_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM meeting_wake_requests w
                              WHERE w.thread_id=a.thread_id AND w.role=a.role
                                AND w.status='pending')""",
        roles,
    ).fetchall()
    for role, thread_id, agenda in invited:
        lines.append(
            f"{role}: invited to waiting meeting {thread_id} ({agenda}) — "
            f"run `deskd meeting check-in`"
        )

    # Unread meeting messages. Two authenticity gates, preserved verbatim in
    # spirit from the engine:
    #   - an AGENT's message only counts once that agent has checked in (no
    #     surfacing messages from a role that is not actually in the meeting);
    #   - a message from anyone NOT in the registry is a supervisor message and
    #     only counts if it carries an auth row (meeting_message_auth). That is
    #     engine security policy: an unauthenticated "supervisor" message must
    #     never reach an agent's context, and stating it as "sender not in the
    #     registry" keeps it true for any supervisor_role the host configures.
    unread = conn.execute(
        f"""SELECT a.role, a.thread_id, COUNT(*) FROM meeting_attendees a
            JOIN meetings m ON m.thread_id=a.thread_id
            JOIN mailbox_messages mm ON mm.thread_id=a.thread_id
                 AND mm.recipient IN (a.role, {recip}) AND mm.sender!=a.role
            LEFT JOIN mailbox_receipts r
                 ON r.message_id=mm.id AND r.role=a.role
            WHERE m.state IN ('active','consensus') AND a.role IN ({ph})
              AND a.checked_in_at IS NOT NULL AND a.stopped_at IS NULL
              AND r.message_id IS NULL
              AND ((mm.sender IN ({ph}) AND EXISTS
                    (SELECT 1 FROM meeting_attendees va
                     WHERE va.thread_id=mm.thread_id AND va.role=mm.sender
                       AND va.checked_in_at IS NOT NULL))
                   OR (mm.sender NOT IN ({ph}) AND EXISTS
                       (SELECT 1 FROM meeting_message_auth ma
                        WHERE ma.message_id=mm.id)))
            GROUP BY a.role, a.thread_id""",
        (*BROADCAST_RECIPIENTS, *roles, *roles, *roles),
    ).fetchall()
    for role, thread_id, count in unread:
        lines.append(
            f"{role}: {count} unread message(s) in {thread_id} — "
            f"run `deskd meeting updates --mark-read`"
        )

    # Overdue work items (soft-deadline visibility). Prefixed with the task
    # marker so a `pending_for <role>` counter (which matches lines *starting*
    # with the role) never treats an overdue task as a reason to wake — soft
    # deadlines surface but never wake. Fully guarded: absent table => no lines.
    try:
        overdue = conn.execute(
            """SELECT assignee_role, COUNT(*) FROM agent_tasks
               WHERE status IN ('pending','in_progress','blocked')
                 AND due_at IS NOT NULL AND due_at < ?
               GROUP BY assignee_role""",
            (_iso_now(),),
        ).fetchall()
        for role, count in overdue:
            lines.append(
                f"{TASK_PREFIX} {role}: {count} overdue task(s) — "
                f"run `deskd task list --for {role}`"
            )
    except sqlite3.Error:
        pass
    return lines


def main() -> None:
    payload = json.load(sys.stdin)
    session_id = str(payload.get("session_id", ""))
    try:
        _capture_todos(payload)   # every TodoWrite, before the rate limit
    except Exception:
        pass
    if _rate_limited(session_id):
        return
    try:
        _heartbeat(session_id)
    except Exception:
        pass
    db = _db_path()
    if not os.path.exists(db):
        return
    # Read-only URI: the notice pass cannot mutate even if a query is wrong.
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
    try:
        lines = _pending(conn, _known_roles(conn))
    finally:
        conn.close()
    # Deliver this session's own inbox items (role from DESKD_ROLE). Separate
    # connection: this one writes.
    try:
        lines += _inbox_deliver(os.environ.get(ROLE_ENV) or "")
    except Exception:
        pass
    if not lines:
        return
    context = (
        "[deskd watch] Pending orchestration work detected:\n- "
        + "\n- ".join(lines)
        + "\nIf one of these roles is YOUR declared role, handle it at the next "
        "natural boundary in your current task (protocol: wake-ack / check-in / "
        "updates; never block on replies). If it is not your role, ignore this "
        "notice. Commands: `deskd meeting ...`, `deskd inbox ...`, "
        "`deskd task ...`."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        },
    }))


if __name__ == "__main__":
    # The outermost guarantee: this hook never raises into its host session.
    # A coordination problem must not become the agent's problem.
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
