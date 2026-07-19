"""Human-facing delivery channels — the pluggable HALF of the escalation path.

The split this module enforces (roadmap P2, "the ledger is not the transport"):

- **ledger** — durable rows and receipts. Owned by the modules that write them
  (`meetings.meeting_escalations`, `orchestration.wake_escalations`). Never
  pluggable: the headline guarantee is manufactured by owning these rows, and
  no third-party chat service can replace them (a bot gets no per-message read
  proof).
- **channel** (this module) — pluggable egress: something that puts a message
  in front of a person. A channel MIRRORS a ledger row out; it never replaces
  it. Read proof comes from the ack path, never from a channel's own
  semantics.

The engine ships ZERO channel implementations by design — it knows nothing
about anyone's Discord, SMTP, or pager. A host registers what it has at
startup; `outbox` is always available as the terminal fallback, so an
escalation is never silently dropped just because nothing is configured. But
an outbox row nobody reads pulls in nobody: hosts must be able to SEE which
rungs are actually wired, which is what :func:`channel_status` exists for
(the board surfaces it).

Layering: config -> channels -> {mailbox, meetings, orchestration}. This
module holds process state (the registry) and network calls (a channel's
`send`), never database state.
"""

from __future__ import annotations

from typing import Callable

#: The always-available terminal fallback: "delivery" is the durable ledger
#: row itself, surfaced by the console. Reserved — a host cannot register it.
OUTBOX_CHANNEL = "outbox"


def _clean(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value


class EscalationChannel:
    """A destination an escalation can be delivered to.

    A host registers what it has (see `register_channel` / `CallableChannel`);
    `outbox` is always available as the terminal fallback, so an escalation is
    never silently dropped just because nothing is configured.
    """

    #: Unique channel name, as stored in the ledger row's `channel` column.
    name: str = ""

    def available(self) -> bool:
        """Is this channel currently usable? `auto` dispatch picks every
        channel that says yes. An unconfigured channel should say no rather
        than fail at send time."""
        return True

    def send(self, subject: str, text: str) -> None:
        """Deliver, or raise. Raising marks this channel failed for this
        escalation; other channels still get their turn."""
        raise NotImplementedError


class CallableChannel(EscalationChannel):
    """Adapter so a host can register a channel with a plain function.

        deskd.channels.register_channel(CallableChannel(
            "discord", send=lambda subject, text: post(text),
            available=lambda: bool(webhook_url),
        ))
    """

    def __init__(self, name: str, send: Callable[[str, str], None],
                 available: Callable[[], bool] | None = None) -> None:
        self.name = _clean(name, "channel name")
        if self.name in {"auto", OUTBOX_CHANNEL}:
            raise ValueError(f"{self.name!r} is a reserved channel name")
        self._send = send
        self._available = available

    def available(self) -> bool:
        return True if self._available is None else bool(self._available())

    def send(self, subject: str, text: str) -> None:
        self._send(subject, text)


_CHANNELS: dict[str, EscalationChannel] = {}


def register_channel(channel: EscalationChannel) -> None:
    """Register (or replace) an escalation channel. Call at host startup."""
    name = _clean(channel.name, "channel name")
    if name in {"auto", OUTBOX_CHANNEL}:
        raise ValueError(f"{name!r} is a reserved channel name")
    _CHANNELS[name] = channel


def unregister_channel(name: str) -> None:
    _CHANNELS.pop(name, None)


def registered_channels() -> tuple[str, ...]:
    return tuple(sorted(_CHANNELS))


def _channel_available(channel: EscalationChannel) -> bool:
    # A broken availability probe must not take down the dispatcher; treat it
    # as unavailable and let the outbox fallback catch the escalation.
    try:
        return bool(channel.available())
    except Exception:
        return False


def _auto_channels() -> list[str]:
    names = [n for n, c in _CHANNELS.items() if _channel_available(c)]
    return sorted(names) or [OUTBOX_CHANNEL]


def channel_status() -> list[dict]:
    """Which channels exist and whether each is usable RIGHT NOW — the
    'is the human rung actually wired' answer, for the console. The outbox is
    listed too, so an empty registration reads as 'outbox only', which is the
    state a host must be told about loudly rather than discover."""
    rows = [{"name": n, "available": _channel_available(c), "outbox": False}
            for n, c in sorted(_CHANNELS.items())]
    rows.append({"name": OUTBOX_CHANNEL, "available": True, "outbox": True})
    return rows


def human_reachable() -> bool:
    """True if at least one registered channel is currently available — i.e.
    an `auto` dispatch reaches a person rather than only the durable outbox."""
    return any(not r["outbox"] and r["available"] for r in channel_status())


def deliver(subject: str, text: str, channel: str = "auto") -> list[dict]:
    """Send through `channel` ('auto' = every available registered channel,
    outbox if none). Returns one result per channel tried:
    {"channel", "status": sent|queued|failed[, "error"]}. `queued` means the
    durable ledger row is the delivery (outbox); the caller owns that row —
    this function never touches a database."""
    names = _auto_channels() if channel == "auto" else [channel]
    results = []
    for name in names:
        try:
            if name == OUTBOX_CHANNEL:
                results.append({"channel": name, "status": "queued"})
                continue
            ch = _CHANNELS.get(name)
            if ch is None:
                raise RuntimeError(f"no such escalation channel: {name}")
            ch.send(subject, text)
            results.append({"channel": name, "status": "sent"})
        except Exception as exc:
            results.append({"channel": name, "status": "failed", "error": str(exc)})
    return results


def summarize(results: list[dict]) -> str:
    """Collapse per-channel results to a ledger status: sent beats queued
    beats failed."""
    if any(r["status"] == "sent" for r in results):
        return "sent"
    if any(r["status"] == "queued" for r in results):
        return "queued"
    return "failed"
