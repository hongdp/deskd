"""Pure projection helpers for the terminal widgets."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from .client import DeskSnapshot


def safe_text(value: object, *, multiline: bool = False) -> str:
    """Remove terminal/bidi controls from untrusted server and agent text."""

    raw = "" if value is None else str(value)
    out = []
    for char in raw:
        if multiline and char in "\n\t":
            out.append(char)
            continue
        # Cc covers ESC/BEL/CR; Cf covers bidi isolates/overrides and other
        # invisible formatting controls that can spoof a role or status line.
        if unicodedata.category(char) in {"Cc", "Cf"}:
            continue
        out.append(char)
    return "".join(out)


def short(value: object, limit: int = 72) -> str:
    text = " ".join(safe_text(value).split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def age(iso: object, *, now: datetime | None = None) -> str:
    if not iso:
        return "—"
    try:
        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except ValueError:
        return short(iso, 20)
    seconds = max(0, int(((now or datetime.now(timezone.utc)) - then).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


def agent_rows(snapshot: DeskSnapshot) -> list[tuple[str, ...]]:
    rows = []
    for item in snapshot.board.get("agents", []):
        if not isinstance(item, Mapping):
            continue
        tasks = item.get("task_counts") or {}
        inbox = item.get("inbox") or {}
        meeting = item.get("meeting") or {}
        wake = item.get("wake") or {}
        open_tasks = sum(int(tasks.get(k) or 0)
                         for k in ("pending", "in_progress", "blocked"))
        meetings = len(meeting.get("active_meetings") or [])
        rows.append((
            str(item.get("role") or "?"),
            str(item.get("state") or "offline"),
            str(item.get("liveness") or "never"),
            short(item.get("activity") or "", 60),
            str(open_tasks),
            str(int(inbox.get("queued_count") or 0)
                + int(inbox.get("delivered_count") or 0)),
            str(meetings),
            str(wake.get("max_level") if wake.get("max_level") is not None else "—"),
            short(item.get("agent_version") or item.get("runtime_version")
                  or item.get("image_digest") or "—", 24),
            age(item.get("last_heartbeat_at")),
        ))
    return rows


def task_rows(snapshot: DeskSnapshot) -> list[tuple[str, ...]]:
    stalled = {int(i) for i in snapshot.tasks.get("stalled_ids", [])}
    rows = []
    for item in snapshot.tasks.get("tasks", []):
        if not isinstance(item, Mapping):
            continue
        task_id = int(item.get("id") or 0)
        flags = []
        if item.get("overdue"):
            flags.append("OVERDUE")
        if task_id in stalled:
            flags.append("STALLED")
        rows.append((
            str(task_id), str(item.get("assignee_role") or "?"),
            str(item.get("status") or "?"), str(item.get("priority") or "normal"),
            short(item.get("title"), 70), short(item.get("blocked_on"), 45),
            ",".join(flags) or "—", age(item.get("updated_at") or item.get("created_at")),
        ))
    return rows


def inbox_rows(snapshot: DeskSnapshot) -> list[tuple[str, ...]]:
    rows = []
    for item in snapshot.inbox:
        state = item.get("delivery_state")
        if not state:
            state = "read" if item.get("acked_at") else (
                "delivered" if item.get("delivered_at") else "queued")
        rows.append((
            str(item.get("id") or "?"), str(item.get("target_role") or "?"),
            str(item.get("priority") or "normal"), str(state),
            str(item.get("source_kind") or "?"), short(item.get("title"), 68),
            age(item.get("enqueued_at")),
        ))
    return rows


def meeting_rows(snapshot: DeskSnapshot) -> list[tuple[str, ...]]:
    rows = []
    for item in snapshot.meetings:
        status = item.get("status") if isinstance(item.get("status"), Mapping) else item
        meeting = status.get("meeting") if isinstance(status.get("meeting"), Mapping) else status
        attendees = status.get("attendees") if isinstance(status, Mapping) else []
        checked = sum(1 for a in attendees or []
                      if isinstance(a, Mapping) and a.get("checked_in_at")
                      and not a.get("stopped_at"))
        required = sum(1 for a in attendees or []
                       if isinstance(a, Mapping) and a.get("required", True))
        rows.append((
            str(meeting.get("thread_id") or item.get("thread_id") or "?"),
            str(meeting.get("state") or "?"), str(meeting.get("priority") or "normal"),
            str(meeting.get("meeting_type") or meeting.get("type") or "?"),
            f"{checked}/{required}", short(meeting.get("agenda"), 72),
            age(meeting.get("updated_at") or meeting.get("created_at")),
        ))
    return rows


def hook_rows(snapshot: DeskSnapshot) -> list[tuple[str, ...]]:
    rows = []
    for item in snapshot.hooks:
        rows.append((
            str(item.get("id") or "?"), str(item.get("owner_role") or "?"),
            str(item.get("status") or "?"), str(item.get("kind") or "?"),
            str(item.get("priority") or "normal"), short(item.get("title"), 65),
            age(item.get("next_fire_at")), short(item.get("last_error"), 42),
        ))
    return rows


def wake_rows(snapshot: DeskSnapshot) -> list[tuple[str, ...]]:
    rows = []
    for item in snapshot.wake.get("attempts", []):
        if not isinstance(item, Mapping):
            continue
        rows.append((
            str(item.get("id") or "?"), str(item.get("role") or "?"),
            str(item.get("level") if item.get("level") is not None else "—"),
            str(item.get("channel") or "?"), str(item.get("outcome") or "?"),
            short(item.get("reason") or item.get("demand_key"), 70),
            age(item.get("attempted_at") or item.get("created_at")),
        ))
    for item in snapshot.wake.get("quarantine", []):
        if not isinstance(item, Mapping):
            continue
        detail = item.get("error") or (
            f"{item.get('mode') or 'unknown'} provider outcome is indeterminate")
        rows.append((
            str(item.get("claim_id") or "?"), str(item.get("role") or "?"),
            "!", str(item.get("channel") or "?"), "QUARANTINED",
            short(detail, 70), age(item.get("claimed_at")),
        ))
    return rows


def runtime_rows(snapshot: DeskSnapshot) -> list[tuple[str, ...]]:
    sessions = snapshot.runtime.get("sessions") or {}
    rows = []
    for item in snapshot.runtime.get("roles", []):
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "?")
        session = sessions.get(role) if isinstance(sessions, Mapping) else {}
        rows.append((
            role, str(item.get("provider") or "default"),
            str(item.get("model") or "default"), str(item.get("reasoning") or "default"),
            str((session or {}).get("state") or "—"),
            str((session or {}).get("session_provider") or "—"),
            short(item.get("agent_version") or item.get("version")
                  or (session or {}).get("agent_version")
                  or (session or {}).get("image_digest") or "—", 28),
        ))
    return rows


#: Desk health as tiles rather than one run-on sentence. The third element is
#: the tone a view should use once the value is non-zero: some of these are
#: simply facts (open tasks, inbox) and some are always bad news (overdue,
#: stalled, unroutable). Rendering them all at one weight, as the single line
#: below does, hides that difference completely.
HEALTH_TILES: tuple[tuple[str, str, str], ...] = (
    ("open", "total_open_tasks", "neutral"),
    ("overdue", "total_overdue", "bad"),
    ("stalled", "stalled_tasks", "bad"),
    ("inbox", "inbox_total", "neutral"),
    ("wakes", "pending_wakes", "neutral"),
    ("rung", "wakes_at_human_level", "warn"),
    ("unroutable", "unroutable_demands", "bad"),
)


def health_metrics(snapshot: DeskSnapshot) -> list[tuple[str, int, str]]:
    """(label, value, tone) per tile; tone falls back to neutral at zero."""

    health = snapshot.board.get("health") or {}
    metrics: list[tuple[str, int, str]] = []
    for label, key, tone in HEALTH_TILES:
        raw = health.get(key, 0)
        value = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
        metrics.append((label, value, tone if value else "neutral"))
    return metrics


def health_alerts(snapshot: DeskSnapshot) -> list[str]:
    """Conditions that deserve a banner rather than a counter."""

    alerts: list[str] = []
    health = snapshot.board.get("health") or {}
    if health.get("human_rung_unwired"):
        alerts.append("human rung unwired")
    quarantine = snapshot.wake.get("quarantine") or []
    if isinstance(quarantine, list) and quarantine:
        alerts.append(f"{len(quarantine)} wake(s) quarantined")
    if not snapshot.consistent:
        alerts.append("compatibility snapshot — not atomic")
    return alerts


def health_text(snapshot: DeskSnapshot) -> str:
    health = snapshot.board.get("health") or {}
    keys = (
        ("open tasks", "total_open_tasks"), ("overdue", "total_overdue"),
        ("stalled", "stalled_tasks"), ("inbox", "inbox_total"),
        ("pending wakes", "pending_wakes"), ("human rung", "wakes_at_human_level"),
        ("unroutable", "unroutable_demands"),
    )
    parts = [f"{label}: {health.get(key, 0)}" for label, key in keys]
    if health.get("human_rung_unwired"):
        parts.append("HUMAN RUNG UNWIRED")
    quarantine = snapshot.wake.get("quarantine") or []
    if isinstance(quarantine, list) and quarantine:
        parts.append(f"WAKE QUARANTINE: {len(quarantine)}")
    if not snapshot.consistent:
        parts.append("compat snapshot (non-atomic)")
    return "  •  ".join(parts)


def transcript_lines(payload: Mapping[str, Any]) -> Iterable[str]:
    status = payload.get("status") or {}
    meeting = status.get("meeting") if isinstance(status, Mapping) else {}
    yield safe_text(
        f"{meeting.get('thread_id', 'meeting')} — {meeting.get('state', '?')} — "
        f"{meeting.get('agenda', '')}", multiline=True)
    for message in payload.get("messages", []):
        if not isinstance(message, Mapping):
            continue
        yield safe_text(
            f"[{message.get('id', '?')}] {message.get('sender', '?')} "
            f"({message.get('kind', 'message')}): {message.get('body', '')}",
            multiline=True)
    for escalation in payload.get("escalations", []):
        if isinstance(escalation, Mapping):
            yield "ESCALATION: " + short(
                escalation.get("reason") or json.dumps(escalation), 200)
