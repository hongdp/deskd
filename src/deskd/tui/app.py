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
from textual.binding import Binding
from textual.containers import Vertical as _Vertical  # noqa: F401
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog, Static
from textual.widgets import TabbedContent, TabPane

from .client import (ClientError, ConnectionNotice, ControlPlaneClient,
                     DeskSnapshot, SSEEvent)
from .commands import (CommandRequest, LocalAction, capabilities_from_meta,
                       command_help, command_hint, parse_command)
from .model import (HEALTH_TILES, agent_rows, health_alerts, health_metrics, health_text, hook_rows, inbox_rows, meeting_rows,
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


#: The default palette leans orange, which put a brown wash under the row
#: cursor and left every state word the same colour as ordinary text. This one
#: keeps a single cool accent for "where you are" and reserves green/amber/red
#: for what a state actually means.
DESK_THEME = Theme(
    name="deskd",
    primary="#4c8bf5",
    secondary="#7aa2f7",
    accent="#4c8bf5",
    foreground="#c9d1d9",
    background="#0d1117",
    surface="#161b22",
    panel="#1c2128",
    success="#3fb950",
    warning="#d29922",
    error="#f85149",
    dark=True,
    variables={
        "text-muted": "#8b949e",
        "scrollbar": "#30363d",
        "scrollbar-hover": "#484f58",
        "scrollbar-active": "#58a6ff",
        "scrollbar-background": "#161b22",
        "scrollbar-background-hover": "#161b22",
        "scrollbar-background-active": "#161b22",
    },
)

#: Words whose meaning should be visible before the word is read. Keyed by
#: table id and column index so only the state-bearing columns are painted --
#: colouring a title or an activity string would be noise, not signal.
_CELL_TONES: dict[tuple[str, int], dict[str, str]] = {
    ("agents", 1): {"working": "bold #4c8bf5", "booting": "bold #d29922",
                    "idle_standby": "#8b949e", "ended": "#8b949e"},
    ("agents", 2): {"online": "#3fb950", "offline": "bold #f85149",
                    "stale": "#d29922"},
    ("tasks", 2): {"blocked": "bold #f85149", "stalled": "bold #f85149",
                   "open": "#c9d1d9", "done": "#8b949e"},
    ("tasks", 3): {"high": "bold #d29922", "urgent": "bold #f85149"},
    ("inbox", 2): {"urgent": "bold #f85149", "high": "bold #d29922"},
    ("meetings", 1): {"open": "bold #3fb950", "closed": "#8b949e"},
    ("wake", 4): {"failed": "bold #f85149", "quarantined": "bold #f85149",
                  "delivered": "#3fb950"},
    ("hooks", 2): {"error": "bold #f85149", "active": "#3fb950"},
}


def _tone_for(table_id: str, column: int, value: str) -> str | None:
    return _CELL_TONES.get((table_id, column), {}).get(value.strip().lower())


#: Columns a table shows at 150 characters. The agents table carried ten and
#: was cut off mid-header while the pane beside it sat empty; the rest are not
#: lost, they open with the row. Absent here means "show every column".
_VISIBLE_COLUMNS: dict[str, tuple[int, ...]] = {
    "agents": (0, 1, 2, 3, 4, 5),
    "tasks": (0, 1, 2, 3, 4, 5, 7),
    "hooks": (0, 1, 2, 3, 5, 6),
}


#: Tab ids in strip order, so a number key and the strip cannot disagree.
_TAB_ORDER = ("overview", "tasks-tab", "inbox-tab", "meetings-tab",
              "wake-tab", "runtime-tab", "activity-tab")

#: Which table a view filters and opens details for, keyed by tab id.
_TAB_TABLES = {"overview": "agents", "tasks-tab": "tasks",
               "inbox-tab": "inbox", "meetings-tab": "meetings",
               "wake-tab": "wake", "runtime-tab": "runtime"}


def _literal_text(value: object, *, style: str | None = None) -> Text:
    """Render untrusted remote text literally, never as Rich markup/ANSI."""

    return Text(safe_text(value, multiline=True), style=style)


class DetailScreen(ModalScreen[None]):
    """One row, in full, instead of squeezed into a truncated column.

    The tables carry more than fits: a 150-column terminal cut the agents
    table off mid-header while the pane beside it sat empty. Rather than
    fight for width, a row opens here with every column on its own line.
    """

    BINDINGS = [Binding("escape", "dismiss", "Close"),
                Binding("q", "dismiss", "Close", show=False)]

    def __init__(self, title: str, fields: list[tuple[str, str]]) -> None:
        super().__init__()
        self._title = title
        self._fields = fields

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-card"):
            yield Label(_literal_text(self._title), id="detail-title")
            for name, value in self._fields:
                yield Label(Text.assemble(
                    (f"{name}  ", "dim"), _literal_text(value)))
            yield Label("esc to close", id="detail-hint")


class DeskTUI(App[None]):
    """Realtime, remote-only operator surface for a deskd control plane."""

    TITLE = "deskd"
    SUB_TITLE = "realtime multi-agent control"
    CSS = """
    Screen { layout: vertical; background: $background; }

    /* Status strip: state first, as a pill, then the facts that qualify it.
       The old line put LIVE, a URL, a cursor and a version at one weight,
       so the one word that changes meaning read like the rest. */
    #statusbar { height: 1; padding: 0 1; background: $panel; }
    #connection { width: auto; padding: 0 1; text-style: bold; }
    #connection.-live { background: $success; color: $panel; }
    #connection.-stale { background: $warning; color: $panel; }
    #connection.-error { background: $error; color: $panel; }
    #connection.-starting { background: $surface; color: $text-muted; }
    #connection-detail { width: 1fr; padding: 0 1; color: $text-muted; }

    /* Health as tiles. A number the eye can land on, a label beneath it,
       and colour only where a non-zero value is bad news. */
    #tiles { height: 4; padding: 0 1; }
    .tile {
      width: 1fr; height: 4; margin: 0 1 0 0; padding: 0 1;
      border: round $surface-lighten-1; color: $text-muted;
      content-align: center middle; text-align: center;
    }
    .tile.-warn { border: round $warning; color: $warning; }
    .tile.-bad { border: round $error; color: $error; }
    #alerts { height: auto; max-height: 2; padding: 0 2; color: $warning; }
    #alerts.-empty { display: none; }

    TabbedContent { height: 1fr; }
    Tabs { background: $panel; }
    TabPane { padding: 1 1 0 1; }

    DataTable {
      height: 1fr; border: round $surface-lighten-1; background: $surface;
      scrollbar-size: 1 1;
    }
    DataTable:focus { border: round $accent; }
    DataTable > .datatable--header {
      background: $surface; color: $text-muted; text-style: bold;
    }
    DataTable > .datatable--cursor { background: $primary 30%; }

    .split { height: 1fr; }
    .split DataTable { width: 2fr; }
    .split .side {
      width: 1fr; height: 1fr; margin: 0 0 0 1;
      border: round $surface-lighten-1; padding: 0 1; scrollbar-size: 1 1;
    }
    /* An empty pane used to be an empty box with a full-height scrollbar in
       it. Say what would appear here instead. */
    .placeholder {
      width: 1fr; height: 1fr; margin: 0 0 0 1; padding: 2 2;
      border: round $surface-lighten-1; color: $text-muted;
    }
    #wake-stack DataTable { height: 1fr; }

    #composer-help { height: 1; padding: 0 2; color: $text-muted; }
    #composer { dock: bottom; height: 3; border: round $accent; }
    #filter { dock: bottom; height: 3; border: round $warning; display: none; }
    #filter.-open { display: block; }
    #events { border: round $surface-lighten-1; scrollbar-size: 1 1; }

    /* Dim the board rather than replacing it: the row you opened should
       still be visible behind its own detail. */
    DetailScreen { align: center middle; background: $background 55%; }
    DetailScreen > #detail-card {
      width: 80%; max-width: 96; height: auto; max-height: 80%;
      border: round $accent; background: $surface; padding: 1 2;
    }
    DetailScreen #detail-title { text-style: bold; color: $accent; }
    DetailScreen #detail-hint { color: $text-muted; padding: 1 0 0 0; }
    """
    BINDINGS = [
        Binding("ctrl+l", "focus_composer", "Command"),
        Binding("slash", "open_filter", "Filter"),
        Binding("o", "open_row", "Open row"),
        Binding("question_mark", "show_help", "Help", key_display="?"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("ctrl+q", "quit", "Quit"),
        # Jumping straight to a view is what an operator does most; make it
        # one keystroke instead of walking the tab strip with arrows.
        *(Binding(str(index), f"show_tab('{pane}')", "", show=False)
          for index, pane in enumerate(_TAB_ORDER, start=1)),
        Binding("f1", "show_help", "Help", show=False),
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
        self._filter = ""
        self._full_rows: dict[tuple[str, str], tuple[str, ...]] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="statusbar"):
            yield Label("STARTING", id="connection", classes="-starting")
            yield Label("loading snapshot", id="connection-detail")
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                with Horizontal(id="tiles"):
                    for label, _key, _tone in HEALTH_TILES:
                        yield Static("—\n" + label, id=f"tile-{_key}",
                                     classes="tile")
                yield Static("", id="alerts", classes="-empty")
                with Horizontal(classes="split"):
                    yield DataTable(id="agents", cursor_type="row")
                    yield RichLog(id="agent-feed", wrap=True, markup=True,
                                  classes="side", auto_scroll=True)
            with TabPane("Tasks", id="tasks-tab"):
                yield DataTable(id="tasks", cursor_type="row")
            with TabPane("Inbox", id="inbox-tab"):
                yield DataTable(id="inbox", cursor_type="row")
            with TabPane("Meetings", id="meetings-tab"):
                with Horizontal(classes="split"):
                    yield DataTable(id="meetings", cursor_type="row")
                    yield RichLog(id="meeting-detail", wrap=True, markup=True,
                                  classes="side")
            with TabPane("Wake", id="wake-tab"):
                with Vertical(id="wake-stack"):
                    yield DataTable(id="wake", cursor_type="row")
                    yield DataTable(id="hooks", cursor_type="row")
            with TabPane("Runtime", id="runtime-tab"):
                yield DataTable(id="runtime", cursor_type="row")
            with TabPane("Activity", id="activity-tab"):
                yield RichLog(id="events", wrap=True, markup=True, highlight=True)
        yield Label(command_hint(allowed_verbs=()), id="composer-help")
        yield Input(placeholder="filter rows — Esc to clear", id="filter")
        yield Input(placeholder="@role instruction or /command", id="composer")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(DESK_THEME)
        self.theme = "deskd"
        # A pane that is empty because nothing has happened yet looks exactly
        # like a pane that is broken. Say which one it is.
        for line in ("Command results and stream",
                     "notices appear here.",
                     "",
                     "enter  load agent feed",
                     "o      open row in full",
                     "/      filter rows",
                     "^p     command palette"):
            self.query_one("#agent-feed", RichLog).write(f"[dim]{line}[/dim]")
        self.query_one("#meeting-detail", RichLog).write(
            "[dim]Select a meeting to read its transcript.[/dim]")
        for table_id, (columns, _) in _TABLES.items():
            visible = _VISIBLE_COLUMNS.get(table_id)
            shown = ([columns[i] for i in visible] if visible else list(columns))
            self.query_one(f"#{table_id}", DataTable).add_columns(*shown)
        # Reading the board is the primary activity and typing a command is
        # the occasional one, but focus started in the composer -- so Enter,
        # the most obvious key on a highlighted row, submitted an empty
        # command instead of opening the row. ^L still reaches the composer.
        self.query_one("#agents", DataTable).focus()
        self.set_interval(1.0, self._update_connection)
        self.set_interval(self.refresh_seconds, self.action_refresh)
        self.action_refresh()

    def on_unmount(self) -> None:
        self._stop_stream.set()

    def action_focus_composer(self) -> None:
        self.query_one("#composer", Input).focus()

    # -- navigation and filtering -------------------------------------------
    #
    # The old surface offered exactly one way to reach anything: walk the tab
    # strip, then scroll. With seven views and tables that grow without bound,
    # that is the whole interaction budget spent on getting to the row.

    def action_show_tab(self, pane: str) -> None:
        self.query_one(TabbedContent).active = pane
        table = _TAB_TABLES.get(pane)
        if table is not None:
            try:
                self.query_one(f"#{table}", DataTable).focus()
            except NoMatches:
                pass

    def _active_table(self) -> DataTable | None:
        table_id = _TAB_TABLES.get(self.query_one(TabbedContent).active)
        if table_id is None:
            return None
        try:
            return self.query_one(f"#{table_id}", DataTable)
        except NoMatches:
            return None

    def action_open_filter(self) -> None:
        if self._active_table() is None:
            self.notify("This view has no table to filter.", severity="warning")
            return
        field = self.query_one("#filter", Input)
        field.add_class("-open")
        field.focus()

    def _close_filter(self) -> None:
        field = self.query_one("#filter", Input)
        field.value = ""
        field.remove_class("-open")
        self._filter = ""
        if self.snapshot is not None:
            self._render(self.snapshot)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._filter = event.value.strip().lower()
            if self.snapshot is not None:
                self._render(self.snapshot)

    def action_open_row(self) -> None:
        """Open the highlighted row in full.

        Enter already means something on two tables -- it loads the agent
        session feed and the meeting transcript into their side panes -- so
        this takes its own key rather than quietly replacing that. On the
        tables with no side pane it is the only way to see the columns the
        150-column layout cannot show.
        """
        table = self._active_table()
        if table is None or table.row_count == 0:
            self.notify("No row to open here.", severity="warning")
            return
        table_id = table.id or ""
        columns = _TABLES.get(table_id, ((), None))[0]
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return
        values = self._full_rows.get((table_id, str(row_key.value)))
        if values is None:
            try:
                values = tuple(str(cell) for cell in table.get_row(row_key))
            except Exception:
                return
        fields = [(name, str(value)) for name, value
                  in zip(columns, values, strict=False)]
        if not fields:
            return
        self.push_screen(DetailScreen(
            f"{table_id.title()} · {fields[0][1]}", fields))

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
        # health_metrics preserves HEALTH_TILES order, so the pairing is
        # positional rather than a lookup by display label.
        for (label, value, tone), (_, key, _tone) in zip(
                health_metrics(snapshot), HEALTH_TILES, strict=True):
            tile = self.query_one(f"#tile-{key}", Static)
            tile.set_classes(f"tile -{tone}" if tone != "neutral" else "tile")
            tile.update(Text.assemble(
                (f"{value}", "bold"), ("\n", ""), (label, "dim")))
        alerts = health_alerts(snapshot)
        banner = self.query_one("#alerts", Static)
        banner.update("  ".join(f"▲ {alert}" for alert in alerts))
        banner.set_classes("" if alerts else "-empty")
        for table_id, (_, row_builder) in _TABLES.items():
            table = self.query_one(f"#{table_id}", DataTable)
            table.clear()
            rows = row_builder(snapshot)
            if self._filter and table_id == _TAB_TABLES.get(
                    self.query_one(TabbedContent).active):
                rows = [row for row in rows
                        if self._filter in " ".join(map(str, row)).lower()]
            for row in rows:
                row_key = row[0] if table_id in {"agents", "tasks", "inbox", "meetings",
                                                     "hooks", "wake", "runtime"} else None
                visible = _VISIBLE_COLUMNS.get(table_id)
                if row_key is not None:
                    # Keep every column for the detail view, including the ones
                    # the table cannot afford to show.
                    self._full_rows[(table_id, str(row_key))] = tuple(
                        str(cell) for cell in row)
                cells = tuple(
                    _literal_text(cell,
                                  style=_tone_for(table_id, index, str(cell)))
                    for index, cell in enumerate(row)
                    if visible is None or index in visible)
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
        # State is the one word here that changes what an operator should do,
        # so it gets a pill of its own; the URL, cursor and version are what
        # qualify it and belong in the muted half. The old line set all four
        # at the same weight, which is why none of them read as important.
        if elapsed > self.stale_after:
            age = int(elapsed) if elapsed != float("inf") else "?"
            state, tone = f"STALE {age}s", "-stale"
            detail = self._connection_message
        elif self._connection_state == "connected":
            state, tone = "LIVE", "-live"
            detail = str(self.client.base_url)
        elif self._connection_state in {"reconnecting", "connecting"}:
            state, tone = self._connection_state.upper(), "-stale"
            detail = self._connection_message
        elif self._connection_state == "error":
            state, tone = "ERROR", "-error"
            detail = self._connection_message
        else:
            state, tone = self._connection_state.upper(), "-starting"
            detail = self._connection_message
        label.update(_literal_text(state))
        label.set_classes(tone)
        version = self.snapshot.server_version if self.snapshot else None
        trailing = "   ".join(part for part in (
            detail,
            f"server {version}" if version else "",
            f"cursor {self.cursor}" if self.cursor else "") if part)
        try:
            self.query_one("#connection-detail", Label).update(
                _literal_text(trailing))
        except NoMatches:
            return

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
