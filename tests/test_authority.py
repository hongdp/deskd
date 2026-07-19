"""Authority as a first-class dimension (roadmap P1).

Two seams under test.

1. **Declaration → enforcement.** A role's registry declaration
   (capabilities + authority) rides in every wake-plan action and rollover,
   so the driver can grant each session exactly the declared tools instead of
   handing every role one global grant. The engine never interprets the
   declaration — it surfaces it; the driver is the enforcement point.

2. **Capability-addressed routing.** `inbox_route` targets an authority, not
   a role. A demand that NO enabled role may take is UNROUTABLE — the wake
   ladder's overdue state on the authority axis: recorded durably, counted
   red on the board, and re-routed by the next planning tick in which a
   qualifying role exists.

Roles here are nonsense on purpose (see conftest): a test that needs a real
role name to pass would mean the engine grew a hardcoded role.
"""

from __future__ import annotations

import pytest

from conftest import scalar
from deskd import config as cfg_mod
from deskd import orchestration as o
from deskd.config import RoleSpec

#: alpha/beta overlap on one capability (routing must choose), beta alone
#: holds another (routing must find it), gamma holds none (routing must not
#: invent one). Only alpha declares a tool grant.
CAP_ROLES = (
    RoleSpec("alpha", "Alpha", ("triage",), {"allowed_tools": ["Read", "Grep"]}),
    RoleSpec("beta", "Beta", ("triage", "deploy"), {}),
    RoleSpec("gamma", "Gamma", (), {"can_push": False}),
)


@pytest.fixture
def cap_desk(desk):
    """The desk fixture's config, with capability-bearing roles — set BEFORE
    the first connection, which is what seeds the registry."""
    cfg_mod.configure(roles=CAP_ROLES)
    return desk


# --- declaration → enforcement seam ------------------------------------------

def test_wake_plan_action_carries_the_declaration(cap_desk):
    """The driver can only enforce what the plan tells it. An action without
    the role's declaration forces the driver back to one global tool grant —
    which is the exact defect this seam exists to close."""
    o.inbox_enqueue("alpha", "alert", "act now", priority="urgent")
    plan = o.plan_wakes(record=True)
    action = next(a for a in plan["actions"] if a["role"] == "alpha")
    assert action["capabilities"] == ["triage"]
    assert action["authority"] == {"allowed_tools": ["Read", "Grep"]}


def test_rollover_carries_the_declaration(cap_desk):
    """A rollover resumes a session too, so it is the same enforcement point:
    a drain that fell back to the global grant would hand a restricted role a
    shell precisely once a day."""
    o.heartbeat("alpha", state="working")
    with o.connect(write=True) as conn:
        conn.execute(
            "UPDATE agent_sessions SET session_day='2000-01-01' WHERE role='alpha'")
    plan = o.rollover_plan(record=False)
    r = next(x for x in plan["rollovers"] if x["role"] == "alpha")
    assert r["capabilities"] == ["triage"]
    assert r["authority"] == {"allowed_tools": ["Read", "Grep"]}


# --- capability-addressed routing --------------------------------------------

def test_route_reaches_the_declaring_role(cap_desk):
    res = o.inbox_route("deploy", "alert", "ship it")
    assert res["routed_to"] == "beta"
    assert [r["title"] for r in o.inbox_pending("beta")] == ["ship it"]


def test_route_balances_by_open_inbox_load(cap_desk):
    """Among qualifying roles: fewest un-acked items, name as tie-break.
    Deterministic and presence-independent — liveness is the wake ladder's
    axis, not the router's."""
    first = o.inbox_route("triage", "alert", "one")
    assert first["routed_to"] == "alpha", "tie must break alphabetically"
    second = o.inbox_route("triage", "alert", "two")
    assert second["routed_to"] == "beta", "alpha now carries one open item"


def test_a_disabled_role_cannot_take_a_demand(cap_desk):
    """enabled=0 is a runtime fact in the registry, and the registry — not
    CONFIG.roles — is what routing must read: _seed_registry never clobbers a
    live row, so config would happily claim the role still qualifies."""
    with o.connect(write=True) as conn:
        conn.execute("UPDATE agent_registry SET enabled=0 WHERE role='beta'")
    res = o.inbox_route("deploy", "alert", "ship it")
    assert res.get("unroutable") is True
    assert o.inbox_pending("beta") == []


def test_unroutable_is_recorded_surfaced_and_rerouted(cap_desk):
    """The whole guarantee end to end: nobody may take it → it does not
    vanish, it turns red; somebody may take it → it moves into a real inbox
    on the next tick and the gauge returns to zero."""
    res = o.inbox_route("audit", "alert", "quarterly audit", priority="urgent")
    assert res["unroutable"] is True
    assert o.board()["health"]["unroutable_demands"] == 1

    # A dry tick decides but records nothing — the demand must still be there.
    o.plan_wakes(record=False)
    assert o.board()["health"]["unroutable_demands"] == 1

    # Grant the capability to a LIVE registry row; the next real tick routes.
    with o.connect(write=True) as conn:
        conn.execute(
            """UPDATE agent_registry SET capabilities='["audit"]'
               WHERE role='gamma'""")
    plan = o.plan_wakes(record=True)
    assert [(r["id"], r["routed_to"]) for r in plan["routed"]] \
        == [(res["id"], "gamma")]
    assert [r["title"] for r in o.inbox_pending("gamma")] == ["quarterly audit"]
    assert o.board()["health"]["unroutable_demands"] == 0
    # Urgent item routed this tick wakes its new owner this tick.
    assert any(a["role"] == "gamma" for a in plan["actions"])


def test_unroutable_dedup_does_not_pile_up(cap_desk):
    """A re-firing watcher must not fill the unroutable ledger with copies any
    more than it may fill an inbox — same contract, keyed on the capability."""
    first = o.inbox_route("audit", "alert", "audit due", dedup_key="audit:q3")
    assert first["unroutable"] is True
    again = o.inbox_route("audit", "alert", "audit due (refire)",
                          dedup_key="audit:q3")
    assert again == {"deduped": True, "unroutable": True}
    with o.connect() as conn:
        assert scalar(conn, "SELECT COUNT(*) FROM unroutable_demands") == 1


def test_unroutable_path_validates_like_the_inbox(cap_desk):
    """The unroutable branch must not be a hole through which an invalid row
    enters the system: same source_kind validation as the inbox insert."""
    with pytest.raises(ValueError, match="source_kind"):
        o.inbox_route("audit", "not_a_real_kind", "should not land")
    with o.connect() as conn:
        assert scalar(conn, "SELECT COUNT(*) FROM unroutable_demands") == 0
