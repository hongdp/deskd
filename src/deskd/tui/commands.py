"""Composer grammar shared by the Textual UI and non-terminal tests."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .client import ClientError


@dataclass(frozen=True)
class CommandRequest:
    verb: str
    params: dict[str, Any]
    summary: str


@dataclass(frozen=True)
class LocalAction:
    action: str


_HELP_ROWS: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"task.add"}),
     "  /task add @ROLE title             create a task"),
    (frozenset({"task.done"}),
     "  /task done ID [note]              finish a task"),
    (frozenset({"task.update"}),
     "  /task block ID dependency         block with a named dependency"),
    (frozenset({"task.update"}),
     "  /task assign ID @ROLE             transfer a task"),
    (frozenset({"task.update"}),
     "  /task cancel ID [note]            cancel a task"),
    (frozenset({"inbox.ack"}),
     "  /inbox ack ID[,ID...]             acknowledge delivered mail"),
    (frozenset({"wake.reconcile"}),
     "  /wake reconcile CLAIM retry|landed NOTE  settle a quarantined wake"),
    (frozenset({"meeting.call"}),
     "  /meeting call @A,@B agenda        call a bounded meeting"),
    (frozenset({"meeting.check_in"}),
     "  /meeting checkin ID               check in as your token-derived role"),
    (frozenset({"meeting.send"}),
     "  /meeting send ID message          send an evidence update"),
    (frozenset({"meeting.send"}),
     "  /meeting reply ID MSG_ID message  answer and resolve one obligation"),
    (frozenset({"meeting.resolve"}),
     "  /meeting resolve ID OWN_ID MSGS   resolve MSG[,MSG] with an existing message"),
    (frozenset({"meeting.position"}),
     "  /meeting position ID body         submit this role's position"),
    (frozenset({"meeting.pause"}),
     "  /meeting pause ID reason          pause the meeting"),
    (frozenset({"meeting.escalate"}),
     "  /meeting escalate ID reason       escalate and pause"),
    (frozenset({"meeting.propose_end"}),
     "  /meeting end ID resolution        propose termination"),
    (frozenset({"meeting.confirm_end"}),
     "  /meeting confirm ID               confirm termination"),
    (frozenset({"hook.add"}),
     "  /hook at ISO TITLE                register a one-shot wake for your role"),
    (frozenset({"hook.add"}),
     "  /hook every SECONDS TITLE         register an interval wake for your role"),
    (frozenset({"hook.add"}),
     '  /hook cron "EXPR" TITLE           register a calendar wake for your role'),
    (frozenset({"hook.cancel"}),
     "  /hook cancel ID                   cancel a wake hook"),
)


def capabilities_from_meta(
        meta: Mapping[str, Any]) -> tuple[frozenset[str] | None, str | None]:
    """Return the server-authoritative composer verbs and credential role.

    ``allowed_verbs`` is principal-specific on current servers. ``verbs`` was
    the older global discovery list, so it remains a compatibility fallback
    only. A malformed advertised list fails closed instead of making the UI
    offer commands whose authority it cannot establish.
    """

    raw_role = meta.get("role")
    principal_role = (raw_role.strip() if isinstance(raw_role, str)
                      and raw_role.strip() else None)
    for field in ("allowed_verbs", "verbs"):
        if field not in meta:
            continue
        raw = meta.get(field)
        if not isinstance(raw, list) or any(
                not isinstance(item, str) or not item.strip() for item in raw):
            return frozenset(), principal_role
        return frozenset(item.strip() for item in raw), principal_role
    return None, principal_role


def _can_send(allowed_verbs: frozenset[str] | None,
              principal_role: str | None) -> bool:
    if allowed_verbs is None:
        return True
    return ("directive.send" in allowed_verbs
            or bool(principal_role and "message.send" in allowed_verbs))


def command_help(*, allowed_verbs: Iterable[str] | None = None,
                 principal_role: str | None = None) -> str:
    """Build help from this credential's server-issued capabilities."""

    allowed = (None if allowed_verbs is None
               else frozenset(str(verb) for verb in allowed_verbs))
    lines = ["Composer"]
    if _can_send(allowed, principal_role):
        mode = "direct role mail" if principal_role and allowed is not None and (
            "message.send" in allowed) else "durable directive"
        lines.extend([
            f"  @ROLE instruction                 send {mode}; wakes the role immediately",
            "  /mail @ROLE message               same as above",
        ])
    for required, line in _HELP_ROWS:
        if allowed is None or required & allowed:
            lines.append(line)
    if allowed is not None and len(lines) == 1:
        lines.append("  (this credential has no remote composer commands)")
    lines.extend([
        "  /refresh | /help | /quit",
        "",
        (f"Credential: role {principal_role}; only that role can be the sender."
         if principal_role else "Credential: service/legacy; no agent role is implied."),
        "The server derives identity and permissions from the bearer credential;",
        "@ROLE always names a target, never an identity you may impersonate.",
    ])
    return "\n".join(lines) + "\n"


def command_hint(*, allowed_verbs: Iterable[str] | None = None,
                 principal_role: str | None = None) -> str:
    """Compact capability-aware composer hint for the persistent footer."""

    allowed = (None if allowed_verbs is None
               else frozenset(str(verb) for verb in allowed_verbs))
    actions = []
    if _can_send(allowed, principal_role):
        actions.append("@ROLE message")
    groups = (
        ("/task", {"task.add", "task.update", "task.done"}),
        ("/inbox", {"inbox.ack"}),
        ("/wake", {"wake.reconcile"}),
        ("/meeting", {verb for verbs, _ in _HELP_ROWS for verb in verbs
                       if verb.startswith("meeting.")}),
        ("/hook", {"hook.add", "hook.cancel"}),
    )
    for label, verbs in groups:
        if allowed is None or verbs & allowed:
            actions.append(label)
    actions.extend(["/help", "Ctrl+L focuses composer"])
    return "  •  ".join(actions)


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


def _message_subject(body: str) -> str:
    """A stable, bounded thread subject derived only from visible user text."""

    return ("deskd TUI: " + " ".join(body.split()))[:160]


def _instruction_request(role: str, body: str, *,
                         allowed_verbs: frozenset[str] | None,
                         principal_role: str | None,
                         summary: str) -> CommandRequest:
    # A role credential speaks as exactly that role through direct mail. A
    # service credential may enqueue an operator directive but never gains a
    # meeting seat or agent sender identity. Interactive work is urgent so the
    # planner wakes it on the next tick instead of waiting for normal batching.
    if (principal_role and allowed_verbs is not None
            and "message.send" in allowed_verbs):
        return CommandRequest(
            "message.send",
            {"target_role": role, "subject": _message_subject(body),
             "body": body, "kind": "note", "priority": "urgent"},
            summary,
        )
    if allowed_verbs is None or "directive.send" in allowed_verbs:
        return CommandRequest(
            "directive.send",
            {"target_role": role, "body": body, "priority": "urgent"},
            summary,
        )
    raise ClientError(
        "this credential cannot send mail or directives; /help shows permitted commands")


def _parse_command(text: str, *, default_role: str | None,
                   allowed_verbs: frozenset[str] | None,
                   principal_role: str | None) -> CommandRequest | LocalAction:
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
        return _instruction_request(
            role, body, allowed_verbs=allowed_verbs,
            principal_role=principal_role, summary=f"instruction → {role}")

    words = _words(text)
    local = {"/help": "help", "/refresh": "refresh", "/quit": "quit"}
    if words[0] in local:
        if len(words) != 1:
            raise ClientError(f"{words[0]} takes no arguments")
        return LocalAction(local[words[0]])

    if words[0] == "/mail":
        _need(words, 3, "/mail @ROLE message")
        role = _target(words[1])
        body = " ".join(words[2:]).strip()
        if not body:
            raise ClientError("mail body is required")
        return _instruction_request(
            role, body, allowed_verbs=allowed_verbs,
            principal_role=principal_role, summary=f"mail → {role}")

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

    if words[0] == "/wake":
        _need(words, 5, "/wake reconcile CLAIM retry|landed NOTE")
        if words[1] != "reconcile":
            raise ClientError("wake action must be reconcile")
        resolution = words[3]
        if resolution not in {"retry", "landed"}:
            raise ClientError("wake reconciliation must be retry or landed")
        note = " ".join(words[4:]).strip()
        if not note:
            raise ClientError("wake reconciliation note is required")
        return CommandRequest(
            "wake.reconcile",
            {"claim_id": words[2], "resolution": resolution, "note": note},
            f"reconcile wake {words[2]} as {resolution}",
        )

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


def parse_command(text: str, *, default_role: str | None = None,
                  allowed_verbs: Iterable[str] | None = None,
                  principal_role: str | None = None) -> CommandRequest | LocalAction:
    """Parse and reject commands outside the authenticated capability set.

    ``None`` retains compatibility with pre-capability servers. An explicit
    empty iterable means the credential has no remote composer authority.
    """

    allowed = (None if allowed_verbs is None
               else frozenset(str(verb) for verb in allowed_verbs))
    parsed = _parse_command(
        text, default_role=default_role, allowed_verbs=allowed,
        principal_role=principal_role)
    if (isinstance(parsed, CommandRequest) and allowed is not None
            and parsed.verb not in allowed):
        raise ClientError(
            f"{parsed.verb} is unavailable for this credential; "
            "/help shows permitted commands")
    return parsed
