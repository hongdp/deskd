"""The wake orchestrator: demand collection, the escalation ladder,
plan_wakes (decide, never execute), human-rung escalations, session
rollover, and the per-role wake_sources answer.
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from pathlib import Path

from .. import channels, meetings
from . import store
from ..config import CONFIG, PROJECT_NAME
from .delivery import _delivery_state, _wake_keys, sync_delivery
from .hooks import _eval_wake_hooks
from .inbox import _inbox_sort_key, _route_unroutable
from .presence import _is_busy, _presence_list
from .store import (_agent_role, _iso, _known_roles, _load_json,
                    _log_event, _session_day, connect)
from .tasks import _URGENT_TASK_WHERE, _queued_tasks, \
    sync_meeting_close_tasks

# --- wake orchestrator ------------------------------------------------------

WAKE_REASONS = {"meeting_wake", "stuck_delivery", "urgent_task", "owed_reply",
                "inbox", "idle_task"}


def _ladder():
    """The escalation ladder in effect (CONFIG.wake_ladder)."""
    return CONFIG.wake_ladder


def _channel_level(ladder, channel: str, fallback: int) -> int:
    """Index of the rung with `channel`, or a clamped fallback.

    Levels are ladder INDICES, so nothing may assume a fixed number here: a host
    can define its own ladder. Look the channel up by name instead.
    """
    for i, rung in enumerate(ladder):
        if rung.channel == channel:
            return i
    return max(0, min(fallback, len(ladder) - 1))


def _human_level(ladder) -> int:
    """First rung that leaves the machine (a human is being pulled in).

    Used for the 'wakes at human level' health counter — the number that should
    make someone look at the board.

    The rung declares this itself (``WakeRung.leaves_machine``) for the same
    reason ``_channel_level`` looks channels up by name: the ladder is the host's
    to define, so matching on the default ladder's channel NAMES would silently
    mis-count the moment a host renamed or reordered its own rungs. A ladder that
    marks nothing keeps the historical positional guess.
    """
    for i, rung in enumerate(ladder):
        if rung.leaves_machine:
            return i
    return max(0, len(ladder) - 2)


def _reason_ceiling(reason_kind: str, ladder) -> int:
    """The highest rung this reason may ever occupy. -1 = it may not wake at all.

    HARD RULE: a task wake must NEVER page a person. The ladder climbs because a
    message MUST land, and it ends at a human because that is the last thing that
    can make it land. A queue has no such property — nobody is owed it and nothing
    breaks if it waits for morning — so for MACHINE_ONLY_REASONS the ladder is
    fenced at the last rung that stays on the machine.

    This is a CEILING, applied to the start rung and to every escalation, not an
    argument that the demand resolves too fast to climb. It does resolve fast (the
    moment the agent boots), and that is exactly the sort of reasoning that stays
    true until some unrelated change makes it false — at which point the failure
    is a person's phone at 3am. So it cannot climb there even if it sits forever.

    Scoped by PURPOSE, not by channel name: the rung declares whether it leaves
    the machine (WakeRung.leaves_machine), so a host that renames its rungs,
    reorders them, or pages a person from rung 1 is still fenced correctly. A
    ladder whose every rung leaves the machine yields -1 and the demand is never
    raised at all: failing to wake an idle agent for its own to-do list is a
    disappointment, waking a person for it is a breach.
    """
    if reason_kind not in MACHINE_ONLY_REASONS:
        return len(ladder) - 1
    return _human_level(ladder) - 1

#: Wake reasons that may never climb to a rung that leaves the machine.
#:
#: The ladder exists because a MESSAGE MUST LAND: it keeps climbing until someone
#: — ultimately a person — reacts. A to-do list has no such property. Nothing is
#: owed to anyone, nothing breaks if it waits until morning, and there is no
#: answer a human woken at 3am could give that the queue needed. So `idle_task`
#: is fenced to the machine rungs BY CONSTRUCTION rather than by the argument
#: that it always resolves quickly (see _reason_ceiling).
MACHINE_ONLY_REASONS = frozenset({"idle_task"})

def _idle_task_demand(conn, role: str, now: dt.datetime) -> dict | None:
    """THE idle_task predicate — the demand, or None. Nothing else defines it.

    "Soft deadlines never wake" was written against INTERRUPTION: deadlines shape
    attention, they don't manufacture interrupts. That is still absolute, and it
    is what `_is_busy` gates. But it was over-broad, because an IDLE agent has no
    turn to interrupt: waking it for its own queue is not an interrupt, it is
    scheduling. Without this, an idle agent with a to-do list sleeps forever and
    the list is write-only — which makes "agents must never manage their own
    waking" false, and that is the framework's whole thesis.

    Note what is NOT in here: due_at. A queue entry is a queue entry; the deadline
    changes ordering and nothing else, or it would be a wake trigger by the back
    door.

    Both callers go through this function — collect_wake_demand to raise it, and
    _demand_resolved to decide it is over — so the two cannot drift. That is not
    tidiness: generation and resolution disagreeing is this module's most-repeated
    bug (see the stuck_delivery branch's comment, and the five it lists).
    """
    if _is_busy(conn, role, now):
        return None
    actionable, _ = _queued_tasks(conn, role)
    if not actionable:
        return None
    titles = ", ".join(t["title"] for t in actionable[:3])
    more = f" (+{len(actionable) - 3} more)" if len(actionable) > 3 else ""
    return {"role": role, "reason_kind": "idle_task",
            "source_ref": f"idle_task:{role}",
            "label": f"{len(actionable)} open task(s): {titles}{more}",
            "since_at": min(t["created_at"] for t in actionable)}


def collect_wake_demand(conn) -> list[dict]:
    """Unify the DB-derived wake demands.

    Note: task ``due_at`` is deliberately NOT a source — soft deadlines never
    wake. An open task wakes its assignee only while that assignee is IDLE
    (``idle_task``, which interrupts nothing); ``priority='urgent'`` is the only
    task state that wakes regardless.
    """
    now = store._now()
    now_iso = _iso(now)
    demands = []
    for r in conn.execute(
            """SELECT w.role, w.thread_id, m.agenda, w.created_at
               FROM meeting_wake_requests w JOIN meetings m ON m.thread_id=w.thread_id
               WHERE w.status='pending'"""):
        demands.append({"role": r["role"], "reason_kind": "meeting_wake",
                        "source_ref": r["thread_id"], "label": r["agenda"],
                        "since_at": r["created_at"]})
    wake = _wake_keys(conn)
    # A CLOSED thread raises no wake. Its ledger rows still read `overdue` — that
    # is an honest record that the message was never read — but waking an agent
    # to go and read a conversation that has already concluded accomplishes
    # nothing, and the demand cannot be resolved by anything the agent does, so it
    # regenerates every tick: a permanent wake loop over dead threads.
    # `paused` and `escalated` threads DO wake: they can still resume, so an
    # unread message in one is genuinely undelivered.
    for r in conn.execute(
            """SELECT d.* FROM message_delivery d
               JOIN mailbox_messages mm ON mm.id=d.message_id
               JOIN mailbox_threads t ON t.id=mm.thread_id
               WHERE t.status != 'closed'"""):
        if _delivery_state(r, now_iso, wake) == "overdue":
            demands.append({"role": r["recipient_role"], "reason_kind": "stuck_delivery",
                            "source_ref": f'{r["thread_id"]}:{r["message_id"]}',
                            "label": f'msg#{r["message_id"]}', "since_at": r["queued_at"]})
    for r in conn.execute(
            f"SELECT id, assignee_role, title, created_at FROM agent_tasks "
            f"WHERE {_URGENT_TASK_WHERE}"):
        demands.append({"role": r["assignee_role"], "reason_kind": "urgent_task",
                        "source_ref": str(r["id"]), "label": r["title"],
                        "since_at": r["created_at"]})
    # An idle agent with queued work. One demand per ROLE, not per task: it asks
    # for the agent to be booted, and a booted agent sees its whole queue
    # (wake_sources), so a demand per task would be N ladders racing to cause the
    # one wake they all want.
    for role in sorted(_known_roles(conn)):
        d = _idle_task_demand(conn, role, now)
        if d is not None:
            demands.append(d)
    # An owed meeting reply past its SLA. Distinct from stuck_delivery, which
    # means "never read it": this one means "read it and has not answered", and
    # only a wake fixes that. meetings used to page a human for this directly,
    # skipping every machine rung; it now leaves the rows and lets the ladder do
    # its job. Restricted to live meetings for the same reason stuck_delivery
    # skips closed threads — an obligation in a stopped meeting cannot be
    # discharged by anything the agent does, so it would regenerate every tick
    # and climb forever.
    for r in conn.execute(
            """SELECT o.message_id, o.thread_id, o.owed_by, o.created_at, m.agenda
               FROM meeting_response_obligations o
               JOIN meetings m ON m.thread_id=o.thread_id
               WHERE o.status='pending' AND o.due_at<=?
                 AND m.state IN ('active','consensus')""", (now_iso,)):
        demands.append({"role": r["owed_by"], "reason_kind": "owed_reply",
                        "source_ref": f'{r["thread_id"]}:{r["message_id"]}',
                        "label": f'owes a reply in {r["agenda"]}',
                        "since_at": r["created_at"]})
    # Unified inbox — batched: an urgent item wakes now; non-urgent items wake
    # once the oldest has waited CONFIG.inbox_batch_seconds (or ride along with
    # any other demand for the role, since plan_wakes batches per role).
    inbox_by_role: dict = {}
    for r in conn.execute(
            "SELECT target_role, priority, enqueued_at FROM agent_inbox "
            "WHERE acked_at IS NULL AND delivered_at IS NULL"):
        inbox_by_role.setdefault(r["target_role"], []).append(r)
    for role, items in inbox_by_role.items():
        oldest = min(i["enqueued_at"] for i in items)
        has_urgent = any(i["priority"] == "urgent" for i in items)
        age = (now - dt.datetime.fromisoformat(oldest)).total_seconds()
        if has_urgent or age > CONFIG.inbox_batch_seconds:
            demands.append({"role": role, "reason_kind": "inbox",
                            "source_ref": f"inbox:{role}",
                            "label": f"{len(items)} notification(s)", "since_at": oldest})
    return demands


def _demand_resolved(conn, role: str, reason_kind: str, source_ref: str,
                     now_iso: str) -> tuple[bool, str]:
    """Closed-loop check: has the underlying demand been satisfied?"""
    if reason_kind == "meeting_wake":
        pend = conn.execute(
            "SELECT 1 FROM meeting_wake_requests WHERE thread_id=? AND role=? "
            "AND status='pending'",
            (source_ref, role)).fetchone()
        return (pend is None, "acked")
    if reason_kind == "urgent_task":
        try:
            tid = int(source_ref)
        except ValueError:
            return (True, "acked")
        r = conn.execute(
            "SELECT status, priority, assignee_role FROM agent_tasks WHERE id=?",
            (tid,)).fetchone()
        # Resolved when the task is gone OR is no longer an urgent-pending demand
        # FOR THIS ROLE. Every clause here mirrors collect_wake_demand's
        # predicate, and `assignee_role` is a clause: reassign a task away and
        # the old assignee's attempt would otherwise escalate forever over work
        # that is no longer theirs.
        resolved = r is None or not (
            r["status"] == "pending" and r["priority"] == "urgent"
            and r["assignee_role"] == role)
        return (resolved, "acked")
    if reason_kind == "idle_task":
        # Resolution is generation, negated — not a mirror of it. Every other
        # branch here re-states its collect_wake_demand predicate by hand and
        # comments about which clause was forgotten last time; the count of those
        # comments is the argument for not doing it a sixth time. This branch
        # asks the collector itself, so "resolved" means exactly "the collector
        # would no longer raise this", for every clause it has and any it grows:
        # role, idleness, actionability, urgency and stall, forever, by
        # construction.
        now = dt.datetime.fromisoformat(now_iso)
        if _idle_task_demand(conn, role, now) is not None:
            return (False, "")
        # It stopped being raised for one of two reasons, and they are not the
        # same event. Busy = the agent booted, which is ALL this demand ever
        # asked for: that is a landed wake, and its latency is real. Still idle =
        # the queue emptied under it, or every task in it stalled — the wake
        # never landed and we have stopped trying, which must not be filed as a
        # success or the stall breaker would hide inside the wake stats it is
        # supposed to be visible against.
        return (True, "acked" if _is_busy(conn, role, now) else "timeout")
    if reason_kind == "owed_reply":
        # Mirrors collect_wake_demand's predicate clause for clause. The commit
        # before this one exists because generation and resolution disagreed in
        # five places; every WHERE above has a line here on purpose.
        thread, _, msg = source_ref.partition(":")
        try:
            mid = int(msg)
        except ValueError:
            return (True, "acked")
        r = conn.execute(
            """SELECT o.status, o.owed_by, m.state
               FROM meeting_response_obligations o
               JOIN meetings m ON m.thread_id=o.thread_id
               WHERE o.message_id=?""", (mid,)).fetchone()
        # Gone, answered, reassigned, or the meeting stopped: collect_wake_demand
        # raises none of these, so resolution must expect none of them either —
        # an attempt outstanding when a meeting closes would otherwise strand
        # pending and climb the ladder forever over a conversation nobody can
        # rejoin.
        resolved = r is None or not (
            r["status"] == "pending" and r["owed_by"] == role
            and r["state"] in ("active", "consensus"))
        return (resolved, "acked")
    if reason_kind == "stuck_delivery":
        thread, _, msg = source_ref.partition(":")
        try:
            mid = int(msg)
        except ValueError:
            return (True, "read")
        r = conn.execute(
            "SELECT read_at FROM message_delivery WHERE message_id=? AND recipient_role=?",
            (mid, role)).fetchone()
        if r is None or r["read_at"]:
            return (True, "read")
        # The thread closed: collect_wake_demand stops raising this, so resolution
        # must stop expecting it — otherwise every attempt outstanding at the
        # moment a thread closes is stranded pending and climbs the ladder
        # forever over a conversation nobody can rejoin. Another clause of
        # collect_wake_demand's predicate, and another one that had to be
        # mirrored by hand.
        if conn.execute("SELECT 1 FROM mailbox_threads WHERE id=? AND status='closed'",
                        (thread,)).fetchone():
            return (True, "superseded")
        # Another channel took over: a PENDING wake request for THIS role.
        #
        # Two independent things have to be right here, and each was wrong once:
        #  - SCOPE: (recipient, item), never the thread that contains it
        #    (design.md §Delivery rule 2). A thread-level escalation is raised BY
        #    one role and says nothing about whether anything is re-driving
        #    delivery to another.
        #  - TENSE: is something reacting NOW, not has something ever reacted.
        #    Without status='pending' this asks the second question, and a single
        #    acknowledged wake — i.e. one that already SUCCEEDED — closes this
        #    demand forever.
        # This predicate must stay identical to _delivery_state()'s `wake` test:
        # collect_wake_demand raises this demand exactly when that returns
        # 'overdue', so any disagreement closes the attempt every tick and
        # re-inserts it at the start rung — the demand looks busy and never
        # climbs the ladder. Identical to a WRONG test is still wrong, though:
        # both read the same table, so both must ask the same, present-tense
        # question. Use _wake_keys() semantics, not a hand-rolled copy.
        if (thread, role) in _wake_keys(conn):
            return (True, "superseded")
        return (False, "")
    if reason_kind == "inbox":
        # Resolved once the role has no UNDELIVERED items (delivery = the wake
        # put them in front of the agent; acking is the agent's own step).
        n = conn.execute(
            "SELECT COUNT(*) FROM agent_inbox WHERE target_role=? "
            "AND acked_at IS NULL AND delivered_at IS NULL", (role,)).fetchone()[0]
        return (n == 0, "acked")
    return (True, "acked")


def _start_level(p: dict | None, ladder) -> int:
    """Which rung a brand-new demand starts on, given the role's presence."""
    spawn = _channel_level(ladder, "spawn", 2)
    if p is None:
        return spawn                              # unknown role -> spawn
    if p["liveness"] == "online":
        return _channel_level(ladder, "hook", 0)  # in-session hook will deliver
    if p.get("session_id") and not p.get("ended_at"):
        return _channel_level(ladder, "resume", 1)  # resumable session exists
    return spawn                                  # no live/resumable session


def _insert_attempt(conn, d: dict, level: int, now_iso: str, ladder) -> int:
    channel = ladder[level].channel
    cur = conn.execute(
        """INSERT INTO wake_attempts
               (role, reason_kind, source_ref, channel, level, attempted_at, outcome, detail)
           VALUES (?,?,?,?,?,?, 'pending', ?)""",
        (d["role"], d["reason_kind"], d["source_ref"], channel, level, now_iso,
         d.get("label")),
    )
    return cur.lastrowid


def _queue_wake_escalation(conn, d: dict, level: int, now_iso: str) -> int:
    """Durable half of the human rung, written on ARRIVAL at a leaves_machine
    rung — once per climb, inside the planning transaction. The row exists
    whether or not any channel is registered; dispatch mirrors it out after
    commit (_dispatch_wake_escalation), exactly the meetings pattern: a slow
    channel must never hold the planning write lock."""
    cur = conn.execute(
        """INSERT INTO wake_escalations
               (role, reason_kind, source_ref, level, channel, reason, created_at)
           VALUES (?,?,?,?, 'auto', ?, ?)""",
        (d["role"], d["reason_kind"], d["source_ref"], level,
         d.get("label"), now_iso))
    _log_event(conn, "orchestrator", "wake_escalation_queued", d["source_ref"],
               {"role": d["role"], "reason": d["reason_kind"], "level": level})
    return cur.lastrowid


def _dispatch_wake_escalation(escalation_id: int,
                              db_path: Path | str | None) -> dict:
    """Mirror a queued wake escalation out through the channel layer. Called
    after the planning transaction committed. The ledger row is already the
    delivery of last resort; channels only improve on it."""
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM wake_escalations WHERE id=?",
                           (escalation_id,)).fetchone()
    if row is None:
        return {"id": escalation_id, "status": "missing"}
    subject = f"{PROJECT_NAME} wake escalation: {row['role']}"
    text = (f"{PROJECT_NAME} wake escalation\n"
            f"Role: {row['role']}\n"
            f"Reason: {row['reason_kind']} ({row['reason'] or row['source_ref']})\n"
            f"The wake ladder climbed past the machine — check the board.")
    results = channels.deliver(subject, text, row["channel"])
    status = channels.summarize(results)
    with connect(db_path, write=True) as conn:
        conn.execute(
            "UPDATE wake_escalations SET status=?, details=?, sent_at=? WHERE id=?",
            (status, json.dumps(results, ensure_ascii=False),
             _iso() if status == "sent" else None, escalation_id))
    return {"id": escalation_id, "role": row["role"],
            "reason_kind": row["reason_kind"], "status": status,
            "results": results}


def _wake_reasons_text(ds: list[dict]) -> str:
    """Summarize a role's demands into one human-readable clause."""
    by: dict = {}
    for d in ds:
        by.setdefault(d["reason_kind"], []).append(d)
    parts = []
    if by.get("meeting_wake"):
        ags = ", ".join(sorted({str(d["label"]) for d in by["meeting_wake"]}))
        parts.append(f'{len(by["meeting_wake"])} meeting(s) need you ({ags})')
    if by.get("stuck_delivery"):
        parts.append(f'{len(by["stuck_delivery"])} message(s) past their read SLA')
    if by.get("urgent_task"):
        ts = ", ".join(sorted({str(d["label"]) for d in by["urgent_task"]}))
        parts.append(f'{len(by["urgent_task"])} urgent task(s) ({ts})')
    if by.get("idle_task"):
        parts.append("; ".join(str(d["label"]) for d in by["idle_task"]))
    if by.get("inbox"):
        parts.append(f'{len(by["inbox"])} inbox batch(es) queued')
    return "; ".join(parts) or "pending work"


def _wake_prompt(role: str, ds: list[dict], inbox_items: list[dict] | None = None) -> str:
    """Build the prompt that boots/resumes a woken session.

    Delegates to CONFIG.prompt_builder: a cold-spawned session has NO context,
    so only the host knows how to tell it what it is.
    """
    titles = [("[!] " if i["priority"] == "urgent" else "") + i["title"]
              for i in sorted(inbox_items or [], key=_inbox_sort_key)]
    return CONFIG.prompt_builder.wake(role, _wake_reasons_text(ds), titles)


@contextmanager
def _planning_txn(db_path, *, record: bool):
    """The transaction one tick of ``plan_wakes`` runs in.

    A dry run does the SAME work — it must, because the plan needs the delivery
    projection to see stuck deliveries, so skipping sync_delivery would change
    the decision the preview is supposed to be previewing — and then throws the
    writes away. That makes record=False inert BY CONSTRUCTION instead of by
    auditing every write on the path (sync_delivery stamps first_projected_at,
    which is never re-stamped, so a preview would otherwise start the SLA clock).
    Probes stay gated on `record` at the call site: a rollback cannot undo a
    network call.
    """
    with connect(db_path, write=True) as conn:
        try:
            yield conn
        finally:
            if not record:
                conn.rollback()


def plan_wakes(db_path: Path | str | None = None, *, record: bool = True) -> dict:
    """The four-step loop as one step: collect demand, close resolved attempts,
    create/escalate the rest, and RETURN a driver plan. Records to wake_attempts
    but never spawns/resumes anything — the driver executes.

    record=False is a truly side-effect-free preview: it makes the SAME decisions
    (using an in-memory resolved set so escalation logic is identical) but writes
    NOTHING — no probe runs, no timer advances, no phantom attempts, and the
    escalation clock does not move.
    """
    now = store._now()
    now_iso = _iso(now)
    ladder = _ladder()
    resolved, changed, esc_ids = [], [], []
    # Advance the meeting SLA clocks BEFORE planning, so a wake request the
    # sweep arms is collected as demand in this same tick. The sweep otherwise
    # runs only on meetings read paths — clocks that advance only while
    # someone happens to be looking, which on a quiet desk is never. Outside
    # the planning txn (it owns its own transaction and dispatches), and
    # record-gated for the same reason probes are: a rollback cannot undo a
    # channel send.
    if record:
        meetings.sweep_timeouts(db_path)
    with _planning_txn(db_path, record=record) as conn:
        sync_delivery(conn)
        sync_meeting_close_tasks(conn)
        # Agent-registered wake hooks fire first (same txn), so their inbox items
        # are visible to this tick's demand collection. Evaluated only in record
        # mode: a dry preview must not run probes or advance timers.
        hooks_fired = _eval_wake_hooks(conn, now) if record else []
        # Re-route capability-addressed demands BEFORE collecting: an urgent
        # demand routed this tick wakes its new owner this tick. Not gated on
        # `record` — the dry preview must make the same decisions; _planning_txn
        # rolls its writes back.
        routed = _route_unroutable(conn)
        pres = {p["role"]: p for p in _presence_list(conn, now)}
        demands = collect_wake_demand(conn)
        # 1) close pending attempts whose demand is resolved or has disappeared
        resolved_keys = set()
        for a in conn.execute(
                "SELECT * FROM wake_attempts WHERE outcome='pending'").fetchall():
            done, outcome = _demand_resolved(conn, a["role"], a["reason_kind"],
                                             a["source_ref"], now_iso)
            if done:
                resolved_keys.add((a["role"], a["reason_kind"], a["source_ref"]))
                lat = int((now - dt.datetime.fromisoformat(a["attempted_at"])).total_seconds())
                if record:
                    conn.execute(
                        "UPDATE wake_attempts SET outcome=?, resolved_at=?, "
                        "latency_seconds=? WHERE id=?",
                        (outcome, now_iso, lat, a["id"]))
                    _log_event(conn, "orchestrator", "wake_resolved", a["source_ref"],
                               {"role": a["role"], "reason": a["reason_kind"],
                                "outcome": outcome, "latency_s": lat})
                resolved.append({"role": a["role"], "reason_kind": a["reason_kind"],
                                 "source_ref": a["source_ref"], "outcome": outcome,
                                 "latency_seconds": lat})
        # 2) create new attempts / escalate stale ones. Treat just-resolved
        # attempts as absent so decisions match whether or not we recorded.
        pend = {(a["role"], a["reason_kind"], a["source_ref"]): a
                for a in conn.execute(
                    "SELECT * FROM wake_attempts WHERE outcome='pending'").fetchall()
                if (a["role"], a["reason_kind"], a["source_ref"]) not in resolved_keys}
        for d in demands:
            # HARD RULE 1 lives here, before anything else can happen to the
            # demand: a reason fenced to the machine on a ladder with no machine
            # rung has nowhere legal to go, so it does not wake at all.
            ceiling = _reason_ceiling(d["reason_kind"], ladder)
            if ceiling < 0:
                continue
            cur = pend.get((d["role"], d["reason_kind"], d["source_ref"]))
            if cur is None:
                lvl = min(_start_level(pres.get(d["role"]), ladder), ceiling)
                if record:
                    _insert_attempt(conn, d, lvl, now_iso, ladder)
                    _log_event(conn, "orchestrator", "wake_attempt", d["source_ref"],
                               {"role": d["role"], "reason": d["reason_kind"],
                                "level": lvl, "channel": ladder[lvl].channel})
                # Only a host-defined ladder can START a demand on a human rung,
                # but arrival is arrival: the sink must fire here too.
                if ladder[lvl].leaves_machine:
                    esc_ids.append(_queue_wake_escalation(conn, d, lvl, now_iso))
                changed.append({**d, "level": lvl, "escalated": False})
            else:
                lvl = min(cur["level"], len(ladder) - 1)
                sla = ladder[lvl].sla_seconds
                age = (now - dt.datetime.fromisoformat(cur["attempted_at"])).total_seconds()
                if sla is not None and age > sla:
                    nl = min(lvl + 1, len(ladder) - 1, ceiling)
                    # Escalation is APPEND-ONLY: supersede the old row, insert a
                    # new one. The wake history of a demand is never rewritten.
                    #
                    # At a ceiling, nl == lvl and this re-attempts the same rung
                    # rather than climbing. That is deliberate and it is not a
                    # loop: an attempt row is not proof a session ran (the driver
                    # skips on a held role lock, and launches fail), so the rung
                    # is retried at-least-once like every other wake — and each
                    # retry is itself an idle_task attempt, which walks the task
                    # towards STALLED and retires the demand. The thing that stops
                    # it is the rule, not a cooldown.
                    escalated = nl > lvl
                    if record:
                        conn.execute(
                            "UPDATE wake_attempts SET outcome='superseded', resolved_at=? "
                            "WHERE id=?",
                            (now_iso, cur["id"]))
                        _insert_attempt(conn, d, nl, now_iso, ladder)
                        _log_event(conn, "orchestrator",
                                   "wake_escalate" if escalated else "wake_retry",
                                   d["source_ref"],
                                   {"role": d["role"], "reason": d["reason_kind"],
                                    "from": lvl, "to": nl, "channel": ladder[nl].channel})
                    # ARRIVAL at a rung that pulls a person in — the terminal
                    # sink's durable row, once per rung climbed, for EVERY
                    # reason kind. The driver's old meeting-only escalation
                    # branch reached nobody for any other reason; this is the
                    # engine-owned replacement (dispatch happens post-commit).
                    if escalated and ladder[nl].leaves_machine:
                        esc_ids.append(_queue_wake_escalation(conn, d, nl, now_iso))
                    changed.append({**d, "level": nl, "escalated": escalated})
        # 3) build the per-role actionable plan (L0 hook needs no driver action)
        changed_roles = {c["role"] for c in changed}
        actions = []
        for role in sorted(changed_roles):
            role_changes = [c for c in changed if c["role"] == role]
            top = max(role_changes, key=lambda x: x["level"])
            channel = ladder[top["level"]].channel
            if channel == "hook":
                continue
            role_demands = [d for d in demands if d["role"] == role]
            # The resume/spawn prompt CARRIES the role's inbox, but we do NOT
            # mark items delivered here: the plan is speculative — the driver may
            # skip (per-role lock) or the launch may fail. Delivered is stamped
            # only by the in-session hook when the session actually runs, or by
            # the agent's own ack. A failed launch therefore leaves the items
            # undelivered, the demand alive, and the ladder escalating.
            inbox_items = [dict(r) for r in conn.execute(
                "SELECT * FROM agent_inbox WHERE target_role=? "
                "AND acked_at IS NULL AND delivered_at IS NULL", (role,)).fetchall()]
            # The role's registry declaration rides in the action. The engine
            # never interprets it — it DECLARES, and the driver (the harness
            # side of the seam) enforces, e.g. mapping authority.allowed_tools
            # to the tool grant of the session it launches. Without this, every
            # role is woken with the driver's one global grant and the
            # declaration is decorative.
            reg = conn.execute(
                "SELECT capabilities, authority FROM agent_registry WHERE role=?",
                (role,)).fetchone()
            actions.append({
                "role": role, "level": top["level"], "channel": channel,
                "session_id": (pres.get(role) or {}).get("session_id"),
                "capabilities": _load_json(reg["capabilities"]) if reg else [],
                "authority": _load_json(reg["authority"]) if reg else {},
                "reasons": [{"reason_kind": d["reason_kind"], "source_ref": d["source_ref"],
                             "label": d.get("label")} for d in role_demands],
                "prompt": _wake_prompt(role, role_demands, inbox_items),
            })
    # Mirror queued human-rung escalations out AFTER the planning transaction
    # committed (their rows are durable regardless of what channels do). A dry
    # run rolled its queue rows back and must not reach any network.
    escalations = ([_dispatch_wake_escalation(e, db_path) for e in esc_ids]
                   if record else [])
    return {"generated_at": now_iso, "actions": actions,
            "resolved": resolved, "changed": changed,
            "hooks_fired": hooks_fired, "routed": routed,
            "escalations": escalations}


def wake_attempts_recent(limit: int = 20,
                         db_path: Path | str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM wake_attempts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def wake_sources(role: str, db_path: Path | str | None = None) -> dict:
    """One-shot answer to 'what can currently wake/remind me, and can I change
    it?' — the role's own registered hooks (self-managed via `hook add/cancel`),
    pending meeting wakes, queued inbox notifications, urgent tasks, its open
    queue, and any in-flight wake attempts.

    The role's OWN QUEUE belongs in this answer and was missing from it, which is
    how an agent could ask this question, be told about hooks and meetings and
    urgent work, and hear nothing about the five open tasks that were the actual
    reason it kept being woken — or, worse, hear nothing about the ones that were
    never going to wake it at all.
    """
    now = store._now()
    now_iso = _iso(now)
    with connect(db_path, write=True) as conn:
        role = _agent_role(conn, role)
        sync_delivery(conn)
        hooks_ = []
        for r in conn.execute(
                "SELECT * FROM wake_hooks WHERE owner_role=? AND status='active' "
                "ORDER BY (next_fire_at IS NULL), next_fire_at", (role,)):
            d = dict(r)
            d["spec"] = _load_json(d["spec"])
            hooks_.append(d)
        meeting_wakes = [dict(r) for r in conn.execute(
            "SELECT w.thread_id, m.agenda, w.created_at FROM meeting_wake_requests w "
            "JOIN meetings m ON m.thread_id=w.thread_id "
            "WHERE w.role=? AND w.status='pending'", (role,)).fetchall()]
        inbox = [dict(r) for r in conn.execute(
            "SELECT id, source_kind, priority, title, delivered_at, enqueued_at "
            "FROM agent_inbox WHERE target_role=? AND acked_at IS NULL", (role,)).fetchall()]
        urgent_tasks = [dict(r) for r in conn.execute(
            f"SELECT id, title, due_at FROM agent_tasks "
            f"WHERE assignee_role=? AND {_URGENT_TASK_WHERE}", (role,)).fetchall()]
        actionable, stalled = _queued_tasks(conn, role)
        attempts = [dict(r) for r in conn.execute(
            "SELECT reason_kind, source_ref, channel, level, attempted_at "
            "FROM wake_attempts WHERE role=? AND outcome='pending' ORDER BY id DESC",
            (role,)).fetchall()]
    return {
        "role": role, "as_of": now_iso,
        "self_hooks": hooks_,                # yours — hook add/cancel to change
        "meeting_wakes": meeting_wakes,      # a meeting needs you (wake-ack/check-in)
        "inbox_queued": [i for i in inbox if not i["delivered_at"]],
        "inbox_delivered_unacked": [i for i in inbox if i["delivered_at"]],
        "urgent_tasks": urgent_tasks,        # wake you whatever you are doing
        # Your open queue. These wake you whenever you are idle — which is why
        # nothing here may be left to rot: move it, block it on a NAMED
        # dependency, transfer it, or escalate it. 'pending forever' is not a
        # resting state.
        "actionable_tasks": actionable,
        # Woken for these idle_task_stall_wakes times since they last moved, and
        # they did not move. They have STOPPED waking you and are now somebody's
        # decision (board health.stalled_tasks). Touching one makes it actionable
        # again — the count is measured from the task's last update.
        "stalled_tasks": stalled,
        "pending_wake_attempts": attempts,
        "manage": (f"{PROJECT_NAME} hook add --for {role} "
                   f"(--at|--every|--cron|--probe) / "
                   f"{PROJECT_NAME} hook cancel <id>; "
                   f"{PROJECT_NAME} inbox ack --for {role}; "
                   f"{PROJECT_NAME} task update <id> "
                   f"--status (in_progress|blocked --blocked-on <dep>|done) "
                   f"--for <role> (transfer)"),
    }


# --- session lifecycle (cross-day rollover) ---------------------------------

def _rollover_prompt(role: str, from_day: str, today: str) -> str:
    """Wind-down prompt for a session left over from a previous day.

    SESSION_DONE on its own line is the engine's end-of-session sentinel: the
    driver watches for it to know the drain completed.
    """
    return (f"{CONFIG.prompt_builder.bootstrap(role)} "
            f"New session day {today} ({CONFIG.timezone}). Your session from "
            f"{from_day} must wind down and hand off: finish or park in-flight "
            f"work, write a handoff note recording open items and the first thing "
            f"the next session should do, then output SESSION_DONE on its own line "
            f"to end this session. A fresh session opens for the new day and picks "
            f"up from your handoff note.")


def rollover_plan(db_path: Path | str | None = None, *, record: bool = True) -> dict:
    """Detect sessions left over from a prior day and plan their wind-down: the
    driver resumes the stale session with the wrap-up prompt; when it outputs
    SESSION_DONE / ends, a fresh session opens and reads the handoff note.
    record=True marks newly-detected sessions 'draining' and returns them;
    record=False is a dry preview that mutates nothing."""
    today = _session_day()
    out = []
    with connect(db_path, write=record) as conn:
        rows = conn.execute(
            "SELECT * FROM agent_sessions WHERE ended_at IS NULL "
            "AND session_day IS NOT NULL AND session_day < ?", (today,)).fetchall()
        for r in rows:
            # Mark draining once (for the board); but ALWAYS return the action
            # while the session is stale-and-not-ended so a lock-busy tick
            # retries. The driver ends the session after draining it, which
            # clears it from this list (ended_at set) — bounding the drain to one
            # successful pass per stale session.
            if record and r["phase"] != "draining":
                conn.execute(
                    "UPDATE agent_sessions SET phase='draining', state='stopping' "
                    "WHERE role=?",
                    (r["role"],))
                _log_event(conn, "orchestrator", "session_rollover", r["role"],
                           {"from_day": r["session_day"], "to_day": today})
            # Same seam as plan_wakes actions: a rollover resumes a session, so
            # the driver needs the role's declared grant here too.
            reg = conn.execute(
                "SELECT capabilities, authority FROM agent_registry WHERE role=?",
                (r["role"],)).fetchone()
            out.append({"role": r["role"], "session_id": r["session_id"],
                        "from_day": r["session_day"], "to_day": today,
                        "capabilities": _load_json(reg["capabilities"]) if reg else [],
                        "authority": _load_json(reg["authority"]) if reg else {},
                        "prompt": _rollover_prompt(r["role"], r["session_day"], today)})
    return {"today": today, "rollovers": out}
