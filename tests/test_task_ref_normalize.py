"""`--ref` is a citation, and a citation nobody can follow is decoration.

Agents cite a meeting the way the console and the wake demand spell it —
`<thread>#412` — but the ledger stores one column and matches it against
`meetings.thread_id`, so the suffixed form resolves to nothing. Measured on the
live desk: of 174 tasks carrying a ref, five `meeting` refs named no meeting,
and four of those were a real thread id with a message number stuck on the end.

The rule here is resolution, not syntax, and these tests exist mostly to pin
that distinction. A "strip everything after the separator" rule passes the
happy cases and quietly mangles `risk_policy.md#2026-07-21`, whose `#` means
something else and whose owner never asked anyone to parse it.
"""

from __future__ import annotations

import json
import warnings

import pytest

from deskd import meetings
from deskd import orchestration as orch
from deskd.orchestration import connect


def _thread() -> str:
    status = meetings.call_meeting(agenda="a meeting to cite",
                                   called_by="alpha", attendees=["alpha", "beta"])
    return status["meeting"]["thread_id"]


def _add(**kw) -> int:
    """Create a task, swallowing the advisory warning the notes emit."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return orch.task_add(kw.pop("title", "cite something"),
                             assignee_role=kw.pop("assignee_role", "alpha"), **kw)


def _stored(task_id: int) -> str | None:
    with connect() as conn:
        return conn.execute("SELECT source_ref FROM agent_tasks WHERE id=?",
                            (task_id,)).fetchone()["source_ref"]


def _notes(task_id: int) -> list[dict]:
    with connect() as conn:
        return [json.loads(r["payload"]) for r in conn.execute(
            "SELECT payload FROM orchestration_events "
            "WHERE kind='task_ref_note' AND ref=?", (str(task_id),))]


@pytest.mark.parametrize("sep", ["#", ":"])
def test_a_message_number_is_reduced_to_the_thread_that_holds_it(desk, sep):
    thread = _thread()
    tid = _add(source_kind="meeting", source_ref=f"{thread}{sep}412")
    assert _stored(tid) == thread
    assert _notes(tid), "the ledger must say the ref was rewritten"


def test_a_bare_thread_id_passes_through_without_comment(desk):
    thread = _thread()
    tid = _add(source_kind="meeting", source_ref=thread)
    assert _stored(tid) == thread
    assert _notes(tid) == [], "a ref that already resolves is not worth a note"


def test_a_ref_naming_no_meeting_is_kept_and_still_creates_the_task(desk):
    """The whole point of the note: it is a note. `esc67` is a real ref an
    agent typed on this desk, and the task it carried was real work."""
    tid = _add(source_kind="meeting", source_ref="esc67")
    assert tid, "an unresolvable citation must not block the work"
    assert _stored(tid) == "esc67", "stored as typed — do not invent a thread"
    assert "names no meeting" in _notes(tid)[0]["note"]


def test_a_lookalike_suffix_is_not_stripped_off_a_stranger(desk):
    """`<something>#<digits>` where the something is not a thread. Stripping
    here would produce a ref that resolves to nothing AND has lost a piece."""
    tid = _add(source_kind="meeting", source_ref="risk_policy.md#2026")
    assert _stored(tid) == "risk_policy.md#2026"


def test_a_non_numeric_suffix_on_a_real_thread_survives(desk):
    """Only a message NUMBER is droppable. Anything else is somebody's meaning."""
    thread = _thread()
    tid = _add(source_kind="meeting", source_ref=f"{thread}#agenda-item-two")
    assert _stored(tid) == f"{thread}#agenda-item-two"


def test_other_kinds_are_left_exactly_as_written(desk):
    """`self` refs are free-form traces that deliberately carry several parts
    (`tasks:230,229`, `task:220;branch:worktree-stale-latest-bar`). Normalizing
    those would delete what the author wrote, which is the opposite of the job.
    """
    thread = _thread()
    for kind, ref in (("self", f"{thread}#413"),
                      ("self", "tasks:230,229"),
                      ("system", "risk_policy.md#2026-07-21")):
        tid = _add(source_kind=kind, source_ref=ref)
        assert _stored(tid) == ref
        assert _notes(tid) == []


def test_the_note_reaches_whoever_typed_the_command(desk):
    """The durable half is the event; this is the half a person actually sees,
    and it is the reason no host CLI needs to learn about any of this."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        orch.task_add("cite something", assignee_role="alpha",
                      source_kind="meeting", source_ref="esc67")
    assert any("names no meeting" in str(w.message) for w in caught)


def test_a_missing_ref_is_not_a_finding(desk):
    tid = _add(source_kind="meeting", source_ref=None)
    assert _stored(tid) is None
    assert _notes(tid) == []
