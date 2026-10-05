"""Authenticated collaboration intents and independently committed delivery.

Gateway command authorization and an outbox intent commit atomically. Applying
that intent to the coordination database is a separate idempotent transaction.
A returned gateway receipt means queued, not delivered or handled by a model.
"""

from __future__ import annotations

from deskd.gateway.actions import tool_catalog as memo_catalog
from deskd.gateway.commands import CommandHandler
from deskd.gateway.identity import ActionPolicy, IdentityError, PrincipalId, identifier

WRITE_FIELDS = {
    "mail.send": {"recipient", "body"},
    "inbox.ack": {"message_ids"},
    "task.create": {"assignee", "title", "body", "depends_on"},
    "task.update": {"task_id", "status", "expected_version"},
}
READ_ACTIONS = ("inbox.read", "tasks.read", "workspace.receipt")
ACTIONS = {
    **{name: ActionPolicy(name) for name in WRITE_FIELDS},
    **{name: ActionPolicy(name) for name in READ_ACTIONS},
}


def _text(value, label, maximum=16384):
    if (
        type(value) is not str
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
    ):
        raise IdentityError("invalid_" + label)


def _ids(value, label):
    if (
        type(value) is not list
        or len(value) > 100
        or any(type(x) is not str for x in value)
        or len(set(value)) != len(value)
    ):
        raise IdentityError("invalid_" + label)
    for item in value:
        identifier(item, label)


class WorkspaceExchange:
    def __init__(self, events, store, *, principals):
        self.events = events
        self.store = store
        self.principals = frozenset(principals)
        self.cursor = 0
        for principal in self.principals:
            desk, seat = principal.split("/")
            PrincipalId(desk, seat)

    def _validate(self, name, args, actor):
        if (
            actor not in self.principals
            or type(args) is not dict
            or set(args) != WRITE_FIELDS[name]
        ):
            raise IdentityError("invalid_workspace_request")
        if name in {"mail.send", "task.create"}:
            target = args["recipient" if name == "mail.send" else "assignee"]
            if (
                type(target) is not str
                or target not in self.principals
                or target.split("/")[0] != actor.split("/")[0]
            ):
                raise IdentityError("unknown_recipient")
            _text(args["body"], "body")
        if name == "task.create":
            _text(args["title"], "title", 256)
            _ids(args["depends_on"], "dependency")
        elif name == "task.update":
            identifier(args["task_id"], "task_id")
            if type(args["status"]) is not str or args["status"] not in {
                "active",
                "blocked",
                "done",
                "cancelled",
            }:
                raise IdentityError("invalid_task_status")
            if (
                type(args["expected_version"]) is not int
                or args["expected_version"] < 1
            ):
                raise IdentityError("invalid_task_version")
        elif name == "inbox.ack":
            _ids(args["message_ids"], "message_id")

    def handlers(self):
        def handler(name):
            def apply(connection, args, identity):
                self._validate(name, args, identity.principal.value)
                return {"queued": True, "action": name}

            return CommandHandler("workspace." + name, apply)

        return {name: handler(name) for name in WRITE_FIELDS}

    def readers(self):
        def inbox(identity, args):
            if args:
                raise IdentityError("invalid_read_arguments")
            return {"messages": self.store.inbox(identity.principal.value)}

        def tasks(identity, args):
            if args:
                raise IdentityError("invalid_read_arguments")
            return {"tasks": self.store.tasks(identity.principal.value)}

        def receipt(identity, args):
            if set(args) != {"event_id"}:
                raise IdentityError("invalid_read_arguments")
            identifier(args["event_id"], "event_id")
            result = self.store.gateway_receipt(
                identity.principal.value, args["event_id"]
            )
            return result if result is not None else {"status": "pending_or_unknown"}

        return {"inbox.read": inbox, "tasks.read": tasks, "workspace.receipt": receipt}

    def pump(self, *, limit=100):
        """Retained outbox replay; the store owns per-event atomic receipts.

        The volatile cursor deliberately starts at zero after restart. Replays
        cannot duplicate a coordination effect or create an extra wake demand.
        """
        count = 0
        for item in self.events.events(after_sequence=self.cursor, limit=limit):
            event = item["event"]
            if event["event_type"] in {"workspace." + n for n in WRITE_FIELDS}:
                self.store.apply_gateway_event(event)
                count += 1
            self.cursor = item["sequence"]
        return count


def tool_catalog():
    string = {"type": "string"}
    ids = {"type": "array", "items": string, "maxItems": 100}
    fields = {
        "mail.send": {"recipient": string, "body": string},
        "inbox.ack": {"message_ids": ids},
        "task.create": {
            "assignee": string,
            "title": string,
            "body": string,
            "depends_on": ids,
        },
        "task.update": {
            "task_id": string,
            "status": {"enum": ["active", "blocked", "done", "cancelled"]},
            "expected_version": {"type": "integer", "minimum": 1},
        },
        "inbox.read": {},
        "tasks.read": {},
        "workspace.receipt": {"event_id": string},
    }
    descriptions = {
        "mail.send": "Queue an untrusted message for another seat. A receipt is not a delivery acknowledgment.",
        "inbox.ack": "Explicitly acknowledge messages handled by your authenticated seat.",
        "task.create": "Queue a task for a seat with optional existing dependency IDs.",
        "task.update": "Request a version-checked update to a task you own or were assigned.",
        "inbox.read": "Read only your authenticated seat's inbox.",
        "tasks.read": "Read only tasks you own or were assigned.",
        "workspace.receipt": "Read the applied or rejected result of your own queued collaboration intent.",
    }
    result = memo_catalog()
    for name, properties in fields.items():
        tool = {
            "name": name,
            "description": descriptions[name],
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        }
        if name in READ_ACTIONS:
            tool["_deskd_read"] = True
            tool["annotations"] = {"readOnlyHint": True}
        result.append(tool)
    return result
