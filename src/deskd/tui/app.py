"""Textual application for ``deskd tui``.

Imported lazily by the CLI so the engine and every non-TUI command remain
usable without the optional ``tui`` dependency.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog, Static
from textual.widgets import TabbedContent, TabPane

from .client import (ClientError, ConnectionNotice, ControlPlaneClient,
                     DeskSnapshot, SSEEvent)
from .commands import (CommandRequest, LocalAction, capabilities_from_meta,
                       command_help, command_hint, parse_command)
from .model import (agent_rows, health_text, hook_rows, inbox_rows, meeting_rows,
                    runtime_rows, safe_text, task_rows, transcript_lines, wake_rows)


_TABLES: dict[str, tuple[tuple[str, ...], Callable[[DeskSnapshot], list[tuple[str, ...]]]]] = {
    "agents": (("Role", "State", "Liveness", "Current activity", "Tasks", "Inbox",
                "Meetings", "Wake L", "Version", "Heartbeat"), agent_rows),
    "tasks": (("ID", "Role", "Status", "Priority", "Title", "Blocked on", "Flags", "Age"),
              task_rows),
    "inbox": (("ID", "Role", "Priority", "State", "Source", "Title", "Age"),
              inbox_rows),
    "meetings": (("ID", "State", "Priority", "Type", "Here", "Agenda", "Age"),
                  meeting_rows),
    "hooks": (("ID", "Role", "Status", "Kind", "Priority", "Title", "Next", "Error"),
              hook_rows),
    "wake": (("ID", "Role", "Level", "Channel", "Outcome", "Reason", "Age"),
             wake_rows),
    "runtime": (("Role", "Provider", "Model", "Reasoning", "State", "Session provider",
                 "Build"),
                runtime_rows),
}


def _literal_text(value: object, *, style: str | None = None) -> Text:
    """Render untrusted remote text literally, never as Rich markup/ANSI."""

    return Text(safe_text(value, multiline=True), style=style)


class DeskTUI(App[None]):
    """Realtime, remote-only operator surface for a deskd control plane."""

    TITLE = "deskd"
    SUB_TITLE = "realtime multi-agent control"
    CSS = """
    Screen { layout: vertical; }
    #connection { height: 1; padding: 0 1; background: $panel; }
    #health { height: 2; padding: 0 1; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0 1; }
    DataTable { height: 1fr; border: round $primary-darken-2; }
    .split { height: 1fr; }
    .split DataTable { width: 2fr; }
    .split RichLog { width: 1fr; border: round $primary-darken-2; padding: 0 1; }
    #wake-stack DataTable { height: 1fr; }
    #composer-help { height: 1; padding: 0 1; color: $text-muted; }
    #composer { dock: bottom; height: 3; border: tall $accent; }
    #events { border: round $primary-darken-2; }
    """
    BINDINGS = [
        ("ctrl+r", "refresh", "Refresh"),
        ("ctrl+l", "focus_composer", "Command"),
        ("f1", "show_help", "Help"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, client: ControlPlaneClient, *, default_role: str | None = None,
                 stale_after: float = 20.0, refresh_seconds: float = 30.0):
        super().__init__()
        self.client = client
        self.default_role = default_role
        self.stale_after = max(stale_after, 2.0)
        self.refresh_seconds = max(refresh_seconds, 5.0)
        self.snapshot: DeskSnapshot | None = None
        self.cursor: str | None = None
        self._stop_stream = threading.Event()
        self._stream_thread: threading.Thread | None = None
        self._stream_generation = 0
        self._stream_needs_snapshot = True
        self._last_contact = 0.0
        self._connection_state = "starting"
        self._connection_message = "loading initial snapshot"
        self._refresh_scheduled = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("STARTING — loading snapshot", id="connection")
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                yield Static("", id="health")
                with Horizontal(classes="split"):
                    yield DataTable(id="agents", cursor_type="row", zebra_stripes=True)
                    yield RichLog(id="agent-feed", wrap=True, markup=True)
            with TabPane("Tasks", id="tasks-tab"):
                yield DataTable(id="tasks", cursor_type="row", zebra_stripes=True)
            with TabPane("Inbox / mail", id="inbox-tab"):
                yield DataTable(id="inbox", cursor_type="row", zebra_stripes=True)
            with TabPane("Meetings", id="meetings-tab"):
                with Horizontal(classes="split"):
                    yield DataTable(id="meetings", cursor_type="row", zebra_stripes=True)
                    yield RichLog(id="meeting-detail", wrap=True, markup=True)
            with TabPane("Wake / hooks", id="wake-tab"):
                with Vertical(id="wake-stack"):
                    yield DataTable(id="wake", cursor_type="row", zebra_stripes=True)
                    yield DataTable(id="hooks", cursor_type="row", zebra_stripes=True)
            with TabPane("Runtime", id="runtime-tab"):
                yield DataTable(id="runtime", cursor_type="row", zebra_stripes=True)
            with TabPane("Activity", id="activity-tab"):
                yield RichLog(id="events", wrap=True, markup=True, highlight=True)
        yield Label(command_hint(allowed_verbs=()), id="composer-help")
        yield Input(placeholder="@role instruction or /command", id="composer")
        yield Footer()

    def on_mount(self) -> None:
        for table_id, (columns, _) in _TABLES.items():
            self.query_one(f"#{table_id}", DataTable).add_columns(*columns)
        self.set_interval(1.0, self._update_connection)
        self.set_interval(self.refresh_seconds, self.action_refresh)
        self.action_refresh()

    def on_unmount(self) -> None:
        self._stop_stream.set()

    def action_focus_composer(self) -> None:
        self.query_one("#composer", Input).focus()

    def _composer_capabilities(self) -> tuple[frozenset[str] | None, str | None]:
        if self.snapshot is None or not self.snapshot.consistent:
            # Do not offer remote commands before authority is known, or while
            # using projection-only compatibility endpoints with no command
            # contract. Local /help, /refresh and /quit remain available.
            return frozenset(), None
        return capabilities_from_meta(self.snapshot.meta)

    def action_refresh(self) -> None:
        if self._refresh_scheduled:
            return
        self._refresh_scheduled = True
        self.run_worker(self._refresh(), group="snapshot", exclusive=True)

    def action_show_help(self) -> None:
        allowed, principal_role = self._composer_capabilities()
        log = self.query_one("#events", RichLog)
        log.clear()
        log.write(command_help(
            allowed_verbs=allowed, principal_role=principal_role))
        self.query_one(TabbedContent).active = "activity-tab"

    async def _refresh(self) -> None:
        try:
            snapshot = await asyncio.to_thread(self.client.fetch_snapshot)
        except Exception as exc:
            self._connection_state = "error"
            self._connection_message = safe_text(exc)
            self._log(f"snapshot failed: {safe_text(exc)}", style="red")
        else:
            self.snapshot = snapshot
            self.cursor = snapshot.cursor if snapshot.consistent else None
            self._last_contact = time.monotonic()
            try:
                self._render(snapshot)
            except NoMatches:
                # Same window as the connection tick, entered from the other
                # side: this runs in a worker, so teardown can remove the
                # widgets while a snapshot is still in flight, and there is
                # nothing left to draw on. A genuinely wrong widget id does not
                # hide here -- every id _render touches is asserted by the
                # mount tests, which fail loudly instead.
                return
            if (snapshot.consistent and snapshot.cursor
                    and (self._stream_thread is None
                         or not self._stream_thread.is_alive())):
                self._start_stream(snapshot.cursor)
                self._stream_needs_snapshot = False
            elif not snapshot.consistent or not snapshot.cursor:
                self._stream_needs_snapshot = False
                self._connection_state = "polling"
                self._connection_message = (
                    "compatibility projections; realtime events unavailable, "
                    f"polling every {self.refresh_seconds:g}s")
        finally:
            self._refresh_scheduled = False
            self._update_connection()

    def _render(self, snapshot: DeskSnapshot) -> None:
        allowed, principal_role = self._composer_capabilities()
        self.query_one("#composer-help", Label).update(command_hint(
            allowed_verbs=allowed, principal_role=principal_role))
        self.query_one("#health", Static).update(health_text(snapshot))
        for table_id, (_, row_builder) in _TABLES.items():
            table = self.query_one(f"#{table_id}", DataTable)
            table.clear()
            rows = row_builder(snapshot)
            for row in rows:
                row_key = row[0] if table_id in {"agents", "tasks", "inbox", "meetings",
                                                     "hooks", "wake", "runtime"} else None
                cells = tuple(_literal_text(cell) for cell in row)
                try:
                    table.add_row(*cells, key=row_key)
                except Exception:
                    # Duplicate IDs from a malformed compatibility projection
                    # must not take down every panel. The row remains visible.
                    table.add_row(*cells)

    def _start_stream(self, cursor: str | None) -> None:
        if self._stop_stream.is_set():
            return
        if self._stream_thread is not None and self._stream_thread.is_alive():
            return
        self._stream_generation += 1
        generation = self._stream_generation
        self._stream_thread = threading.Thread(
            target=self._stream_loop, args=(generation, cursor),
            name="deskd-tui-sse", daemon=True)
        self._stream_thread.start()

    def _stream_loop(self, generation: int, cursor: str | None) -> None:
        try:
            for item in self.client.follow_events(
                    self._stop_stream, cursor=cursor):
                if self._stop_stream.is_set():
                    return
                self.call_from_thread(self._receive_stream_item, item)
        except Exception as exc:
            if not self._stop_stream.is_set():
                self.call_from_thread(
                    self._receive_stream_item,
                    ConnectionNotice("error", f"event follower stopped: {exc}"))
        finally:
            if not self._stop_stream.is_set():
                self.call_from_thread(self._stream_finished, generation)

    def _stream_finished(self, generation: int) -> None:
        if generation != self._stream_generation:
            return
        self._stream_thread = None
        # cursor_expired may race the tail end of initial bootstrap. Ensure a
        # NEW snapshot starts after any in-flight refresh releases its worker;
        # never reconnect this follower from an empty cursor.
        self._stream_needs_snapshot = True
        self._ensure_stream_boundary()

    def _ensure_stream_boundary(self) -> None:
        if self._stop_stream.is_set() or not self._stream_needs_snapshot:
            return
        if self._refresh_scheduled:
            self.set_timer(0.05, self._ensure_stream_boundary)
            return
        self.action_refresh()

    def _receive_stream_item(self, item: SSEEvent | ConnectionNotice) -> None:
        if isinstance(item, ConnectionNotice):
            self._connection_state = item.state
            self._connection_message = item.message
            if item.state == "connected":
                self._last_contact = time.monotonic()
            if item.cursor:
                self.cursor = item.cursor
            if item.state in {"cursor_expired", "reconnecting", "error"}:
                self._log(f"{item.state}: {safe_text(item.message)}", style="yellow")
            if item.state == "cursor_expired":
                self.cursor = None
                self._stream_needs_snapshot = True
                self._ensure_stream_boundary()
        else:
            self._last_contact = time.monotonic()
            if item.id:
                self.cursor = item.id
            if item.event != "heartbeat":
                self._log(f"{safe_text(item.event)} {safe_text(item.id or '')} "
                          f"{safe_text(item.data)}", style="cyan")
                # Events are invalidations. Coalesce a burst into one immediate
                # projection refresh; durable state is read from the API.
                if not self._refresh_scheduled:
                    self.set_timer(0.12, self.action_refresh)
            self._connection_state = "connected"
            self._connection_message = "event stream connected"
        self._update_connection()

    def _update_connection(self) -> None:
        # This runs on a one-second interval, so it can fire in the two windows
        # where the label is not in the DOM: before compose finishes, and after
        # unmount begins. query_one raises there, and an exception inside a
        # timer tick takes the whole app down -- on exit, in ordinary use.
        try:
            label = self.query_one("#connection", Label)
        except NoMatches:
            return
        elapsed = time.monotonic() - self._last_contact if self._last_contact else float("inf")
        version = self.snapshot.server_version if self.snapshot else None
        suffix = f"  server {version}" if version else ""
        cursor = f"  cursor {self.cursor}" if self.cursor else ""
        if elapsed > self.stale_after:
            detail = (f" {int(elapsed) if elapsed != float('inf') else '?'}s  "
                      f"{self._connection_message}{cursor}{suffix}")
            label.update(Text.assemble(("STALE", "bold yellow"), _literal_text(detail)))
        elif self._connection_state == "connected":
            label.update(Text.assemble(("LIVE", "bold green"),
                                       _literal_text(f"  {self.client.base_url}{cursor}{suffix}")))
        elif self._connection_state in {"reconnecting", "connecting"}:
            label.update(Text.assemble(
                (self._connection_state.upper(), "bold yellow"),
                _literal_text(f"  {self._connection_message}{cursor}{suffix}")))
        elif self._connection_state == "error":
            label.update(Text.assemble(("ERROR", "bold red"),
                                       _literal_text(f"  {self._connection_message}{suffix}")))
        else:
            label.update(Text.assemble(
                (self._connection_state.upper(), "bold cyan"),
                _literal_text(f"  {self._connection_message}{suffix}")))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.clear()
        try:
            allowed, principal_role = self._composer_capabilities()
            parsed = parse_command(
                event.value, default_role=self.default_role,
                allowed_verbs=allowed, principal_role=principal_role)
            if isinstance(parsed, LocalAction):
                if parsed.action == "help":
                    self.action_show_help()
                elif parsed.action == "refresh":
                    self.action_refresh()
                elif parsed.action == "quit":
                    self.exit()
                return
            await self._submit(parsed)
        except ClientError as exc:
            self.notify(safe_text(exc), severity="error", timeout=6)
            self._log(f"rejected: {safe_text(exc)}", style="red")
        except Exception as exc:
            self.notify(safe_text(exc), severity="error", timeout=6)
            self._log(f"command failed: {safe_text(exc)}", style="red")

    async def _submit(self, command: CommandRequest) -> None:
        self._log(f"submit: {safe_text(command.summary)}", style="bold")
        answer = await asyncio.to_thread(
            self.client.submit_command, command.verb, command.params)
        request_id = answer.get("request_id") or answer.get("id") or "accepted"
        self.notify(f"{command.summary}: {request_id}", severity="information", timeout=4)
        self._log(f"accepted: {safe_text(request_id)} {safe_text(answer)}", style="green")
        # Do not wait for SSE to acknowledge the command in the UI. The 202 is
        # enough to refresh now; SSE independently proves the state transition.
        self.action_refresh()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        if event.data_table.id == "meetings":
            log = self.query_one("#meeting-detail", RichLog)
            log.clear()
            log.write(_literal_text(f"Loading {key}…"))
            try:
                detail = await asyncio.to_thread(self.client.fetch_meeting, key)
            except Exception as exc:
                log.write(_literal_text(f"Unable to load meeting: {exc}", style="red"))
            else:
                log.clear()
                for line in transcript_lines(detail):
                    log.write(_literal_text(line))
        elif event.data_table.id == "agents":
            log = self.query_one("#agent-feed", RichLog)
            log.clear()
            log.write(_literal_text(f"Loading {key} session feed…"))
            try:
                feed = await asyncio.to_thread(self.client.fetch_agent_feed, key)
            except Exception as exc:
                log.write(_literal_text(f"Unable to load feed: {exc}", style="red"))
            else:
                log.clear()
                raw_lines = feed.get("lines", [])
                lines: Iterable[dict[str, Any]] = (
                    raw_lines if isinstance(raw_lines, list) else [])
                for line in lines:
                    if not isinstance(line, dict):
                        continue
                    log.write(_literal_text(
                        f"[{line.get('seq', '?')}] {line.get('kind', '?')}: "
                        f"{line.get('text') or '…'}"))

    def _log(self, message: str, *, style: str | None = None) -> None:
        try:
            self.query_one("#events", RichLog).write(_literal_text(message, style=style))
        except Exception:
            pass


def run_tui(client: ControlPlaneClient, *, default_role: str | None = None,
            stale_after: float = 20.0, refresh_seconds: float = 30.0) -> None:
    DeskTUI(client, default_role=default_role, stale_after=stale_after,
            refresh_seconds=refresh_seconds).run()
