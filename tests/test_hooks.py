"""Self-service wake hooks: timers, cron, and probes.

Hooks are the *only* sanctioned way for an agent to say "wake me later" — the
alternative the design forbids is an agent sleeping/polling in its own turn. So
two failure modes here are load-bearing and get most of the coverage:

1. **A hook that lies about when it fires.** A one-shot that re-fires forever, or
   a cron that drifts an hour twice a year, is worse than no hook at all: the
   agent trusted it and ended its turn.
2. **A probe is host code the engine imports and calls.** The allowlist is the
   only thing between "an agent registers a watcher" and "an agent names an
   arbitrary dotted path and the orchestrator runs it". Every test that touches
   the allowlist is a security test, not a feature test.

Time is injected, never slept: `clock` replaces `orchestration._now`, which is
the single source every timestamp and every tick reads.
"""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import sys
import time
from zoneinfo import ZoneInfo

import pytest

from deskd import orchestration
from deskd.config import configure

from conftest import ROLES

NY = ZoneInfo("America/New_York")

#: Real 2026 US DST transitions. A 06:15 local cron must land on 06:15 local on
#: both sides of each — the reason the implementation scans wall-clock minutes
#: through zoneinfo instead of doing UTC arithmetic.
SPRING_FORWARD = dt.date(2026, 3, 8)   # 02:00 EST -> 03:00 EDT
FALL_BACK = dt.date(2026, 11, 1)       # 02:00 EDT -> 01:00 EST


# --- time injection ---------------------------------------------------------

class _Clock:
    """A hand-cranked replacement for `orchestration._now`."""

    def __init__(self, start: dt.datetime):
        self.now = start

    def advance(self, seconds: float) -> dt.datetime:
        self.now += dt.timedelta(seconds=seconds)
        return self.now

    def iso(self, offset: float = 0.0) -> str:
        return orchestration._iso(self.now + dt.timedelta(seconds=offset))


@pytest.fixture
def clock(desk, monkeypatch):
    """Freeze the engine's clock at a Tuesday noon in the configured zone."""
    holder = _Clock(dt.datetime(2026, 7, 14, 16, 0, tzinfo=dt.timezone.utc))
    monkeypatch.setattr(orchestration.store, "_now", lambda: holder.now)
    return holder


def tick():
    """One orchestrator pass. Hooks are evaluated inside plan_wakes(record=True)."""
    return orchestration.plan_wakes()


def local(iso_utc: str) -> dt.datetime:
    """A stored UTC timestamp as wall-clock time in the configured zone."""
    return dt.datetime.fromisoformat(iso_utc).astimezone(NY)


def hook_row(hook_id: int) -> dict:
    for h in orchestration.hooks(include_closed=True):
        if h["id"] == hook_id:
            return h
    raise AssertionError(f"hook {hook_id} not found")


def ack_all(role: str) -> None:
    """Drain a role's inbox.

    Needed between ticks of a recurring hook because an un-acked item holds the
    (role, dedup_key) slot — that dedup is deliberate, but it means a test that
    never acks measures dedup rather than the timer.
    """
    orchestration.inbox_ack(ids=[i["id"] for i in orchestration.inbox_pending(role)])


# --- probe modules on disk --------------------------------------------------

PROBE_MODULE = "deskd_test_probes"
#: Shares PROBE_MODULE's dotted prefix as a plain string but NOT as a dotted
#: path — the allowlist must not admit it on a naive startswith().
LOOKALIKE_MODULE = PROBE_MODULE + "_evil"

_PROBE_SOURCE = '''
"""Written to disk by the test suite; imported by the engine's probe loader."""

CALLS = []
#: Test-controlled: one entry consumed per call. Empty = stay quiet.
SCRIPT = []
#: Test-controlled: paths `marker_writer` touches when the engine calls it.
MARKER = []


def marker_writer():
    """A probe whose side effect lives OUTSIDE the database.

    Every other probe here records its calls in memory, which a test could only
    ever observe as "some Python ran". A file is the honest stand-in for what a
    real probe does — hit an API, post a message, page someone: a rollback can
    undo a row, but it cannot un-send that. Returns None (no inbox item) so the
    file is the only thing it proves.
    """
    for path in MARKER:
        open(path, "a").close()
    return None


def watcher():
    CALLS.append(len(CALLS) + 1)
    action = SCRIPT.pop(0) if SCRIPT else "quiet"
    if action == "boom":
        raise RuntimeError("probe exploded")
    if action == "quiet":
        return None
    # Unique dedup_key per call: the inbox dedups same-key un-acked items, which
    # would otherwise swallow a fire this module is trying to prove happened.
    return {"title": "watcher fired", "dedup_key": f"call-{len(CALLS)}"}
'''


@pytest.fixture
def probes(tmp_path, monkeypatch):
    """Put two importable probe modules on sys.path and return the real one's name.

    On disk and importable *on purpose*: a refusal test only proves the allowlist
    if the module it names would otherwise load fine. Tests import the module
    themselves when they need its state — the deny tests must not.
    """
    d = tmp_path / "probe_root"
    d.mkdir()
    (d / f"{PROBE_MODULE}.py").write_text(_PROBE_SOURCE)
    (d / f"{LOOKALIKE_MODULE}.py").write_text(_PROBE_SOURCE)
    monkeypatch.syspath_prepend(str(d))
    importlib.invalidate_caches()
    try:
        yield PROBE_MODULE
    finally:
        for name in (PROBE_MODULE, LOOKALIKE_MODULE):
            sys.modules.pop(name, None)


def probe_state():
    return importlib.import_module(PROBE_MODULE)


# --- kind: at ---------------------------------------------------------------

def test_at_hook_fires_once_then_retires(clock):
    """A one-shot must not become a permanent alarm.

    The retirement is `status='done'` + `next_fire_at=NULL`. If either is missed
    the row stays due forever and the owner is woken every single tick, which is
    exactly the self-polling the hook API exists to prevent.
    """
    h = orchestration.hook_add("beta", "one shot", at=clock.iso())["hook"]

    assert [f["hook"] for f in tick()["hooks_fired"]] == [h]
    row = hook_row(h)
    assert row["status"] == "done"
    assert row["next_fire_at"] is None
    assert row["fire_count"] == 1

    ack_all("beta")
    clock.advance(86_400)
    assert tick()["hooks_fired"] == []
    assert hook_row(h)["fire_count"] == 1


# --- kind: interval ---------------------------------------------------------

def test_interval_hook_fires_repeatedly(clock):
    """An interval hook stays active and re-arms; it is not a one-shot."""
    every = orchestration.CONFIG.min_hook_every
    h = orchestration.hook_add("alpha", "standup nudge", every=every)["hook"]

    for expected in (1, 2, 3):
        clock.advance(every + 1)
        assert [f["hook"] for f in tick()["hooks_fired"]] == [h]
        row = hook_row(h)
        assert row["status"] == "active"
        assert row["fire_count"] == expected
        ack_all("alpha")


@pytest.mark.parametrize("kwargs", [
    pytest.param({"every": 1}, id="interval"),
    pytest.param({"callable_path": f"{PROBE_MODULE}:watcher", "every": 1}, id="probe"),
])
def test_sub_minimum_interval_is_rejected_not_clamped(clock, probes, kwargs):
    """A 1-second hook is REJECTED, not silently widened to the floor.

    Clamping would hand the agent a hook whose schedule is not the one it asked
    for and never tell it — the agent would reason about a cadence that does not
    exist. min_hook_every is a hard floor because a probe runs inside the tick.
    """
    configure(probe_allowlist=(PROBE_MODULE,))

    with pytest.raises(ValueError, match=r"every must be >= 60s"):
        orchestration.hook_add("alpha", "too eager", **kwargs)

    assert orchestration.hooks(include_closed=True) == []


# --- kind: cron -------------------------------------------------------------

@pytest.mark.parametrize("day, expected_day", [
    pytest.param("2026-07-13", "2026-07-13", id="mon"),
    pytest.param("2026-07-14", "2026-07-14", id="tue"),
    pytest.param("2026-07-15", "2026-07-15", id="wed"),
    pytest.param("2026-07-16", "2026-07-16", id="thu"),
    pytest.param("2026-07-17", "2026-07-17", id="fri"),
    # Sat/Sun must SKIP to Monday, not fire at the weekend's 06:15.
    pytest.param("2026-07-18", "2026-07-20", id="sat"),
    pytest.param("2026-07-19", "2026-07-20", id="sun"),
])
def test_cron_weekday_spec_skips_the_weekend(desk, day, expected_day):
    """'15 6 * * 1-5' is the canonical weekday schedule (it is in the README).

    Asserted from 05:00 local on each day of one week: a weekday resolves to its
    own 06:15, a weekend day jumps to Monday. An off-by-one in the dow mapping
    shows up here as a fire on Saturday or a silent skip of Monday.
    """
    after = dt.datetime.fromisoformat(f"{day}T05:00").replace(tzinfo=NY)

    fire = local(orchestration._next_cron_fire("15 6 * * 1-5", "America/New_York", after))

    assert fire.date().isoformat() == expected_day
    assert (fire.hour, fire.minute) == (6, 15)
    assert fire.weekday() <= 4  # Mon..Fri


@pytest.mark.parametrize("dow, expected_day", [
    # dow 0 is SUNDAY, not Monday. Python's weekday() is Mon=0, cron's is Sun=0;
    # the conversion between them is the single easiest off-by-one in this file.
    pytest.param("0", "2026-07-19", id="0-is-sunday"),
    pytest.param("1", "2026-07-20", id="1-is-monday"),
    pytest.param("6", "2026-07-18", id="6-is-saturday"),
])
def test_cron_dow_zero_is_sunday(desk, dow, expected_day):
    after = dt.datetime(2026, 7, 17, 12, 0, tzinfo=NY)  # a Friday

    fire = local(orchestration._next_cron_fire(f"15 6 * * {dow}", "America/New_York", after))

    assert fire.date().isoformat() == expected_day
    assert (fire.hour, fire.minute) == (6, 15)


def test_cron_dow_seven_is_refused_loudly_not_silently(desk, clock):
    """dow=7 is outside this implementation's accepted range (it takes 0..6).

    POSIX cron also spells Sunday as 7, so '0 6 * * 7' is a spec a user may well
    write. What matters for the hook contract is that it can never be *accepted
    and then never fire*: hook_add validates by computing the first firing time,
    so an unmatchable spec is rejected at registration. This test guards that
    fail-fast — if dow=7 is later given the Sunday meaning, it should fire on
    2026-07-19 (see test_cron_dow_zero_is_sunday) and this test should be
    replaced, never merely deleted.
    """
    with pytest.raises(ValueError, match="never matches"):
        orchestration.hook_add("alpha", "sunday sweep", cron="15 6 * * 7")

    assert orchestration.hooks(include_closed=True) == []


@pytest.mark.parametrize("transition, after_day, before_offset, after_offset", [
    pytest.param(SPRING_FORWARD, "2026-03-07", -5, -4, id="spring-forward"),
    pytest.param(FALL_BACK, "2026-10-31", -4, -5, id="fall-back"),
])
def test_cron_holds_local_time_across_dst(desk, transition, after_day,
                                          before_offset, after_offset):
    """06:15 local means 06:15 local on BOTH sides of a DST transition.

    This is the entire reason the scan walks wall-clock minutes through zoneinfo
    rather than adding 86400s in UTC: UTC arithmetic silently slides a daily cron
    by an hour twice a year, so a pre-open hook fires after the open every spring.
    The UTC assertions are the teeth — a wrong implementation still reports 06:15
    local while the actual instant it scheduled is an hour off.
    """
    expr = "15 6 * * *"

    # The day BEFORE the transition, still on the old offset.
    before = local(orchestration._next_cron_fire(
        expr, "America/New_York",
        dt.datetime.fromisoformat(f"{after_day}T00:01").replace(tzinfo=NY)))
    assert (before.hour, before.minute) == (6, 15)
    assert before.utcoffset() == dt.timedelta(hours=before_offset)

    # The transition day itself, on the new offset.
    across = local(orchestration._next_cron_fire(
        expr, "America/New_York",
        dt.datetime.fromisoformat(f"{after_day}T12:00").replace(tzinfo=NY)))
    assert across.date() == transition
    assert (across.hour, across.minute) == (6, 15)
    assert across.utcoffset() == dt.timedelta(hours=after_offset)

    # ...and the day after, so the new offset is not a one-off.
    nxt = local(orchestration._next_cron_fire(expr, "America/New_York",
                                              across + dt.timedelta(minutes=1)))
    assert nxt.date() == transition + dt.timedelta(days=1)
    assert (nxt.hour, nxt.minute) == (6, 15)
    assert nxt.utcoffset() == dt.timedelta(hours=after_offset)


def test_cron_hook_reschedules_itself_in_the_configured_zone(clock):
    """End-to-end: hook_add and the post-fire re-arm both resolve in local time.

    Guards the seam between the two — hook_add computes the first fire, but
    _eval_wake_hooks computes every one after it, from a different call site.
    """
    h = orchestration.hook_add("gamma", "pre-open prep", cron="15 6 * * 1-5")["hook"]
    first = hook_row(h)["next_fire_at"]
    assert (local(first).hour, local(first).minute) == (6, 15)
    assert local(first).date() == dt.date(2026, 7, 15)  # clock is Tue 12:00 local

    clock.now = dt.datetime.fromisoformat(first)
    assert [f["hook"] for f in tick()["hooks_fired"]] == [h]

    row = hook_row(h)
    assert row["status"] == "active"
    assert (local(row["next_fire_at"]).hour, local(row["next_fire_at"]).minute) == (6, 15)
    assert local(row["next_fire_at"]).date() == dt.date(2026, 7, 16)


def test_cron_that_never_matches_is_rejected_and_does_not_hang(clock):
    """February 30th matches nothing. The scan is bounded, so this returns.

    An unbounded "search until you find one" would spin forever on a spec an
    agent can register at will — the rejection is also a liveness property.
    """
    started = time.monotonic()
    with pytest.raises(ValueError, match="never matches"):
        orchestration.hook_add("alpha", "impossible", cron="15 6 30 2 *")
    assert time.monotonic() - started < 5.0
    assert orchestration.hooks(include_closed=True) == []


# --- kind: probe ------------------------------------------------------------

def test_dry_run_never_runs_a_probe(clock, probes, tmp_path):
    """A preview must not execute host code. The rollback cannot save it here.

    `plan_wakes(record=False)` is inert by ROLLBACK: it does the real work and
    throws the writes away. That trick covers every table the tick touches and
    exactly nothing else — a probe is host code the engine imports and CALLS, and
    its effects land outside the transaction. So probes are gated on `record` at
    the call site, and this is the only test that can tell the two mechanisms
    apart: drop the gate and every dry-run assertion in the suite still passes,
    because the phantom probe's writes get rolled back and the file does not.

    `wake plan --dry-run` is a thing an operator runs to LOOK. If it can fire a
    watcher, previewing the desk pages someone.
    """
    configure(probe_allowlist=(PROBE_MODULE,))
    marker = tmp_path / "probe_ran"
    probe_state().MARKER[:] = [str(marker)]
    orchestration.hook_add("beta", "watcher",
                           callable_path=f"{PROBE_MODULE}:marker_writer", every=60)

    plan = orchestration.plan_wakes(record=False)

    assert not marker.exists()          # the host's code never ran
    assert plan["hooks_fired"] == []
    assert probe_state().MARKER == [str(marker)]

    # ...and the real tick DOES run it, or the assertion above passes against an
    # engine whose probes are simply broken.
    orchestration.plan_wakes(record=True)
    assert marker.exists()


def test_probe_returning_dict_wakes_owner(clock, probes):
    """The probe contract's fire half: a dict becomes an inbox item."""
    configure(probe_allowlist=(PROBE_MODULE,))
    probe_state().SCRIPT[:] = ["fire"]
    h = orchestration.hook_add("alpha", "watcher", callable_path=f"{PROBE_MODULE}:watcher",
                               every=60)["hook"]

    fired = tick()["hooks_fired"]

    assert [f["hook"] for f in fired] == [h]
    inbox = orchestration.inbox_pending("alpha")
    assert [i["title"] for i in inbox] == ["watcher fired"]
    assert inbox[0]["ref"] == f"hook:{h}"
    assert hook_row(h)["status"] == "active"


def test_probe_returning_none_wakes_nobody(clock, probes):
    """"None = don't wake anyone" — a quiet watcher is the normal case.

    A probe evaluates on every tick; if a falsy return enqueued anything, every
    watcher would wake its owner every interval regardless of the condition it
    was written to watch, and the whole abstraction would be a spammer.
    """
    configure(probe_allowlist=(PROBE_MODULE,))
    probe_state().SCRIPT[:] = ["quiet"]
    h = orchestration.hook_add("alpha", "watcher", callable_path=f"{PROBE_MODULE}:watcher",
                               every=60)["hook"]

    assert tick()["hooks_fired"] == []
    assert orchestration.inbox_pending("alpha") == []
    assert probe_state().CALLS == [1]  # it really did run

    row = hook_row(h)
    assert row["status"] == "active"        # staying quiet is not an error
    assert row["fire_count"] == 0
    assert row["next_fire_at"] is not None  # still armed for the next tick


# --- probe safety: the allowlist --------------------------------------------

@pytest.mark.parametrize("module", [
    pytest.param("os", id="stdlib-os"),
    pytest.param("json", id="stdlib-json"),
    pytest.param("deskd.orchestration", id="the-engine-itself"),
    pytest.param(PROBE_MODULE, id="a-real-probe-module"),
])
def test_empty_allowlist_denies_every_probe(clock, probes, module):
    """probe_allowlist=() is deny-all, and it denies BEFORE importing.

    This is the engine's default posture. A hook is agent-supplied input, so if a
    dotted path could reach import machinery the allowlist were not consulted
    first, an agent could execute arbitrary importable code by registering a hook
    — import side effects alone are enough. Nothing may load.
    """
    assert orchestration.CONFIG.probe_allowlist == ()
    sys.modules.pop(module, None)

    with pytest.raises(ValueError, match="probes are disabled"):
        orchestration.hook_add("alpha", "sneaky", callable_path=f"{module}:anything")

    assert module not in sys.modules
    assert orchestration.hooks(include_closed=True) == []


def test_probe_outside_allowlist_is_refused_by_the_allowlist_not_by_import(clock, probes):
    """The refusal must be the policy, not an accident of the module missing.

    The named module is on sys.path and would import cleanly — proven with
    find_spec, which resolves without executing it. So the ValueError can only
    come from the allowlist check, and the module stays unimported.
    """
    configure(probe_allowlist=("some.other.namespace",))
    assert importlib.util.find_spec(PROBE_MODULE) is not None
    assert PROBE_MODULE not in sys.modules

    with pytest.raises(ValueError, match="is not allowed"):
        orchestration.hook_add("alpha", "outsider",
                               callable_path=f"{PROBE_MODULE}:watcher")

    assert PROBE_MODULE not in sys.modules
    assert orchestration.hooks(include_closed=True) == []


def test_allowlist_prefix_stops_at_a_dot(clock, probes):
    """'deskd_test_probes' must not admit 'deskd_test_probes_evil'.

    A bare startswith() would — and the lookalike module here is importable, so
    that bug would be a live arbitrary-import, not a theoretical one. Prefixes
    are namespaces, and namespaces end at a dot.
    """
    configure(probe_allowlist=(PROBE_MODULE,))
    assert importlib.util.find_spec(LOOKALIKE_MODULE) is not None

    with pytest.raises(ValueError, match="is not allowed"):
        orchestration.hook_add("alpha", "lookalike",
                               callable_path=f"{LOOKALIKE_MODULE}:watcher")

    assert LOOKALIKE_MODULE not in sys.modules
    # ...while the genuinely allowlisted module is accepted, so the test is
    # proving a boundary rather than a blanket refusal.
    orchestration.hook_add("alpha", "genuine", callable_path=f"{PROBE_MODULE}:watcher")


# --- probe safety: error handling -------------------------------------------

def test_probe_auto_disables_on_the_third_consecutive_error(clock, probes):
    """max_error_streak is a boundary: 2 errors survive, the 3rd disables.

    Both halves matter. Disabling too early kills a watcher over one blip;
    never disabling lets a permanently broken watcher burn tick time forever
    while its owner believes it is being watched. The inbox notice is the other
    half of the contract — a hook that dies silently is the worst outcome of all,
    because the agent is still relying on it.
    """
    configure(probe_allowlist=(PROBE_MODULE,))
    assert orchestration.CONFIG.max_error_streak == 3
    probe_state().SCRIPT[:] = ["boom", "boom", "boom"]
    h = orchestration.hook_add("gamma", "boom watcher",
                               callable_path=f"{PROBE_MODULE}:watcher", every=60)["hook"]

    for expected_streak in (1, 2):
        tick()
        row = hook_row(h)
        assert row["status"] == "active", f"disabled early at streak {expected_streak}"
        assert row["error_streak"] == expected_streak
        assert "RuntimeError: probe exploded" in row["last_error"]
        assert orchestration.inbox_pending("gamma") == []
        clock.advance(61)

    tick()

    row = hook_row(h)
    assert row["status"] == "error"
    assert row["error_streak"] == 3
    # The owner is told, and told which hook.
    notices = orchestration.inbox_pending("gamma")
    assert len(notices) == 1
    assert f"#{h}" in notices[0]["title"]
    assert "disabled" in notices[0]["title"]
    assert notices[0]["ref"] == f"hook:{h}"
    assert "RuntimeError: probe exploded" in notices[0]["body"]


def test_probe_success_resets_the_error_streak(clock, probes):
    """2 errors, 1 success, 2 errors: still enabled.

    A streak that only ever counts up is a countdown, not a streak — it would
    eventually disable every watcher that ever hiccups, including one that is
    working. Flaky-but-working is the common case for anything observing a real
    system, and it must survive.
    """
    configure(probe_allowlist=(PROBE_MODULE,))
    probe_state().SCRIPT[:] = ["boom", "boom", "fire", "boom", "boom"]
    h = orchestration.hook_add("gamma", "flaky watcher",
                               callable_path=f"{PROBE_MODULE}:watcher", every=60)["hook"]

    for _ in range(2):
        tick()
        clock.advance(61)
    assert hook_row(h)["error_streak"] == 2

    tick()  # the success
    assert hook_row(h)["error_streak"] == 0
    ack_all("gamma")
    clock.advance(61)

    for _ in range(2):
        tick()
        clock.advance(61)

    row = hook_row(h)
    assert row["status"] == "active", "a success must reset the streak, not pause it"
    assert row["error_streak"] == 2
    assert probe_state().SCRIPT == []  # all five calls really happened


def test_a_raising_probe_never_stalls_the_tick(clock, probes):
    """One broken watcher must not stop the orchestrator waking everyone else.

    The probe is registered first, so it is evaluated first: if its exception
    escaped _eval_wake_hooks, plan_wakes would raise and beta's due hook — and
    every other demand in the tick — would never be looked at. The blast radius
    of host code the engine calls is exactly one hook.
    """
    configure(probe_allowlist=(PROBE_MODULE,))
    probe_state().SCRIPT[:] = ["boom"]
    broken = orchestration.hook_add("gamma", "broken watcher",
                                    callable_path=f"{PROBE_MODULE}:watcher",
                                    every=60)["hook"]
    healthy = orchestration.hook_add("beta", "unrelated one shot", at=clock.iso())["hook"]

    plan = tick()  # must not raise

    assert [f["hook"] for f in plan["hooks_fired"]] == [healthy]
    assert orchestration.inbox_pending("beta")[0]["title"] == "unrelated one shot"
    assert hook_row(broken)["error_streak"] == 1
    assert hook_row(healthy)["status"] == "done"


# --- ownership / registry ---------------------------------------------------

@pytest.mark.parametrize("role", [r.name for r in ROLES])
def test_a_hook_can_be_registered_for_any_registered_role(clock, role):
    """No blessed roles. The registry decides who exists, not the engine.

    Parametrized over every configured role because a hardcoded role name is
    invisible to a suite that only ever exercises one.
    """
    h = orchestration.hook_add(role, f"{role} nudge", at=clock.iso())["hook"]

    assert hook_row(h)["owner_role"] == role
    assert [f["role"] for f in tick()["hooks_fired"]] == [role]
    assert [i["target_role"] for i in orchestration.inbox_pending(role)] == [role]


@pytest.mark.parametrize("role", [
    pytest.param("delta", id="never-registered"),
    pytest.param("", id="empty"),
    pytest.param("supervisor", id="supervisor-is-not-an-agent"),
])
def test_hook_for_an_unregistered_role_is_rejected(clock, role):
    """A hook owned by nobody can never be delivered, so it is refused at add.

    'supervisor' is included because it is a human identity outside the agent
    role space: it has no session and no inbox, so a hook aimed at it would fire
    into a void forever.
    """
    with pytest.raises(ValueError):
        orchestration.hook_add(role, "orphan", at=clock.iso())

    assert orchestration.hooks(include_closed=True) == []
