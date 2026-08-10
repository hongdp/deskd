"""The unified inbox: one queue per role, deduped, prioritised, batched.

The inbox is the host's public ingress (`inbox_enqueue`) and the engine's only
notification queue. Everything here guards a property the engine promises to a
host that it cannot see: that a re-firing watcher doesn't pile up, that a custom
source kind works, and that a quiet notification doesn't manufacture an
interrupt while an urgent one isn't held behind a batch window.
"""

from __future__ import annotations

import pytest

from conftest import iso, scalar
from deskd import config as cfg_mod
from deskd import orchestration as o
from deskd.config import CONFIG


# --- dedup ------------------------------------------------------------------

def test_dedup_key_does_not_pile_up_while_unacked(desk):
    """A watcher that re-fires every tick must not fill the queue with copies.

    The partial unique index is scoped (target_role, dedup_key), so the second
    assert pins that scope: dedup must not leak ACROSS roles, or one role's
    pending alert would swallow another role's identical one.
    """
    first = o.inbox_enqueue("alpha", "alert", "vol spike AAA", dedup_key="vol:AAA")
    assert first is not None

    again = o.inbox_enqueue("alpha", "alert", "vol spike AAA (refire)",
                            dedup_key="vol:AAA")
    assert again is None, "re-firing the same dedup key must be a no-op"
    assert [r["id"] for r in o.inbox_pending("alpha")] == [first]

    # Same key, different role: a different item entirely.
    other = o.inbox_enqueue("beta", "alert", "vol spike AAA", dedup_key="vol:AAA")
    assert other is not None
    assert [r["id"] for r in o.inbox_pending("beta")] == [other]


def test_dedup_key_is_allowed_again_after_ack(desk):
    """The other half, and the one that matters most: an alert that fires,
    gets handled, and fires AGAIN is new news and must be able to wake you a
    second time. A dedup index without the `acked_at IS NULL` clause would
    suppress it forever after the first ack.
    """
    first = o.inbox_enqueue("alpha", "alert", "vol spike AAA", dedup_key="vol:AAA")
    assert o.inbox_ack("alpha", ids=[first]) == 1

    second = o.inbox_enqueue("alpha", "alert", "vol spike AAA", dedup_key="vol:AAA")
    assert second is not None, "an acked key must be re-enqueueable"
    assert second != first
    assert [r["id"] for r in o.inbox_pending("alpha")] == [second]


# --- source_kind validation + the host extension seam ------------------------

def test_unknown_source_kind_is_rejected(desk):
    """source_kind is validated in Python (no DDL CHECK), so the validation has
    to actually happen here or the column becomes free text."""
    with pytest.raises(ValueError, match="source_kind"):
        o.inbox_enqueue("alpha", "not_a_real_kind", "should not land")
    assert o.inbox_pending("alpha") == []


def test_host_can_extend_inbox_sources(desk):
    """The extension seam: a host injects its own domain events by widening
    CONFIG.inbox_sources. If the engine only accepted its built-in kinds, every
    host would have to lie about what its notifications are.
    """
    assert "risk_breach" not in CONFIG.inbox_sources
    cfg_mod.configure(inbox_sources=CONFIG.inbox_sources + ("risk_breach",))

    item = o.inbox_enqueue("alpha", "risk_breach", "gross exposure over limit")
    assert item is not None
    assert o.inbox_pending("alpha")[0]["source_kind"] == "risk_breach"


# --- ordering ---------------------------------------------------------------

def test_priority_orders_urgent_then_normal_then_low(desk):
    """Enqueued in the WRONG order on purpose: if the queue were returned in
    insertion order this would pass by accident."""
    low = o.inbox_enqueue("alpha", "signal", "fyi", priority="low")
    normal = o.inbox_enqueue("alpha", "signal", "worth a look", priority="normal")
    urgent = o.inbox_enqueue("alpha", "alert", "act now", priority="urgent")

    assert [r["id"] for r in o.inbox_pending("alpha")] == [urgent, normal, low]


# --- batching ---------------------------------------------------------------

def test_non_urgent_items_coalesce_before_they_wake_anyone(conn):
    """Quiet news must not manufacture an interrupt the moment it lands: it
    coalesces for inbox_batch_seconds so one wake carries the whole batch.
    Timestamps are injected, never slept on.
    """
    o._inbox_insert(conn, "alpha", "signal", "quiet news", priority="normal")

    fresh = [d for d in o.collect_wake_demand(conn) if d["reason_kind"] == "inbox"]
    assert fresh == [], "a just-enqueued non-urgent item must not wake anyone yet"

    # Age it past the batch window rather than sleeping through it.
    conn.execute("UPDATE agent_inbox SET enqueued_at=? WHERE target_role='alpha'",
                 (iso(-(CONFIG.inbox_batch_seconds + 10)),))

    aged = [d for d in o.collect_wake_demand(conn) if d["reason_kind"] == "inbox"]
    assert [d["role"] for d in aged] == ["alpha"]


def test_urgent_bypasses_the_batch_window_entirely(conn):
    """An urgent item is the one thing that must not wait out the coalescing
    window — that window is exactly the latency an urgent alert cannot afford.
    """
    o._inbox_insert(conn, "beta", "alert", "spike", priority="urgent")

    demands = [d for d in o.collect_wake_demand(conn) if d["reason_kind"] == "inbox"]
    assert [d["role"] for d in demands] == ["beta"]


# --- ack --------------------------------------------------------------------

def test_inbox_ack_by_explicit_ids_acks_only_those(desk):
    one = o.inbox_enqueue("alpha", "signal", "first")
    two = o.inbox_enqueue("alpha", "signal", "second")

    assert o.inbox_ack("alpha", ids=[one]) == 1
    assert [r["id"] for r in o.inbox_pending("alpha")] == [two]


def test_inbox_ack_is_idempotent(desk):
    """Wake semantics are at-least-once, so an agent CAN see the same batch
    twice and ack it twice. The second ack must be a no-op, not a re-stamp.
    """
    item = o.inbox_enqueue("alpha", "signal", "handled once")
    assert o.inbox_ack("alpha", ids=[item]) == 1

    with o.connect() as c:
        stamped = scalar(c, "SELECT acked_at FROM agent_inbox WHERE id=?", (item,))

    assert o.inbox_ack("alpha", ids=[item]) == 0, "re-acking must not count again"
    with o.connect() as c:
        assert scalar(c, "SELECT acked_at FROM agent_inbox WHERE id=?",
                      (item,)) == stamped


# --- reading a queue is not delivering it ------------------------------------
# `desk-inbox list` has to make a following `ack` work, or an agent that reads
# a notice and acks in the same breath gets {"acked": 0} and cannot tell that
# from "nothing was waiting". Marking the listed items delivered achieves that
# and costs too much: delivery is what takes an item off the wake path, so a
# session that dies before acking silences exactly what it was just shown.

def test_noting_a_read_settles_what_was_already_queued(desk):
    o.inbox_enqueue("alpha", "system", "was on screen")
    o.inbox_note_listed("alpha")

    assert o.inbox_ack("alpha") == 1
    assert o.inbox_pending("alpha") == []


def test_noting_a_read_leaves_the_items_on_the_wake_path(desk):
    """The whole reason for recording the read against the role instead of the
    items: an unacked notice must still be able to wake somebody."""
    o.inbox_enqueue("alpha", "system", "still owed")
    o.inbox_note_listed("alpha")

    (item,) = o.inbox_pending("alpha")
    assert item["delivered_at"] is None, (
        "listing must not deliver — a delivered item stops raising wakes, so a "
        "session that dies before acking would silence it for good")


def test_an_item_that_arrived_after_the_read_survives_a_blanket_ack(desk):
    """The protection that was there before and must not be traded away."""
    o.inbox_enqueue("alpha", "system", "seen")
    o.inbox_note_listed("alpha")
    o.inbox_enqueue("alpha", "system", "arrived afterwards")

    assert o.inbox_ack("alpha") == 1
    assert [i["title"] for i in o.inbox_pending("alpha")] == ["arrived afterwards"]


def test_delivery_still_settles_on_its_own(desk):
    """A role that never listed anything is unaffected: the in-session hook
    delivers, and delivered items ack exactly as they always did."""
    o.inbox_enqueue("alpha", "system", "hook delivered this")
    o.inbox_mark_delivered([i["id"] for i in o.inbox_pending("alpha")])

    assert o.inbox_ack("alpha") == 1


def test_a_read_by_one_role_does_not_settle_anothers_queue(desk):
    o.inbox_enqueue("beta", "system", "beta's own")
    o.inbox_note_listed("alpha")

    assert o.inbox_ack("beta") == 0
    assert len(o.inbox_pending("beta")) == 1


def test_an_older_reads_table_is_migrated_rather_than_ignored(tmp_path):
    """CREATE TABLE IF NOT EXISTS says nothing about a table that already
    exists in an older shape, and every other test here builds a fresh
    database — so the suite was green while the live desk raised
    'no column named last_id' on the first call. A migration is only tested by
    starting from the old shape.
    """
    import sqlite3
    from deskd.orchestration import store

    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE agent_inbox_reads (role TEXT PRIMARY KEY, "
                "listed_at TEXT NOT NULL)")
    raw.execute("INSERT INTO agent_inbox_reads VALUES ('alpha', '2026-01-01T00:00:00+00:00')")
    raw.commit()
    raw.close()

    with store.connect(db, write=True) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_inbox_reads)")}
        assert "last_id" in cols
        row = conn.execute(
            "SELECT last_id FROM agent_inbox_reads WHERE role='alpha'").fetchone()
        assert row["last_id"] == 0, (
            "the old row cannot say what had been seen; 0 claims nothing was, "
            "which costs an extra wake and never acks an unread notice")
