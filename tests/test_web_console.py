"""Console tests: every page serves, every projection holds its shape.

Two rules shape this file:

1. **Everything runs against an EMPTY database first.** The empty states are
   the first thing a new user sees, and the console's history includes exactly
   this regression (a hardcoded broadcast token silently emptying every read
   pill): a view that only ever rendered populated data is a view nobody has
   watched fail. Populated cases then layer on top through the public engine
   API, never by hand-crafting response dicts.

2. **The web layer is projection-only, so the tests pin SHAPES, not values.**
   A projection endpoint's contract is "the engine fn's answer, over HTTP" —
   asserting deep business behavior here would duplicate the engine suites.
   What belongs here: status codes, key sets, and the couple of places where
   app.py itself makes a decision (400 for a bad day, 404 for a bad role,
   401/403 on the supervisor adapter).
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="web extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

from deskd import meetings, orchestration  # noqa: E402
from deskd.web.app import create_app  # noqa: E402

from conftest import iso  # noqa: E402

PAGES = ["/", "/board", "/agent/alpha", "/meetings", "/wake", "/escalations", "/tasks"]
GET_APIS = [
    "/api/board",
    "/api/agent/alpha",
    "/api/agent/alpha/wake-sources",
    "/api/delivery",
    "/api/meetings",
    "/api/meetings?include_closed=true",
    "/api/meeting-days",
    "/api/meeting-meta",
    "/api/wake",
    "/api/escalations",
    "/api/tasks",
    "/api/hooks",
    "/api/channels",
]


@pytest.fixture
def client(desk):
    """A console over the desk fixture's fresh, EMPTY engine."""
    return TestClient(create_app())


# --- pages -------------------------------------------------------------------

def test_every_page_serves_html(client):
    for page in PAGES:
        r = client.get(page)
        assert r.status_code == 200, page
        assert "text/html" in r.headers["content-type"], page


def test_every_page_uses_the_shared_shell(client):
    """One design system: each page loads deskd.css + shell.js and carries no
    pasted per-page stylesheet (the drift between three inline copies is what
    the shell exists to end)."""
    for page in PAGES:
        body = client.get(page).text
        assert "/static/deskd.css" in body, page
        assert "/static/shell.js" in body, page
        assert "<style>" not in body, f"{page} regrew an inline stylesheet"
        assert "<title>" in body, page


def test_shell_assets_serve_and_carry_both_themes(client):
    css = client.get("/static/deskd.css")
    assert css.status_code == 200
    # Light AND dark, with the explicit toggle able to win over the media query.
    assert "prefers-color-scheme: dark" in css.text
    assert 'data-theme="dark"' in css.text
    assert 'data-theme="light"' in css.text
    js = client.get("/static/shell.js")
    assert js.status_code == 200
    assert "localStorage" in js.text


def test_access_code_never_prefilled_from_a_literal(client):
    """The meetings page once shipped the live access code as a client-side
    default — a literal there IS the credential, published to every reader of
    the page source. The only permitted write to the code input reads the
    browser's own sessionStorage."""
    body = client.get("/meetings").text
    assert 'sessionStorage.getItem("deskdSupervisorCode")' in body
    for line in body.splitlines():
        if "access-code" in line and ".value =" in line:
            assert "sessionStorage" in line, f"literal code prefill: {line.strip()}"
        if 'id="access-code"' in line:
            assert "value=" not in line, "the input must ship empty"


# --- every GET api answers on an empty DB -------------------------------------

def test_every_api_200s_on_empty_db(client):
    for url in GET_APIS:
        r = client.get(url)
        assert r.status_code == 200, f"{url} -> {r.status_code}"


# --- new projections: shapes ----------------------------------------------------

def test_wake_projection_mirrors_the_configured_ladder(client, desk):
    d = client.get("/api/wake").json()
    assert set(d) == {"ladder", "attempts"}
    assert d["attempts"] == []
    ladder = d["ladder"]
    assert [r["level"] for r in ladder] == list(range(len(desk.wake_ladder)))
    for rung, spec in zip(ladder, desk.wake_ladder):
        assert rung["channel"] == spec.channel
        assert rung["sla_seconds"] == spec.sla_seconds
        assert rung["leaves_machine"] == spec.leaves_machine
        assert rung["terminal"] == (spec.sla_seconds is None)
    # No channels are registered in tests: machine rungs are always wired (the
    # driver runs them); rungs that pull a human in are not.
    for rung in ladder:
        assert rung["wired"] == (not rung["leaves_machine"])


def test_escalations_projection_shape_empty(client):
    d = client.get("/api/escalations").json()
    assert set(d) == {"wake", "meetings", "unroutable", "channels", "human_reachable"}
    assert d["wake"] == [] and d["meetings"] == [] and d["unroutable"] == []
    assert d["human_reachable"] is False
    # The outbox is always listed: with nothing registered it IS the delivery.
    assert any(c["outbox"] for c in d["channels"])


def test_escalations_projection_surfaces_queued_rows(client):
    # A queued row is the state the view exists to show: a 'pull a human in'
    # that pulled in nobody. Written through the engine's own schema.
    with orchestration.connect(write=True) as conn:
        conn.execute(
            "INSERT INTO wake_escalations (role, reason_kind, source_ref, level,"
            " channel, reason, created_at) VALUES ('alpha','meeting_wake','m-1',3,"
            " 'auto','attendance overdue', ?)", (iso(),))
    d = client.get("/api/escalations").json()
    assert [w["status"] for w in d["wake"]] == ["queued"]
    assert d["wake"][0]["role"] == "alpha"


def test_unroutable_demand_reaches_the_ledger_view(client):
    res = orchestration.inbox_route("no_such_capability", "system", "orphan work")
    assert res.get("unroutable") is True
    d = client.get("/api/escalations").json()
    assert [u["require_capability"] for u in d["unroutable"]] == ["no_such_capability"]
    assert d["unroutable"][0]["routed_at"] is None


def test_tasks_projection_filters_and_stall_shape(client):
    d = client.get("/api/tasks").json()
    assert d == {"tasks": [], "stalled_ids": []}
    tid = orchestration.task_add("write report", assignee_role="alpha")
    done = orchestration.task_add("old chore", assignee_role="beta")
    orchestration.task_update(done, status="done", actor="beta")
    open_only = client.get("/api/tasks").json()
    assert [t["id"] for t in open_only["tasks"]] == [tid]
    everything = client.get("/api/tasks?include_closed=true").json()
    assert {t["id"] for t in everything["tasks"]} == {tid, done}
    filtered = client.get("/api/tasks?role=gamma").json()
    assert filtered["tasks"] == []


def test_hooks_projection_lists_specs_and_filters_by_role(client):
    assert client.get("/api/hooks").json() == []
    orchestration.hook_add("alpha", "nightly sweep", every=3600)
    rows = client.get("/api/hooks").json()
    assert len(rows) == 1
    assert rows[0]["owner_role"] == "alpha"
    assert rows[0]["spec"]["every"] == 3600  # spec arrives decoded, not a JSON string
    assert client.get("/api/hooks?role=beta").json() == []


def test_wake_sources_endpoint_is_the_engine_answer(client):
    d = client.get("/api/agent/alpha/wake-sources").json()
    assert d["role"] == "alpha"
    for key in ("self_hooks", "meeting_wakes", "urgent_tasks",
                "actionable_tasks", "stalled_tasks", "pending_wake_attempts"):
        assert d[key] == [], key
    assert client.get("/api/agent/nope/wake-sources").status_code == 404


def test_channels_projection(client):
    d = client.get("/api/channels").json()
    assert set(d) == {"channels", "human_reachable"}
    assert d["human_reachable"] is False


# --- meeting history navigation ---------------------------------------------------

def test_meeting_days_empty_and_day_validation(client):
    assert client.get("/api/meeting-days").json() == []
    assert client.get("/api/meetings?include_closed=true&day=2026-07-25").json() == []
    # A malformed day is app.py's own decision: client error, not a 500.
    assert client.get("/api/meetings?include_closed=true&day=july").status_code == 400


def test_closed_meeting_appears_under_its_day(client):
    m = meetings.call_meeting(agenda="retro", called_by="alpha",
                              attendees=["alpha", "beta"])
    tid = m["meeting"]["thread_id"]
    meetings.check_in(tid, role="alpha")
    meetings.check_in(tid, role="beta")
    meetings.send_update(tid, role="alpha", body="done here")
    meetings.propose_end(tid, role="alpha", resolution="wrap")
    meetings.confirm_end(tid, role="beta")
    days = client.get("/api/meeting-days").json()
    assert len(days) == 1
    listed = client.get(
        f"/api/meetings?include_closed=true&day={days[0]}").json()
    assert [s["meeting"]["thread_id"] for s in listed] == [tid]


def test_transcript_carries_termination_votes(client):
    """The meetings view renders status.votes — pin that the payload has them."""
    m = meetings.call_meeting(agenda="vote check", called_by="alpha",
                              attendees=["alpha", "beta", "gamma"])
    tid = m["meeting"]["thread_id"]
    for role in ("alpha", "beta", "gamma"):
        meetings.check_in(tid, role=role)
    meetings.propose_end(tid, role="alpha", resolution="finish")
    meetings.confirm_end(tid, role="beta")   # gamma's vote still owed
    s = client.get(f"/api/meetings/{tid}").json()["status"]
    assert s["meeting"]["state"] == "termination_pending"
    votes = {(v["role"], v["vote"]) for v in s["votes"]}
    assert ("beta", "confirm") in votes
    assert not any(role == "gamma" for role, _ in votes)


# --- the trust boundary is unchanged -----------------------------------------------

def test_supervisor_adapter_still_fails_closed(client):
    """New read surface must not have loosened the only write path."""
    r = client.post("/api/meetings/supervisor-action",
                    json={"payload": {"action": "join", "meeting_id": "x"}})
    assert r.status_code in (401, 403)


def test_new_surface_is_get_only(client):
    for url in ["/api/wake", "/api/escalations", "/api/tasks",
                "/api/hooks", "/api/channels", "/api/meeting-days"]:
        assert client.post(url, json={}).status_code == 405, url
