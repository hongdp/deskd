from __future__ import annotations

import pytest

from deskd.tui.client import ClientError, DeskSnapshot
from deskd.tui.commands import CommandRequest, LocalAction, parse_command
from deskd.tui.model import agent_rows, health_text, meeting_rows, safe_text, task_rows


def test_plain_instruction_requires_or_uses_a_target():
    parsed = parse_command('@engineer "fix the wake path"')
    assert parsed == CommandRequest(
        "directive.send",
        {"target_role": "engineer", "body": "fix the wake path", "priority": "normal"},
        "instruction → engineer",
    )
    assert parse_command("fix it", default_role="engineer").params["target_role"] == "engineer"
    with pytest.raises(ClientError, match="@ROLE"):
        parse_command("fix it")


@pytest.mark.parametrize(("line", "verb"), [
    ("/mail @operator inspect alert", "directive.send"),
    ("/task add @engineer fix service", "task.add"),
    ("/task done 7 shipped", "task.done"),
    ("/task block 8 waiting for image", "task.update"),
    ("/task assign 9 @operator", "task.update"),
    ("/task cancel 10 obsolete", "task.update"),
    ("/inbox ack 1,2,3", "inbox.ack"),
    ("/meeting call @analyst,@trader review risk", "meeting.call"),
    ("/meeting checkin live-1", "meeting.check_in"),
    ("/meeting send live-1 evidence follows", "meeting.send"),
    ("/meeting reply live-1 8 answer follows", "meeting.send"),
    ("/meeting resolve live-1 12 8,9", "meeting.resolve"),
    ("/meeting position live-1 ship after tests", "meeting.position"),
    ("/meeting pause live-1 waiting for dependency", "meeting.pause"),
    ("/meeting escalate live-1 need supervisor", "meeting.escalate"),
    ("/meeting end live-1 resolved", "meeting.propose_end"),
    ("/meeting confirm live-1", "meeting.confirm_end"),
    ("/hook at 2026-08-11T09:00:00Z deploy", "hook.add"),
    ("/hook every 60 monitor", "hook.add"),
    ('/hook cron "0 9 * * 1-5" weekday check', "hook.add"),
    ("/hook cancel 4", "hook.cancel"),
])
def test_framework_commands_have_fixed_verbs(line, verb):
    parsed = parse_command(line)
    assert isinstance(parsed, CommandRequest)
    assert parsed.verb == verb
    # Actor identity is never supplied by composer text.
    assert "actor" not in parsed.params


def test_local_commands_never_reach_the_server():
    assert parse_command("/refresh") == LocalAction("refresh")
    assert parse_command("/help") == LocalAction("help")
    assert parse_command("/quit") == LocalAction("quit")


@pytest.mark.parametrize("line", [
    "", "/task", "/task done nope", "/inbox ack nope", "/hook every nope x",
    "/meeting call analyst,trader x", "/unknown",
])
def test_bad_commands_have_operator_facing_errors(line):
    with pytest.raises(ClientError):
        parse_command(line)


def sample_snapshot():
    return DeskSnapshot.from_payload({
        "cursor": "evt_1", "board": {"health": {"total_open_tasks": 1},
            "agents": [{"role": "engineer", "state": "working", "liveness": "online",
                        "activity": "building TUI", "task_counts": {"in_progress": 1},
                        "inbox": {"queued_count": 2}, "meeting": {"active_meetings": []},
                        "wake": {"max_level": 0}, "agent_version": "sha256:abc"}]},
        "tasks": {"tasks": [{"id": 7, "assignee_role": "engineer",
                               "status": "in_progress", "priority": "urgent",
                               "title": "Build TUI", "overdue": True}],
                  "stalled_ids": [7]},
        "inbox": [],
        "meetings": [{"status": {"meeting": {"thread_id": "live-1",
                                                 "state": "active", "agenda": "Ship"},
                                  "attendees": [{"role": "a", "required": 1,
                                                 "checked_in_at": "now"}]}}],
        "hooks": [], "wake": {}, "runtime": {}, "meta": {},
    }, consistent=True)


def test_pure_widget_projection_marks_risk_states():
    snap = sample_snapshot()
    assert agent_rows(snap)[0][0:4] == ("engineer", "working", "online", "building TUI")
    assert agent_rows(snap)[0][8] == "sha256:abc"
    assert task_rows(snap)[0][6] == "OVERDUE,STALLED"
    assert meeting_rows(snap)[0][0:2] == ("live-1", "active")
    assert "open tasks: 1" in health_text(snap)


def test_untrusted_terminal_and_bidi_controls_are_removed():
    hostile = "[bold red]PWN[/]\x1b[2J\x07\u202eetihw"
    cleaned = safe_text(hostile)
    assert cleaned == "[bold red]PWN[/][2Jetihw"
    assert "\x1b" not in cleaned and "\u202e" not in cleaned
