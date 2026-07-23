"""Presence, session lifecycle, cross-day rollover, and the board aggregates.

The engine derives liveness and day-staleness at READ time from stored
timestamps, so every test here injects a clock rather than sleeping: a suite
that sleeps for a 600s threshold is a suite nobody runs.

Two rules shape the assertions:

- Liveness thresholds are read from CONFIG, never spelled as 120/600. A host
  retunes them; a test that hardcodes them would pass against an engine that
  ignores the config entirely.
- The day boundary is computed here with `zoneinfo` directly, NOT by calling
  `orch._session_day()`. Asserting the engine's day against the engine's own
  day function is a tautology: a naive `utcnow().date()` would be naive on both
  sides of the comparison and the test would still pass. The independent
  computation is the whole point.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from deskd import config as cfg_mod
from deskd import orchestration as orch
from deskd.config import CONFIG, RoleSpec

NY = ZoneInfo("America/New_York")

#: Mid-January, mid-afternoon UTC: same calendar day in UTC and in New York, so
#: nothing in the presence tests accidentally rides on the day boundary.
BASE = dt.datetime(2026, 1, 15, 15, 0, 0, tzinfo=dt.timezone.utc)

#: 03:00 UTC on the 15th is 22:00 on the 14th in New York — the UTC date and the
#: NY date disagree. Every rollover-timezone test is anchored here.
SPLIT = dt.datetime(2026, 1, 15, 3, 0, 0, tzinfo=dt.timezone.utc)


class Clock:
    """A hand-cranked replacement for the engine clock (orch.store._now).

    Every timestamp the engine writes and every age it derives flows through
    `_now()`, so patching this one function moves the engine's whole notion of
    time coherently — no partially-frozen state.
    """

    def __init__(self, monkeypatch, start: dt.datetime):
        self.t = start
        monkeypatch.setattr(orch.store, "_now", lambda: self.t)

    def advance(self, seconds: float) -> None:
        self.t = self.t + dt.timedelta(seconds=seconds)

    def set(self, when: dt.datetime) -> None:
        self.t = when


@pytest.fixture
def clock(desk, monkeypatch):
    return Clock(monkeypatch, BASE)


def ny_day(when: dt.datetime) -> str:
    """The New York calendar date of `when`, computed without the engine."""
    return when.astimezone(NY).date().isoformat()


def agent_of(payload: dict, role: str) -> dict:
    (found,) = [a for a in payload["agents"] if a["role"] == role]
    return found


def presence_of(role: str) -> dict:
    (found,) = [p for p in orch.presence() if p["role"] == role]
    return found


def force_session_day(role: str, day: str | None) -> None:
    """Stamp `session_day` directly, bypassing the engine's own day function."""
    with orch.connect(write=True) as c:
        c.execute("UPDATE agent_sessions SET session_day=? WHERE role=?", (day, role))


def snapshot(db_path) -> dict:
    """Every row of every table, for proving a dry run wrote nothing.

    Opened with a bare sqlite3 connection on purpose: `orch.connect()` applies
    schema and seeds the registry, so using it to observe would be measuring
    with an instrument that mutates.
    """
    con = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: con.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
    finally:
        con.close()


# --- presence / liveness ----------------------------------------------------

def test_never_registered_role_reports_never_not_error(clock):
    """A role in the registry that has never run is a normal state, not a gap.

    The board renders every registered role from the first tick, before any
    agent has ever started — so 'never' has to be a value, not an exception or
    a missing entry.
    """
    rows = orch.presence()
    assert {p["role"] for p in rows} == {"alpha", "beta", "gamma"}
    for p in rows:
        assert p["liveness"] == "never"
        assert p["heartbeat_age_seconds"] is None
        assert p["last_heartbeat_at"] is None


def test_liveness_online_just_under_threshold(clock):
    orch.heartbeat("alpha", state="working")
    clock.advance(CONFIG.online_max_seconds - 1)
    p = presence_of("alpha")
    assert p["liveness"] == "online"
    assert p["heartbeat_age_seconds"] == CONFIG.online_max_seconds - 1


def test_liveness_flips_to_suspect_exactly_at_online_threshold(clock):
    """The boundary is exclusive: age == online_max is already suspect.

    Pinned because an off-by-one here silently widens 'online' by a threshold's
    worth of staleness, and the wake ladder starts a demand on the L0 in-session
    hook for anything it believes is online — a wake delivered to a session that
    is no longer there.
    """
    orch.heartbeat("alpha", state="working")
    clock.advance(CONFIG.online_max_seconds)
    assert presence_of("alpha")["liveness"] == "suspect"


def test_liveness_suspect_just_under_dead_threshold(clock):
    orch.heartbeat("alpha", state="working")
    clock.advance(CONFIG.suspect_max_seconds - 1)
    assert presence_of("alpha")["liveness"] == "suspect"


def test_liveness_flips_to_dead_exactly_at_suspect_threshold(clock):
    orch.heartbeat("alpha", state="working")
    clock.advance(CONFIG.suspect_max_seconds)
    assert presence_of("alpha")["liveness"] == "dead"


def test_liveness_offline_after_end_session_regardless_of_age(clock):
    """An ended session is 'offline', not 'online' — ending beats heartbeat age.

    `end_session` refreshes `last_heartbeat_at`, so a purely age-based reading
    would call a just-ended session 'online'. `ended_at` has to win.
    """
    orch.heartbeat("alpha", state="working")
    orch.end_session("alpha")
    p = presence_of("alpha")
    assert p["liveness"] == "offline"
    assert p["ended_at"] is not None


def test_liveness_is_derived_at_read_time_not_stored(clock):
    """Same untouched row, three answers — liveness must never be persisted.

    If a future change caches liveness in the table, this is the test that
    fails: nothing writes to the session between these three reads.
    """
    orch.heartbeat("alpha", state="working")
    assert presence_of("alpha")["liveness"] == "online"
    clock.advance(CONFIG.online_max_seconds)
    assert presence_of("alpha")["liveness"] == "suspect"
    clock.advance(CONFIG.suspect_max_seconds)
    assert presence_of("alpha")["liveness"] == "dead"


def test_thresholds_track_config_not_constants(clock, desk):
    """Retuning CONFIG must actually retune liveness.

    Guards the engine reading its own hardcoded 120/600 instead of the host's
    values — invisible to any test that asserts the defaults.
    """
    cfg_mod.configure(online_max_seconds=10, suspect_max_seconds=20)
    orch.heartbeat("alpha", state="working")
    clock.advance(9)
    assert presence_of("alpha")["liveness"] == "online"
    clock.advance(1)          # age == 10
    assert presence_of("alpha")["liveness"] == "suspect"
    clock.advance(10)         # age == 20
    assert presence_of("alpha")["liveness"] == "dead"


def test_second_registration_for_a_role_never_creates_a_second_session(clock):
    """`agent_sessions` is keyed by ROLE: at most one session per role, ever.

    The role-scoped flock is what enforces this upstream, but the table is the
    last line of defence — a second row would give the board two presences for
    one agent and the wake ladder two session ids to resume.
    """
    orch.heartbeat("alpha", session_id="s1", state="working")
    orch.heartbeat("alpha", session_id="s2", state="working")
    with orch.connect() as c:
        rows = c.execute(
            "SELECT session_id FROM agent_sessions WHERE role='alpha'").fetchall()
        total = c.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s2"
    assert total == 1


def test_reviving_an_ended_role_reuses_the_single_row(clock):
    """Even across an end/restart cycle the role keeps exactly one row."""
    orch.heartbeat("alpha", session_id="s1")
    orch.end_session("alpha")
    orch.heartbeat("alpha", session_id="s2")
    with orch.connect() as c:
        rows = c.execute("SELECT * FROM agent_sessions WHERE role='alpha'").fetchall()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s2"
    assert rows[0]["ended_at"] is None
    assert rows[0]["phase"] == "active"


# --- session lifecycle ------------------------------------------------------

def test_board_shows_no_current_work_after_end_session(clock):
    """The bug that started this: an ended session still showed as executing.

    An agent that had finished for the day still rendered an in-progress item on
    the board, because the session's live todo breakdown outlived the session
    that wrote it. design.md: "The board shows 'now executing' only for a live
    session. An ended session's last activity is shown as history, never as
    current work."
    """
    orch.heartbeat("alpha", state="working", activity="wrap-up review")
    orch.record_todos("alpha", [
        {"content": "wrap-up review", "status": "in_progress"},
    ])
    assert agent_of(orch.board(), "alpha")["session_todos"] is not None

    orch.end_session("alpha")

    a = agent_of(orch.board(), "alpha")
    assert a["session_todos"] is None
    assert a["liveness"] == "offline"
    assert a["ended_at"] is not None
    assert a["state"] != "working"


def test_ended_session_todos_are_gone_from_storage(clock):
    """The todo mirror belongs to its session; a fresh one writes its own."""
    orch.heartbeat("alpha", state="working")
    orch.record_todos("alpha", [{"content": "x", "status": "in_progress"}])
    orch.end_session("alpha")
    with orch.connect() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM session_todos WHERE role='alpha'").fetchone()[0] == 0


def test_agent_detail_hides_todos_for_a_dead_session(clock):
    """A crashed session (no `end_session`) is not live, so it is not executing.

    This is the engine stating the intent in its own code: `agent_detail` gates
    the todo mirror on liveness in (online, suspect). The board is asserted
    against the same rule below.
    """
    orch.heartbeat("alpha", state="working", activity="EOD meeting")
    orch.record_todos("alpha", [{"content": "EOD meeting", "status": "in_progress"}])
    clock.advance(CONFIG.suspect_max_seconds + 1)

    d = orch.agent_detail("alpha")
    assert d["presence"]["liveness"] == "dead"
    assert d["session_todos"] is None


def test_board_hides_todos_for_a_dead_session(clock):
    """Same invariant as the end_session case, via the path that has no cleanup.

    `end_session` deletes the todo mirror, which is why the reported bug looked
    fixed. A session that dies without calling it never triggers that delete,
    and the board has no liveness check to fall back on — so the original
    symptom returns for exactly the sessions most likely to hit it.
    """
    orch.heartbeat("alpha", state="working", activity="EOD meeting")
    orch.record_todos("alpha", [{"content": "EOD meeting", "status": "in_progress"}])
    clock.advance(CONFIG.suspect_max_seconds + 1)

    a = agent_of(orch.board(), "alpha")
    assert a["liveness"] == "dead"
    assert a["session_todos"] is None


def test_board_shows_todos_for_a_suspect_session(clock):
    """The other half of the gate: `suspect` is LIVE, so it is still executing.

    The dead-session tests above only pin one direction — that a gate exists at
    all. They are equally happy with a gate that is too TIGHT, and the tempting
    way to write this one is `liveness == "online"`, which reads correct and
    passes every other test in this suite. It would blank the live work of any
    agent merely slow to heartbeat — a long tool call — which is precisely the
    agent an operator is staring at the board to watch. `suspect` means "late,
    not gone": the engine keeps it in LIVE_LIVENESS, so the board must too.

    Asserted against LIVE_LIVENESS rather than the literals, because the tuple is
    the engine's statement of what "live" means and a host may retune the
    thresholds underneath it.
    """
    orch.heartbeat("alpha", state="working", activity="long tool call")
    orch.record_todos("alpha", [{"content": "long tool call", "status": "in_progress"}])
    clock.advance(CONFIG.online_max_seconds + 1)

    a = agent_of(orch.board(), "alpha")
    assert a["liveness"] == "suspect"
    assert "suspect" in orch.LIVE_LIVENESS
    assert a["session_todos"] is not None
    assert a["session_todos"]["snapshot"] == [
        {"content": "long tool call", "status": "in_progress"}]

    # board() mirrors agent_detail()'s gate; a fix applied to only one of the two
    # leaves the console contradicting itself, so both are pinned here.
    d = orch.agent_detail("alpha")
    assert d["presence"]["liveness"] == "suspect"
    assert d["session_todos"] is not None


def test_end_session_is_idempotent(clock):
    """The driver may end a session it already ended (retry, lock race)."""
    orch.heartbeat("alpha", state="working")
    orch.end_session("alpha")
    first = presence_of("alpha")["ended_at"]
    clock.advance(60)
    orch.end_session("alpha")
    again = presence_of("alpha")

    assert again["liveness"] == "offline"
    assert again["phase"] == "closed"
    with orch.connect() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM agent_sessions WHERE role='alpha'").fetchone()[0] == 1
    assert first is not None and again["ended_at"] is not None


def test_end_session_on_a_never_started_role_does_not_blow_up(clock):
    """Rollover and shutdown paths call this blind; a missing row is normal."""
    orch.end_session("beta")
    p = presence_of("beta")
    assert p["liveness"] == "never"
    assert p["ended_at"] is None


def test_end_session_rejects_an_unknown_role(clock):
    with pytest.raises(ValueError):
        orch.end_session("nonesuch")


# --- rollover ---------------------------------------------------------------

def test_same_day_session_never_rolls(clock):
    """Rolling a live same-day session would kill an agent mid-workday.

    The day is stamped independently of the engine, so this fails if the
    engine's 'today' disagrees with the real New York date.
    """
    orch.heartbeat("alpha", state="working")
    force_session_day("alpha", ny_day(BASE))

    plan = orch.rollover_plan(record=False)
    assert plan["rollovers"] == []
    assert plan["today"] == ny_day(BASE)


def test_prior_day_session_rolls(clock):
    orch.heartbeat("alpha", state="working")
    force_session_day("alpha", ny_day(BASE - dt.timedelta(days=1)))

    plan = orch.rollover_plan(record=False)
    assert [r["role"] for r in plan["rollovers"]] == ["alpha"]
    assert plan["rollovers"][0]["from_day"] == ny_day(BASE - dt.timedelta(days=1))
    assert plan["rollovers"][0]["to_day"] == ny_day(BASE)


def test_null_session_day_never_rolls(clock):
    """Unknown day is not 'old'.

    A row predating the session_day migration has NULL there. `NULL < today` is
    NULL in SQL — falsy — but the intent is explicit rather than incidental: an
    unknown day must never be treated as stale and wound down.
    """
    orch.heartbeat("alpha", state="working")
    force_session_day("alpha", None)

    assert orch.rollover_plan(record=False)["rollovers"] == []
    assert presence_of("alpha")["stale_day"] is False


def test_ended_session_never_rolls(clock):
    """Rollover winds down live leftovers; an ended session is already done."""
    orch.heartbeat("alpha", state="working")
    force_session_day("alpha", ny_day(BASE - dt.timedelta(days=1)))
    orch.end_session("alpha")

    assert orch.rollover_plan(record=False)["rollovers"] == []


def test_day_boundary_is_new_york_not_utc(clock):
    """22:00 in New York is still today, even though UTC has ticked over.

    At 03:00 UTC the UTC date is the 15th while New York is still on the 14th. A
    session stamped with the NY day is a LIVE, same-day session; a naive
    `utcnow().date()` computes today as the 15th, sees "2026-01-14" < "2026-01-15",
    and winds down an agent that is mid-session. This is the exact hour the
    engine's own overnight sessions run.
    """
    clock.set(SPLIT)
    assert ny_day(SPLIT) != SPLIT.date().isoformat()   # the trap is armed

    orch.heartbeat("alpha", state="working")
    force_session_day("alpha", ny_day(SPLIT))

    plan = orch.rollover_plan(record=False)
    assert plan["today"] == ny_day(SPLIT)
    assert plan["rollovers"] == []


def test_day_boundary_new_york_still_rolls_a_genuinely_old_session(clock):
    """The mirror of the case above: the NY day wins in BOTH directions.

    Guards a fix that over-corrects into never rolling anything near midnight.
    At 03:00 UTC on the 15th, a session from the NY 13th is genuinely stale.
    """
    clock.set(SPLIT)
    orch.heartbeat("alpha", state="working")
    force_session_day("alpha", ny_day(SPLIT - dt.timedelta(days=1)))

    plan = orch.rollover_plan(record=False)
    assert [r["role"] for r in plan["rollovers"]] == ["alpha"]
    assert plan["rollovers"][0]["from_day"] == ny_day(SPLIT - dt.timedelta(days=1))


def test_stale_day_flag_uses_new_york_too(clock):
    """The board's own staleness badge must agree with rollover_plan.

    Two independent day computations that can disagree would show an agent as
    stale on the board while rollover never touches it.
    """
    clock.set(SPLIT)
    orch.heartbeat("alpha", state="working")
    force_session_day("alpha", ny_day(SPLIT))
    assert presence_of("alpha")["stale_day"] is False

    force_session_day("alpha", ny_day(SPLIT - dt.timedelta(days=1)))
    assert presence_of("alpha")["stale_day"] is True


def test_rollover_dry_run_mutates_nothing(clock, desk):
    """record=False is a preview: the driver calls it to decide, not to act.

    Every table, byte for byte. A dry run that marked sessions 'draining' or
    logged an event would make previewing indistinguishable from executing.
    """
    orch.heartbeat("alpha", state="working")
    orch.record_todos("alpha", [{"content": "x", "status": "in_progress"}])
    orch.task_add("something", assignee_role="alpha")
    force_session_day("alpha", ny_day(BASE - dt.timedelta(days=1)))

    before = snapshot(desk.db_path)
    plan = orch.rollover_plan(record=False)
    after = snapshot(desk.db_path)

    assert plan["rollovers"], "precondition: there is something to roll"
    assert before == after


def test_rollover_record_marks_draining_and_logs(clock, desk):
    """The other half of the dry-run contract: record=True really does record."""
    orch.heartbeat("alpha", state="working")
    force_session_day("alpha", ny_day(BASE - dt.timedelta(days=1)))

    before = snapshot(desk.db_path)
    orch.rollover_plan(record=True)
    after = snapshot(desk.db_path)
    assert before != after

    p = presence_of("alpha")
    assert p["phase"] == "draining"
    assert p["state"] == "stopping"
    kinds = [e["kind"] for e in orch.recent_events(20)]
    assert "session_rollover" in kinds


def test_rollover_hands_off_with_the_session_done_sentinel(clock):
    """The wind-down is a handoff, not a kill: note first, then the sentinel.

    SESSION_DONE on its own line is what the driver watches for to know the
    drain finished; without it the driver cannot tell a wound-down session from
    a hung one. The prompt must also name both days, since the note is the only
    thing that survives into tomorrow's session.
    """
    orch.heartbeat("alpha", state="working")
    yesterday = ny_day(BASE - dt.timedelta(days=1))
    force_session_day("alpha", yesterday)

    (action,) = orch.rollover_plan(record=True)["rollovers"]
    prompt = action["prompt"]
    assert "handoff note" in prompt
    assert "SESSION_DONE" in prompt
    assert yesterday in prompt and ny_day(BASE) in prompt
    assert CONFIG.timezone in prompt
    assert "BOOTSTRAP:alpha" in prompt   # the host's own bootstrap, not a stock one


def test_rollover_reported_until_the_session_actually_ends(clock):
    """A lock-busy tick must retry, so the action stands until ended_at is set.

    Marking 'draining' is a board hint, not a completion record — dropping the
    action after the first tick would strand a session the driver never got to.
    """
    orch.heartbeat("alpha", state="working")
    force_session_day("alpha", ny_day(BASE - dt.timedelta(days=1)))

    assert orch.rollover_plan(record=True)["rollovers"]
    assert orch.rollover_plan(record=True)["rollovers"], "second tick must retry"

    orch.end_session("alpha")
    assert orch.rollover_plan(record=True)["rollovers"] == []


def test_fresh_session_opens_on_the_new_day_after_the_drain(clock):
    """The other side of rollover: drained, ended, then a clean same-day session.

    Restart-across-days only pays off if the new session is genuinely new — the
    stale day and the draining phase must not survive into it.
    """
    orch.heartbeat("alpha", state="working", session_id="yesterday")
    force_session_day("alpha", ny_day(BASE - dt.timedelta(days=1)))
    orch.rollover_plan(record=True)
    orch.end_session("alpha")

    orch.heartbeat("alpha", state="working", session_id="today")

    p = presence_of("alpha")
    assert p["session_day"] == ny_day(BASE)
    assert p["phase"] == "active"
    assert p["stale_day"] is False
    assert p["ended_at"] is None
    assert p["session_id"] == "today"
    assert orch.rollover_plan(record=False)["rollovers"] == []


def test_rollover_covers_every_stale_role_not_just_the_first(clock):
    """Three roles, three drains — the loop must not stop at one."""
    for role in ("alpha", "beta", "gamma"):
        orch.heartbeat(role, state="working")
        force_session_day(role, ny_day(BASE - dt.timedelta(days=1)))

    plan = orch.rollover_plan(record=True)
    assert sorted(r["role"] for r in plan["rollovers"]) == ["alpha", "beta", "gamma"]


# --- board / agent detail ---------------------------------------------------

def test_board_includes_every_registry_role(clock):
    """The registry is the source of truth — no hardcoded roster anywhere."""
    assert sorted(a["role"] for a in orch.board()["agents"]) == ["alpha", "beta", "gamma"]


def test_board_picks_up_a_newly_configured_role(clock, desk):
    """A host adding a fourth role gets a fourth agent, with no code change.

    The extraction bug this suite exists for: a third role silently got no
    board presence because the engine knew two names.
    """
    cfg_mod.configure(roles=tuple(CONFIG.roles) + (RoleSpec("delta", "Delta"),))

    agents = {a["role"]: a for a in orch.board()["agents"]}
    assert sorted(agents) == ["alpha", "beta", "delta", "gamma"]
    assert agents["delta"]["display_name"] == "Delta"
    assert agents["delta"]["liveness"] == "never"


def test_agent_detail_works_for_every_role(clock):
    for role in ("alpha", "beta", "gamma"):
        orch.heartbeat(role, state="working", activity=f"{role} work")
        d = orch.agent_detail(role)
        assert d["role"] == role
        assert d["presence"]["liveness"] == "online"
        assert d["presence"]["activity"] == f"{role} work"


def test_agent_detail_with_no_history_is_empty_not_a_crash(clock):
    """A detail page opened before the agent has ever run is a normal request."""
    d = orch.agent_detail("gamma")
    assert d["presence"]["liveness"] == "never"
    assert d["session_todos"] is None
    assert d["tasks"] == []
    assert d["inbox"] == []
    assert d["wake_history"] == []
    assert d["hooks"] == []
    assert d["delivery"] == []
    assert d["profile"]["display_name"] == "Gamma"


def test_agent_detail_rejects_an_unknown_role(clock):
    """Fail loudly: an empty page for a typo'd role reads as 'agent is idle'.

    `_agent_role` raises for anything outside the registry — asserted here as
    the intended contract, not merely the current behaviour.
    """
    with pytest.raises(ValueError, match="unknown or disabled agent role"):
        orch.agent_detail("nonesuch")


def test_agent_detail_rejects_the_supervisor(clock):
    """The supervisor is a human identity: no session, no heartbeat, no page."""
    with pytest.raises(ValueError, match="not an agent role"):
        orch.agent_detail(CONFIG.supervisor_role)


def test_agent_detail_skips_a_disabled_role(clock):
    """Disabling a role in the DB retires it; the registry row is authoritative."""
    orch.heartbeat("beta", state="working")
    with orch.connect(write=True) as c:
        c.execute("UPDATE agent_registry SET enabled=0 WHERE role='beta'")

    assert "beta" not in [a["role"] for a in orch.board()["agents"]]
    with pytest.raises(ValueError):
        orch.agent_detail("beta")
