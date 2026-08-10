"""The wake orchestrator: demand collection, the escalation ladder,
plan_wakes (decide, never execute), human-rung escalations, session
rollover, and the per-role wake_sources answer.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .. import channels, meetings
from . import store
from ..config import CONFIG, PROJECT_NAME, WakeRung
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

#: Who currently owes a termination vote — one row per (voter, meeting). ONE
#: definition on purpose: collect_wake_demand raises and labels demand from it,
#: wake_sources answers "what does the engine want from me" from it, and
#: _demand_resolved mirrors its predicate. The trailing `{extra}` slot exists
#: so wake_sources can scope it to one role without a second copy of the WHERE
#: clause drifting from this one.
_OWED_VOTE_SQL = """\
    SELECT a.role, t.id AS proposal_id, t.thread_id, m.agenda,
           t.created_at, w.generation AS wake_generation
      FROM meeting_terminations t
      JOIN meetings m ON m.thread_id=t.thread_id
      JOIN meeting_attendees a ON a.thread_id=t.thread_id
      LEFT JOIN meeting_wake_requests w
        ON w.thread_id=t.thread_id AND w.role=a.role
     WHERE t.status='pending' AND m.state='termination_pending'
       AND a.required=1 AND a.checked_in_at IS NOT NULL
       AND a.stopped_at IS NULL
       AND NOT EXISTS (SELECT 1 FROM meeting_termination_votes v
                        WHERE v.proposal_id=t.id AND v.role=a.role){extra}"""


def _ladder() -> tuple[WakeRung, ...]:
    """The escalation ladder in effect (CONFIG.wake_ladder)."""
    return CONFIG.wake_ladder


def _channel_level(ladder: tuple[WakeRung, ...], channel: str,
                   fallback: int) -> int:
    """Index of the rung with `channel`, or a clamped fallback.

    Levels are ladder INDICES, so nothing may assume a fixed number here: a host
    can define its own ladder. Look the channel up by name instead.
    """
    for i, rung in enumerate(ladder):
        if rung.channel == channel:
            return i
    return max(0, min(fallback, len(ladder) - 1))


def _human_level(ladder: tuple[WakeRung, ...]) -> int:
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


def _reason_ceiling(reason_kind: str, ladder: tuple[WakeRung, ...]) -> int:
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


def _role_floor(role: str, ladder) -> int:
    """The LOWEST rung a demand OWED BY this role may occupy.

    Mirror image of _reason_ceiling, and it exists for the mirror-image reason.
    Every machine rung ends in the same act: boot or resume a SESSION for the
    role that owes the thing. That is incoherent when the role is the
    SUPERVISOR, who is a person and enters only through the authenticated
    console — there is no session to resume and nothing to spawn. What actually
    happened (2026-07-26) is worse than a no-op: a supervisor-owed meeting reply
    started at `spawn`, so the orchestrator launched a headless agent session
    every ~38 minutes for hours and handed each one a prompt telling it to
    declare itself the supervisor. The engine's own invariant forbids exactly
    that (store.py: "supervisor is not an agent role"), so every one of those
    sessions could do nothing but refuse and burn a session.

    Scoped by ROLE, not by reason kind: any demand can name the supervisor as
    the owed party (owed_reply does today; stuck_delivery can, since a
    supervisor can leave a message unread), and every one of them wants the same
    routing. A person is reached by leaving the machine — so that is where these
    demands start, and the machine rungs below are skipped, not climbed through.
    """
    if role != CONFIG.supervisor_role:
        return 0
    return _human_level(ladder)

#: Wake reasons that may never climb to a rung that leaves the machine.
#:
#: The ladder exists because a MESSAGE MUST LAND: it keeps climbing until someone
#: — ultimately a person — reacts. A to-do list has no such property. Nothing is
#: owed to anyone, nothing breaks if it waits until morning, and there is no
#: answer a human woken at 3am could give that the queue needed. So `idle_task`
#: is fenced to the machine rungs BY CONSTRUCTION rather than by the argument
#: that it always resolves quickly (see _reason_ceiling).
MACHINE_ONLY_REASONS = frozenset({"idle_task"})

def _idle_task_demand(conn: sqlite3.Connection, role: str, now: dt.datetime) -> dict | None:
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


def collect_wake_demand(conn: sqlite3.Connection) -> list[dict]:
    """Unify the DB-derived wake demands.

    Note: task ``due_at`` is deliberately NOT a source — soft deadlines never
    wake. An open task wakes its assignee only while that assignee is IDLE
    (``idle_task``, which interrupts nothing); ``priority='urgent'`` is the only
    task state that wakes regardless.
    """
    now = store._now()
    now_iso = _iso(now)
    demands = []
    # The unpaid-vote ledger, read ONCE and used twice below. An unpaid vote is
    # demand even when the notification was signed for. The request table
    # records what was SENT; this asks what is OWED, and for a termination vote
    # the ledger can tell the difference. Measured on a live desk 2026-07-27:
    # proposal at 07:38:31, the voter acked within 30s while mid-turn on other
    # work, never read the thread, and the demand vanished with the ack — four
    # planner ticks passed with a meeting that could not close and nothing
    # asking anyone to close it, until the sweep's timeout re-arm five minutes
    # later. A safety net was doing a first-order job.
    #
    # Deliberately narrow: only the vote, because only the vote has a ledger
    # row that says whether the work happened. Ordinary meeting wakes keep
    # settling on the ack — an agent that acks has the thread in front of it,
    # and a demand that no action can clear would climb forever.
    vote_rows = conn.execute(_OWED_VOTE_SQL.format(extra="")).fetchall()
    owed_votes = {(r["role"], r["thread_id"]) for r in vote_rows}
    # `m.state!='closed'`: _close_meeting settles pending wake requests, but
    # rows that predate that rule (or arrive through a future path it misses)
    # must not wake anyone — a closed meeting cannot even be checked in to, so
    # the demand would regenerate every tick with no legal way to satisfy it.
    #
    # A wake request whose meeting is really waiting on this role's VOTE must
    # say so: propose_end arms a request for every missing voter, so this
    # branch — not the vote branch below, which seen_meeting dedups away —
    # carries the common case, and an agenda-only label hides the one action
    # that settles the demand. Measured live 2026-08-04: the analyst was
    # resumed for exactly this, did adjacent meeting work, idled without
    # voting, and the ladder paged a human eleven minutes later. The label is
    # all the wake prompt and the human-rung page ever see.
    seen_meeting: set = set()
    for r in conn.execute(
            """SELECT w.role, w.thread_id, w.generation, m.agenda, w.created_at
               FROM meeting_wake_requests w JOIN meetings m ON m.thread_id=w.thread_id
               WHERE w.status='pending' AND m.state!='closed'"""):
        seen_meeting.add((r["role"], r["thread_id"]))
        label = r["agenda"]
        if (r["role"], r["thread_id"]) in owed_votes:
            label = f"{label} — your vote closes it"
        demands.append({"role": r["role"], "reason_kind": "meeting_wake",
                        "source_ref": r["thread_id"], "label": label,
                        "since_at": r["created_at"],
                        "generation": f"request:{r['generation']}"})
    for r in vote_rows:
        if (r["role"], r["thread_id"]) in seen_meeting:
            continue
        if r["role"] not in _known_roles(conn):
            continue    # the supervisor votes from the console, not on an SLA
        # Signing for the proposal-time request does not create new work: the
        # same vote is still owed. Keep the request's generation so the live
        # attempt continues climbing instead of restarting from hook on every
        # pending -> acknowledged -> pending transition. The proposal token is
        # only a legacy fallback for a missing request row.
        generation = (
            f"request:{r['wake_generation']}"
            if r["wake_generation"] is not None
            else f"vote:{r['proposal_id']}"
        )
        demands.append({"role": r["role"], "reason_kind": "meeting_wake",
                        "source_ref": r["thread_id"],
                        "label": f'{r["agenda"]} — your vote closes it',
                        "since_at": r["created_at"],
                        "generation": generation})
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
    # its job.
    #
    # Two conditions, and the second is the one experience added: the demand
    # must be PAYABLE. `m.state IN ('active','consensus')` was written to mean
    # that, but a meeting has three terminal states and only one of them is
    # `closed` — the mailbox retires a thread on its idle deadline or its spent
    # message budget by pausing the THREAD, leaving meetings.state at 'active'
    # or 'consensus'. In that shape the debtor is physically unable to answer
    # (send_update refuses: "thread is paused"), so demand regenerated every
    # tick and climbed to a human forever — 76 pages in 24h, measured, for a
    # conversation both parties had politely finished. Requiring the thread to
    # be open is what makes the comment above true; if the supervisor resumes
    # the thread, the demand comes back on its own.
    for r in conn.execute(
            """SELECT o.message_id, o.thread_id, o.owed_by, o.created_at, m.agenda
               FROM meeting_response_obligations o
               JOIN meetings m ON m.thread_id=o.thread_id
               JOIN mailbox_threads t ON t.id=o.thread_id
               WHERE o.status='pending' AND o.due_at<=?
                 AND m.state IN ('active','consensus')
                 AND t.status='open'""", (now_iso,)):
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


def _demand_resolved(conn: sqlite3.Connection, role: str, reason_kind: str, source_ref: str,
                     now_iso: str) -> tuple[bool, str]:
    """Closed-loop check: has the underlying demand been satisfied?"""
    if reason_kind == "meeting_wake":
        pend = conn.execute(
            "SELECT 1 FROM meeting_wake_requests WHERE thread_id=? AND role=? "
            "AND status='pending'",
            (source_ref, role)).fetchone()
        if pend is not None:
            return (False, "")
        # An ack is a signature, not the work. For most meeting wakes the two
        # are close enough — an agent that acks has the thread in front of it.
        # A termination vote is the exception, because the ledger can tell the
        # difference: acking says "seen", voting is the thing the meeting is
        # actually waiting for, and an agent that acks mid-turn and returns to
        # what it was doing leaves a meeting that cannot close with no demand
        # standing anywhere. Measured on a live desk 2026-07-27: proposal
        # 07:38:31, ack within 30s, thread never read, and nothing raised it
        # again until the sweep's timeout re-arm at 07:44:01.
        owes_vote = conn.execute(
            """SELECT 1 FROM meeting_terminations t
                JOIN meetings m ON m.thread_id=t.thread_id
                JOIN meeting_attendees a ON a.thread_id=t.thread_id AND a.role=?
               WHERE t.thread_id=? AND t.status='pending'
                 AND m.state='termination_pending'
                 AND a.required=1 AND a.checked_in_at IS NOT NULL
                 AND a.stopped_at IS NULL
                 AND NOT EXISTS (SELECT 1 FROM meeting_termination_votes v
                                  WHERE v.proposal_id=t.id AND v.role=a.role)""",
            (role, source_ref)).fetchone()
        return (owes_vote is None, "acked")
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


def _start_level(p: dict | None, ladder: tuple[WakeRung, ...]) -> int:
    """Which rung a brand-new demand starts on, given the role's presence."""
    spawn = _channel_level(ladder, "spawn", 2)
    if p is None:
        return spawn                              # unknown role -> spawn
    if p["liveness"] == "online":
        return _channel_level(ladder, "hook", 0)  # in-session hook will deliver
    if p.get("session_id") and not p.get("ended_at"):
        return _channel_level(ladder, "resume", 1)  # resumable session exists
    return spawn                                  # no live/resumable session


def _insert_attempt(conn: sqlite3.Connection, d: dict, level: int, now_iso: str,
                    ladder: tuple[WakeRung, ...]) -> int:
    channel = ladder[level].channel
    cur = conn.execute(
        """INSERT INTO wake_attempts
               (role, reason_kind, source_ref, channel, level, attempted_at,
                outcome, detail, source_generation)
           VALUES (?,?,?,?,?,?, 'pending', ?,?)""",
        (d["role"], d["reason_kind"], d["source_ref"], channel, level, now_iso,
         d.get("label"), d.get("generation")),
    )
    return cur.lastrowid


def _queue_wake_escalation(conn: sqlite3.Connection, d: dict, level: int, now_iso: str) -> int:
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


def _resolve_wake_escalations(conn: sqlite3.Connection, role: str,
                              reason_kind: str, source_ref: str,
                              outcome: str, now_iso: str) -> list[int]:
    """Mark this demand's still-open escalations settled. Returns the ids that
    actually reached a person and therefore owe a retraction.

    The ladder is one-directional by construction: it exists to push "you owe
    this" harder and harder until someone reacts, and it had no way to say
    "that one is settled". `status` only ever recorded whether the SEND worked.
    So a page that reached a human stayed a standing red card — on 2026-08-09
    two Discord pages went out and the demand settled six minutes later, with
    the rows still reading `sent` days afterwards. The cost is not symmetric
    with a missed page: a false standing alarm spends a person's attention with
    no floor, and it is exactly what makes the next real page ignorable.
    """
    rows = conn.execute(
        "SELECT id, status FROM wake_escalations "
        "WHERE role=? AND reason_kind=? AND source_ref=? AND resolved_at IS NULL",
        (role, reason_kind, source_ref)).fetchall()
    if not rows:
        return []
    conn.execute(
        "UPDATE wake_escalations SET resolved_at=?, resolved_reason=? "
        "WHERE role=? AND reason_kind=? AND source_ref=? AND resolved_at IS NULL",
        (now_iso, outcome, role, reason_kind, source_ref))
    _log_event(conn, "orchestrator", "wake_escalation_resolved", source_ref,
               {"role": role, "reason": reason_kind, "outcome": outcome,
                "ids": [r["id"] for r in rows]})
    # Only the ones that actually left the machine. A queued row nobody ever
    # saw needs no correction, and paging someone to retract a page they never
    # got would be the same disease.
    return [r["id"] for r in rows if r["status"] == "sent"]


def _dispatch_escalation_retraction(escalation_id: int,
                                    db_path: Path | str | None) -> dict:
    """Tell the channel that carried a page that it is settled.

    Same after-commit path as the page itself. Deliberately one line and
    explicitly "no action needed": a retraction that reads like another alarm
    costs what it was sent to refund.
    """
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM wake_escalations WHERE id=?",
                           (escalation_id,)).fetchone()
    if row is None:
        return {"id": escalation_id, "status": "missing"}
    subject = f"{PROJECT_NAME} wake escalation SETTLED: {row['role']}"
    text = (f"{PROJECT_NAME} wake escalation settled — no action needed.\n"
            f"Role: {row['role']}\n"
            f"Reason: {row['reason_kind']} ({row['reason'] or row['source_ref']})\n"
            f"Ref: {row['source_ref']}\n"
            f"Paged at {row['sent_at'] or row['created_at']}; "
            f"settled at {row['resolved_at']} ({row['resolved_reason']}).")
    results = channels.deliver(subject, text, row["channel"])
    return {"id": escalation_id, "role": row["role"],
            "reason_kind": row["reason_kind"],
            "status": channels.summarize(results), "results": results}


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
    # `reason` is the human label; `source_ref` is the only DURABLE pointer.
    # A demand that self-heals after the page leaves nothing on the default
    # board view (closed meetings are hidden), so without the ref in the text
    # "check the board" dead-ends for exactly the pages that resolved
    # themselves — measured live 2026-08-04, a 39-minute gap between page and
    # close. Both lines, always: the label says why, the ref says where.
    text = (f"{PROJECT_NAME} wake escalation\n"
            f"Role: {row['role']}\n"
            f"Reason: {row['reason_kind']} ({row['reason'] or row['source_ref']})\n"
            f"Ref: {row['source_ref']}\n"
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
    if by.get("owed_reply"):
        # Without this the commonest wake of all reads "pending work" and names
        # neither the meeting nor the message — an agent booted to answer
        # something has to go hunting for what.
        refs = ", ".join(sorted({str(d["label"]) for d in by["owed_reply"]}))
        parts.append(f'{len(by["owed_reply"])} unanswered question(s) ({refs})')
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
def _planning_txn(db_path: Path | str | None, *,
                  record: bool) -> Iterator[sqlite3.Connection]:
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
    resolved, changed, esc_ids, retract_ids = [], [], [], []
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
                    # A page that reached a person is not undone by the demand
                    # quietly going away: the ladder only ever pushes "you owe
                    # this", so an unretracted red card costs that person's
                    # attention until they go and query the database.
                    retract_ids.extend(_resolve_wake_escalations(
                        conn, a["role"], a["reason_kind"], a["source_ref"],
                        outcome, now_iso))
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
            # The floor (see _role_floor) can outrank the ceiling: that reads as
            # "must reach a person, may never reach a person". Unsatisfiable, so
            # the demand is not raised rather than resolved towards either rule —
            # honouring the ceiling would spawn sessions for a human, honouring
            # the floor would page a person for a to-do list.
            floor = _role_floor(d["role"], ladder)
            if floor > ceiling:
                continue
            cur = pend.get((d["role"], d["reason_kind"], d["source_ref"]))
            fresh_generation = (
                cur is not None
                and d["reason_kind"] == "meeting_wake"
                and d.get("generation") is not None
                and cur["source_generation"] != d["generation"]
            )
            if fresh_generation:
                # Same durable key, new request. Reusing the old live attempt
                # inherits its age and rung: a resumed meeting can otherwise
                # skip hook/resume/spawn and page a human immediately, or wait
                # out the old rung's SLA before trying at all. Preserve the old
                # row as superseded evidence and restart this generation from
                # the role's current machine rung.
                lvl = min(_start_level(pres.get(d["role"]), ladder), ceiling)
                if record:
                    conn.execute(
                        """UPDATE wake_attempts
                           SET outcome='superseded',resolved_at=?
                           WHERE id=?""",
                        (now_iso, cur["id"]),
                    )
                    _insert_attempt(conn, d, lvl, now_iso, ladder)
                    _log_event(
                        conn, "orchestrator", "wake_restart", d["source_ref"],
                        {"role": d["role"], "reason": d["reason_kind"],
                         "from": cur["level"], "to": lvl,
                         "channel": ladder[lvl].channel,
                         "generation": d["generation"]},
                    )
                if ladder[lvl].leaves_machine:
                    esc_ids.append(_queue_wake_escalation(conn, d, lvl, now_iso))
                changed.append({
                    **d, "level": lvl, "escalated": False, "restarted": True,
                })
                continue
            if cur is None:
                lvl = max(min(_start_level(pres.get(d["role"]), ladder), ceiling), floor)
                if record:
                    _insert_attempt(conn, d, lvl, now_iso, ladder)
                    _log_event(conn, "orchestrator", "wake_attempt", d["source_ref"],
                               {"role": d["role"], "reason": d["reason_kind"],
                                "level": lvl, "channel": ladder[lvl].channel})
                # A host-defined ladder, or a role floor (_role_floor), can START
                # a demand on a human rung; arrival is arrival either way, so the
                # sink must fire here too.
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
                elif sla is None and age > CONFIG.terminal_retry_seconds:
                    # The TERMINAL rung is a badge, not a parking brake. Left
                    # alone, a demand that reached it never moves again — and
                    # on 2026-07-23 that turned a transient DNS outage into a
                    # permanent one: every spawn died at the API, Discord was
                    # down too, two inbox demands terminal'd, and when the
                    # network came back nothing retried (the inbox demand key
                    # aggregates per role, so all later notifications rode the
                    # parked attempt). Recycle: after terminal_retry_seconds,
                    # supersede and climb again from the machine rungs. The
                    # escalation ledger keeps the badge red; the machine keeps
                    # trying. No new escalation is queued by the recycle itself
                    # — one fires only if the ladder genuinely climbs back up.
                    # The floor applies to the recycle too, or a supervisor-owed
                    # demand would drop back onto `spawn` every cycle: that is
                    # precisely the loop _role_floor was added to end.
                    nl = max(min(_start_level(pres.get(d["role"]), ladder), ceiling), floor)
                    if record:
                        conn.execute(
                            "UPDATE wake_attempts SET outcome='superseded', resolved_at=? "
                            "WHERE id=?",
                            (now_iso, cur["id"]))
                        _insert_attempt(conn, d, nl, now_iso, ladder)
                        _log_event(conn, "orchestrator", "wake_recycle",
                                   d["source_ref"],
                                   {"role": d["role"], "reason": d["reason_kind"],
                                    "from": lvl, "to": nl,
                                    "channel": ladder[nl].channel})
                    changed.append({**d, "level": nl, "escalated": False})
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
    # Retractions ride the same after-commit path, and go out on the channel
    # that carried the page: telling the ledger a red card is settled while
    # leaving the person who was paged uninformed puts the correction where
    # they will never look.
    retractions = ([_dispatch_escalation_retraction(e, db_path)
                    for e in retract_ids] if record else [])
    retried = _retry_wake_escalations(db_path) if record else []
    return {"generated_at": now_iso, "actions": actions,
            "retractions": retractions,
            "resolved": resolved, "changed": changed,
            "hooks_fired": hooks_fired, "routed": routed,
            "escalations": escalations, "escalations_retried": retried}


def _retry_wake_escalations(db_path: Path | str | None) -> list[dict]:
    """Re-mirror human-rung escalations that never reached a person, once per
    planning tick while they are fresh (24h).

    Two states qualify, and both were exposed on 2026-07-23 when the network
    outage that broke the machine rungs ALSO broke the Discord channel:

    - ``failed``: every channel raised at send time. Failure was a terminal
      state, so the page that mattered most — the one explaining why nothing
      else works — was dropped exactly when the transport was down. Retried
      every tick until something takes it.
    - ``queued``: only the durable outbox took it (no channel was available).
      Retried only once a registered channel reports available again — a host
      that wires no channels keeps outbox-as-delivery semantics untouched.

    The 24h bound keeps a permanently broken channel from paging about
    ancient history: past it, the board's undelivered_escalations gauge is
    the record.
    """
    cutoff = _iso(store._now() - dt.timedelta(hours=24))
    reachable = channels.human_reachable()
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, status FROM wake_escalations "
            "WHERE created_at > ? AND (status='failed' "
            "      OR (status='queued' AND ?))",
            (cutoff, 1 if reachable else 0)).fetchall()]
    out = []
    for r in rows:
        res = _dispatch_wake_escalation(r["id"], db_path)
        if res.get("status") != r["status"]:
            out.append(res)
    return out


def wake_attempts_recent(limit: int = 20,
                         db_path: Path | str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM wake_attempts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def wake_ladder_view() -> list[dict]:
    """The configured ladder as the console renders it: each rung with its
    index, SLA, and whether the promise it makes currently means anything.

    `wired` is the operational half of the answer: a machine rung is executed
    by the driver and is always wired; a rung that leaves the machine is only
    as real as the channel layer behind it — unwired, its escalations stop at
    the durable outbox row. Computed here, not in the web layer, because 'is
    the human rung wired' is an engine fact the host must be TOLD, and two
    definitions of it is how a console and its engine come to disagree.
    """
    reachable = channels.human_reachable()
    ladder = _ladder()
    return [{
        "level": i,
        "channel": rung.channel,
        "sla_seconds": rung.sla_seconds,
        "terminal": rung.sla_seconds is None,
        "leaves_machine": rung.leaves_machine,
        "wired": reachable if rung.leaves_machine else True,
    } for i, rung in enumerate(ladder)]


def wake_escalations_recent(limit: int = 100,
                            db_path: Path | str | None = None) -> list[dict]:
    """The human-rung outbox, newest first — every arrival of a demand at a
    rung that leaves the machine. status='queued' means the ledger row is the
    ONLY delivery so far: nobody was pulled in unless somebody reads this."""
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM wake_escalations ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()]


def wake_sources(role: str, db_path: Path | str | None = None) -> dict:
    """One-shot answer to 'what can currently wake/remind me, and can I change
    it?' — the role's own registered hooks (self-managed via `hook add/cancel`),
    pending meeting wakes, termination votes it owes, queued inbox
    notifications, urgent tasks, its open queue, and any in-flight wake
    attempts.

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
        # The votes this role owes RIGHT NOW — same ledger the demand collector
        # reads, scoped to the asker. Listed separately from meeting_wakes
        # because the two disagree in exactly the cases that page humans: a
        # vote can be owed with the wake request already acknowledged (the
        # 2026-07-27 parked vote), and a wake request can stand for a meeting
        # whose real ask is the vote (the 2026-08-04 page). The wake prompt
        # says "sources first"; this is where sources says "vote first" —
        # confirm-end or reject-end both settle it, going idle settles nothing.
        votes_owed = [dict(r) for r in conn.execute(
            _OWED_VOTE_SQL.format(extra=" AND a.role=?"), (role,)).fetchall()]
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
        # Termination votes you owe. THE priority item: the meeting cannot
        # close, the ladder is climbing towards a person, and only your
        # confirm-end / reject-end settles it — rejecting IS a legal answer,
        # going idle to "wait for the other side" is not.
        "votes_owed": votes_owed,
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
