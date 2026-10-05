"""Persistent attention and mocked delivery, without contacting any endpoint."""

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from deskd.workspace.notifications import NotificationDispatcher, NotificationStore, WebhookTarget
from deskd.workspace.store import WorkspaceError, WorkspaceStore


@pytest.fixture
def setup(tmp_path):
    now = [100.0]
    store = WorkspaceStore(tmp_path / "workspace.sqlite", clock=lambda: now[0])
    return now, store, NotificationStore(store)


def emit(notices, revision=1):
    return notices.emit("decision", "goal-local", revision, "需要你决定", "仅限工作台内的上下文", principal="demo/analyst")


def test_notice_is_stable_persistent_quiet_and_atomic(setup):
    _, store, notices = setup
    first = emit(notices)
    assert emit(notices) == first
    reopened = NotificationStore(WorkspaceStore(store.db_path, clock=store._clock))
    assert reopened.list_notifications()["notifications"] == [first]
    assert reopened.counters()["unread"] == 1
    with pytest.raises(WorkspaceError, match="notification_conflict"):
        notices.emit("decision", "goal-local", 1, "changed title")
    with pytest.raises(RuntimeError):
        with store._connect(write=True):
            emit(notices, 2)
            raise RuntimeError("rollback")
    assert notices.counters()["unread"] == 1
    assert notices.acknowledge([first["id"]]) == notices.acknowledge([first["id"]])
    assert notices.list_notifications(unread_only=True)["notifications"] == []
    assert len(notices.list_notifications()["notifications"]) == 1


def test_concurrent_emit_and_delivery_claims_are_unique(setup):
    _, _, notices = setup
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: emit(notices), range(10)))
    assert len({row["id"] for row in rows}) == 1
    notices.enqueue_target("approved-target")
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: notices.claim("approved-target"), range(4)))
    assert sum(row is not None for row in claims) == 1


def test_mock_webhook_has_stable_id_backoff_and_no_private_content(setup):
    now, _, notices = setup
    emit(notices)
    calls = []
    def sender(target, payload):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("private destination or body must never be recorded")
        return True
    target = WebhookTarget("https://example.test/attention", approved=True)
    dispatcher = NotificationDispatcher(notices, target, sender=sender)
    assert dispatcher.dispatch() == {"attempted": 1, "delivered": 0, "unconfirmed": 1}
    assert dispatcher.dispatch()["attempted"] == 0
    now[0] += 31
    assert dispatcher.dispatch() == {"attempted": 1, "delivered": 1, "unconfirmed": 0}
    assert calls[0] == calls[1]
    encoded = json.dumps(calls)
    assert "demo/analyst" not in encoded
    assert "goal-local" not in encoded
    assert "需要你决定" not in encoded
    assert "上下文" not in encoded
    assert dispatcher.dispatch()["attempted"] == 0


def test_unconfirmed_delivery_recovery_and_stale_worker_denial(setup):
    now, _, notices = setup
    emit(notices)
    notices.enqueue_target("approved-target")
    first = notices.claim("approved-target")
    now[0] += 61
    second = notices.claim("approved-target")
    assert first["id"] == second["id"]
    assert second["attempt"] == 2
    with pytest.raises(WorkspaceError, match="stale_notification_delivery"):
        notices.finish(first["id"], first["attempt"], delivered=True)
    notices.finish(second["id"], second["attempt"], delivered=True)


def test_ack_cancels_pending_delivery_and_failure_is_bounded(setup):
    now, _, notices = setup
    first = emit(notices)
    target = WebhookTarget("https://example.test/attention", approved=True)
    notices.enqueue_target(target.identifier)
    notices.acknowledge([first["id"]])
    assert notices.claim(target.identifier) is None
    emit(notices, 2)
    dispatcher = NotificationDispatcher(notices, target, sender=lambda *_: False, max_attempts=2)
    assert dispatcher.dispatch()["unconfirmed"] == 1
    now[0] += 31
    assert dispatcher.dispatch()["unconfirmed"] == 1
    now[0] += 10000
    assert dispatcher.dispatch()["attempted"] == 0
    assert notices.counters()["deliveries"] == {"cancelled": 1, "failed": 1}


@pytest.mark.parametrize("url", ["http://example.test/a", "https://127.0.0.1/a", "https://user:pass@example.test/a", "https://example.test/a?token=x", "https://example.test/a#b"])
def test_endpoint_requires_explicit_approval_and_safe_exact_url(url):
    with pytest.raises(WorkspaceError):
        WebhookTarget(url, approved=True)
    with pytest.raises(WorkspaceError, match="webhook_approval_required"):
        WebhookTarget("https://example.test/attention")


def test_ack_is_all_or_nothing_and_pagination_exact_count(setup):
    _, _, notices = setup
    first = emit(notices)
    emit(notices, 2)
    assert notices.list_notifications(limit=1)["has_more"] is True
    assert notices.list_notifications(limit=1)["unread_count"] == 2
    with pytest.raises(WorkspaceError, match="unknown_notification"):
        notices.acknowledge([first["id"], "unknown"])
    assert notices.counters()["unread"] == 2
