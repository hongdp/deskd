"""Real SQLite pagination and wire budgets using only synthetic scratch data."""

import base64
import json
from types import SimpleNamespace

import pytest

from deskd.gateway.bridge import MAX_MCP_RESPONSE_BYTES
from deskd.gateway.events import canonical_json
from deskd.gateway.identity import IdentityError, PrincipalId
from deskd.gateway.wire import MAX_RESPONSE_BYTES, encode_frame
from deskd.workspace.exchange import WorkspaceExchange, tool_catalog
from deskd.workspace.store import (
    READ_PAGE_BYTES,
    READ_SINGLE_ITEM_BYTES,
    WorkspaceError,
    WorkspaceStore,
)


@pytest.fixture
def store(tmp_path):
    value = WorkspaceStore(tmp_path / "workspace.sqlite", clock=lambda: 100.0)
    for seat in ("operator", "reviewer", "engineer"):
        value.register_seat("demo/" + seat, "root-" + seat, "a" * 64)
    return value


def cursor(kind, actor, item_id, *, version=1):
    return (
        base64.urlsafe_b64encode(
            canonical_json([version, kind, actor, item_id]).encode()
        )
        .decode()
        .rstrip("=")
    )


def assert_wire_budget(page, key):
    serialized = canonical_json(page)
    budget = READ_SINGLE_ITEM_BYTES if len(page[key]) == 1 else READ_PAGE_BYTES
    assert len(serialized.encode()) <= budget
    assert (
        len(encode_frame({"ok": True, "result": page}, response=True))
        <= MAX_RESPONSE_BYTES
    )
    response = {
        "jsonrpc": "2.0",
        "id": "synthetic-page",
        "result": {"content": [{"type": "text", "text": serialized}], "isError": False},
    }
    assert (
        len(json.dumps(response, ensure_ascii=False).encode()) < MAX_MCP_RESPONSE_BYTES
    )


def pages(method, actor, *, key, limit=100):
    position = None
    seen = set()
    while True:
        page = method(actor, cursor=position, limit=limit)
        assert_wire_budget(page, key)
        yield page
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        assert page[key] and page["next_cursor"] not in seen
        seen.add(page["next_cursor"])
        position = page["next_cursor"]


def test_inbox_accumulation_uses_actual_bytes_and_preserves_every_body(store):
    # These individually legal messages previously overflowed the response
    # frame together. Escaped controls also defeat raw UTF-8/count estimates.
    expected = {}
    for index in range(70):
        body = ("\x01" if index % 2 else "多") * (16384 if index % 2 else 5000)
        message = store.enqueue(
            "demo/operator",
            "demo/reviewer",
            body,
            request_id=f"own-{index}",
            priority=index % 3,
        )
        expected[message["id"]] = body
        store.enqueue(
            "demo/operator",
            "demo/engineer",
            "synthetic-foreign-body",
            request_id=f"foreign-{index}",
        )
    result = list(pages(store.inbox_page, "demo/reviewer", key="messages"))
    assert len(result) > 2
    messages = [message for page in result for message in page["messages"]]
    assert [m["id"] for m in messages] == list(expected)
    assert {m["id"]: m["body"] for m in messages} == expected
    assert {m["recipient"] for m in messages} == {"demo/reviewer"}
    assert {m["state"] for m in messages} == {"queued"}
    # Reading does not ACK; a fresh traversal returns the same items.
    assert store.inbox_page("demo/reviewer") == result[0]


def test_maximum_single_body_gets_whole_item_budget(store):
    body = "\x01" * 65536
    for index in range(2):
        store.enqueue(
            "demo/operator", "demo/reviewer", body, request_id=f"large-{index}"
        )
    result = list(pages(store.inbox_page, "demo/reviewer", key="messages"))
    assert len(result) == 2
    for page in result:
        assert len(canonical_json(page).encode()) > READ_PAGE_BYTES
        assert page["messages"][0]["body"] == body
    task = store.add_task(
        "demo/operator",
        "demo/reviewer",
        "\x01" * 1024,
        detail="\x00" * 65536,
        request_id="maximum-task",
    )
    page = store.tasks_page("demo/reviewer", task_id=task["id"])
    assert_wire_budget(page, "tasks")
    assert page["tasks"][0]["detail"] == "\x00" * 65536
    assert page["tasks"][0]["title"] == "\x01" * 1024


def test_ack_and_append_between_pages_do_not_skip_existing_messages(store):
    expected = [
        store.enqueue(
            "demo/operator", "demo/reviewer", str(index), request_id=f"message-{index}"
        )["id"]
        for index in range(5)
    ]
    first = store.inbox_page("demo/reviewer", limit=2)
    store.acknowledge("demo/reviewer", expected[:2])
    expected.append(
        store.enqueue("demo/operator", "demo/reviewer", "new", request_id="new")["id"]
    )
    second = store.inbox_page("demo/reviewer", cursor=first["next_cursor"], limit=100)
    assert [m["id"] for m in second["messages"]] == expected[2:]
    assert second == store.inbox_page("demo/reviewer")
    assert not second["has_more"]


def test_tasks_pass_the_old_thousand_row_cutoff_with_stable_ties(store):
    expected = []
    for index in range(1007):
        # Same timestamps and then a clock rollback must not reorder reads.
        store._clock = lambda: 100.0 if index < 600 else 90.0
        expected.append(
            store.add_task(
                "demo/operator",
                "demo/reviewer",
                f"task-{index}",
                detail="x" * 16384 if index < 70 else "synthetic-detail",
                request_id=f"task-{index}",
            )["id"]
        )
        if index % 100 == 0:
            store.add_task(
                "demo/engineer",
                "demo/engineer",
                "private",
                detail="private-body",
                request_id=f"private-{index}",
            )
    result = list(pages(store.tasks_page, "demo/reviewer", key="tasks"))
    tasks = [task for page in result for task in page["tasks"]]
    assert [task["id"] for task in tasks] == expected
    assert len(tasks) == 1007
    assert all(task["creator"] == "demo/operator" for task in tasks)
    assert all("rowid" not in task for task in tasks)
    # A wake can directly fetch its task even after a thousand earlier tasks.
    last = store.tasks_page("demo/reviewer", task_id=expected[-1])
    assert [task["id"] for task in last["tasks"]] == [expected[-1]]
    assert not last["has_more"] and last["next_cursor"] is None
    assert store.tasks_page("demo/engineer", task_id=expected[-1]) == {
        "tasks": [],
        "has_more": False,
        "next_cursor": None,
    }


def test_cursors_cannot_cross_actors_or_resource_types(store):
    for index in range(2):
        store.enqueue("demo/operator", "demo/reviewer", "r", request_id=f"r-{index}")
        store.enqueue("demo/operator", "demo/engineer", "e", request_id=f"e-{index}")
        store.add_task("demo/operator", "demo/reviewer", "t", request_id=f"t-{index}")
    inbox = store.inbox_page("demo/reviewer", limit=1)["next_cursor"]
    task = store.tasks_page("demo/reviewer", limit=1)["next_cursor"]
    for method, actor, value in (
        (store.inbox_page, "demo/engineer", inbox),
        # Creator can read the same tasks, but cannot reuse another actor's cursor.
        (store.tasks_page, "demo/operator", task),
        (store.tasks_page, "demo/reviewer", inbox),
        (store.inbox_page, "demo/reviewer", task),
        (store.inbox_page, "demo/reviewer", cursor("inbox", "demo/reviewer", 2)),
    ):
        with pytest.raises(WorkspaceError, match="^invalid_cursor$"):
            method(actor, cursor=value)
    with pytest.raises(WorkspaceError, match="^invalid_read_arguments$"):
        store.tasks_page("demo/reviewer", cursor=task, task_id="anything")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * 1025,
        "bad!",
        "[]",
        "é",
        [],
        {},
        True,
        1,
        cursor("inbox", "demo/reviewer", 0),
        cursor("inbox", "demo/reviewer", 2**63),
        cursor("inbox", "demo/reviewer", True),
        cursor("inbox", "demo/reviewer", "1"),
        cursor("inbox", "demo/reviewer", 1, version=True),
        cursor("inbox", "demo/reviewer", 1, version=2),
        cursor("inbox", "demo/reviewer", 1) + "=",
        cursor("inbox", "demo/reviewer", 999999),
        cursor("tasks", "demo/reviewer", "' OR 1=1--"),
        cursor("tasks", "demo/reviewer", "a" * 32),
    ],
)
def test_malformed_unknown_and_noncanonical_cursors_rejected(store, value):
    with pytest.raises(WorkspaceError, match="^invalid_cursor$"):
        store.inbox_page("demo/reviewer", cursor=value)
    with pytest.raises(WorkspaceError, match="^invalid_cursor$"):
        store.tasks_page("demo/reviewer", cursor=value)


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.0, "1", None, []])
def test_page_limit_is_bounded_and_strict(store, limit):
    for method in (store.inbox_page, store.tasks_page):
        with pytest.raises(WorkspaceError, match="^invalid_limit$"):
            method("demo/reviewer", limit=limit)


def test_fixed_readers_offer_optional_pages_and_keep_errors_public(store):
    exchange = WorkspaceExchange(
        None, store, principals={"demo/operator", "demo/reviewer"}
    )
    readers = exchange.readers()
    identity = SimpleNamespace(principal=PrincipalId("demo", "reviewer"))
    task = store.add_task("demo/operator", "demo/reviewer", "task", request_id="task")
    assert (
        readers["tasks.read"](identity, {"task_id": task["id"]})["tasks"][0]["id"]
        == task["id"]
    )
    for name, args, code in (
        ("inbox.read", {"cursor": None}, "invalid_cursor"),
        ("inbox.read", {"limit": True}, "invalid_limit"),
        ("inbox.read", {"recipient": "demo/operator"}, "invalid_read_arguments"),
        ("tasks.read", {"actor": "demo/operator"}, "invalid_read_arguments"),
        ("tasks.read", {"task_id": []}, "invalid_task_id"),
        ("tasks.read", {"cursor": "bad!"}, "invalid_cursor"),
    ):
        with pytest.raises(IdentityError, match="^" + code + "$"):
            readers[name](identity, args)
    catalog = {tool["name"]: tool for tool in tool_catalog()}
    for name in ("inbox.read", "tasks.read"):
        schema = catalog[name]["inputSchema"]
        assert schema["required"] == []
        assert schema["additionalProperties"] is False
        assert schema["properties"]["limit"]["maximum"] == 100
        assert schema["properties"]["cursor"]["maxLength"] == 1024
        assert catalog[name]["_deskd_read"] is True
    assert "task_id" in catalog["tasks.read"]["inputSchema"]["properties"]
    assert catalog["mail.send"]["inputSchema"]["required"] == ["recipient", "body"]
