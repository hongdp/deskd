"""Live tool trace: written cheaply mid-turn, shown only while fresh.

A dashboard line reading "doing X" that is ten minutes old is a lie with a
timestamp; the engine hides stale traces rather than trusting every
consumer to check the age."""

import datetime as dt

from deskd import orchestration as orch
from deskd.orchestration.presence import TOOL_TRACE_FRESH_SECONDS
from deskd.orchestration.store import _iso, connect


def test_trace_appears_fresh_and_hides_stale(desk):
    orch.set_status("alpha", state="working", session_id="s-1",
                    harness="wake-alpha")
    orch.tool_trace("alpha", "Bash: pytest tests/ -q")

    row = [r for r in orch.presence() if r["role"] == "alpha"][0]
    assert row["last_tool"] == "Bash: pytest tests/ -q"
    assert row["last_tool_age_seconds"] < TOOL_TRACE_FRESH_SECONDS

    stale = _iso(dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(seconds=TOOL_TRACE_FRESH_SECONDS + 5))
    with connect(write=True) as conn:
        conn.execute("UPDATE agent_sessions SET last_tool_at=? WHERE role='alpha'",
                     (stale,))
    row = [r for r in orch.presence() if r["role"] == "alpha"][0]
    assert row["last_tool"] is None, "stale trace is hidden, not shown old"


def test_trace_never_creates_a_session(desk):
    orch.tool_trace("beta", "Read: somefile.py")     # no live session row
    rows = [r for r in orch.presence() if r["role"] == "beta"]
    assert not rows or rows[0].get("session_id") is None
