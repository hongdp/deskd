from __future__ import annotations

import asyncio

import pytest

textual = pytest.importorskip("textual", reason="tui extra not installed")

from textual.widgets import DataTable, Input  # noqa: E402

from deskd.tui.app import DeskTUI  # noqa: E402
from deskd.tui.app import _literal_text  # noqa: E402
from deskd.tui.client import ConnectionNotice, DeskSnapshot, SSEEvent  # noqa: E402


class FakeClient:
    base_url = "https://deskd.example"

    def __init__(self):
        self.commands = []
        self.stream_cursors = []
        self.snapshot_count = 0
        self.meta = {}

    def fetch_snapshot(self):
        self.snapshot_count += 1
        cursor = "evt_0001" if self.snapshot_count == 1 else "evt_0002"
        return DeskSnapshot.from_payload({
            "cursor": cursor, "server_version": "0.4.0",
            "board": {"health": {}, "agents": [{
                "role": "engineer", "state": "working", "liveness": "online",
                "activity": "testing terminal", "task_counts": {}, "inbox": {},
                "meeting": {}, "wake": {},
            }]},
            "tasks": {"tasks": [], "stalled_ids": []}, "inbox": [],
            "meetings": [], "hooks": [], "wake": {"attempts": []},
            "runtime": {"roles": [{"role": "engineer", "provider": "codex"}]},
            "meta": self.meta,
        }, consistent=True)

    def follow_events(self, stop, *, cursor=None):
        self.stream_cursors.append(cursor)
        yield ConnectionNotice("connected", "connected", cursor=cursor)
        yield SSEEvent("task.changed", {"id": 7}, id="evt_0002")
        stop.wait()

    def submit_command(self, verb, params):
        self.commands.append((verb, params))
        return {"request_id": "req-1", "accepted": True}


def test_app_mounts_renders_and_fast_composer_submits_without_waiting_for_sse():
    async def scenario():
        client = FakeClient()
        app = DeskTUI(client, stale_after=20, refresh_seconds=30)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause(0.3)
            assert client.stream_cursors == ["evt_0001"]
            assert app.cursor == "evt_0002"
            assert app.query_one("#agents", DataTable).row_count == 1
            composer = app.query_one("#composer", Input)
            composer.focus()
            composer.value = "@engineer ship the terminal"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert client.commands == [(
                "directive.send",
                {"target_role": "engineer", "body": "ship the terminal",
                 "priority": "urgent"},
            )]
        assert app._stop_stream.is_set()

    asyncio.run(scenario())


def test_app_uses_snapshot_capabilities_for_role_mail():
    async def scenario():
        client = FakeClient()
        client.meta = {
            "role": "engineer",
            "allowed_verbs": ["message.send", "meeting.send", "hook.add"],
        }
        app = DeskTUI(client, stale_after=20, refresh_seconds=30)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause(0.3)
            composer = app.query_one("#composer", Input)
            composer.focus()
            composer.value = "@operator inspect the alert"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert client.commands == [(
                "message.send",
                {"target_role": "operator",
                 "subject": "deskd TUI: inspect the alert",
                 "body": "inspect the alert", "kind": "note",
                 "priority": "urgent"},
            )]

    asyncio.run(scenario())


def test_legacy_projection_mode_polls_without_sse_or_remote_composer():
    class LegacyClient(FakeClient):
        def fetch_snapshot(self):
            snapshot = super().fetch_snapshot()
            object.__setattr__(snapshot, "consistent", False)
            object.__setattr__(snapshot, "cursor", None)
            return snapshot

        def follow_events(self, stop, *, cursor=None):
            raise AssertionError("compatibility mode must not start an SSE follower")
            yield  # pragma: no cover - keep this a generator

    async def scenario():
        client = LegacyClient()
        app = DeskTUI(client, stale_after=20, refresh_seconds=30)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause(0.3)
            assert app._connection_state == "polling"
            assert client.stream_cursors == []
            assert app._composer_capabilities() == (frozenset(), None)
            composer = app.query_one("#composer", Input)
            composer.focus()
            composer.value = "@engineer should not dispatch"
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert client.commands == []

    asyncio.run(scenario())


def test_cursor_expiry_fetches_snapshot_before_starting_a_new_follower():
    class ExpiringClient(FakeClient):
        def fetch_snapshot(self):
            snap = super().fetch_snapshot()
            cursor = "evt_old" if self.snapshot_count == 1 else "evt_new"
            object.__setattr__(snap, "cursor", cursor)
            return snap

        def follow_events(self, stop, *, cursor=None):
            self.stream_cursors.append(cursor)
            if len(self.stream_cursors) == 1:
                yield ConnectionNotice("cursor_expired", "cursor expired")
                return
            yield ConnectionNotice("connected", "connected", cursor=cursor)
            stop.wait()

    async def scenario():
        client = ExpiringClient()
        app = DeskTUI(client, stale_after=20, refresh_seconds=30)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause(0.6)
            assert client.stream_cursors[:2] == ["evt_old", "evt_new"]
            assert client.snapshot_count >= 2

    asyncio.run(scenario())


def test_untrusted_rich_markup_is_literal_and_controls_are_removed():
    rendered = _literal_text("[bold red]fake outage[/]\x1b[2J\u202elive")
    assert rendered.plain == "[bold red]fake outage[/][2Jlive"
    assert rendered.spans == []
