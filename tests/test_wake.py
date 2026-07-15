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
def test_overdue_task_never_wakes(desk, status):
    """A due date is attention, not an interrupt.

    The bug: `due_at < now` was treated as a wake demand, so every stale task on
    the board spawned a session at 3am. Overdue must be loud and inert.
    """
    tid = orch.task_add("quarterly thing", assignee_role="alpha",
                        priority="normal", due_at=iso(-YEAR))
    if status != "pending":
        orch.task_update(tid, status=status)

    plan = orch.plan_wakes()

    assert plan["changed"] == []
    assert plan["actions"] == []
    assert _attempts() == []
    # ...and it is still visibly overdue. Inert, not hidden.
    assert orch.tasks(assignee_role="alpha")[0]["overdue"] is True


def test_no_amount_of_overdue_wakes(desk):
    """Overdue is not a gradient that eventually crosses into waking."""
    for years, prio in ((1, "normal"), (5, "low"), (20, "normal")):
        orch.task_add(f"{years}y late", assignee_role="beta",
                      priority=prio, due_at=iso(-YEAR * years))

    plan = orch.plan_wakes()

    assert plan["changed"] == []
    assert _attempts() == []


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
    year-late normal task sitting beside it does not."""
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
    """
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
    assert [a["role"] for a in _open_attempts()] == ["beta"]

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
    assert [a["role"] for a in _open_attempts()] == ["beta"]

    meetings.apply_simple_supervisor_action(
        {"action": "force_close", "meeting_id": thread_id, "reason": "done here"})

    with orch.connect() as c:
        assert [d for d in orch.collect_wake_demand(c)
                if d["reason_kind"] == "owed_reply"] == []
    orch.plan_wakes()
    assert [a for a in _open_attempts() if a["reason_kind"] == "owed_reply"] == [], (
        "an attempt outstanding when the meeting closed must retire, not strand"
    )


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
    interrupt telling you to end it, so this must surface WITHOUT waking: normal
    priority, and the module's headline rule holds — only urgent wakes.
    """
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
