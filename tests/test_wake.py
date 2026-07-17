"""Wake orchestration: what may wake an agent, and how an attempt is proven.

Every test here guards a bug that actually shipped. The headline one is the
first: a soft deadline that manufactured an interrupt. `due_at` sorts and
surfaces; only `priority=urgent` wakes. If that rule ever regresses, the desk
starts waking agents for calendar entries, which is precisely the failure mode
deskd exists to avoid.

Two mechanical notes:

- These tests do NOT use the `conn` fixture. It holds `BEGIN IMMEDIATE` for the
  test's lifetime, and every public entry point here (`task_add`, `plan_wakes`,
  ...) opens its own write transaction — so pairing them buys a 30-second
  `busy_timeout` stall and an opaque "database is locked". Direct-SQL setup and
  inspection therefore go through short-lived connections (`_backdate`, `_rows`).
- Nothing sleeps. The escalation clock is driven by rewriting `attempted_at`
  into the past, which is the same arithmetic `plan_wakes` does against `now`
  and costs no wall time.
"""

from __future__ import annotations

import sqlite3

import pytest

from deskd import meetings
from deskd import orchestration as orch
from deskd.config import CONFIG

from conftest import iso

YEAR = 365 * 24 * 3600


# --- helpers ----------------------------------------------------------------

def _rows(sql: str, params=()) -> list[dict]:
    with orch.connect() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def _attempts() -> list[dict]:
    """Every wake attempt ever, oldest first — the audit order."""
    return _rows("SELECT * FROM wake_attempts ORDER BY id")


def _open_attempts() -> list[dict]:
    return [a for a in _attempts() if a["outcome"] == "pending"]


def _level_of(channel: str) -> int:
    """The rung index for a channel. Levels are ladder INDICES, so a test that
    hardcodes 2 is asserting the host's ladder, not the engine's behaviour."""
    for i, rung in enumerate(CONFIG.wake_ladder):
        if rung.channel == channel:
            return i
    raise AssertionError(f"no {channel!r} rung in the configured ladder")


def _backdate(seconds: float, attempt_id: int | None = None) -> None:
    """Age open wake attempts by `seconds` so the next tick sees their SLA blown."""
    with orch.connect(write=True) as c:
        if attempt_id is None:
            c.execute("UPDATE wake_attempts SET attempted_at=? WHERE outcome='pending'",
                      (iso(-seconds),))
        else:
            c.execute("UPDATE wake_attempts SET attempted_at=? WHERE id=?",
                      (iso(-seconds), attempt_id))


def _snapshot() -> dict:
    """Every row of every table, including sqlite_sequence, so an insert that is
    later rolled back still shows up as a bumped AUTOINCREMENT counter."""
    with orch.connect() as c:
        names = sorted(r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        return {n: [tuple(r) for r in c.execute(f"SELECT * FROM {n}")] for n in names}


def _plan_without_clock(plan: dict) -> dict:
    return {k: v for k, v in plan.items() if k != "generated_at"}


def _escalate_once(seconds: float | None = None) -> dict:
    """Blow the current rung's SLA and tick."""
    lvl = _open_attempts()[0]["level"]
    sla = CONFIG.wake_ladder[lvl].sla_seconds
    _backdate(seconds if seconds is not None else (sla or 0) + 60)
    return orch.plan_wakes()


def _busy(role: str, *, liveness: str = "online") -> None:
    """A role with a turn actually running — a wake would INTERRUPT it.

    Both LIVE_LIVENESS values are reachable, because both mean "executing right
    now" and `suspect` is the one a naive `liveness == "online"` check would get
    wrong: a session mid-heartbeat-gap is still working, and cutting into it is
    the thing the interrupt rule forbids.
    """
    orch.heartbeat(role, state="working", session_id=f"sess-{role}")
    if liveness == "suspect":
        _age_heartbeat(role, CONFIG.online_max_seconds + 60)


def _idle(role: str) -> None:
    """A session that parked itself and went quiet: liveness `idle`, resumable.

    This — not "no session row" — is what the live desk's agents actually are
    between wakes, and it is the state the whole idle_task demand is about.
    """
    orch.heartbeat(role, state="idle_standby", session_id=f"sess-{role}")
    _age_heartbeat(role, CONFIG.suspect_max_seconds + 60)


def _age_heartbeat(role: str, seconds: float) -> None:
    with orch.connect(write=True) as c:
        c.execute("UPDATE agent_sessions SET last_heartbeat_at=? WHERE role=?",
                  (iso(-seconds), role))


def _liveness(role: str) -> str:
    return next(p["liveness"] for p in orch.presence() if p["role"] == role)


def _meeting_message() -> str:
    """A real, projectable meeting message — the only way to get a
    message_delivery row, which is the one table a tick projects into."""
    tid = meetings.call_meeting(agenda="sync", called_by="alpha",
                                attendees=["alpha", "beta"])["meeting"]["thread_id"]
    meetings.check_in(tid, role="alpha")
    meetings.check_in(tid, role="beta")
    meetings.send_update(tid, role="alpha", body="something for beta to read")
    return tid


# --- 1. the headline rule: a soft deadline never wakes ----------------------

@pytest.mark.parametrize("status", ["pending", "in_progress", "blocked"])
@pytest.mark.parametrize("liveness", ["online", "suspect"])
def test_overdue_task_never_wakes(desk, status, liveness):
    """A due date is attention, not an interrupt.

    The bug: `due_at < now` was treated as a wake demand, so every stale task on
    the board spawned a session at 3am. Overdue must be loud and inert.

    The rule was AMENDED, not repealed, and this is the half that did not move.
    "Soft deadlines never wake" was written against INTERRUPTION — and against a
    working agent it is still absolute: no deadline, at any age, in any status,
    may cut into a running turn. What changed is only the idle case, where there
    is no turn to cut into (test_an_idle_agent_is_woken_for_its_open_queue), and
    even there the deadline itself contributes exactly nothing
    (test_a_deadline_contributes_nothing_to_an_idle_wake). So alpha is working
    here, which is the condition the bug was always about.
    """
    _busy("alpha", liveness=liveness)
    tid = orch.task_add("quarterly thing", assignee_role="alpha",
                        priority="normal", due_at=iso(-YEAR))
    if status == "blocked":
        orch.task_update(tid, status=status, blocked_on="the vendor's Q3 filing")
    elif status != "pending":
        orch.task_update(tid, status=status)

    plan = orch.plan_wakes()

    assert plan["changed"] == []
    assert plan["actions"] == []
    assert _attempts() == []
    # ...and it is still visibly overdue. Inert, not hidden.
    assert orch.tasks(assignee_role="alpha")[0]["overdue"] is True


def test_no_amount_of_overdue_wakes(desk):
    """Overdue is not a gradient that eventually crosses into waking."""
    _busy("beta")
    for years, prio in ((1, "normal"), (5, "low"), (20, "normal")):
        orch.task_add(f"{years}y late", assignee_role="beta",
                      priority=prio, due_at=iso(-YEAR * years))

    plan = orch.plan_wakes()

    assert plan["changed"] == []
    assert _attempts() == []


def test_a_deadline_contributes_nothing_to_an_idle_wake(desk):
    """The amended rule must not smuggle `due_at` back in as a wake input.

    An idle agent is woken for its QUEUE, and a queue entry is a queue entry: the
    decision has to be identical for a task a year overdue and one with no
    deadline at all. The moment those two diverge, `due_at` has become a wake
    trigger through the back door — the very bug this section exists to pin,
    wearing the new rule as a disguise.
    """
    orch.task_add("a year late", assignee_role="alpha",
                  priority="normal", due_at=iso(-YEAR))
    orch.task_add("no deadline, ever", assignee_role="beta", priority="normal")

    plan = orch.plan_wakes()

    idle = {c["role"]: c for c in plan["changed"] if c["reason_kind"] == "idle_task"}
    assert set(idle) == {"alpha", "beta"}, "the deadline neither adds nor removes a wake"
    assert idle["alpha"]["level"] == idle["beta"]["level"], "nor changes the rung"
    assert len(_attempts()) == 2, "one wake per idle role — not one per deadline"


# --- 2. urgent is the only task-driven wake path -----------------------------

def test_urgent_task_wakes(desk):
    """The other half of the rule: priority=urgent is the ONLY task path to a
    wake. A desk where nothing can wake anyone is as broken as one where
    everything can."""
    orch.task_add("halt the line", assignee_role="alpha", priority="urgent")

    plan = orch.plan_wakes()

    assert [c["reason_kind"] for c in plan["changed"]] == ["urgent_task"]
    assert [a["role"] for a in plan["actions"]] == ["alpha"]
    attempt = _open_attempts()[0]
    assert (attempt["role"], attempt["reason_kind"]) == ("alpha", "urgent_task")


def test_urgent_wakes_while_overdue_normal_does_not(desk):
    """Both rules in one tick: the urgent task with no deadline wakes; the
    year-late normal task sitting beside it does not.

    Alpha is working, which is what isolates the two rules from each other: the
    urgent task must interrupt it anyway ("regardless of state" is the whole
    point of urgent), and the overdue one must not — while a wake for the queue
    is off the table entirely, because there is a turn in flight.
    """
    _busy("alpha")
    urgent = orch.task_add("halt the line", assignee_role="alpha", priority="urgent")
    orch.task_add("year late", assignee_role="alpha",
                  priority="normal", due_at=iso(-YEAR))

    plan = orch.plan_wakes()

    assert [c["source_ref"] for c in plan["changed"]] == [str(urgent)]
    assert len(_attempts()) == 1


# --- 3. resolution mirrors generation ---------------------------------------

def test_deprioritized_urgent_task_retires_its_attempt(desk):
    """The resolution predicate MUST mirror the generation predicate.

    The bug: generation asked "urgent AND pending", resolution only asked "does
    the task still exist". De-prioritizing an urgent task left its attempt open
    forever — nothing regenerated it, nothing closed it, so it climbed the
    ladder to a red supervisor badge for a task nobody considered urgent.

    Gamma is working, so urgent_task's own mirror is the only thing in play: a
    de-prioritized task is still open work, and an IDLE gamma would rightly be
    woken for it by `idle_task` — a different demand, on a different rung,
    answering a different question.
    """
    _busy("gamma")
    tid = orch.task_add("was urgent", assignee_role="gamma", priority="urgent")
    orch.plan_wakes()
    assert len(_open_attempts()) == 1

    orch.task_update(tid, priority="normal")
    plan = orch.plan_wakes()

    assert [r["reason_kind"] for r in plan["resolved"]] == ["urgent_task"]
    assert _open_attempts() == []
    # And having retired it, the next tick must not resurrect it.
    assert orch.plan_wakes()["changed"] == []


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_closed_urgent_task_resolves_its_attempt(desk, status):
    """Same mirror, the ordinary way round: the work got done."""
    tid = orch.task_add("urgent", assignee_role="alpha", priority="urgent")
    orch.plan_wakes()

    orch.task_close(tid, status=status)
    plan = orch.plan_wakes()

    assert [r["source_ref"] for r in plan["resolved"]] == [str(tid)]
    assert _open_attempts() == []


def test_in_progress_urgent_task_retires_its_attempt(desk):
    """Generation requires status='pending', so resolution must accept
    'in_progress' as satisfied — the agent is on it, which is what the wake was
    for. Any predicate drift here orphans the attempt."""
    _busy("alpha")     # ...and only urgent_task's mirror is in play
    tid = orch.task_add("urgent", assignee_role="alpha", priority="urgent")
    orch.plan_wakes()

    orch.task_update(tid, status="in_progress")

    assert orch.plan_wakes()["resolved"][0]["source_ref"] == str(tid)
    assert _open_attempts() == []


# --- 4. _normalize_due ------------------------------------------------------

def test_normalize_due_normalizes_offsets_for_lexicographic_order(desk):
    """Timestamps are compared as STRINGS everywhere (_task_sort_key,
    _task_view, the delivery SLA). So a non-UTC offset must be converted, not
    stored verbatim.

    The bug: '2026-01-01T00:00:00+08:00' stored as-is compares LATER than
    '2026-01-01T00:00:00+00:00' as a string, when it is in fact eight hours
    EARLIER. Overdue tasks from a non-UTC caller sorted to the bottom.
    """
    east = orch._normalize_due("2026-01-01T00:00:00+08:00")
    utc = orch._normalize_due("2026-01-01T00:00:00+00:00")
    west = orch._normalize_due("2026-01-01T00:00:00-08:00")

    assert east == "2025-12-31T16:00:00+00:00"
    assert utc == "2026-01-01T00:00:00+00:00"
    assert east < utc < west          # chronological == lexicographic. The point.


def test_normalize_due_treats_naive_as_utc(desk):
    """An offset-less input is UTC by fiat — never local time, which would make
    the same string mean different moments on different hosts."""
    assert orch._normalize_due("2026-01-01T00:00:00") == "2026-01-01T00:00:00+00:00"


def test_normalize_due_passes_none_through(desk):
    """No deadline is a legitimate state — most tasks have none."""
    assert orch._normalize_due(None) is None
    assert orch._normalize_due("   ") is None


@pytest.mark.parametrize("garbage", ["tomorrow", "2026-13-01T00:00:00", "not a date", "42"])
def test_normalize_due_rejects_garbage(desk, garbage):
    """Rejected loudly rather than stored and silently mis-sorted: an unparsable
    string still compares fine against ISO text, so it would just sit in the
    wrong place in every ordering forever."""
    with pytest.raises(ValueError):
        orch._normalize_due(garbage)


def test_task_add_stores_canonical_utc(desk):
    """The normalizer is only worth anything if the write path uses it."""
    tid = orch.task_add("t", assignee_role="alpha", due_at="2026-01-01T00:00:00+08:00")
    assert _rows("SELECT due_at FROM agent_tasks WHERE id=?",
                 (tid,))[0]["due_at"] == "2025-12-31T16:00:00+00:00"


# --- 5. ordering ------------------------------------------------------------

def test_overdue_sorts_above_urgent(desk):
    """Deadlines shape attention: the compensation for never waking anyone is
    that overdue outranks even urgent in every ordered view."""
    overdue = orch.task_add("year late", assignee_role="alpha",
                            priority="normal", due_at=iso(-YEAR))
    urgent = orch.task_add("urgent, on time", assignee_role="alpha", priority="urgent")
    later = orch.task_add("low, due tomorrow", assignee_role="alpha",
                          priority="low", due_at=iso(+86400))

    assert [t["id"] for t in orch.tasks(assignee_role="alpha")] == [overdue, urgent, later]

    # The board renders the same list — an ordering that only holds in one
    # accessor is not an ordering.
    alpha = next(a for a in orch.board()["agents"] if a["role"] == "alpha")
    assert [t["id"] for t in alpha["tasks"]] == [overdue, urgent, later]
    assert alpha["overdue_count"] == 1


def test_most_overdue_sorts_first_within_overdue(desk):
    """Within the overdue band the tiebreak is how late, not how urgent."""
    older = orch.task_add("older", assignee_role="beta", priority="low",
                          due_at=iso(-YEAR))
    newer = orch.task_add("newer", assignee_role="beta", priority="urgent",
                          due_at=iso(-60))

    assert [t["id"] for t in orch.tasks(assignee_role="beta")] == [older, newer]


# --- 6. dry run --------------------------------------------------------------

def test_dry_run_writes_nothing(desk):
    """record=False is a preview, not a tick.

    The bug: a dry run recorded its attempts, so `wake list` filled with phantom
    wakes nobody ever executed, and — worse — the real tick that followed saw
    them as already-attempted and started the escalation clock from a wake that
    never left the building.
    """
    orch.task_add("urgent", assignee_role="alpha", priority="urgent")
    orch.inbox_enqueue("alpha", "alert", "something happened", priority="urgent")

    before = _snapshot()
    dry = orch.plan_wakes(record=False)
    assert _snapshot() == before

    # Same decisions, or the preview is a lie.
    rec = orch.plan_wakes(record=True)
    assert _plan_without_clock(dry) == _plan_without_clock(rec)

    # ...and record=True really does record, or the test above proves nothing.
    assert len(_attempts()) == len(rec["changed"]) == 2

    # A dry run over ALREADY-recorded state is still inert.
    after_record = _snapshot()
    orch.plan_wakes(record=False)
    assert _snapshot() == after_record


def test_dry_run_writes_nothing_with_a_projectable_message(desk):
    """The dry-run promise has to hold for the tick's OTHER write path too.

    A preview that projects the delivery ledger is not side-effect-free: it
    stamps first_projected_at, which is never re-stamped, so an audit trail says
    a message was first projected by a run that decided nothing.
    """
    _meeting_message()

    before = _snapshot()
    orch.plan_wakes(record=False)

    assert _snapshot() == before


def test_dry_run_on_a_fresh_db_still_leaves_schema_and_roles(desk, tmp_path):
    """The rollback must discard the TICK's writes, not the SETUP's.

    The dry-run tests above cannot see this: `_snapshot()` calls `orch.connect()`,
    which applies the schema and seeds the registry, so by the time they preview
    anything the DB is already built. Only a preview that is the FIRST thing ever
    to touch a database exercises the ordering — and that is the real driver's
    cold start.

    `connect()` applies the schema, migrates, seeds, and COMMITS all of it before
    `BEGIN IMMEDIATE`, precisely so the preview's rollback cannot take it along.
    Let the seed drift inside that transaction and a dry run against a brand-new
    DB leaves an engine with no roles: no projection, no demand, no wakes — and a
    plan of `[]` that is indistinguishable from an honest "nothing to do".
    """
    fresh = tmp_path / "brand-new.db"
    assert not fresh.exists()

    plan = orch.plan_wakes(db_path=fresh, record=False)
    assert plan["actions"] == []

    # Observed with a BARE sqlite3 connection: orch.connect() would re-apply the
    # schema and re-seed the registry, i.e. manufacture the very evidence under
    # test. This must read what the dry run actually left behind.
    con = sqlite3.connect(fresh)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        roles = sorted(r[0] for r in con.execute("SELECT role FROM agent_registry"))
    finally:
        con.close()

    assert "wake_attempts" in tables and "agent_registry" in tables
    assert roles == sorted(r.name for r in CONFIG.roles)


# --- 7. escalation is append-only -------------------------------------------

def test_escalation_supersedes_and_appends(desk):
    """A wake's history is evidence; evidence is never rewritten.

    The bug: escalation UPDATEd the attempt's level in place. The board showed
    the current rung and nothing else — you could not tell whether a supervisor
    badge had climbed there over twenty minutes or been raised instantly.
    """
    orch.task_add("urgent", assignee_role="alpha", priority="urgent")
    orch.plan_wakes()
    first = _open_attempts()[0]
    assert first["level"] == _level_of("spawn")   # no session -> cold spawn

    plan = _escalate_once()

    assert plan["changed"][0]["escalated"] is True
    rung_two, rung_three = _attempts()
    # The superseded row still exists, un-mutilated: same id, same level, same
    # channel it was actually attempted on.
    assert rung_two["id"] == first["id"]
    assert (rung_two["outcome"], rung_two["level"]) == ("superseded", first["level"])
    assert rung_two["channel"] == first["channel"]
    assert rung_two["resolved_at"] is not None
    # The new rung is a NEW row, one step up.
    assert rung_three["id"] > rung_two["id"]
    assert rung_three["level"] == first["level"] + 1
    assert rung_three["channel"] == CONFIG.wake_ladder[first["level"] + 1].channel
    assert rung_three["outcome"] == "pending"
    assert len(_open_attempts()) == 1             # exactly one live rung


def test_ladder_history_is_auditable_end_to_end(desk):
    """Reading the rows in id order must replay the climb, rung by rung, with
    no gaps and nothing deleted."""
    orch.task_add("urgent", assignee_role="alpha", priority="urgent")
    orch.plan_wakes()
    start = _open_attempts()[0]["level"]

    for _ in range(len(CONFIG.wake_ladder) - start - 1):
        _escalate_once()

    history = _attempts()
    assert [a["level"] for a in history] == list(range(start, len(CONFIG.wake_ladder)))
    assert [a["channel"] for a in history] == [
        r.channel for r in CONFIG.wake_ladder[start:]]
    assert [a["outcome"] for a in history] == ["superseded"] * (len(history) - 1) + ["pending"]


# --- 8. at-least-once + idempotent ack --------------------------------------

def test_duplicate_demand_does_not_open_a_second_attempt(desk):
    """At-least-once delivery means ticks repeat. The dedup key is
    (role, reason_kind, source_ref); when it drifted, a tick every minute meant
    a new 'open' attempt every minute and an escalation ladder per minute."""
    orch.task_add("urgent", assignee_role="alpha", priority="urgent")

    first = orch.plan_wakes()
    second = orch.plan_wakes()
    third = orch.plan_wakes()

    assert len(first["changed"]) == 1
    assert second["changed"] == third["changed"] == []   # within SLA: nothing new
    assert len(_open_attempts()) == 1
    assert len(_attempts()) == 1


def test_acking_twice_is_safe(desk):
    """Idempotent ack, because the agent may be woken twice for one item and
    must not have to remember whether it already acked."""
    item = orch.inbox_enqueue("beta", "alert", "check the thing")
    orch.inbox_mark_delivered([item])

    assert orch.inbox_ack(ids=[item]) == 1
    acked_at = _rows("SELECT acked_at FROM agent_inbox WHERE id=?", (item,))[0]["acked_at"]

    assert orch.inbox_ack(ids=[item]) == 0
    assert orch.inbox_ack(target_role="beta") == 0
    # The second ack must not re-stamp the time — that would rewrite when the
    # agent actually handled it.
    assert _rows("SELECT acked_at FROM agent_inbox WHERE id=?",
                 (item,))[0]["acked_at"] == acked_at
    assert orch.inbox_pending("beta") == []


def test_duplicate_notification_does_not_requeue_while_unacked(desk):
    """The other half of at-least-once: a re-firing source (a probe on an
    interval) must coalesce onto its open item rather than pile up."""
    first = orch.inbox_enqueue("beta", "alert", "disk full", dedup_key="disk")
    again = orch.inbox_enqueue("beta", "alert", "disk full", dedup_key="disk")

    assert first is not None
    assert again is None
    assert len(orch.inbox_pending("beta")) == 1


# --- 9. resolution closes the loop ------------------------------------------

def test_resolution_records_latency(desk):
    """A wake is only proven by its close. The latency is the number that says
    whether the ladder is working — an attempt closed without one is an
    unmeasurable wake."""
    tid = orch.task_add("urgent", assignee_role="alpha", priority="urgent")
    orch.plan_wakes()
    _backdate(90)                                  # the agent took 90s to land

    orch.task_close(tid)
    plan = orch.plan_wakes()

    resolved = _attempts()[0]
    assert resolved["outcome"] == "acked"
    assert resolved["resolved_at"] is not None
    assert 90 <= resolved["latency_seconds"] < 120
    assert plan["resolved"][0]["latency_seconds"] == resolved["latency_seconds"]
    assert _open_attempts() == []


def test_resolution_keeps_the_superseded_history(desk):
    """Closing the loop closes the LIVE rung; the climb that preceded it stays
    on the record."""
    tid = orch.task_add("urgent", assignee_role="alpha", priority="urgent")
    orch.plan_wakes()
    _escalate_once()

    orch.task_close(tid)
    orch.plan_wakes()

    assert [a["outcome"] for a in _attempts()] == ["superseded", "acked"]


def test_reassigning_an_urgent_task_retires_the_old_assignees_attempt(desk):
    """A demand is (role, item) here too — the task's id is only half of it.

    Alpha is woken for an urgent task, which is then handed to beta.
    `collect_wake_demand` immediately stops raising it for alpha and starts
    raising it for beta, so alpha's attempt is now answering a demand that no
    longer exists and must retire. Matching on the id alone says "still urgent,
    still pending" and holds it open forever: alpha's wake never records a
    latency, and if the task is ever handed back, that stale row re-escalates
    alpha from an ancient `attempted_at` instead of starting at the bottom rung.

    The same rule the stuck_delivery branch spells out ("this predicate must stay
    identical to _delivery_state()'s wake test") applies to this branch's own
    comment: it claims to mirror collect_wake_demand's predicate, and does not.
    """
    tid = orch.task_add("urgent", assignee_role="alpha", priority="urgent")
    orch.plan_wakes()
    assert [(a["role"], a["outcome"]) for a in _open_attempts()] == [("alpha", "pending")]

    with orch.connect(write=True) as c:
        c.execute("UPDATE agent_tasks SET assignee_role='beta' WHERE id=?", (tid,))

    plan = orch.plan_wakes()

    assert [(r["role"], r["reason_kind"]) for r in plan["resolved"]] \
        == [("alpha", "urgent_task")], \
        "the task is beta's now: alpha's attempt must close, not linger pending"
    assert [(a["role"], a["outcome"]) for a in _open_attempts()] == [("beta", "pending")]


# --- 10. the terminal rung ---------------------------------------------------

def test_terminal_rung_never_times_out(desk):
    """The last rung has nowhere to climb, so it must not move: no escalation
    past the end of the ladder, and — the actual bug — no auto-resolve either.
    A timed-out badge would silently clear the one signal that says a human
    never came."""
    orch.task_add("urgent", assignee_role="alpha", priority="urgent")
    orch.plan_wakes()
    while _open_attempts()[0]["level"] < len(CONFIG.wake_ladder) - 1:
        _escalate_once()

    terminal = _open_attempts()[0]
    assert CONFIG.wake_ladder[terminal["level"]].sla_seconds is None
    history = len(_attempts())

    _backdate(100 * YEAR)                          # a century past any SLA
    orch.plan_wakes()
    orch.plan_wakes()

    still = _open_attempts()
    assert len(still) == 1
    assert still[0]["id"] == terminal["id"]        # not superseded, not replaced
    assert still[0]["level"] == terminal["level"]  # no rung 5
    assert still[0]["outcome"] == "pending"        # stays red
    assert still[0]["resolved_at"] is None
    assert len(_attempts()) == history             # nothing appended


def test_terminal_rung_still_resolves_when_the_demand_dies(desk):
    """'Never times out' is not 'never closes' — the badge must clear when the
    work is actually done, or it is just noise."""
    tid = orch.task_add("urgent", assignee_role="alpha", priority="urgent")
    orch.plan_wakes()
    while _open_attempts()[0]["level"] < len(CONFIG.wake_ladder) - 1:
        _escalate_once()

    orch.task_close(tid)
    orch.plan_wakes()

    assert _open_attempts() == []
    assert _attempts()[-1]["outcome"] == "acked"


# --- an owed meeting reply is machine-recoverable demand ---------------------

def _owed_setup() -> tuple[str, int]:
    """A question alpha asked beta, whose reply SLA has already lapsed."""
    status = meetings.call_meeting(agenda="sla", called_by="alpha",
                                   attendees=["alpha", "beta"],
                                   wait_timeout_seconds=30)
    thread_id = status["meeting"]["thread_id"]
    meetings.check_in(thread_id, role="beta")
    mid = meetings.send_update(thread_id, role="alpha", kind="question",
                               body="a question that goes unanswered")["message_id"]
    with orch.connect(write=True) as c:
        c.execute("UPDATE meeting_response_obligations SET due_at=? WHERE message_id=?",
                  (iso(-60), mid))
    return thread_id, mid


def test_an_overdue_reply_climbs_the_ladder_instead_of_paging_a_human(desk):
    """meetings used to escalate this straight from its sweep — one hop, at a
    human, past every machine rung built to fix it without waking anyone. It is
    demand like any other now: it starts at the bottom of the ladder and only
    reaches a person if the machine cannot deliver."""
    thread_id, mid = _owed_setup()

    with orch.connect() as c:
        owed = [d for d in orch.collect_wake_demand(c)
                if d["reason_kind"] == "owed_reply"]
    assert [(d["role"], d["source_ref"]) for d in owed] == [
        ("beta", f"{thread_id}:{mid}")], "the debtor is woken, not the asker"

    orch.plan_wakes()
    attempt = [a for a in _open_attempts() if a["reason_kind"] == "owed_reply"]
    assert len(attempt) == 1 and attempt[0]["role"] == "beta"
    assert not CONFIG.wake_ladder[attempt[0]["level"]].leaves_machine, (
        "a slow agent must not open on a rung that pulls a person in")


def test_answering_retires_the_owed_reply_attempt(desk):
    """Generation and resolution must agree clause for clause — the commit that
    precedes this branch exists because they disagreed in five places. Answering
    stops collect_wake_demand raising it, so resolution must stop expecting it."""
    thread_id, mid = _owed_setup()
    orch.plan_wakes()
    assert [a["role"] for a in _open_attempts()
            if a["reason_kind"] == "owed_reply"] == ["beta"]

    meetings.send_update(thread_id, role="beta", kind="answer", reply_to=mid,
                         body="answering at last")

    orch.plan_wakes()
    assert [a for a in _open_attempts() if a["reason_kind"] == "owed_reply"] == []
    assert _attempts()[-1]["outcome"] == "acked"


def test_a_stopped_meetings_owed_reply_stops_waking_anyone(desk):
    """Same trap the stuck_delivery branch documents: an obligation in a meeting
    nobody can rejoin cannot be discharged by anything the agent does, so a
    demand that kept raising it would climb the ladder forever over a dead
    conversation — and reach a human, which is the outcome this whole branch is
    trying to stop being routine."""
    thread_id, _ = _owed_setup()
    orch.plan_wakes()
    assert [a["role"] for a in _open_attempts()
            if a["reason_kind"] == "owed_reply"] == ["beta"]

    meetings.apply_simple_supervisor_action(
        {"action": "force_close", "meeting_id": thread_id, "reason": "done here"})

    with orch.connect() as c:
        assert [d for d in orch.collect_wake_demand(c)
                if d["reason_kind"] == "owed_reply"] == []
    orch.plan_wakes()
    assert [a for a in _open_attempts() if a["reason_kind"] == "owed_reply"] == [], (
        "an attempt outstanding when the meeting closed must retire, not strand"
    )


# --- 11. an idle agent is woken for its own queue ----------------------------

def _idle_attempts() -> list[dict]:
    return [a for a in _open_attempts() if a["reason_kind"] == "idle_task"]


def test_an_idle_agent_is_woken_for_its_open_queue(desk):
    """The hole this whole change exists to close.

    Observed live: the analyst had five open tasks, three of them orchestration
    bugs it had found itself, written down, and never returned to. Nothing would
    ever have reminded it — priority=normal with no due_at is invisible to every
    path — so it sat parked with a to-do list it could not be reminded of. Three
    got done only because it happened to be awake for something else and happened
    to look.

    "Agents must never manage their own waking" is the framework's core promise.
    If nothing wakes an idle agent for its queue, that promise is false and the
    list is write-only.
    """
    _idle("alpha")
    assert _liveness("alpha") == "idle", "the parked-session state, not a crash"
    tid = orch.task_add("fix the thing I found", assignee_role="alpha",
                        priority="normal")          # no due_at: invisible before

    plan = orch.plan_wakes()

    assert [c["reason_kind"] for c in plan["changed"]] == ["idle_task"]
    assert [a["role"] for a in plan["actions"]] == ["alpha"]
    attempt = _idle_attempts()[0]
    assert attempt["source_ref"] == "idle_task:alpha"
    assert str(tid) not in attempt["source_ref"], "one demand per ROLE, not per task"
    # A parked session is resumable, and resuming is the cheap path design.md
    # advertises — a queue wake must not be paying for a cold start.
    assert attempt["channel"] == "resume"


@pytest.mark.parametrize("liveness", ["online", "suspect"])
def test_a_busy_agent_is_never_woken_for_its_queue(desk, liveness):
    """The half of the old rule that was right, and stays.

    An open task is not an interrupt. `suspect` is the one that matters: a
    session in a heartbeat gap is still executing, so anything that treats only
    `online` as busy will cut into a running turn — and the minimum latency to a
    busy agent is its current sub-task precisely because interrupting is worse.
    """
    _busy("beta", liveness=liveness)
    assert _liveness("beta") == liveness
    orch.task_add("can wait for a natural boundary", assignee_role="beta")

    plan = orch.plan_wakes()

    assert plan["changed"] == []
    assert _attempts() == []


@pytest.mark.parametrize("liveness", ["idle", "offline", "dead", "never"])
def test_every_non_executing_state_is_wakeable(desk, liveness):
    """"Idle" is a claim about the TURN, not about the session row.

    Parked, ended, crashed, never started: no turn is in flight in any of them,
    so a wake interrupts nothing and the queue is owed one. Pinning this to
    `liveness == "idle"` alone would have left the hole open for every agent
    whose session ended — which, after a cross-day rollover, is all of them.
    """
    if liveness == "idle":
        _idle("gamma")
    elif liveness == "offline":
        orch.heartbeat("gamma", state="working", session_id="s")
        orch.end_session("gamma")
    elif liveness == "dead":
        orch.heartbeat("gamma", state="working", session_id="s")
        _age_heartbeat("gamma", CONFIG.suspect_max_seconds + 60)
    assert _liveness("gamma") == liveness
    orch.task_add("homework", assignee_role="gamma")

    assert [c["reason_kind"] for c in orch.plan_wakes()["changed"]] == ["idle_task"]


def test_a_blocked_task_never_wakes_anyone(desk):
    """Blocked means it waits on someone else. Waking its assignee cannot make
    that happen, so the wake would be pure cost — and, because the demand could
    not be resolved by anything the agent did, it would regenerate every tick and
    climb forever. That is the same trap the stuck_delivery and owed_reply
    branches document; blocked is where a task queue steps in it."""
    _idle("alpha")
    tid = orch.task_add("needs the vendor", assignee_role="alpha")
    orch.task_update(tid, status="blocked", blocked_on="vendor's Q3 filing")

    plan = orch.plan_wakes()

    assert plan["changed"] == []
    assert _attempts() == []
    # Not waking is not the same as not knowing: it is still open work, and the
    # dependency is on the record for whoever asks.
    assert orch.tasks(assignee_role="alpha")[0]["blocked_on"] == "vendor's Q3 filing"


def test_an_urgent_task_is_not_also_an_idle_task_wake(desk):
    """One work item, one demand. An urgent task already wakes its assignee by a
    stronger rule (regardless of state), so raising idle_task for it too would
    give one task two ladders, two escalation clocks and two resolution
    predicates — the drift this module has been bitten by five times, built in on
    purpose."""
    _idle("alpha")
    orch.task_add("halt the line", assignee_role="alpha", priority="urgent")

    plan = orch.plan_wakes()

    assert [c["reason_kind"] for c in plan["changed"]] == ["urgent_task"]


def test_an_urgent_task_in_progress_is_still_covered_by_a_wake(desk):
    """The gap in "just exclude urgent": urgent_task's predicate is `urgent AND
    PENDING`, so an urgent task the agent started and then parked on raises no
    urgent_task demand at all. Excluding it from the queue by priority alone
    would drop the desk's most expensive work item out of every wake path there
    is — re-opening this change's own bug at the worst possible place.

    The exclusion must therefore mirror what urgent_task actually raises, not
    what it is called.
    """
    _idle("alpha")
    tid = orch.task_add("halt the line", assignee_role="alpha", priority="urgent")
    orch.task_update(tid, status="in_progress")

    plan = orch.plan_wakes()

    assert [c["reason_kind"] for c in plan["changed"]] == ["idle_task"], (
        "nothing else raises this task any more — the queue must")


def test_every_open_task_is_woken_for_or_reported(desk):
    """The thesis, as a partition: a task must never rot on a list.

    Each open task is either raising a wake (urgent, or queued-and-idle) or is a
    stated fact someone must decide about (blocked on a NAMED dependency, or
    stalled). There is no fifth bucket, and "pending forever, invisible" is not a
    resting state. Any future clause that removes a task from the wake path
    without putting it in a reported one lands here as a failure.
    """
    stalled = orch.task_add("stalled", assignee_role="alpha")
    _stall()                              # only what is in the queue NOW stalls
    urgent = orch.task_add("urgent", assignee_role="alpha", priority="urgent")
    queued = orch.task_add("queued", assignee_role="alpha")
    blocked = orch.task_add("blocked", assignee_role="alpha")
    orch.task_update(blocked, status="blocked", blocked_on="beta's review")

    orch.plan_wakes()
    sources = orch.wake_sources("alpha")
    health = orch.board()["health"]

    assert [t["id"] for t in sources["urgent_tasks"]] == [urgent]
    assert [t["id"] for t in sources["actionable_tasks"]] == [queued]
    assert [t["id"] for t in sources["stalled_tasks"]] == [stalled]
    assert health["stalled_tasks"] == 1
    assert [t["id"] for t in orch.tasks(assignee_role="alpha", status="blocked")] \
        == [blocked]

    accounted = {urgent, queued, blocked, stalled}
    assert {t["id"] for t in orch.tasks(assignee_role="alpha")} == accounted


def test_booting_resolves_the_idle_task_demand(desk):
    """Resolution is generation negated — and this demand asked for exactly one
    thing: boot the agent. The moment it is executing, the wake LANDED, and that
    is the whole of it. Waiting for the tasks to be done instead would hold the
    attempt open across the agent's entire turn and escalate underneath it."""
    _idle("alpha")
    orch.task_add("homework", assignee_role="alpha")
    orch.plan_wakes()
    assert len(_idle_attempts()) == 1

    _busy("alpha")                       # it woke up. The task is untouched.
    plan = orch.plan_wakes()

    assert [(r["reason_kind"], r["outcome"]) for r in plan["resolved"]] \
        == [("idle_task", "acked")]
    assert _idle_attempts() == []
    assert orch.tasks(assignee_role="alpha")[0]["status"] == "pending", (
        "resolved by the boot, not by the work")


@pytest.mark.parametrize("how", ["done", "transferred", "blocked"])
def test_emptying_the_queue_resolves_the_idle_task_demand(desk, how):
    """The other resolve clause, and every way the actionable set can empty.

    Each of these stops collect_wake_demand raising it, so resolution must stop
    expecting it — the failure mode this module has shipped five times is an
    attempt left open for a demand nothing regenerates, climbing the ladder over
    work that no longer exists.
    """
    _idle("alpha")
    tid = orch.task_add("homework", assignee_role="alpha")
    orch.plan_wakes()
    assert len(_idle_attempts()) == 1

    if how == "done":
        orch.task_close(tid)
    elif how == "transferred":
        orch.task_update(tid, assignee_role="beta")
    else:
        orch.task_update(tid, status="blocked", blocked_on="beta's review")

    plan = orch.plan_wakes()

    assert ("idle_task", "alpha") in [(r["reason_kind"], r["role"])
                                      for r in plan["resolved"]]
    assert [a for a in _idle_attempts() if a["role"] == "alpha"] == []


def test_transferring_a_task_moves_the_wake_with_it(desk):
    """"It is someone else's -> transfer it" is one of the four live states, so
    the wake has to follow the work. Alpha's demand dies and beta's is born, both
    from the same predicate — the assignee is a clause in it, and an attempt that
    outlives the transfer escalates over work that is not that role's any more."""
    _idle("alpha")
    _idle("beta")
    tid = orch.task_add("actually beta's", assignee_role="alpha")
    orch.plan_wakes()
    assert [a["role"] for a in _idle_attempts()] == ["alpha"]

    orch.task_update(tid, assignee_role="beta")
    plan = orch.plan_wakes()

    assert [(r["role"], r["reason_kind"]) for r in plan["resolved"]] \
        == [("alpha", "idle_task")]
    assert [a["role"] for a in _idle_attempts()] == ["beta"]


def test_an_idle_agent_with_an_empty_queue_is_left_alone(desk):
    """Idle is not itself a demand. The engine's normal resting condition is a
    parked agent with nothing to do, and waking one to tell it so would make
    every tick a wake."""
    _idle("alpha")

    assert orch.plan_wakes()["changed"] == []
    assert _attempts() == []


# --- blocked must name its dependency ----------------------------------------

def test_blocked_without_a_dependency_is_refused(desk):
    """`blocked` was the graveyard: the status existed, no column recorded what
    it was blocked ON, so anything could be marked blocked and die there
    unaccountably. The live desk has one, blocked since 07:02, and the engine
    cannot say what it waits for. A blocked task also leaves the wake path — so
    an unnamed dependency is a task nothing will ever raise again."""
    tid = orch.task_add("vague", assignee_role="alpha")

    with pytest.raises(ValueError, match="blocked_on"):
        orch.task_update(tid, status="blocked")

    assert orch.tasks(assignee_role="alpha")[0]["status"] == "pending", (
        "refused means unchanged — not blocked-with-a-NULL")


@pytest.mark.parametrize("dep", ["", "   "])
def test_a_blank_dependency_is_not_a_dependency(desk, dep):
    """The check is for a NAMED dependency, not for a present argument: ''
    satisfies "did you pass blocked_on" while naming nothing at all."""
    tid = orch.task_add("vague", assignee_role="alpha")

    with pytest.raises(ValueError, match="blocked_on"):
        orch.task_update(tid, status="blocked", blocked_on=dep)


def test_leaving_blocked_clears_the_dependency(desk):
    """The wait is over, so the record of it must not linger: a stale blocked_on
    on an actionable task claims it waits on something its status says it does
    not, and the next reader believes the column."""
    tid = orch.task_add("was blocked", assignee_role="alpha")
    orch.task_update(tid, status="blocked", blocked_on="beta's review")

    orch.task_update(tid, status="in_progress")

    assert orch.tasks(assignee_role="alpha")[0]["blocked_on"] is None


def test_blocked_on_alone_keeps_a_blocked_task_blocked(desk):
    """Re-naming the dependency is a legitimate move — what it waits on can
    change without it becoming unblocked."""
    tid = orch.task_add("blocked", assignee_role="alpha")
    orch.task_update(tid, status="blocked", blocked_on="beta's review")

    orch.task_update(tid, blocked_on="gamma's review instead")

    task = orch.tasks(assignee_role="alpha")[0]
    assert (task["status"], task["blocked_on"]) == ("blocked", "gamma's review instead")


def test_blocked_on_is_refused_on_a_task_that_is_not_blocked(desk):
    """A dependency on an ACTIONABLE task is a contradiction the wake path would
    then act on: it says it waits on something, while its status keeps it in the
    set of things an idle agent gets woken for."""
    tid = orch.task_add("actionable", assignee_role="alpha")

    with pytest.raises(ValueError, match="blocked_on"):
        orch.task_update(tid, blocked_on="something")


def test_a_legacy_blocked_row_backfills_to_null_and_is_reported(desk):
    """The honest backfill. A row blocked before the column existed records no
    dependency, and this migration has no evidence of one: 'unknown' would make
    an illegal state look legal to every later reader, and flipping it to
    'pending' would overwrite an agent's own judgement with a guess. NULL is the
    only true value.

    NULL must not mean invisible, though, or the backfill has just rebuilt the
    graveyard: nothing wakes for a blocked task, so an unnamed one is reported
    for a human to decide about instead. That is what "genuinely cannot move ->
    escalate; the supervisor decides whether it still matters" asks for.
    """
    _idle("alpha")
    tid = orch.task_add("blocked since 07:02", assignee_role="alpha")
    # Rewind to a genuinely pre-migration DB: a row already sitting in `blocked`,
    # and no column in which a dependency could ever have been recorded. Asserting
    # this against a migrated DB would prove nothing — the migration would never
    # run, and a backfill that lies would sail straight through. The live desk has
    # exactly this row, blocked since 07:02.
    with orch.connect(write=True) as c:
        c.execute("UPDATE agent_tasks SET status='blocked' WHERE id=?", (tid,))
        c.execute("ALTER TABLE agent_tasks DROP COLUMN blocked_on")

    # Read with a BARE connection: orch.connect() migrates, i.e. it would
    # manufacture the very evidence under test.
    con = sqlite3.connect(CONFIG.db_path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(agent_tasks)")}
    finally:
        con.close()
    assert "blocked_on" not in cols, "the DB this migration actually meets"

    # ...and now the migration runs, on the next connect.
    assert _rows("SELECT blocked_on FROM agent_tasks WHERE id=?",
                 (tid,))[0]["blocked_on"] is None
    assert orch.plan_wakes()["changed"] == [], "still blocked: it wakes nobody"
    assert orch.board()["health"]["blocked_unspecified"] == 1, (
        "and it is SAID OUT LOUD, or NULL is just the graveyard again")

    # The first touch has to make it honest, one way or the other.
    with pytest.raises(ValueError, match="blocked_on"):
        orch.task_update(tid, status="blocked")
    orch.task_update(tid, status="pending")
    assert orch.board()["health"]["blocked_unspecified"] == 0


#: agent_tasks as a host that predates deskd left it: its own vocabulary frozen
#: into CHECK constraints, and no blocked_on. This is the shape _migrate exists
#: for, and (per its own comment) a real desk is running on one.
_LEGACY_AGENT_TASKS_DDL = """
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, detail TEXT,
    assignee_role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','in_progress','blocked','done','cancelled')),
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('urgent','normal','low')),
    source_kind TEXT NOT NULL DEFAULT 'self'
        CHECK (source_kind IN ('meeting','self','system')),
    source_ref TEXT, due_at TEXT, created_by TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_note TEXT
"""


def test_a_legacy_db_migrates_the_new_column_and_the_old_check_together(desk):
    """The two migrations on this table have to survive EACH OTHER.

    Dropping a legacy CHECK means rebuilding the table by copying every row with
    `SELECT *` into the new DDL — which carries blocked_on. So the column has to
    be added BEFORE that copy, or the rebuild dies on a column-count mismatch:
    "table agent_tasks__new has 14 columns but 13 values were supplied", raised
    from `connect()`, i.e. from every command on the desk at once.

    Nothing covered the legacy shape, which is exactly why it is worth covering:
    it exists on precisely the databases nobody develops against and every real
    desk runs on. A fresh DB gets the column from ORCH_SCHEMA and skips both
    branches, so the whole suite can pass with this hopelessly broken.
    """
    tid = orch.task_add("blocked since 07:02", assignee_role="alpha")
    with orch.connect(write=True) as c:
        c.execute("UPDATE agent_tasks SET status='blocked' WHERE id=?", (tid,))

    con = sqlite3.connect(CONFIG.db_path)          # bare: orch.connect() migrates
    try:
        con.executescript(f"""
            CREATE TABLE legacy ({_LEGACY_AGENT_TASKS_DDL});
            INSERT INTO legacy SELECT id,title,detail,assignee_role,status,priority,
                   source_kind,source_ref,due_at,created_by,created_at,updated_at,
                   result_note FROM agent_tasks;
            DROP TABLE agent_tasks;
            ALTER TABLE legacy RENAME TO agent_tasks;
        """)
        con.commit()
    finally:
        con.close()

    # The whole point: this call is the migration, and it must not raise.
    task = orch.tasks(assignee_role="alpha", include_closed=True)[0]

    assert (task["id"], task["status"]) == (tid, "blocked")
    assert task["blocked_on"] is None, "no dependency was recorded, so none is invented"
    assert orch.board()["health"]["blocked_unspecified"] == 1
    # ...and the OTHER migration still did its job: the host owns its vocabulary.
    with orch.connect() as c:
        assert not orch._has_enum_check(c, "agent_tasks", "source_kind")


# --- 12. the stall breaker: a task nobody moves stops waking anyone ------------

def _stall(role: str = "alpha") -> None:
    """Wake `role` for its queue until everything already in it stalls.

    Ages the queue first. The engine stamps to the second and a whole test runs
    inside one, so without this every wake lands in the same second the task was
    created and none of them counts — a wake only counts against a task it
    happened AFTER. Nothing sleeps here, exactly like `_backdate`.

    Role-scoped, because the stall count is: a wake shows the agent its WHOLE
    queue. A task added after this returns is untouched by these wakes and is
    still actionable, which is the only way to have both kinds at once.
    """
    with orch.connect(write=True) as c:
        c.execute("UPDATE agent_tasks SET updated_at=? WHERE assignee_role=?",
                  (iso(-3600), role))
    for _ in range(CONFIG.idle_task_stall_wakes):
        _idle(role)
        orch.plan_wakes()
        _busy(role)              # it booted, looked, and moved something else
        orch.plan_wakes()
    _idle(role)


def test_a_stalled_task_stops_waking_anyone(desk):
    """THE loop breaker, and it must be a rule rather than a cooldown.

    Wake the agent for its queue, it does not move the task, so the task wakes it
    again — forever. A cooldown would only slow that down, and would be a patch
    over the missing rule, which is the mistake this codebase keeps making. The
    rule: a task the agent has been woken for N times and has not moved has
    stopped being a reason to wake anyone. It leaves the actionable set and
    becomes a fact someone must decide about.
    """
    orch.task_add("nobody will ever do this", assignee_role="alpha")

    _stall()

    assert orch.plan_wakes()["changed"] == [], "a task nobody moves stops waking"
    assert _idle_attempts() == []
    assert orch.board()["health"]["stalled_tasks"] == 1, (
        "and it is REPORTED — it stopped waking, it did not disappear")


def test_the_stall_count_is_derived_and_never_stored(desk):
    """Time-dependent state is computed at read time, never stored (design.md).

    A `stalled_at` / `stall_count` column would be a second, staler copy of what
    the wake ledger already records, and the first thing to write it during a
    rolled-back dry run would make the desk permanently wrong. The count is a
    COUNT over wake_attempts, so deleting the attempts un-stalls the task with no
    fixup anywhere.
    """
    orch.task_add("stalls", assignee_role="alpha")
    _stall()
    assert orch.board()["health"]["stalled_tasks"] == 1

    assert not any("stall" in c["name"] for c in
                   _rows("SELECT name FROM pragma_table_info('agent_tasks')"))
    with orch.connect(write=True) as c:
        c.execute("DELETE FROM wake_attempts")

    assert orch.board()["health"]["stalled_tasks"] == 0, "derived, not stored"


def test_a_task_that_moves_resets_its_stall_count(desk):
    """Stall is measured from the task's last MOVE, so working on it buys it a
    fresh N. Otherwise a long, real piece of work would go silent halfway through
    — a task the agent is actively grinding on would stop waking it, which is the
    precise opposite of the point."""
    tid = orch.task_add("long but real work", assignee_role="alpha")
    _stall()
    assert orch.plan_wakes()["changed"] == []

    orch.task_update(tid, status="in_progress")   # it moved
    _idle("alpha")

    plan = orch.plan_wakes()
    assert [c["reason_kind"] for c in plan["changed"]] == ["idle_task"], (
        "a task that moved is live work again")
    assert orch.board()["health"]["stalled_tasks"] == 0


def test_only_queue_wakes_count_towards_a_stall(desk):
    """Stall means "we woke you FOR YOUR QUEUE and you did not move this".

    A wake for something else is not evidence about the queue at all: an agent
    hauled out of bed three times for urgent alerts has not been asked about its
    homework once. Counting every reason_kind would retire tasks the agent was
    never shown — silently, which is the exact disease this change treats, caused
    this time by its cure.
    """
    _busy("alpha")                      # so nothing here can be an idle_task wake
    orch.task_add("homework", assignee_role="alpha")
    with orch.connect(write=True) as c:
        c.execute("UPDATE agent_tasks SET updated_at=?", (iso(-3600),))
    for i in range(CONFIG.idle_task_stall_wakes * 2):
        alarm = orch.task_add(f"alarm {i}", assignee_role="alpha", priority="urgent")
        orch.plan_wakes()
        orch.task_close(alarm)
        orch.plan_wakes()

    assert {a["reason_kind"] for a in _attempts()} == {"urgent_task"}
    assert len(_attempts()) > CONFIG.idle_task_stall_wakes, "plenty of wakes..."
    assert orch.board()["health"]["stalled_tasks"] == 0, "...none of them about the queue"

    _idle("alpha")
    assert [c["reason_kind"] for c in orch.plan_wakes()["changed"]] == ["idle_task"], (
        "the homework is still live work and must still wake it")


def test_a_live_idle_task_demand_is_not_churned_every_tick(desk):
    """At-least-once means ticks repeat, so an unresolved demand's attempt has to
    SURVIVE them.

    A resolve-and-recreate every minute looks almost identical from the outside —
    one open attempt, agent still woken — but the attempt's `attempted_at` resets
    each tick, so its SLA never lapses, it never climbs its rung, and every
    latency in the ledger reads as a few seconds. The wake path would look
    perfectly healthy while never actually escalating anything.
    """
    _idle("alpha")
    orch.task_add("homework", assignee_role="alpha")

    first = orch.plan_wakes()
    second = orch.plan_wakes()
    third = orch.plan_wakes()

    assert len(first["changed"]) == 1
    assert second["changed"] == third["changed"] == []      # within SLA: nothing new
    assert second["resolved"] == third["resolved"] == []    # and nothing closed
    assert len(_attempts()) == 1


def test_a_demand_the_ladder_walks_to_stalled_closes_as_a_timeout(desk):
    """"We gave up" must never be filed as "the wake landed".

    An agent that never boots keeps its demand alive, so the ladder retries — and
    each retry is itself a queue wake, which walks the task to stalled. The demand
    then dies without ever having landed. Recording that as `acked` would post a
    fictional latency and average the stall breaker into the wake stats as a
    success, hiding the one event those stats exist to expose.
    """
    _idle("alpha")
    orch.task_add("nobody will ever do this", assignee_role="alpha")
    with orch.connect(write=True) as c:                 # older than any backdating
        c.execute("UPDATE agent_tasks SET updated_at=?", (iso(-20 * YEAR),))

    outcomes = []
    for _ in range(CONFIG.idle_task_stall_wakes + 2):
        _backdate(10 * YEAR)                           # alpha never comes back
        outcomes += [(r["reason_kind"], r["outcome"])
                     for r in orch.plan_wakes()["resolved"]]

    assert ("idle_task", "timeout") in outcomes
    assert ("idle_task", "acked") not in outcomes, "alpha never woke — nothing landed"
    assert _idle_attempts() == []
    assert orch.board()["health"]["stalled_tasks"] == 1


def test_a_stalled_task_still_wakes_nobody_after_a_century(desk):
    """A stalled task is not a suppressed one waiting for a timer to lapse: there
    is no timer. It is out of the actionable set until something MOVES it, which
    is what makes the loop impossible rather than merely slow."""
    orch.task_add("nobody will ever do this", assignee_role="alpha")
    _stall()

    for _ in range(5):
        _backdate(100 * YEAR)
        _idle("alpha")
        assert orch.plan_wakes()["changed"] == []


# --- 13. HARD RULE: homework never pages a person ----------------------------

def test_an_idle_task_wake_can_never_reach_a_human(desk):
    """A task wake must NEVER escalate to a human. Getting this wrong is the
    worst possible outcome of this change.

    The ladder climbs to a person because a MESSAGE MUST LAND. A to-do list has
    no such property: nobody is owed it, and there is no answer a human woken at
    3am could give that a queue needed.

    idle_task resolves the moment the agent boots, so in practice it never sits
    long enough to climb. That is an ARGUMENT, and this is the one guarantee that
    must not rest on one — arguments stay true until an unrelated change makes
    them false, and the failure here is somebody's phone at 3am. So: an agent
    that never boots, and a task kept moving so the stall breaker never retires
    the demand (that is the other safety net, and this test must not be able to
    pass because of it). Drive the clock a decade past every rung's SLA, twenty
    times over. The ladder must stop dead below the machine boundary.
    """
    _idle("alpha")
    tid = orch.task_add("homework", assignee_role="alpha")
    human = orch._human_level(CONFIG.wake_ladder)
    assert human > 0, "a ladder with no machine rung would make this vacuous"

    for i in range(20):
        orch.task_update(tid, detail=f"still moving {i}")   # never stalls
        _backdate(10 * YEAR)                               # every SLA, blown
        orch.plan_wakes()
        live = _idle_attempts()
        assert len(live) == 1, "the demand is still alive — nothing else is capping it"
        assert live[0]["level"] < human
        assert not CONFIG.wake_ladder[live[0]["level"]].leaves_machine

    # Every rung it EVER touched, not just the one it is on now.
    assert {a["channel"] for a in _attempts()} <= {
        r.channel for r in CONFIG.wake_ladder[:human]}
    assert orch.board()["health"]["wakes_at_human_level"] == 0
    assert all(not CONFIG.wake_ladder[a["level"]].leaves_machine for a in _attempts())


def test_a_capped_idle_task_wake_still_retries_its_top_rung(desk):
    """The ceiling must not turn into a dead end.

    An attempt row is not proof a session ran — the driver skips when the role
    lock is held, and launches fail — so the top machine rung has to keep being
    retried, exactly like every other wake (at-least-once). If the cap instead
    froze the attempt, the demand would sit pending forever AND its stall count
    would stop advancing, so the task would never be reported either: a new
    graveyard, built by the fix for the old one.
    """
    _idle("alpha")
    tid = orch.task_add("homework", assignee_role="alpha")
    orch.plan_wakes()
    ceiling = orch._reason_ceiling("idle_task", CONFIG.wake_ladder)
    assert _idle_attempts()[0]["level"] < ceiling, "it has somewhere to climb first"

    levels = []
    for i in range(4):
        orch.task_update(tid, detail=f"moved {i}")   # the stall breaker stays out
        _backdate(10 * YEAR)                        # this rung's SLA, blown
        plan = orch.plan_wakes()
        levels.append((plan["changed"][0]["level"], plan["changed"][0]["escalated"]))

    # It climbs to the ceiling, then keeps re-attempting it — never freezing, and
    # never calling a retry an escalation.
    assert levels == [(ceiling, True)] + [(ceiling, False)] * 3
    assert len(_idle_attempts()) == 1, "exactly one live rung, as ever"
    assert [a["outcome"] for a in _attempts()] == ["superseded"] * 4 + ["pending"]


# --- an open work meeting is work, and lives in the work queue ---------------

def _close_tasks(role: str | None = None) -> list[dict]:
    sql = ("SELECT * FROM agent_tasks WHERE source_kind='meeting' "
           "AND status='pending'")
    rows = _rows(sql)
    return [r for r in rows if role is None or r["assignee_role"] == role]


def test_an_open_work_meeting_sits_in_its_attendees_task_lists_softly(desk):
    """A meeting lives in its own tables, not in anyone's queue — so "I still owe
    this a close" existed nowhere an agent looks between wakes, and the only
    thing that noticed was the idle deadline retiring it an hour later.

    Soft is the whole point. A conversation that is still going does not need an
    interrupt telling you to end it, so this must surface WITHOUT interrupting:
    normal priority never cuts into a running turn.

    Both attendees are working here, which is what "still going" MEANS — they are
    the ones holding the conversation. Park them both and the close task becomes
    ordinary queued work that an idle_task wake will rightly pick up, because a
    meeting nobody is attending and nobody has closed is exactly the thing that
    used to be noticed only by an idle deadline an hour later.
    """
    _busy("alpha")
    _busy("beta")
    status = meetings.call_meeting(agenda="rotation review", called_by="alpha",
                                   attendees=["alpha", "beta"])
    thread_id = status["meeting"]["thread_id"]
    meetings.check_in(thread_id, role="beta")

    orch.plan_wakes()

    owed = {t["assignee_role"]: t for t in _close_tasks()}
    assert set(owed) == {"alpha", "beta"}, "both attendees own finishing it"
    assert all(t["priority"] == "normal" for t in owed.values())
    assert all(t["source_ref"] == thread_id for t in owed.values())
    assert _open_attempts() == [], "a live meeting must not wake anyone to end it"


def test_a_meeting_left_open_after_it_went_quiet_turns_urgent_and_wakes(desk):
    """The escalation the supervisor never has to make. Idle means the
    conversation is over in every sense except the ledger's: it stopped, nobody
    ended it, and a reminder sitting in a list the agent is not reading has
    already failed. Only then does it become an interrupt."""
    status = meetings.call_meeting(agenda="forgotten", called_by="alpha",
                                   attendees=["alpha", "beta"])
    thread_id = status["meeting"]["thread_id"]
    meetings.check_in(thread_id, role="beta")
    orch.plan_wakes()
    assert [t["priority"] for t in _close_tasks()] == ["normal", "normal"]

    with orch.connect(write=True) as c:
        c.execute("UPDATE mailbox_threads SET expires_at=? WHERE id=?",
                  (iso(-3600), thread_id))
    meetings.meeting_status(thread_id)  # a read is what retires an idle thread

    orch.plan_wakes()

    assert [t["priority"] for t in _close_tasks()] == ["urgent", "urgent"]
    woken = {a["role"] for a in _open_attempts() if a["reason_kind"] == "urgent_task"}
    assert woken == {"alpha", "beta"}


def test_a_dm_with_the_supervisor_never_becomes_a_close_task(desk):
    """The agent is refused if it tries (meetings._propose_end: theirs to end),
    so a task demanding it would be one the agent cannot discharge — pending
    forever, and once urgent climbing the ladder to the very human it was told
    not to bother."""
    called = meetings.apply_simple_supervisor_action(
        {"action": "call", "agenda": "one on one", "attendees": ["alpha"]})
    meetings.check_in(called["meeting"]["thread_id"], role="alpha")

    orch.plan_wakes()

    assert _close_tasks() == [], "a DM is not the agent's to close"


def test_closing_the_meeting_retires_the_close_task(desk):
    """Generation and resolution agree here too. A queue that fills with closes
    nobody can perform is worse than no queue, and any that had gone urgent
    would climb the ladder over a finished conversation."""
    status = meetings.call_meeting(agenda="finished properly", called_by="alpha",
                                   attendees=["alpha", "beta"])
    thread_id = status["meeting"]["thread_id"]
    meetings.check_in(thread_id, role="beta")
    orch.plan_wakes()
    assert len(_close_tasks()) == 2

    meetings.propose_end(thread_id, role="alpha", resolution="done")
    assert meetings.confirm_end(thread_id, role="beta")["closed"] is True

    orch.plan_wakes()
    assert _close_tasks() == []
