"""The host-injection seams.

deskd was extracted from a host that had hardcoded its own two roles and its own
timezone into the engine. Everything here is a guard against that regressing:
each test asserts that a domain decision the engine used to make for itself is
now genuinely the host's to make. If one of these fails, a domain assumption has
leaked back into the engine — the failure is the point, not a nuisance.

The roles below are deliberately arbitrary (`r0`, `zzz_weird-name`). A test that
needs a plausible-looking role name to pass is testing the wrong thing.
"""

from __future__ import annotations

import datetime as dt

import pytest

from deskd import config as cfg_mod
from deskd import orchestration
from deskd.config import CONFIG, DEFAULT_WAKE_LADDER, RoleSpec, WakeRung

from conftest import ROLES, iso


# --- N arbitrary roles ------------------------------------------------------

def test_conftest_roles_are_all_seeded(desk, conn):
    """The suite's own three nonsense roles must each be first-class.

    The extraction bug was a third role silently getting no registry row — and
    therefore no obligations, no delivery projection, and no wakes — while the
    two blessed names worked fine.
    """
    assert orchestration._known_roles(conn) == {r.name for r in ROLES}
    seeded = {r["role"]: r["display_name"] for r in conn.execute(
        "SELECT role, display_name FROM agent_registry")}
    assert seeded == {r.name: r.display_name for r in ROLES}


@pytest.mark.parametrize("count", [1, 3, 5])
def test_no_role_count_is_special(desk, tmp_path, count):
    """One role, three, five — the engine must not care.

    Two is the count that a two-role hardcoding passes at, so it is the one
    count this test must not be alone in covering.
    """
    roles = tuple(RoleSpec(f"r{i}", f"Role {i}") for i in range(count))
    cfg_mod.configure(roles=roles, db_path=tmp_path / f"n{count}.db")

    with orchestration.connect() as conn:
        assert orchestration._known_roles(conn) == {r.name for r in roles}
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_registry").fetchone()[0] == count
    assert {p["role"] for p in orchestration.presence()} == {r.name for r in roles}


def test_arbitrary_role_name_gets_full_treatment(desk, tmp_path):
    """A role name the engine has never heard of must reach every surface.

    Not a naming nit: a role that can be enqueued to but never appears on the
    board is exactly the silent-hole failure the extraction produced.
    """
    weird = "zzz_weird-name"
    cfg_mod.configure(
        roles=(RoleSpec(weird, "Weird", capabilities=("odd",),
                        authority={"limit": 1}),
               RoleSpec("r1", "Role 1")),
        db_path=tmp_path / "weird.db",
    )

    assert orchestration.inbox_enqueue(weird, "alert", "hello") is not None
    task_id = orchestration.task_add("do a thing", assignee_role=weird)

    board = orchestration.board()
    entry = next(a for a in board["agents"] if a["role"] == weird)
    assert entry["inbox"]["queued_count"] == 1
    assert [t["id"] for t in entry["tasks"]] == [task_id]

    detail = orchestration.agent_detail(weird)
    assert detail["role"] == weird
    # The opaque host-supplied fields survive the round trip untouched.
    assert detail["profile"] == {"display_name": "Weird",
                                 "capabilities": ["odd"],
                                 "authority": {"limit": 1}}


# --- configure() ------------------------------------------------------------

def test_configure_rejects_an_unknown_field(desk):
    """The typo guard. A silently-ignored `timezones=` would leave the host
    believing it had configured a zone it had not."""
    with pytest.raises(ValueError, match="unknown config field: timezones"):
        cfg_mod.configure(timezones="America/New_York")

    assert not hasattr(CONFIG, "timezones")


def test_configure_reaches_already_imported_modules(desk, tmp_path):
    """Engine modules must read CONFIG at call time, never freeze it at import.

    `orchestration` was imported at the top of this file, long before this
    configure() call. A module that captured `CONFIG.inbox_sources` into a
    module-level constant would still work in a host that configures before its
    first import — and break in one that reconfigures later.
    """
    cfg_mod.configure(
        roles=(RoleSpec("r1"), RoleSpec("r2")),
        db_path=tmp_path / "late.db",
        inbox_sources=(*CONFIG.inbox_sources, "host_invented_kind"),
    )

    assert orchestration.inbox_enqueue("r1", "host_invented_kind", "x") is not None


def test_host_can_extend_task_sources(desk, tmp_path):
    """The inbox seam's twin, on task provenance.

    Tasks arrive from the host's own systems, so their provenance kinds are the
    host's vocabulary — which is exactly why `agent_tasks.source_kind` carries no
    DDL CHECK. But dropping the CHECK only moves the decision into Python; the
    seam is real only if the validator reads CONFIG. The closed set survived here
    as a module constant long after the inbox's was opened, because a suite can
    only cover the seam that exists — it cannot notice the one that is missing.
    """
    assert "pagerduty" not in CONFIG.task_sources
    cfg_mod.configure(
        roles=(RoleSpec("r1"),),
        db_path=tmp_path / "tasks.db",
        task_sources=(*CONFIG.task_sources, "pagerduty"),
    )

    # The asymmetry that gave the bug away: same host, same process, same kind.
    assert orchestration.inbox_enqueue(
        "r1", "alert", "page fired") is not None
    task_id = orchestration.task_add("ack the page", assignee_role="r1",
                                     source_kind="pagerduty")

    assert orchestration.tasks(assignee_role="r1")[0]["id"] == task_id
    assert orchestration.tasks(assignee_role="r1")[0]["source_kind"] == "pagerduty"


def test_unknown_task_source_is_still_rejected(desk):
    """Opening the seam must not turn the column into free text: a kind the host
    did NOT declare is still refused, exactly like an inbox source."""
    with pytest.raises(ValueError, match="source_kind"):
        orchestration.task_add("x", assignee_role="alpha", source_kind="pagerduty")


def test_engine_intrinsic_task_sources_survive_a_host_override(desk, tmp_path):
    """`self` is how an agent files its own work, and the supervisor role is
    configurable — so neither may depend on the host remembering to list it."""
    cfg_mod.configure(roles=(RoleSpec("r1"),), db_path=tmp_path / "sup_task.db",
                      task_sources=("self",), supervisor_role="overlord")

    assert orchestration.task_add("mine", assignee_role="r1",
                                  source_kind="self") is not None
    # The supervisor is not an agent, but it IS a legitimate task ORIGIN.
    assert orchestration.task_add("from the boss", assignee_role="r1",
                                  source_kind="overlord") is not None
    with pytest.raises(ValueError, match="source_kind"):
        orchestration.task_add("nope", assignee_role="r1", source_kind="meeting")


# --- the wake ladder --------------------------------------------------------

def test_human_level_follows_a_renamed_ladder(desk, tmp_path):
    """`wake_ladder` is advertised as configurable, so a rung's NAME cannot be
    what tells the engine a human is being pulled in.

    The engine used to match `channel in ("human", "supervisor_badge")` — the
    default ladder's names — and fall through to a positional guess for any other
    ladder. This host pages a person across THREE rungs, so the guess (len-2) and
    the truth (the first rung that leaves the machine) disagree, which is the only
    shape that can catch the bug: a ladder whose human rung happens to sit at
    len-2 passes either way.
    """
    ladder = (
        WakeRung("nudge", 60),
        WakeRung("respawn", 120),
        WakeRung("cold_start", 180),
        WakeRung("page_oncall", 300, leaves_machine=True),      # index 3
        WakeRung("sms_the_lead", 600, leaves_machine=True),
        WakeRung("wall_of_shame", None, leaves_machine=True),
    )
    cfg_mod.configure(roles=(RoleSpec("r1"),), db_path=tmp_path / "ladder.db",
                      wake_ladder=ladder)

    assert orchestration._human_level(ladder) == 3        # declared, not guessed

    # ...and the health counter the level exists to feed agrees, end to end.
    orchestration.task_add("urgent", assignee_role="r1", priority="urgent")
    orchestration.plan_wakes()
    assert orchestration.board()["health"]["wakes_at_human_level"] == 0

    with orchestration.connect(write=True) as c:      # blow the rung SLA
        c.execute("UPDATE wake_attempts SET attempted_at=? WHERE outcome='pending'",
                  (iso(-10_000),))
    plan = orchestration.plan_wakes()

    assert plan["changed"][0]["level"] == 3
    assert ladder[plan["changed"][0]["level"]].channel == "page_oncall"
    assert orchestration.board()["health"]["wakes_at_human_level"] == 1


def test_a_ladder_that_declares_no_human_rung_keeps_the_positional_default(desk):
    """Backwards compatibility: `leaves_machine` defaults to False, so a host's
    existing ladder — built before the field existed — must behave exactly as it
    did rather than reporting no human rung at all."""
    ladder = (WakeRung("a", 60), WakeRung("b", 120), WakeRung("c", None))

    assert orchestration._human_level(ladder) == 1


def test_default_ladder_human_level_did_not_move(desk):
    """The DEFAULT ladder's human level must survive the change of rule.

    `_human_level` used to match the default ladder's channel NAMES; it now reads
    the rung's `leaves_machine` declaration. Both rules answer 3 here, and that
    equality is the entire reason the new rule was safe to adopt — but nothing
    pinned it. Every other ladder test builds its OWN ladder, so the default —
    the one every host that never calls `configure(wake_ladder=...)` runs on —
    was the single ladder covered by nothing.

    The regression this catches is silent. Drop `leaves_machine=True` from the
    `human` rung and the level slides 3 -> 4: the positional fallback no longer
    applies, the next declared rung (`supervisor_badge`) answers instead, and
    `wakes_at_human_level` quietly stops counting the rung that actually pages a
    person. No exception, a green suite, and a health counter that under-reports
    exactly the thing it exists to surface.
    """
    level = orchestration._human_level(DEFAULT_WAKE_LADDER)

    assert level == 3
    assert DEFAULT_WAKE_LADDER[level].channel == "human"
    # The pre-fix rule restated. It must keep agreeing: that is the fix's premise.
    assert level == next(i for i, r in enumerate(DEFAULT_WAKE_LADDER)
                         if r.channel in ("human", "supervisor_badge"))
    # Nothing below the human rung may claim to leave the machine, or the counter
    # would fire on wakes that never reached a person.
    assert not any(r.leaves_machine for r in DEFAULT_WAKE_LADDER[:level])


def test_supervisor_role_change_reaches_already_imported_modules(desk, tmp_path):
    """Same seam, on the identity that matters: whatever the host names its
    supervisor is what agent-facing APIs must refuse."""
    cfg_mod.configure(roles=(RoleSpec("r1"),), db_path=tmp_path / "sup.db",
                      supervisor_role="overlord")

    with pytest.raises(ValueError, match="not an agent role"):
        orchestration.inbox_enqueue("overlord", "alert", "x")


# --- timezone ---------------------------------------------------------------

def test_non_utc_timezone_resolves(desk):
    """The day boundary is the host's, not UTC's. The engine used to inherit
    its old host's zone."""
    assert CONFIG.timezone == "America/New_York"
    assert str(CONFIG.tzinfo()) == "America/New_York"


def test_invalid_timezone_falls_back_to_utc(desk):
    """A typo'd zone must not take the desk down mid-tick: the rollover boundary
    moves, but wakes keep being delivered."""
    cfg_mod.configure(timezone="Not/AZone")

    assert CONFIG.tzinfo() == dt.timezone.utc


def test_session_day_boundary_honours_configured_timezone(desk):
    """The timezone seam is only real if the engine actually *uses* the zone.

    `CONFIG.tzinfo()` resolving is not the invariant — the old host hardcoded its
    timezone into the engine, and a seam test that stops at the config object
    would have watched that happen. `_session_day` is where the injected zone has
    to land, so this is the assertion that has teeth.
    """
    # 23:30 on the 13th in New York. Only a UTC-hardcoded boundary calls this the 14th.
    instant = dt.datetime(2026, 7, 14, 3, 30, tzinfo=dt.timezone.utc)

    assert orchestration._session_day(instant) == "2026-07-13"


# --- probes -----------------------------------------------------------------

def test_empty_probe_allowlist_denies_everything(desk):
    """Deny-all is the default the host opts out of, never into.

    Only the allowlist predicate is asserted here — probe execution is covered
    elsewhere; this is the config semantics that gate it.
    """
    assert CONFIG.probe_allowlist == ()
    assert orchestration._probe_path_ok("anything:fn") is False
    assert orchestration._probe_path_ok("deskd.orchestration:board") is False


# --- the supervisor is not a role -------------------------------------------

def test_supervisor_is_not_an_agent_role(desk, conn):
    """The supervisor is a human identity. It has no session, no heartbeat and
    no inbox, so a registry row for it would be a row nothing can ever fill —
    and a role name agents could then legitimately address."""
    assert CONFIG.supervisor_role not in CONFIG.role_names()
    assert CONFIG.supervisor_role not in orchestration._known_roles(conn)
    assert conn.execute("SELECT COUNT(*) FROM agent_registry WHERE role=?",
                        (CONFIG.supervisor_role,)).fetchone()[0] == 0
