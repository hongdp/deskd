"""The close-the-meeting projection must only ever retire its own rows.

`source_kind='meeting'` says "a meeting is where this work came from" — which
is exactly what `--ref` is documented to accept. It does not say "this task IS
closing that meeting". The retire pass conflated the two and marked an agent's
own meeting-sourced work `done` the moment the meeting closed.

Measured on the live desk before the fix, four times, each within a minute of
the meeting closing — a research task, a feature request, and an order-execution
task among them. The failure mode is the worst available here: a queue is the
only thing that wakes anyone on this desk, so a destroyed task does not go
stalled or overdue. It stops existing, and the note it leaves behind reads like
an ordinary completion.
"""

from __future__ import annotations

from deskd import meetings
from deskd import orchestration as orch
from deskd.orchestration import connect
from deskd.orchestration.tasks import _CLOSE_TASK_AUTHOR, sync_meeting_close_tasks


def _live_meeting(agenda="weekly", attendees=("alpha", "beta")):
    status = meetings.call_meeting(agenda=agenda, called_by=attendees[0],
                                   attendees=list(attendees))
    thread_id = status["meeting"]["thread_id"]
    for role in attendees:
        try:
            meetings.check_in(thread_id, role=role)
        except Exception:
            pass
    return thread_id


def _sync():
    with connect(write=True) as conn:
        return sync_meeting_close_tasks(conn)


def _close(thread_id, attendees=("alpha", "beta")):
    meetings.propose_end(thread_id, role=attendees[0], resolution="done")
    for role in attendees[1:]:
        meetings.confirm_end(thread_id, role=role)


def _task(tid):
    return [t for t in orch.tasks(include_closed=True) if t["id"] == tid][0]


class TestOwnRowsOnly:

    def test_an_agents_meeting_sourced_task_survives_the_meeting_closing(self, desk):
        """The reported defect. A research task that CITES a meeting is not a
        task to close that meeting."""
        thread_id = _live_meeting()
        tid = orch.task_add("measure flip_* co-occurrence with same-day moves",
                            assignee_role="alpha", source_kind="meeting",
                            source_ref=thread_id, created_by="alpha")
        _sync()
        _close(thread_id)
        _sync()

        row = _task(tid)
        assert row["status"] == "pending", (
            "an agent's own work was retired because the meeting it cited closed")
        assert not row["result_note"]

    def test_a_ref_with_a_message_suffix_is_also_left_alone(self, desk):
        """One of the four was `<thread>:473` — a meeting id plus the message
        that prompted the task.

        `task_add` now reduces that spelling to the thread id, so the ref this
        case is named for no longer reaches the ledger by this route. Both
        facts are asserted rather than one quietly replacing the other: the
        normalization happened, AND ownership — not resolvability — is still
        what keeps an agent's own work off the retire pass.
        """
        thread_id = _live_meeting()
        tid = orch.task_add("engineer: fix the notification timing",
                            assignee_role="beta", source_kind="meeting",
                            source_ref=f"{thread_id}:473", created_by="alpha")
        assert _task(tid)["source_ref"] == thread_id
        _close(thread_id)
        _sync()
        assert _task(tid)["status"] == "pending"

    def test_a_ref_that_still_resolves_to_nothing_is_left_alone(self, desk):
        """Normalization is not a promise that every ref resolves. `esc67` was
        typed on this desk too, and rows written before this change keep
        whatever they already carry. The fail-open branch has to hold for
        those, so it needs a case that actually reaches it."""
        thread_id = _live_meeting()
        tid = orch.task_add("chase the escalation", assignee_role="beta",
                            source_kind="meeting", source_ref="esc67",
                            created_by="alpha")
        assert _task(tid)["source_ref"] == "esc67"
        _close(thread_id)
        _sync()
        assert _task(tid)["status"] == "pending"

    def test_the_projections_own_close_task_is_still_retired(self, desk):
        """The behaviour that must not regress: a close task for a meeting that
        has closed is no longer owed, and leaving it would put a demand on the
        ladder over a finished conversation."""
        thread_id = _live_meeting()
        _sync()
        mine = [t for t in orch.tasks(assignee_role="alpha")
                if t["source_ref"] == thread_id
                and t["created_by"] == _CLOSE_TASK_AUTHOR]
        assert mine, "the projection did not create its close task"

        _close(thread_id)
        _sync()
        row = _task(mine[0]["id"])
        assert row["status"] == "done"
        assert "orchestrator" in row["result_note"]

    def test_the_retire_note_says_who_closed_it(self, desk):
        """The old note read like a conclusion an agent had reached, which is
        how four destroyed tasks passed for ordinary completions."""
        thread_id = _live_meeting()
        _sync()
        tid = [t for t in orch.tasks(assignee_role="alpha")
               if t["source_ref"] == thread_id
               and t["created_by"] == _CLOSE_TASK_AUTHOR][0]["id"]
        _close(thread_id)
        _sync()
        note = _task(tid)["result_note"]
        assert "closed automatically" in note and "orchestrator" in note

    def test_an_agent_task_does_not_shadow_the_projections_own(self, desk):
        """The same conflation in the other direction: the lookup that decides
        "have I already made this one" matched an agent's row, so the real
        close task was never created and nobody was ever reminded."""
        thread_id = _live_meeting()
        orch.task_add("notes from this meeting", assignee_role="alpha",
                      source_kind="meeting", source_ref=thread_id,
                      created_by="alpha")
        _sync()
        mine = [t for t in orch.tasks(assignee_role="alpha")
                if t["source_ref"] == thread_id
                and t["created_by"] == _CLOSE_TASK_AUTHOR]
        assert len(mine) == 1, "the close task was shadowed by an agent's row"


class TestFailOpen:

    def test_an_unresolvable_ref_is_not_treated_as_closed(self, desk):
        """"I cannot tell whether this is still owed" is not "it is finished".
        The two mistakes do not cost the same: a stale close task is visible
        and dischargeable, a retired one is gone."""
        with connect(write=True) as conn:
            conn.execute(
                """INSERT INTO agent_tasks
                   (title,detail,assignee_role,status,priority,source_kind,
                    source_ref,created_by,created_at,updated_at)
                   VALUES ('close the meeting: ghost','',?,'pending','normal',
                           'meeting','no-such-thread',?,?,?)""",
                ("alpha", _CLOSE_TASK_AUTHOR, orch._iso(), orch._iso()))
        _sync()
        ghost = [t for t in orch.tasks(assignee_role="alpha")
                 if t["source_ref"] == "no-such-thread"][0]
        assert ghost["status"] == "pending"

    def test_a_null_ref_is_not_treated_as_closed(self, desk):
        """One of the four had no ref at all while its meeting was open."""
        tid = orch.task_add("no ref at all", assignee_role="alpha",
                            source_kind="meeting", created_by="alpha")
        _sync()
        assert _task(tid)["status"] == "pending"
