"""Real temporary SQLite checks for event/receipt transaction boundaries.

All paths come from pytest's temporary directory. No service, account data,
network adapter or credential store is opened.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from deskd.gateway.events import (
    ConsumerReceipt, EventConflict, EventValidationError, GatewayEventStore,
    MAX_JSON_DEPTH, canonical_json,
)


def prepare(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE business (id INTEGER PRIMARY KEY, amount INTEGER NOT NULL)")
        conn.execute("CREATE TABLE projection (consumer TEXT PRIMARY KEY, amount INTEGER NOT NULL)")


@pytest.mark.parametrize("value", ["", "   ", ":memory:", "file::memory:", None, 12])
def test_invalid_database_path_does_not_create_a_file(tmp_path, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(EventValidationError):
        GatewayEventStore(value)
    assert list(tmp_path.iterdir()) == []


def test_directory_is_not_a_database_path(tmp_path):
    with pytest.raises(EventValidationError, match="must name a file"):
        GatewayEventStore(tmp_path)
    assert list(tmp_path.iterdir()) == []


def count(path, table):
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def amount(path, consumer):
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT amount FROM projection WHERE consumer=?", (consumer,)).fetchone()
        return row[0] if row else 0


def effect(conn):
    cur = conn.execute("INSERT INTO business(amount) VALUES (7)")
    return {"business_id": cur.lastrowid}


def projector(consumer, *, fail=False):
    def apply(conn, event):
        conn.execute(
            "INSERT INTO projection VALUES (?,?) ON CONFLICT(consumer) DO UPDATE "
            "SET amount=amount+excluded.amount", (consumer, event["payload"]["amount"]))
        if fail:
            raise RuntimeError("injected projection failure")
        return {"consumer": consumer, "event_id": event["event_id"]}
    return apply


@pytest.fixture
def stores(tmp_path):
    producer = GatewayEventStore(tmp_path / "producer.sqlite")
    consumer = GatewayEventStore(tmp_path / "consumer.sqlite")
    prepare(producer.db_path)
    prepare(consumer.db_path)
    return producer, consumer


def publish(store, **kwargs):
    return store.publish("seat:analyst", "req-1", "proposal.created", {"amount": 7}, **kwargs)


def test_publish_effect_event_receipt_and_replay(stores):
    source, _ = stores
    first = publish(source, effect=effect)
    second = publish(source, effect=lambda _: pytest.fail("effect ran on replay"))
    assert first == second
    assert first.result == {"business_id": 1}
    assert count(source.db_path, "business") == 1
    assert count(source.db_path, "events_requests") == 1
    assert source.events() == [{"sequence": 1, "event": first.event}]
    assert source.events(after_sequence=1) == []
    assert publish(GatewayEventStore(source.db_path)) == first


@pytest.mark.parametrize("changed", [
    {"payload": {"amount": 8}}, {"event_type": "proposal.deleted"},
    {"causation_id": "different"}, {"correlation_id": "different"},
])
def test_request_reuse_changed_content_conflicts(stores, changed):
    source, _ = stores
    publish(source, effect=effect)
    args = dict(principal_id="seat:analyst", request_id="req-1",
                event_type="proposal.created", payload={"amount": 7})
    args.update(changed)
    with pytest.raises(EventConflict):
        source.publish(**args, effect=lambda _: pytest.fail("conflicting effect ran"))
    assert count(source.db_path, "business") == 1
    assert len(source.events()) == 1


def test_same_request_id_different_principals_are_independent(stores):
    source, _ = stores
    publish(source, effect=effect)
    source.publish("seat:trader", "req-1", "proposal.created", {"amount": 7}, effect=effect)
    assert count(source.db_path, "business") == 2
    assert len(source.events()) == 2


def test_failed_effect_leaves_no_fact_outbox_or_receipt(stores):
    source, _ = stores
    class SimulatedDeath(BaseException):
        pass
    def die(conn):
        effect(conn)
        raise SimulatedDeath()
    with pytest.raises(SimulatedDeath):
        publish(source, effect=die)
    assert count(source.db_path, "business") == 0
    assert count(source.db_path, "events_requests") == 0
    assert source.events() == []
    assert publish(source, effect=effect).result == {"business_id": 1}


def test_non_json_effect_result_rolls_back(stores):
    source, _ = stores
    def invalid(conn):
        effect(conn)
        return object()
    with pytest.raises(EventValidationError):
        publish(source, effect=invalid)
    assert count(source.db_path, "business") == 0
    assert source.events() == []


def test_lost_ack_retry_and_independent_consumers(stores):
    source, sink = stores
    event = publish(source).event
    original_ack = sink.consume("board", event, project=projector("board"))
    # Pretend the returned ACK was lost. Reopening the DB is a fresh process view.
    retry_ack = GatewayEventStore(sink.db_path).consume(
        "board", event, project=lambda *_: pytest.fail("duplicate projection"))
    other_ack = sink.consume("audit", event, project=projector("audit"))
    assert isinstance(original_ack, ConsumerReceipt)
    assert original_ack == retry_ack
    assert other_ack.consumer_id == "audit"
    assert amount(sink.db_path, "board") == 7
    assert amount(sink.db_path, "audit") == 7
    assert count(sink.db_path, "events_received") == 1
    assert count(sink.db_path, "events_consumers") == 2
    # Consumer receipt is observable on another connection after consume returns.
    assert original_ack.event_id == event["event_id"]
    assert sink.events() == []


def test_projection_and_receipt_rollback_together(stores):
    source, sink = stores
    event = publish(source).event
    with pytest.raises(RuntimeError, match="injected"):
        sink.consume("board", event, project=projector("board", fail=True))
    assert amount(sink.db_path, "board") == 0
    assert count(sink.db_path, "events_received") == 0
    assert count(sink.db_path, "events_consumers") == 0
    sink.consume("board", event, project=projector("board"))
    assert amount(sink.db_path, "board") == 7


@pytest.mark.parametrize("consumer", ["board", "new-consumer"])
def test_changed_event_id_content_conflicts_across_consumers(stores, consumer):
    source, sink = stores
    event = publish(source).event
    sink.consume("board", event, project=projector("board"))
    changed = deepcopy(event)
    changed["payload"]["amount"] = 900
    changed.pop("fingerprint")
    changed["fingerprint"] = hashlib.sha256(canonical_json(changed).encode()).hexdigest()
    with pytest.raises(EventConflict):
        sink.consume(consumer, changed, project=projector(consumer))
    assert amount(sink.db_path, "board") == 7
    assert amount(sink.db_path, "new-consumer") == 0


def test_fingerprint_and_unknown_event_schema_rejected(stores):
    source, sink = stores
    event = publish(source).event
    bad = deepcopy(event)
    bad["payload"]["amount"] = 99
    with pytest.raises(EventValidationError, match="fingerprint"):
        sink.consume("board", bad, project=projector("board"))
    bad["schema_version"] = 2
    with pytest.raises(EventValidationError, match="schema version"):
        sink.consume("board", bad, project=projector("board"))
    assert count(sink.db_path, "events_received") == 0


def test_projection_inputs_are_read_only_history_not_callback_replay(stores):
    source, sink = stores
    events = [publish(source).event,
              source.publish("seat:analyst", "req-2", "proposal.created", {"amount": 3}).event]
    for event in events:
        sink.consume("board", event, project=projector("board"))
    before = amount(sink.db_path, "board")
    assert sink.projection_events("board") == events
    assert sink.projection_events("other") == []
    assert sink.projection_events("board") == events
    assert amount(sink.db_path, "board") == before == 10
    assert count(sink.db_path, "events_consumers") == 2
    assert sink.events() == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"),
                               {1: "integer key"}, {"x": object()}, (1, 2), "\ud800"])
def test_canonical_json_rejects_non_json_input(bad):
    with pytest.raises(EventValidationError):
        canonical_json(bad)


def test_json_depth_cycle_size_and_equivalent_object_order(stores):
    nested = {}
    for _ in range(MAX_JSON_DEPTH + 1):
        nested = {"child": nested}
    with pytest.raises(EventValidationError, match="depth"):
        canonical_json(nested)
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(EventValidationError, match="depth"):
        canonical_json(cyclic)
    with pytest.raises(EventValidationError, match="size"):
        canonical_json({"large": "x" * 1_048_576})
    source, _ = stores
    first = source.publish("seat:a", "ordered", "test.event", {"z": 1, "a": [2]})
    assert source.publish("seat:a", "ordered", "test.event", {"a": [2], "z": 1}) == first


@pytest.mark.parametrize("escape", ["commit", "rollback", "executescript", "begin", "savepoint"])
def test_callback_transaction_control_is_denied_and_effect_rolls_back(stores, escape):
    source, _ = stores
    def illegal(conn):
        effect(conn)
        if escape == "commit": conn.commit()
        elif escape == "rollback": conn.rollback()
        elif escape == "executescript": conn.executescript("INSERT INTO business(amount) VALUES (99);")
        elif escape == "begin": conn.execute("BEGIN")
        else: conn.execute("SAVEPOINT hidden")
    with pytest.raises(sqlite3.DatabaseError):
        publish(source, effect=illegal)
    assert count(source.db_path, "business") == 0
    assert source.events() == []
    assert count(source.db_path, "events_requests") == 0


def test_projection_cannot_commit_or_mutate_internal_ledger(stores):
    source, sink = stores
    event = publish(source).event
    def commits(conn, event):
        projector("board")(conn, event)
        conn.commit()
    with pytest.raises(sqlite3.DatabaseError):
        sink.consume("board", event, project=commits)
    assert amount(sink.db_path, "board") == 0
    def corrupts(conn, event):
        conn.execute("DELETE FROM events_received")
    with pytest.raises(sqlite3.DatabaseError):
        sink.consume("board", event, project=corrupts)
    assert count(sink.db_path, "events_received") == 0


def test_validate_is_inside_transaction_and_runs_on_replay(stores):
    source, _ = stores
    allowed = True
    calls = []
    def authorize(conn):
        assert conn.in_transaction
        calls.append(1)
        if not allowed:
            raise PermissionError("revoked")
        return object()  # Authentication objects need not be JSON.
    receipt = publish(source, effect=effect, validate=authorize)
    assert publish(source, validate=authorize) == receipt
    allowed = False
    with pytest.raises(PermissionError, match="revoked"):
        publish(source, validate=authorize)
    assert len(calls) == 3
    assert count(source.db_path, "business") == 1


def test_concurrent_publish_and_consume_have_one_local_effect(stores):
    source, sink = stores
    barrier = threading.Barrier(2)
    def produce(_):
        barrier.wait()
        return publish(source, effect=effect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(produce, range(2)))
    assert receipts[0] == receipts[1]
    assert count(source.db_path, "business") == 1
    barrier = threading.Barrier(2)
    def consume(_):
        barrier.wait()
        return sink.consume("board", receipts[0].event, project=projector("board"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        acks = list(pool.map(consume, range(2)))
    assert acks[0] == acks[1]
    assert amount(sink.db_path, "board") == 7


@pytest.mark.parametrize("side", ["publish", "consume"])
def test_actual_process_exit_rolls_back_uncommitted_fact_and_receipt(stores, side):
    source, sink = stores
    event = publish(source).event if side == "consume" else None
    target = sink if side == "consume" else source
    script = '''
import json, os, sys
from deskd.gateway.events import GatewayEventStore
store = GatewayEventStore(sys.argv[1])
def die(conn, *args):
    conn.execute("INSERT INTO business(amount) VALUES (7)")
    os._exit(23)
if sys.argv[2] == "publish":
    store.publish("seat:analyst", "crash", "test.event", {}, effect=die)
else:
    store.consume("board", json.loads(sys.argv[3]), project=die)
'''
    run = subprocess.run([sys.executable, "-c", script, str(target.db_path), side,
                          json.dumps(event)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 23, run.stderr
    assert count(target.db_path, "business") == 0
    assert count(target.db_path, "events_requests") == 0
    assert count(target.db_path, "events_received") == 0
    assert count(target.db_path, "events_consumers") == 0
    assert target.events() == []


def test_schema_is_additive_idempotent_and_does_not_initialize_legacy_layers(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE existing_registry (seat TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO existing_registry VALUES ('alpha')")
        conn.execute("PRAGMA user_version=47")
    first = GatewayEventStore(path)
    receipt = publish(first)
    assert publish(GatewayEventStore(path)) == receipt
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT seat FROM existing_registry").fetchall() == [("alpha",)]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 47
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "mailbox_messages" not in names
        assert "control_events" not in names
        assert "agent_registry" not in names


def test_unknown_database_version_is_not_downgraded(tmp_path):
    path = tmp_path / "future.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE events_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO events_meta VALUES ('schema_version','99')")
    with pytest.raises(EventValidationError, match="database schema"):
        GatewayEventStore(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM events_meta").fetchone()[0] == "99"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='events_outbox'").fetchone() is None


def test_replacement_root_replay_keeps_original_event_provenance(stores):
    source, sink = stores
    checks = []
    first = publish(source, provenance={"root": "old", "generation": 1},
                    validate=lambda conn: checks.append(conn.in_transaction), effect=effect)
    replay = publish(source, provenance={"root": "new", "generation": 2},
                     validate=lambda conn: checks.append(conn.in_transaction),
                     effect=lambda _: pytest.fail("replacement root repeated the effect"))
    assert replay == first
    assert replay.event["provenance"] == {"root": "old", "generation": 1}
    assert checks == [True, True]
    sink.consume("board", first.event, project=projector("board"))
    changed = deepcopy(first.event)
    changed["provenance"] = {"root": "new", "generation": 2}
    changed.pop("fingerprint")
    changed["fingerprint"] = hashlib.sha256(canonical_json(changed).encode()).hexdigest()
    with pytest.raises(EventConflict):
        sink.consume("audit", changed, project=projector("audit"))


def test_projection_history_preserves_each_consumer_application_order(stores):
    source, sink = stores
    first = publish(source).event
    second = source.publish("seat:analyst", "req-2", "proposal.created", {"amount": 3}).event
    sink.consume("audit", first, project=projector("audit"))
    sink.consume("board", second, project=projector("board"))
    sink.consume("board", first, project=projector("board"))
    assert sink.projection_events("audit") == [first]
    assert sink.projection_events("board") == [second, first]


def test_explicit_database_path_remains_bound_when_cwd_changes(tmp_path, monkeypatch):
    original = tmp_path / "first"
    later = tmp_path / "second"
    original.mkdir()
    later.mkdir()
    monkeypatch.chdir(original)
    store = GatewayEventStore("relative.sqlite")
    assert store.db_path == original / "relative.sqlite"
    first = publish(store)
    monkeypatch.chdir(later)
    assert publish(store) == first
    assert not (later / "relative.sqlite").exists()


@pytest.mark.parametrize("side", ["publish", "consume"])
def test_receipt_insert_failure_rolls_back_preceding_effect_and_event(stores, side):
    source, sink = stores
    target = source if side == "publish" else sink
    receipt_table = "events_requests" if side == "publish" else "events_consumers"
    with sqlite3.connect(target.db_path) as conn:
        conn.execute(f"CREATE TRIGGER fail_receipt BEFORE INSERT ON {receipt_table} "
                     "BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="receipt failure"):
        if side == "publish":
            publish(source, effect=effect)
        else:
            sink.consume("board", publish(source).event, project=projector("board"))
    assert count(target.db_path, "business") == 0
    assert amount(target.db_path, "board") == 0
    assert count(target.db_path, "events_received") == 0
    assert count(target.db_path, receipt_table) == 0
    assert target.events() == []


@pytest.mark.parametrize("side", ["publish", "consume"])
def test_commit_failure_never_returns_ack_or_keeps_partial_state(stores, side):
    source, sink = stores
    target = source if side == "publish" else sink
    with sqlite3.connect(target.db_path) as conn:
        conn.execute("CREATE TABLE parent_fact (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE deferred_fact (parent_id INTEGER REFERENCES parent_fact(id) "
                     "DEFERRABLE INITIALLY DEFERRED)")
    callbacks_returned = []
    def invalid_at_commit(conn, *args):
        conn.execute("INSERT INTO deferred_fact VALUES (999)")
        # Deferred FK means the trusted callback returns normally. The error
        # happens only when the outer store commits, after receipt insertion.
        callbacks_returned.append(True)
        return {"effect": "prepared"}
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        if side == "publish":
            publish(source, effect=invalid_at_commit)
        else:
            sink.consume("board", publish(source).event, project=invalid_at_commit)
    assert callbacks_returned == [True]
    assert count(target.db_path, "deferred_fact") == 0
    assert count(target.db_path, "events_requests") == 0
    assert count(target.db_path, "events_received") == 0
    assert count(target.db_path, "events_consumers") == 0
    assert target.events() == []
