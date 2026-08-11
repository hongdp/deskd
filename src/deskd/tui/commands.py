"""Composer grammar shared by the Textual UI and non-terminal tests."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from .client import ClientError


@dataclass(frozen=True)
class CommandRequest:
    verb: str
    params: dict[str, Any]
    summary: str


@dataclass(frozen=True)
class LocalAction:
    action: str


_HELP = """Composer
  @ROLE instruction                 send durable work; wakes the role immediately
  /mail @ROLE message               same as above
  /task add @ROLE title             create a task
  /task done ID [note]              finish a task
  /task block ID dependency         block with a named dependency
  /task assign ID @ROLE             transfer a task
  /task cancel ID [note]            cancel a task
  /inbox ack ID[,ID...]             acknowledge delivered mail
  /meeting call @A,@B agenda        call a bounded meeting
  /meeting checkin ID               check in as your token-derived role
  /meeting send ID message          send an evidence update
  /meeting reply ID MSG_ID message  answer and resolve one obligation
  /meeting resolve ID OWN_ID MSGS   resolve MSG[,MSG] with an existing message
  /meeting position ID body         submit this role's position
  /meeting pause ID reason          pause the meeting
  /meeting escalate ID reason       escalate and pause
  /meeting end ID resolution        propose termination
  /meeting confirm ID               confirm termination
  /hook at ISO TITLE                register a one-shot wake for your role
  /hook every SECONDS TITLE         register an interval wake for your role
  /hook cron "EXPR" TITLE           register a calendar wake for your role
  /hook cancel ID                   cancel a wake hook
  /refresh | /help | /quit

The server derives your identity and permissions from the bearer credential;
@ROLE always names a target, never an identity you may impersonate.
"""


def command_help() -> str:
    return _HELP


def _target(value: str) -> str:
    if not value.startswith("@") or len(value) == 1:
        raise ClientError("target must be written as @ROLE")
    return value[1:]


def _words(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError as exc:
        raise ClientError(f"invalid command quoting: {exc}") from None


def _need(words: list[str], count: int, usage: str) -> None:
    if len(words) < count:
        raise ClientError(f"usage: {usage}")


def parse_command(text: str, *, default_role: str | None = None) -> CommandRequest | LocalAction:
    """Parse one fast-composer line without assigning authority client-side."""

    text = text.strip()
    if not text:
        raise ClientError("enter an instruction or /help")
    if not text.startswith("/"):
        words = _words(text)
        if words and words[0].startswith("@"):
            role = _target(words[0])
            body = " ".join(words[1:]).strip()
        elif default_role:
            role, body = default_role, text
        else:
            raise ClientError("prefix an instruction with @ROLE")
        if not body:
            raise ClientError("instruction body is required")
        return CommandRequest(
            "directive.send", {"target_role": role, "body": body,
                               "priority": "normal"}, f"instruction → {role}")

    words = _words(text)
    local = {"/help": "help", "/refresh": "refresh", "/quit": "quit"}
    if words[0] in local:
        if len(words) != 1:
            raise ClientError(f"{words[0]} takes no arguments")
        return LocalAction(local[words[0]])

    if words[0] == "/mail":
        _need(words, 3, "/mail @ROLE message")
        role = _target(words[1])
        return CommandRequest(
            "directive.send", {"target_role": role, "body": " ".join(words[2:]),
                               "priority": "normal"}, f"mail → {role}")

    if words[0] == "/task":
        _need(words, 3, "/task add|done|block|assign ...")
        action = words[1]
        if action == "add":
            _need(words, 4, "/task add @ROLE title")
            role = _target(words[2])
            return CommandRequest(
                "task.add", {"assignee_role": role, "title": " ".join(words[3:]),
                             "priority": "normal"}, f"task → {role}")
        try:
            task_id = int(words[2])
        except ValueError:
            raise ClientError("task ID must be an integer") from None
        if action == "done":
            return CommandRequest(
                "task.done", {"task_id": task_id,
                              "note": " ".join(words[3:]) or None},
                f"complete task #{task_id}")
        if action == "block":
            _need(words, 4, "/task block ID dependency")
            return CommandRequest(
                "task.update", {"task_id": task_id, "status": "blocked",
                                "blocked_on": " ".join(words[3:])},
                f"block task #{task_id}")
        if action == "assign":
            _need(words, 4, "/task assign ID @ROLE")
            role = _target(words[3])
            return CommandRequest(
                "task.update", {"task_id": task_id, "assignee_role": role},
                f"assign task #{task_id} → {role}")
        if action == "cancel":
            return CommandRequest(
                "task.update", {"task_id": task_id, "status": "cancelled",
                                "result_note": " ".join(words[3:]) or None},
                f"cancel task #{task_id}")
        raise ClientError("task action must be add, done, block, assign, or cancel")

    if words[0] == "/inbox":
        if len(words) != 3 or words[1] != "ack":
            raise ClientError("usage: /inbox ack ID[,ID...]")
        try:
            ids = [int(v) for v in words[2].split(",") if v]
        except ValueError:
            raise ClientError("inbox IDs must be comma-separated integers") from None
        if not ids:
            raise ClientError("at least one inbox ID is required")
        return CommandRequest("inbox.ack", {"ids": ids}, f"ack {len(ids)} inbox item(s)")

    if words[0] == "/meeting":
        _need(words, 3, "/meeting call|checkin|send|end|confirm ...")
        action = words[1]
        if action == "call":
            _need(words, 4, "/meeting call @A,@B agenda")
            attendees = [_target(v) for v in words[2].split(",")]
            return CommandRequest(
                "meeting.call", {"attendees": attendees, "agenda": " ".join(words[3:]),
                                 "meeting_type": "ad-hoc", "priority": "normal"},
                "call meeting")
        meeting_id = words[2]
        if action == "checkin":
            return CommandRequest("meeting.check_in", {"meeting_id": meeting_id},
                                  f"check in {meeting_id}")
        if action == "send":
            _need(words, 4, "/meeting send ID message")
            return CommandRequest(
                "meeting.send", {"meeting_id": meeting_id, "kind": "evidence",
                                 "body": " ".join(words[3:])},
                f"send to {meeting_id}")
        if action == "reply":
            _need(words, 5, "/meeting reply ID MSG_ID message")
            try:
                message_id = int(words[3])
            except ValueError:
                raise ClientError("meeting message ID must be an integer") from None
            return CommandRequest(
                "meeting.send", {"meeting_id": meeting_id, "kind": "answer",
                                 "body": " ".join(words[4:]),
                                 "reply_to": message_id, "resolves": [message_id]},
                f"reply in {meeting_id}")
        if action == "resolve":
            _need(words, 5, "/meeting resolve ID OWN_MSG_ID MSG_ID[,MSG_ID...]")
            try:
                covered_by = int(words[3])
                message_ids = [int(v) for v in words[4].split(",") if v]
            except ValueError:
                raise ClientError("meeting message IDs must be integers") from None
            if not message_ids:
                raise ClientError("at least one resolved message ID is required")
            return CommandRequest(
                "meeting.resolve", {"meeting_id": meeting_id,
                                    "covered_by": covered_by,
                                    "message_ids": message_ids},
                f"resolve obligations in {meeting_id}")
        if action == "position":
            _need(words, 4, "/meeting position ID body")
            return CommandRequest(
                "meeting.position", {"meeting_id": meeting_id,
                                     "body": " ".join(words[3:])},
                f"position in {meeting_id}")
        if action in {"leave", "pause", "reject", "escalate"}:
            _need(words, 4, f"/meeting {action} ID reason")
            verb = {"leave": "meeting.leave", "pause": "meeting.pause",
                    "reject": "meeting.reject_end",
                    "escalate": "meeting.escalate"}[action]
            params = {"meeting_id": meeting_id, "reason": " ".join(words[3:])}
            if action == "escalate":
                params["pause"] = True
            return CommandRequest(verb, params, f"{action} {meeting_id}")
        if action == "end":
            _need(words, 4, "/meeting end ID resolution")
            return CommandRequest(
                "meeting.propose_end", {"meeting_id": meeting_id,
                                        "resolution": " ".join(words[3:])},
                f"propose end {meeting_id}")
        if action == "confirm":
            if len(words) != 3:
                raise ClientError("usage: /meeting confirm ID")
            return CommandRequest("meeting.confirm_end", {"meeting_id": meeting_id},
                                  f"confirm end {meeting_id}")
        if action == "wake-ack":
            if len(words) != 3:
                raise ClientError("usage: /meeting wake-ack ID")
            return CommandRequest("meeting.wake_ack", {"meeting_id": meeting_id},
                                  f"ack meeting wake {meeting_id}")
        raise ClientError(
            "meeting action must be call, checkin, send, reply, resolve, position, "
            "leave, pause, reject, escalate, end, confirm, or wake-ack")

    if words[0] == "/hook":
        _need(words, 3, "/hook at|every|cancel ...")
        action = words[1]
        if action == "cancel":
            if len(words) != 3:
                raise ClientError("usage: /hook cancel ID")
            try:
                hook_id = int(words[2])
            except ValueError:
                raise ClientError("hook ID must be an integer") from None
            return CommandRequest("hook.cancel", {"hook_id": hook_id},
                                  f"cancel hook #{hook_id}")
        _need(words, 4, f"/hook {action} VALUE TITLE")
        value, title = words[2], " ".join(words[3:])
        if action == "at":
            params: dict[str, Any] = {"at": value, "title": title}
        elif action == "every":
            try:
                seconds = int(value)
            except ValueError:
                raise ClientError("hook interval must be an integer number of seconds") from None
            params = {"every": seconds, "title": title}
        elif action == "cron":
            params = {"cron": value, "title": title}
        else:
            raise ClientError("hook action must be at, every, cron, or cancel")
        return CommandRequest("hook.add", params, f"add {action} hook for credential role")

    raise ClientError(f"unknown command {words[0]!r}; use /help")
