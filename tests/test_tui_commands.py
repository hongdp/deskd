from __future__ import annotations

import pytest

from deskd.tui.client import ClientError, DeskSnapshot
from deskd.tui.commands import (CommandRequest, LocalAction, capabilities_from_meta,
                                command_help, command_hint, parse_command)
from deskd.tui.model import (agent_rows, health_text, meeting_rows, safe_text,
                             task_rows, wake_rows)


def test_plain_instruction_requires_or_uses_a_target():
    parsed = parse_command('@engineer "fix the wake path"')
    assert parsed == CommandRequest(
        "directive.send",
        {"target_role": "engineer", "body": "fix the wake path", "priority": "urgent"},
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
    ("/wake reconcile wake-7 retry provider crashed before receipt", "wake.reconcile"),
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


def test_role_credential_uses_urgent_direct_mail_with_deterministic_subject():
    allowed = {"message.send", "meeting.send", "hook.add", "inbox.ack"}
    expected = CommandRequest(
        "message.send",
        {"target_role": "operator", "subject": "deskd TUI: inspect alert",
         "body": "inspect alert", "kind": "note", "priority": "urgent"},
        "instruction → operator",
    )
    assert parse_command(
        "@operator inspect alert", allowed_verbs=allowed,
        principal_role="engineer") == expected
    mailed = parse_command(
        "/mail @operator inspect alert", allowed_verbs=allowed,
        principal_role="engineer")
    assert isinstance(mailed, CommandRequest)
    assert mailed.verb == "message.send"
    assert mailed.params == expected.params
    assert "actor" not in mailed.params and "sender" not in mailed.params


def test_service_credential_uses_urgent_directive_and_rejects_role_verbs():
    allowed = {"directive.send", "task.add"}
    sent = parse_command("@engineer ship", allowed_verbs=allowed)
    assert isinstance(sent, CommandRequest)
    assert sent.params["priority"] == "urgent"
    with pytest.raises(ClientError, match="meeting.call is unavailable"):
        parse_command(
            "/meeting call @analyst review", allowed_verbs=allowed)


def test_operator_wake_reconciliation_is_capability_gated():
    parsed = parse_command(
        "/wake reconcile wake-7 landed provider audit proves session started",
        allowed_verbs={"wake.reconcile"})
    assert parsed == CommandRequest(
        "wake.reconcile",
        {"claim_id": "wake-7", "resolution": "landed",
         "note": "provider audit proves session started"},
        "reconcile wake wake-7 as landed",
    )
    with pytest.raises(ClientError, match="wake.reconcile is unavailable"):
        parse_command(
            "/wake reconcile wake-7 retry launch receipt absent",
            allowed_verbs={"directive.send"})


def test_capability_metadata_prefers_principal_allowlist_and_fails_closed():
    allowed, role = capabilities_from_meta({
        "role": " engineer ",
        "allowed_verbs": ["message.send", "meeting.send"],
        "verbs": ["directive.send", "scheduler.tick"],
    })
    assert allowed == {"message.send", "meeting.send"}
    assert role == "engineer"
    assert capabilities_from_meta({"verbs": ["task.add"]}) == (
        frozenset({"task.add"}), None)
    assert capabilities_from_meta({"allowed_verbs": "message.send"}) == (
        frozenset(), None)


def test_dynamic_help_and_hint_only_advertise_credential_capabilities():
    service = command_help(allowed_verbs={"directive.send", "task.add"})
    assert "@ROLE instruction" in service and "/task add" in service
    assert "/meeting" not in service
    assert "/hook" not in service
    assert "/inbox" not in service
    assert "/wake reconcile" not in service
    assert "/meeting" not in command_hint(
        allowed_verbs={"directive.send", "task.add"})

    role = command_help(
        allowed_verbs={"message.send", "meeting.send", "hook.add", "inbox.ack"},
        principal_role="engineer")
    assert "direct role mail" in role
    assert "/meeting send" in role and "/hook at" in role and "/inbox ack" in role
    assert "only that role can be the sender" in role

    operator = command_help(allowed_verbs={"wake.reconcile"})
    assert "/wake reconcile" in operator
    assert "/wake" in command_hint(allowed_verbs={"wake.reconcile"})


@pytest.mark.parametrize("line", [
    "", "/task", "/task done nope", "/inbox ack nope", "/hook every nope x",
    '/mail @operator ""', "/meeting call analyst,trader x", "/unknown",
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
        "hooks": [], "wake": {"quarantine": [{
            "claim_id": "wake-7", "role": "engineer", "state": "indeterminate",
            "channel": "spawn", "mode": "spawn", "claimed_at": "2026-08-11T00:00:00Z",
            "error": "provider exited before launch receipt",
        }]}, "runtime": {}, "meta": {},
    }, consistent=True)


def test_pure_widget_projection_marks_risk_states():
    snap = sample_snapshot()
    assert agent_rows(snap)[0][0:4] == ("engineer", "working", "online", "building TUI")
    assert agent_rows(snap)[0][8] == "sha256:abc"
    assert task_rows(snap)[0][6] == "OVERDUE,STALLED"
    assert meeting_rows(snap)[0][0:2] == ("live-1", "active")
    assert wake_rows(snap)[0][0:5] == (
        "wake-7", "engineer", "!", "spawn", "QUARANTINED")
    assert "open tasks: 1" in health_text(snap)
    assert "WAKE QUARANTINE: 1" in health_text(snap)


def test_untrusted_terminal_and_bidi_controls_are_removed():
    hostile = "[bold red]PWN[/]\x1b[2J\x07\u202eetihw"
    cleaned = safe_text(hostile)
    assert cleaned == "[bold red]PWN[/][2Jetihw"
    assert "\x1b" not in cleaned and "\u202e" not in cleaned
