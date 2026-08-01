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

import json
import shutil
import subprocess

import pytest

fastapi = pytest.importorskip("fastapi", reason="web extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

from deskd import ConsoleLink, meetings, orchestration  # noqa: E402
from deskd.config import SUPERVISOR_CODE_HEADER  # noqa: E402
from deskd.web.app import STATIC, create_app  # noqa: E402

from conftest import iso  # noqa: E402

PAGES = ["/", "/board", "/office", "/agent/alpha", "/meetings", "/wake",
         "/escalations", "/tasks"]
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


# --- the office floor: page + nav ---------------------------------------------

def test_office_is_in_the_shared_nav(client):
    """The floor plan is reachable from every other view, not just by URL."""
    js = client.get("/static/shell.js").text
    assert '["office", "/office", "Office floor"]' in js
    # …and the page tells the shell which nav item to light up.
    assert 'S.init("office"' in client.get("/office").text


def test_standalone_console_has_no_host_nav_links(client):
    assert client.get("/api/meeting-meta").json()["console_links"] == []


def test_host_pages_can_extend_the_shared_nav(client, desk):
    """The engine stays generic while a host can expose its adjacent pages."""
    desk.console_links = (
        ConsoleLink("runtime", "/runtime", "Runtime"),
    )
    meta = client.get("/api/meeting-meta").json()
    assert meta["console_links"] == [{
        "page": "runtime",
        "href": "/runtime",
        "label": "Runtime",
    }]
    js = client.get("/static/shell.js").text
    assert "console_links" in js
    assert "NAV.concat(hostNav(m))" in js
    assert 'id="sh-nav"' in js


@pytest.mark.parametrize("kwargs", [
    {"page": "", "href": "/runtime", "label": "Runtime"},
    {"page": "runtime", "href": "javascript:alert(1)", "label": "Runtime"},
    {"page": "runtime", "href": "//elsewhere/runtime", "label": "Runtime"},
    {"page": "runtime", "href": "/\\elsewhere/runtime", "label": "Runtime"},
    {"page": "runtime", "href": "/runtime\nnext", "label": "Runtime"},
    {"page": "runtime", "href": "/runtime", "label": " "},
])
def test_host_nav_rejects_unsafe_or_empty_links(kwargs):
    with pytest.raises(ValueError):
        ConsoleLink(**kwargs)


def test_office_ships_its_model_script(client):
    """The join lives in office.js so it can be unit-tested headlessly; the
    page must actually load it."""
    assert '/static/office.js' in client.get("/office").text
    js = client.get("/static/office.js")
    assert js.status_code == 200
    assert "floorPlan" in js.text


def test_meetings_ships_its_operational_state_model(client):
    body = client.get("/meetings").text
    assert '/static/meetings.js' in body
    assert "Resume & wake agents" in body
    assert "Thread paused after inactivity" in body
    assert "V.effectiveState(m)" in body
    assert "V.canSend(m, pendingTermination)" in body
    assert "This escalation interrupted a pending termination" in body
    assert "if (V.canResume(m))" in body
    assert "m.thread_stop_reason" in body
    assert "!META.simple_auth_enabled" in body
    assert "off-host signed assertion" in body
    js = client.get("/static/meetings.js")
    assert js.status_code == 200
    assert "effectiveState" in js.text


def test_office_page_carries_its_empty_states(client):
    """Zero agents and zero meetings is a fresh install, not a broken floor."""
    body = client.get("/office").text
    assert "No meeting in progress" in body
    assert "No agents registered yet" in body


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


# --- the office floor model (office.js, run headlessly) ----------------------------
#
# `Office.floorPlan(board, meetings, opts)` is the only logic on the floor page:
# it decides who is seated in which room and whose desk is therefore empty. It
# is a pure function of the two projections above, so it is testable without a
# browser — node evaluates the SHIPPED file (no copy, no build step) and hands
# the model back as JSON for the assertions to run in Python. Skipped where node
# is not installed; the page's own tests above still cover serving and nav.

def _floor_plan(tmp_path, board, meeting_list, opts=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed — office.js unit tests need it")
    payload = tmp_path / "floor-input.json"
    payload.write_text(json.dumps({"board": board, "meetings": meeting_list,
                                   "opts": opts or {}}), encoding="utf-8")
    script = tmp_path / "floor-run.js"
    script.write_text(
        "const fs = require('fs'), vm = require('vm');\n"
        "const ctx = {}; vm.createContext(ctx);\n"
        f"vm.runInContext(fs.readFileSync({json.dumps(str(STATIC / 'office.js'))},"
        " 'utf8'), ctx);\n"
        f"const d = JSON.parse(fs.readFileSync({json.dumps(str(payload))}, 'utf8'));\n"
        "process.stdout.write(JSON.stringify("
        "ctx.Office.floorPlan(d.board, d.meetings, d.opts)));\n",
        encoding="utf-8")
    run = subprocess.run([node, str(script)], capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def _meeting_ui_state(meeting, *, has_termination=False):
    """Evaluate the shipped meetings model, not a Python copy of its rules."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed — meetings.js unit tests need it")
    script = (
        "const fs=require('fs'),vm=require('vm'),ctx={};"
        "vm.createContext(ctx);"
        f"vm.runInContext(fs.readFileSync({json.dumps(str(STATIC / 'meetings.js'))},"
        "'utf8'),ctx);"
        "const m=JSON.parse(process.argv[1]),t=process.argv[2]==='true',V=ctx.MeetingView;"
        "process.stdout.write(JSON.stringify({"
        "effective:V.effectiveState(m),mismatch:V.stateMismatch(m),"
        "send:V.canSend(m,t),auto:V.autoResumesOnSend(m),"
        "join:V.resumesOnJoin(m),resume:V.canResume(m),"
        "payable:V.obligationsPayable(m)}));"
    )
    run = subprocess.run(
        [node, "-e", script, json.dumps(meeting), str(has_termination).lower()],
        capture_output=True, text=True, timeout=60,
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_meeting_ui_uses_the_message_threads_operational_state():
    open_state = _meeting_ui_state({
        "state": "active", "thread_status": "open",
        "effective_state": "active", "state_in_sync": True,
    })
    assert open_state == {
        "effective": "active", "mismatch": False, "send": True,
        "auto": False, "join": False, "resume": False, "payable": True,
    }

    idle = _meeting_ui_state({
        "state": "active", "thread_status": "paused",
        "thread_stop_reason": "idle timeout",
        "effective_state": "paused", "state_in_sync": False,
    })
    assert idle == {
        "effective": "paused", "mismatch": True, "send": True,
        "auto": True, "join": False, "resume": True, "payable": False,
    }

    bounded = _meeting_ui_state({
        "state": "consensus", "thread_status": "paused",
        "thread_stop_reason": "message budget exhausted",
        "effective_state": "paused", "state_in_sync": False,
    })
    assert bounded == {
        "effective": "paused", "mismatch": True, "send": False,
        "auto": False, "join": False, "resume": True, "payable": False,
    }

    escalated = _meeting_ui_state({
        "state": "escalated", "thread_status": "escalated",
        "effective_state": "escalated", "state_in_sync": True,
    })
    assert escalated == {
        "effective": "escalated", "mismatch": False, "send": True,
        "auto": False, "join": True, "resume": True, "payable": False,
    }
    escalated_termination = _meeting_ui_state({
        "state": "escalated", "thread_status": "escalated",
        "effective_state": "escalated", "state_in_sync": True,
    }, has_termination=True)
    assert escalated_termination == {
        "effective": "escalated", "mismatch": False, "send": False,
        "auto": False, "join": True, "resume": True, "payable": False,
    }

    termination = {
        "state": "termination_pending", "thread_status": "paused",
        "thread_stop_reason": "idle timeout",
        "effective_state": "paused", "state_in_sync": False,
    }
    assert _meeting_ui_state(termination)["send"] is False


def _plan_from(client, tmp_path, opts=None):
    """The model built from the LIVE payloads, exactly as the page builds it."""
    return _floor_plan(tmp_path,
                       client.get("/api/board").json(),
                       client.get("/api/meetings?include_closed=false").json(),
                       opts)


def test_floor_is_calm_on_an_empty_db(client, tmp_path):
    """Nobody in any meeting is the common case: an untouched desk for every
    registered role, no rooms, and not one alarm."""
    plan = _plan_from(client, tmp_path)
    assert plan["rooms"] == []
    assert [d["role"] for d in plan["desks"]] == ["alpha", "beta", "gamma"]
    assert all(d["atDesk"] and d["liveness"] == "never" and not d["ringing"]
               for d in plan["desks"])
    assert plan["stats"]["atDesk"] == 3
    assert (plan["stats"]["inMeetings"], plan["stats"]["ringing"],
            plan["stats"]["owed"], plan["stats"]["guests"]) == (0, 0, 0, 0)


def test_floor_seats_checked_in_agents_and_empties_their_desks(client, tmp_path):
    """The whole point of the view, against the real payloads: two agents in a
    room, drawn in the room; the third still at its desk."""
    orchestration.set_status("gamma", state="working", activity="reading logs")
    m = meetings.call_meeting(agenda="login outage — response plan",
                              called_by="alpha", attendees=["alpha", "beta"])
    tid = m["meeting"]["thread_id"]
    meetings.check_in(tid, role="alpha")
    meetings.check_in(tid, role="beta")
    meetings.send_update(tid, role="alpha", kind="question",
                         body="do we hedge the ETA?")

    plan = _plan_from(client, tmp_path)
    assert len(plan["rooms"]) == 1
    room = plan["rooms"][0]
    assert {p["role"] for p in room["seated"]} == {"alpha", "beta"}
    assert room["empty"] is False and room["mode"] == "one_to_one"
    # The engine holds beta to a reply; the room shows who owes it.
    assert room["nextOwed"]["role"] == "beta"
    assert any(p["role"] == "beta" and p["owes"] for p in room["seated"])

    desks = {d["role"]: d for d in plan["desks"]}
    assert desks["alpha"]["atDesk"] is False
    assert [a["agenda"] for a in desks["beta"]["away"]] == [room["agenda"]]
    assert desks["gamma"]["atDesk"] is True
    assert desks["gamma"]["activity"] == "reading logs"
    assert plan["stats"] == {**plan["stats"], "atDesk": 1, "inMeetings": 2,
                             "rooms": 1, "roomsEmpty": 0}


def test_floor_separates_who_is_at_the_table_from_who_is_still_invited(client, tmp_path):
    """A `waiting` room: the caller is in it (the engine checks it in), the
    invitee is an empty chair — and only the caller's desk goes dark."""
    meetings.call_meeting(agenda="standby", called_by="alpha",
                          attendees=["alpha", "beta"])
    plan = _plan_from(client, tmp_path)
    room = plan["rooms"][0]
    assert room["state"] == "waiting" and room["mode"] == "waiting"
    assert [p["role"] for p in room["seated"]] == ["alpha"]
    assert [p["role"] for p in room["invited"]] == ["beta"]
    assert room["empty"] is False
    desks = {d["role"]: d for d in plan["desks"]}
    assert desks["alpha"]["atDesk"] is False and desks["beta"]["atDesk"] is True


def test_floor_marks_an_active_lifecycle_with_a_retired_thread_as_paused(tmp_path):
    board = {"generated_at": iso(), "agents": [
        {"role": "alpha", "display_name": "Alpha", "liveness": "idle"},
        {"role": "beta", "display_name": "Beta", "liveness": "idle"},
    ]}
    meeting_list = [{
        "meeting": {
            "thread_id": "m-paused", "agenda": "quiet room",
            "state": "active", "effective_state": "paused",
            "state_in_sync": False, "thread_status": "paused",
            "thread_stop_reason": "idle timeout",
            "meeting_type": "ad-hoc", "called_by": "alpha",
            "priority": "normal", "message_count": 1, "max_messages": 20,
            "messages_remaining": 19, "created_at": iso(-3600),
            "updated_at": iso(-300),
        },
        "attendees": [
            {"role": role, "required": 1, "invited_at": iso(-3600),
             "checked_in_at": iso(-3500), "stopped_at": None}
            for role in ("alpha", "beta")
        ],
        "mode": "one_to_one", "termination": None, "votes": [],
        "response_obligations": [{
            "status": "pending", "owed_by": "beta", "message_id": 1,
            "sender": "alpha", "kind": "question", "due_at": iso(-60),
        }],
    }]

    room = _floor_plan(tmp_path, board, meeting_list)["rooms"][0]

    assert room["state"] == "paused"
    assert room["lifecycleState"] == "active"
    assert room["stateMismatch"] is True
    assert room["threadStopReason"] == "idle timeout"
    assert room["nextOwed"] is None, "a paused thread cannot pay a reply debt"


def test_floor_shows_a_room_nobody_has_walked_into(tmp_path):
    """`waiting` with an empty table — every invitee is still an empty chair,
    so the room must not read as a live meeting."""
    board = {"generated_at": iso(), "agents": [
        {"role": "alpha", "display_name": "Alpha", "liveness": "idle"},
        {"role": "beta", "display_name": "Beta", "liveness": "idle"}]}
    meeting_list = [{
        "meeting": {"thread_id": "m-9", "agenda": "kickoff", "state": "waiting",
                    "meeting_type": "ad-hoc", "called_by": "alpha",
                    "priority": "normal", "message_count": 0, "max_messages": 20,
                    "messages_remaining": 20, "thread_status": "open",
                    "created_at": iso(-20), "updated_at": iso(-20)},
        "attendees": [
            {"role": "alpha", "required": 1, "invited_at": iso(-20),
             "checked_in_at": None, "stopped_at": None},
            {"role": "beta", "required": 1, "invited_at": iso(-20),
             "checked_in_at": None, "stopped_at": None}],
        "mode": "waiting", "termination": None, "votes": [],
        "response_obligations": [],
    }]
    plan = _floor_plan(tmp_path, board, meeting_list)
    room = plan["rooms"][0]
    assert room["empty"] is True and room["seated"] == []
    assert {p["role"] for p in room["invited"]} == {"alpha", "beta"}
    # Nobody is in a meeting: every desk is still occupied.
    assert all(d["atDesk"] for d in plan["desks"])
    assert plan["stats"]["roomsEmpty"] == 1 and plan["stats"]["inMeetings"] == 0


def test_floor_draws_an_agent_in_two_meetings_at_both_tables(client, tmp_path):
    first = meetings.call_meeting(agenda="incident", called_by="alpha",
                                  attendees=["alpha", "beta"])["meeting"]["thread_id"]
    second = meetings.call_meeting(agenda="handoff", called_by="alpha",
                                   attendees=["alpha", "gamma"])["meeting"]["thread_id"]
    for tid, roles in ((first, ("alpha", "beta")), (second, ("alpha", "gamma"))):
        for role in roles:
            meetings.check_in(tid, role=role)
    plan = _plan_from(client, tmp_path)
    alpha = next(d for d in plan["desks"] if d["role"] == "alpha")
    assert sorted(a["agenda"] for a in alpha["away"]) == ["handoff", "incident"]
    assert alpha["atDesk"] is False
    # Seated in both rooms — one person, two chairs, and the desk says so.
    assert all(any(p["role"] == "alpha" for p in r["seated"]) for r in plan["rooms"])


def test_floor_names_who_has_not_voted_when_a_meeting_is_wrapping_up(client, tmp_path):
    m = meetings.call_meeting(agenda="retro", called_by="alpha",
                              attendees=["alpha", "beta", "gamma"])
    tid = m["meeting"]["thread_id"]
    for role in ("alpha", "beta", "gamma"):
        meetings.check_in(tid, role=role)
    meetings.propose_end(tid, role="alpha", resolution="ship it")
    meetings.confirm_end(tid, role="beta")   # gamma's vote is still owed
    room = _plan_from(client, tmp_path)["rooms"][0]
    assert room["wrappingUp"] is True
    assert room["termination"]["proposer"] == "alpha"
    # The proposer's confirm is implicit and beta voted: only gamma is left.
    assert room["termination"]["waitingOn"] == ["gamma"]
    assert any(p["role"] == "gamma" and p["voteOwed"] for p in room["seated"])


def test_floor_seats_the_supervisor_as_a_person_without_a_desk(tmp_path):
    """A meeting whose only attendee is the human. They are drawn at the table
    and marked human — and they get no desk, because they do not work here."""
    board = {"generated_at": iso(), "agents": []}
    meeting_list = [{
        "meeting": {"thread_id": "m-1", "agenda": "1:1", "state": "active",
                    "meeting_type": "ad-hoc", "called_by": "boss",
                    "priority": "normal", "message_count": 1, "max_messages": 8,
                    "messages_remaining": 7, "thread_status": "open",
                    "created_at": iso(-60), "updated_at": iso()},
        "attendees": [{"role": "boss", "required": 0, "invited_at": iso(-60),
                       "checked_in_at": iso(-30), "stopped_at": None}],
        "mode": "waiting", "termination": None, "votes": [],
        "response_obligations": [],
    }]
    plan = _floor_plan(tmp_path, board, meeting_list, {"supervisorRole": "boss"})
    person = plan["rooms"][0]["seated"][0]
    assert person["isSupervisor"] is True and person["hasDesk"] is False
    assert plan["desks"] == [] and plan["stats"]["guests"] == 1


def test_floor_model_survives_a_never_seen_agent_and_a_long_activity(tmp_path):
    """`never` is a real liveness (registered, never heartbeated) and long text
    is data, not an error: the model passes both through for the view to style
    and clamp."""
    activity = "reconciling " + "the overnight ticket backlog " * 20
    board = {"generated_at": iso(), "agents": [
        {"role": "alpha", "display_name": "Alpha", "liveness": "never",
         "state": None, "activity": None, "heartbeat_age_seconds": None},
        {"role": "beta", "display_name": "Beta", "liveness": "online",
         "state": "working", "activity": activity,
         "heartbeat_age_seconds": 3,
         "wake": {"pending": 2, "max_level": 3},
         "inbox": {"queued_count": 4, "urgent_queued": 1},
         "tasks": [{"id": 1}], "overdue_count": 1, "hooks": [{"id": 9}],
         "meeting": {"unread_messages": 2,
                     "response_obligations": {"pending": 1, "next_due_at": iso(30)}}},
    ]}
    ladder = [{"level": i, "channel": c, "leaves_machine": i >= 3, "wired": True}
              for i, c in enumerate(["hook", "resume", "spawn", "human"])]
    plan = _floor_plan(tmp_path, board, [], {"ladder": ladder})
    alpha, beta = plan["desks"]
    assert alpha["liveness"] == "never" and alpha["atDesk"] is True
    assert alpha["ringing"] is None and alpha["trays"]["inbox"] == 0
    assert beta["activity"] == activity           # untruncated in the model
    # A climbing ladder names its rung, and says it has left the machine.
    # `mode` is the honest half: only a wired, non-terminal rung is a phone
    # actually ringing. A terminal rung is a badge parked on the console and an
    # unwired one reaches nobody — drawing all three the same told an operator
    # help was coming when it was not.
    assert beta["ringing"] == {"pending": 2, "level": 3, "channel": "human",
                               "leavesMachine": True, "wired": True,
                               "terminal": False, "mode": "ringing"}
    assert beta["trays"] == {"inbox": 4, "inboxUrgent": 1, "tasks": 1,
                             "overdue": 1, "hooks": 1, "owed": 1,
                             "owedDueAt": beta["trays"]["owedDueAt"],
                             "unread": 2}
    assert plan["stats"]["ringing"] == 1


# --- the trust boundary is unchanged -----------------------------------------------

def test_authenticated_ui_resume_reopens_and_rearms_the_thread(desk, monkeypatch):
    """The button's real HTTP path must do more than repaint the page: it opens
    transport, refreshes the projection, and queues agent wake demand."""
    monkeypatch.setenv("DESKD_SUPERVISOR_AUTH_MODE", "simple")
    monkeypatch.setenv("DESKD_SUPERVISOR_ACCESS_CODE", "test-only-code")
    thread_id = meetings.call_meeting(
        agenda="resume from console", called_by="alpha",
        attendees=["alpha", "beta"], idle_minutes=1,
    )["meeting"]["thread_id"]
    meetings.check_in(thread_id, role="beta")
    with meetings.connect(write=True) as conn:
        conn.execute(
            "UPDATE mailbox_threads SET expires_at=? WHERE id=?",
            (iso(-3600), thread_id),
        )

    web = TestClient(create_app(desk))
    before = web.get(f"/api/meetings/{thread_id}").json()
    assert before["status"]["meeting"]["effective_state"] == "paused"

    def wake_rows():
        with meetings.connect() as conn:
            return [tuple(r) for r in conn.execute(
                """SELECT role,status,created_at,acknowledged_at,generation
                   FROM meeting_wake_requests WHERE thread_id=? ORDER BY role""",
                (thread_id,)).fetchall()]

    before_wakes = wake_rows()
    for headers in ({}, {SUPERVISOR_CODE_HEADER: "wrong-code"}):
        denied = web.post(
            "/api/meetings/supervisor-action", headers=headers,
            json={"payload": {"action": "resume", "meeting_id": thread_id}},
        )
        assert denied.status_code == 401
        assert web.get(f"/api/meetings/{thread_id}").json()[
            "status"]["meeting"]["effective_state"] == "paused"
        assert wake_rows() == before_wakes, "failed auth cannot re-arm wake demand"

    resumed = web.post(
        "/api/meetings/supervisor-action",
        headers={SUPERVISOR_CODE_HEADER: "test-only-code"},
        json={"payload": {"action": "resume", "meeting_id": thread_id}},
    )
    assert resumed.status_code == 200
    body = resumed.json()
    assert body["woken_roles"] == ["alpha", "beta"]
    assert body["meeting"]["meeting"]["thread_status"] == "open"
    assert body["meeting"]["meeting"]["thread_stop_reason"] is None
    assert body["meeting"]["meeting"]["thread_expires_at"] > iso()
    assert body["meeting"]["meeting"]["effective_state"] == "active"
    assert all(
        thread_id in [w["thread_id"] for w in meetings.wake_requests(role)]
        for role in body["woken_roles"]
    )


def test_supervisor_adapter_still_fails_closed(client):
    """New read surface must not have loosened the only write path."""
    r = client.post("/api/meetings/supervisor-action",
                    json={"payload": {"action": "join", "meeting_id": "x"}})
    assert r.status_code in (401, 403)


def test_new_surface_is_get_only(client):
    for url in ["/api/wake", "/api/escalations", "/api/tasks",
                "/api/hooks", "/api/channels", "/api/meeting-days"]:
        assert client.post(url, json={}).status_code == 405, url


def test_an_away_desk_draws_the_desk_not_the_person(client):
    """One person, one place. An agent seated in a meeting room used to keep a
    desk card carrying the same avatar, liveness dot and state line as an
    occupied one, so the floor showed them upstairs at a table AND downstairs
    at their desk — on a map whose only job is where everybody is. The chair is
    hollow now (`ghost`, no liveness dot), the room is the headline, and what
    the engine still knows survives as a footnote: deleting it was the earlier
    mistake, in the other direction."""
    page = client.get("/office").text
    # The view is data-driven, so pin the vocabulary the model and CSS agree on
    # rather than a rendered instance the test would have to fabricate.
    assert "ghost" in page and "vacated" in page
    assert "dk-elsewhere" in page
    assert "Sitting in:" in page

def test_the_console_pages_and_assets_must_be_revalidated_not_reused(client):
    """The nav lives in shell.js, so a browser holding an old copy silently
    hides every view added since — the page is there, the link is not, and
    nothing looks broken. Starlette sends ETag and Last-Modified but no
    Cache-Control, which leaves browsers on heuristic freshness, and an asset
    stable for days can then be served from cache for hours after a release.
    Observed on a live desk the day the office view shipped: the page 200'd and
    the operator could not find it.

    `no-cache` still permits storage; it forbids using the copy without asking.
    Static assets additionally support a cheap 304. Page FileResponses may
    answer that revalidation with 200, but can no longer be reused unseen."""
    for asset in ("/static/shell.js", "/static/deskd.css",
                  "/static/meetings.js", "/static/office.js"):
        r = client.get(asset)
        assert r.status_code == 200, asset
        assert "no-cache" in r.headers.get("cache-control", ""), asset
        assert r.headers.get("etag"), asset + " must still support a cheap 304"
    for page in PAGES:
        r = client.get(page)
        assert "no-cache" in r.headers.get("cache-control", ""), page

    # And the revalidation is genuinely cheap: a conditional request is a 304
    # with no body, not a re-download.
    first = client.get("/static/shell.js")
    again = client.get("/static/shell.js",
                       headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert not again.content
