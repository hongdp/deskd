"""The delivery ledger: a projection of durable messages, judged at read time.

Every test here is a bug that was actually hit and fixed. `docs/design.md`
§Delivery is the spec; these are its teeth:

- the ledger is a PROJECTION, so a lost row re-derives itself;
- delivery is PROVEN (hook or ack), never assumed at plan time;
- "is this handled?" is scoped to (recipient, item), never to the container;
- `overdue` is the guarantee breaking and must never be dressed up as
  `escalated`.

Rows are seeded with raw SQL on purpose: the state under test is time-dependent,
and the public senders stamp `created_at` as *now*, which would leave these tests
sleeping through an SLA instead of injecting one.
"""

from __future__ import annotations

import pytest

from conftest import ROLES, iso, rows, scalar
from deskd import config as cfg_mod
from deskd import mailbox
from deskd import orchestration as o
from deskd.config import CONFIG, RoleSpec


# --- seeding ----------------------------------------------------------------

def seed_meeting(conn, thread_id: str, attendees, *, sla: int = 300) -> None:
    """A thread + meeting + attendee set — the shape sync_delivery projects from."""
    now = iso()
    conn.execute(
        "INSERT INTO mailbox_threads (id, kind, subject, status, phase, created_at,"
        " updated_at, expires_at, idle_minutes, max_messages)"
        " VALUES (?, 'live', ?, 'open', 'discuss', ?, ?, ?, 45, 50)",
        (thread_id, f"subject-{thread_id}", now, now, iso(3600)))
    conn.execute(
        "INSERT INTO meetings (thread_id, meeting_type, agenda, called_by, priority,"
        " state, consensus_threshold, wait_timeout_seconds, created_at, updated_at)"
        " VALUES (?, 'sync', ?, 'alpha', 'normal', 'open', 2, ?, ?, ?)",
        (thread_id, f"agenda-{thread_id}", sla, now, now))
    for role in attendees:
        conn.execute(
            "INSERT INTO meeting_attendees (thread_id, role, required, invited_at)"
            " VALUES (?, ?, 1, ?)", (thread_id, role, now))


def seed_message(conn, thread_id: str, *, sender: str, recipient: str,
                 created_at: str, body: str = "body") -> int:
    cur = conn.execute(
        "INSERT INTO mailbox_messages (thread_id, sender, recipient, kind, body,"
        " body_hash, created_at) VALUES (?, ?, ?, 'note', ?, ?, ?)",
        (thread_id, sender, recipient, body, f"hash-{body}", created_at))
    return cur.lastrowid


def seed_wake_request(conn, thread_id: str, role: str, *,
                      status: str = "pending") -> None:
    """A PER-ROLE wake request: the signal that something is re-driving delivery
    to this specific role.

    `status` matters as much as the row's existence. The table is PRIMARY KEY
    (thread_id, role), so an acknowledged request is not deleted — it sits there
    forever as the permanent record of a wake that already SUCCEEDED. Seeding
    'acknowledged' is therefore the normal steady state of any thread a role has
    ever been woken on, not an exotic case.
    """
    conn.execute(
        "INSERT INTO meeting_wake_requests (thread_id, role, status, created_at,"
        " acknowledged_at) VALUES (?, ?, ?, ?, ?)",
        (thread_id, role, status, iso(-60),
         iso(-30) if status == "acknowledged" else None))


def set_thread_status(conn, thread_id: str, status: str) -> None:
    """Thread lifecycle vocabulary per mailbox_threads' CHECK:
    open / paused / closed / escalated."""
    conn.execute("UPDATE mailbox_threads SET status=? WHERE id=?",
                 (status, thread_id))


def seed_thread_escalation(conn, thread_id: str, requested_by: str) -> None:
    """A thread-level (container-scoped) escalation raised by ONE role."""
    conn.execute(
        "INSERT INTO meeting_escalations (thread_id, requested_by, reason, channel,"
        " status, created_at) VALUES (?, ?, 'stuck', 'human', 'sent', ?)",
        (thread_id, requested_by, iso(-600)))


def ledger_row(conn, message_id: int, role: str) -> dict:
    return dict(conn.execute(
        "SELECT * FROM message_delivery WHERE message_id=? AND recipient_role=?",
        (message_id, role)).fetchone())


def backdate_attempts(when: str) -> None:
    """Push every open wake attempt past its rung SLA without sleeping."""
    with o.connect(write=True) as c:
        c.execute("UPDATE wake_attempts SET attempted_at=? WHERE outcome='pending'",
                  (when,))


# --- the ledger is a projection, and it self-heals ---------------------------

def test_deleted_ledger_row_is_rederived_from_the_source_message(conn):
    """The ledger is a projection of durable messages, not an independent store.

    Lose a row — a bad migration, a manual delete, a crash mid-write — and the
    obligation must come back, because the message that created it still exists.
    A ledger that could permanently lose a row would silently drop the delivery
    guarantee with no trace.
    """
    seed_meeting(conn, "T", ("alpha", "beta"))
    mid = seed_message(conn, "T", sender="alpha", recipient="beta",
                       created_at=iso(-10))
    assert o.sync_delivery(conn) == 1
    before = ledger_row(conn, mid, "beta")

    conn.execute("DELETE FROM message_delivery WHERE message_id=?", (mid,))
    assert scalar(conn, "SELECT COUNT(*) FROM message_delivery") == 0

    o.sync_delivery(conn)
    after = ledger_row(conn, mid, "beta")
    assert after["queued_at"] == before["queued_at"]
    assert after["sla_due_at"] == before["sla_due_at"]


def test_sync_delivery_is_idempotent_and_never_deletes_rows(conn):
    """Re-projecting must converge, not churn: the ledger is re-synced on every
    read, so a sync that dropped or duplicated rows would corrupt it constantly.
    """
    seed_meeting(conn, "T", ("alpha", "beta"))
    mid = seed_message(conn, "T", sender="alpha", recipient="beta",
                       created_at=iso(-10))
    o.sync_delivery(conn)
    conn.execute("INSERT INTO mailbox_receipts (message_id, role, read_at)"
                 " VALUES (?, 'beta', ?)", (mid, iso(-5)))

    for _ in range(3):
        o.sync_delivery(conn)

    assert scalar(conn, "SELECT COUNT(*) FROM message_delivery") == 1
    assert ledger_row(conn, mid, "beta")["read_at"] is not None


# --- never mark delivered at plan time --------------------------------------

def test_plan_wakes_does_not_mark_inbox_items_delivered(desk):
    """The worst bug this suite guards. plan_wakes() is speculative: the driver
    may skip the role (lock held) or the launch may fail. Marking delivered here
    lost the item AND suppressed the escalation that should have caught it,
    because an item that looks delivered stops generating wake demand.
    """
    item = o.inbox_enqueue("alpha", "alert", "spike", priority="urgent")

    plan = o.plan_wakes()
    assert [a["role"] for a in plan["actions"]] == ["alpha"]
    assert [r["reason_kind"] for r in plan["actions"][0]["reasons"]] == ["inbox"], \
        "the plan must still CARRY the item — it just must not claim delivery"

    # The driver is simulated as having skipped/failed: it never ran.
    with o.connect() as c:
        assert scalar(c, "SELECT delivered_at FROM agent_inbox WHERE id=?",
                      (item,)) is None
    assert [r["id"] for r in o.inbox_pending("alpha", include_delivered=False)] == [item]


def test_item_from_a_skipped_plan_keeps_escalating(desk):
    """The other half of the same bug: an undelivered item must stay a live
    demand and climb the ladder. If plan time had marked it delivered, this
    escalation — the only thing that would ever have surfaced the lost item —
    would never fire.
    """
    o.inbox_enqueue("alpha", "alert", "spike", priority="urgent")
    first = o.plan_wakes()["changed"]
    assert [c["escalated"] for c in first] == [False]
    start_level = first[0]["level"]

    # Nothing delivered it; let the rung's SLA lapse.
    backdate_attempts(iso(-10_000))

    second = o.plan_wakes()["changed"]
    assert [c["reason_kind"] for c in second] == ["inbox"]
    assert second[0]["escalated"] is True
    assert second[0]["level"] > start_level


# --- a blanket ack may only ack what was proven delivered --------------------

def test_blanket_ack_spares_items_that_were_never_delivered(desk):
    """`inbox_ack --for ROLE` means "I handled what you showed me", not "clear
    the queue". An item that arrived while the agent was processing has never
    been in front of anyone; blanket-acking it drops a notification silently.
    """
    delivered = o.inbox_enqueue("alpha", "signal", "shown to the agent")
    o.inbox_mark_delivered([delivered])
    arrived_late = o.inbox_enqueue("alpha", "alert", "arrived mid-turn")

    assert o.inbox_ack("alpha") == 1, "only the delivered item may be acked"

    with o.connect() as c:
        assert scalar(c, "SELECT acked_at FROM agent_inbox WHERE id=?",
                      (delivered,)) is not None
        assert scalar(c, "SELECT acked_at FROM agent_inbox WHERE id=?",
                      (arrived_late,)) is None
    assert [r["id"] for r in o.inbox_pending("alpha")] == [arrived_late]


# --- "is this handled?" is scoped to (recipient, item) -----------------------

def test_thread_escalation_for_one_role_does_not_mask_another_roles_state(desk):
    """The subtle one. A single historical escalation on a thread once masked
    EVERY role's stuck message on that thread, forever.

    The shape: alpha is genuinely being re-driven on thread T (it has a PER-ROLE
    wake request, and someone raised a thread-level escalation for it). Beta has
    its own stuck, un-reacted message on the SAME thread. Nothing is reacting to
    beta, so beta's message is `overdue` — the container's state must not answer
    for it.
    """
    with o.connect(write=True) as c:
        seed_meeting(c, "T", ("alpha", "beta", "gamma"))
        to_alpha = seed_message(c, "T", sender="gamma", recipient="alpha",
                                created_at=iso(-9_999), body="for-alpha")
        to_beta = seed_message(c, "T", sender="gamma", recipient="beta",
                               created_at=iso(-9_999), body="for-beta")
        o.sync_delivery(c)
        seed_wake_request(c, "T", "alpha")
        seed_thread_escalation(c, "T", requested_by="alpha")

    led = o.delivery_ledger()
    assert led[str(to_alpha)]["alpha"]["state"] == "escalated"
    assert led[str(to_beta)]["beta"]["state"] == "overdue", \
        "alpha's escalation on T must not answer for beta's message on T"


def test_thread_escalation_for_one_role_does_not_suppress_another_roles_wake(desk):
    """The state being right is not enough — the escalation must actually fire.

    Same shape as the test above, driven one rung further: beta's overdue
    message raises a stuck_delivery demand, and once its rung SLA lapses that
    demand MUST escalate. A container-scoped "handled?" check freezes beta on
    the bottom rung permanently, which is precisely how the original bug hid a
    stuck message forever while looking busy in the ledger.
    """
    with o.connect(write=True) as c:
        seed_meeting(c, "T", ("alpha", "beta", "gamma"))
        seed_message(c, "T", sender="gamma", recipient="beta",
                     created_at=iso(-9_999), body="for-beta")
        o.sync_delivery(c)
        seed_thread_escalation(c, "T", requested_by="alpha")

    first = o.plan_wakes()["changed"]
    assert [c["role"] for c in first] == ["beta"]
    start_level = first[0]["level"]

    backdate_attempts(iso(-10_000))

    plan = o.plan_wakes()
    assert plan["resolved"] == [], \
        "beta's demand is NOT resolved: nothing read it and nothing woke beta"
    assert [c["escalated"] for c in plan["changed"]] == [True]
    assert plan["changed"][0]["level"] > start_level


def test_another_roles_wake_request_does_not_supersede_a_stuck_delivery(desk):
    """The same bug as the two above, one table over — and the one the ledger
    tests cannot see.

    `_delivery_state` and `_demand_resolved` must answer "is something re-driving
    this?" IDENTICALLY: `collect_wake_demand` raises the stuck_delivery demand
    exactly when the former says `overdue`, so if the latter reads the wake table
    thread-wide it calls the demand `superseded` every tick and re-inserts it at
    the start rung — the message never climbs the ladder while the ledger
    cheerfully reports it `overdue`.

    The shape: alpha has a pending PER-ROLE wake request on thread T — alpha is
    genuinely being re-driven. Beta's stuck message is on that SAME thread with
    nothing whatsoever reacting to it. Alpha's wake request is alpha's; it must
    never answer for beta.
    """
    with o.connect(write=True) as c:
        seed_meeting(c, "T", ("alpha", "beta", "gamma"))
        seed_message(c, "T", sender="gamma", recipient="beta",
                     created_at=iso(-9_999), body="for-beta")
        o.sync_delivery(c)
        seed_wake_request(c, "T", "alpha")

    def beta_stuck(changes: list[dict]) -> list[dict]:
        """Beta's stuck_delivery demand alone: alpha's own meeting_wake demand
        rides along in every plan and is not what this test is about."""
        return [c for c in changes
                if c["role"] == "beta" and c["reason_kind"] == "stuck_delivery"]

    first = beta_stuck(o.plan_wakes()["changed"])
    assert [c["escalated"] for c in first] == [False]
    start_level = first[0]["level"]

    backdate_attempts(iso(-10_000))

    plan = o.plan_wakes()
    assert [r for r in plan["resolved"] if r["role"] == "beta"] == [], \
        "alpha's wake request on T is not a wake request for beta: nothing read " \
        "beta's message and nothing woke beta, so beta's demand is NOT resolved"
    escalated = beta_stuck(plan["changed"])
    assert [c["escalated"] for c in escalated] == [True]
    assert escalated[0]["level"] > start_level


def test_another_recipients_read_receipt_does_not_resolve_my_stuck_delivery(desk):
    """A broadcast creates one obligation PER recipient, and each is discharged
    only by ITS OWN receipt.

    Beta reads the all-hands; gamma never does. Gamma's copy is the one past its
    SLA, so gamma's stuck_delivery demand must climb the ladder. Answering "has
    this been read?" per MESSAGE instead of per (message, recipient) retires
    gamma's demand as `read` on the strength of beta's receipt — the stuck
    message is closed out having been read by someone who is not gamma.
    """
    with o.connect(write=True) as c:
        seed_meeting(c, "T", ("alpha", "beta", "gamma"))
        mid = seed_message(c, "T", sender="alpha", recipient=mailbox.BROADCAST,
                           created_at=iso(-9_999), body="all-hands")
        o.sync_delivery(c)
        c.execute("INSERT INTO mailbox_receipts (message_id, role, read_at)"
                  " VALUES (?, 'beta', ?)", (mid, iso(-9_000)))
        o.sync_delivery(c)
        assert ledger_row(c, mid, "beta")["read_at"] is not None
        assert ledger_row(c, mid, "gamma")["read_at"] is None

    def gamma_stuck(changes: list[dict]) -> list[dict]:
        return [c for c in changes
                if c["role"] == "gamma" and c["reason_kind"] == "stuck_delivery"]

    first = gamma_stuck(o.plan_wakes()["changed"])
    assert [c["escalated"] for c in first] == [False]
    start_level = first[0]["level"]

    backdate_attempts(iso(-10_000))

    plan = o.plan_wakes()
    assert [r for r in plan["resolved"] if r["role"] == "gamma"] == [], \
        "beta's receipt is not gamma's: gamma never read the broadcast"
    escalated = gamma_stuck(plan["changed"])
    assert [c["escalated"] for c in escalated] == [True]
    assert escalated[0]["level"] > start_level


def test_another_roles_pending_wake_request_does_not_hold_my_attempt_open(desk):
    """The mirror image, on the meeting_wake demand: scoping decides when an
    attempt CLOSES, not just when it fires.

    Alpha and beta are both woken for thread T. Beta answers; alpha has not yet.
    Beta's attempt must retire — `collect_wake_demand` already stopped raising
    it, so a resolution check that reads the wake table thread-wide leaves beta's
    attempt pending forever: it never records a latency, and if beta is ever
    woken for T again the stale row re-escalates it from an ancient clock.
    """
    with o.connect(write=True) as c:
        seed_meeting(c, "T", ("alpha", "beta"))
        seed_wake_request(c, "T", "alpha")
        seed_wake_request(c, "T", "beta")

    first = o.plan_wakes()["changed"]
    assert sorted(c["role"] for c in first) == ["alpha", "beta"]
    assert {c["reason_kind"] for c in first} == {"meeting_wake"}

    # Beta answers its own wake request. Alpha's stays pending.
    with o.connect(write=True) as c:
        c.execute("UPDATE meeting_wake_requests SET status='acknowledged',"
                  " acknowledged_at=? WHERE thread_id='T' AND role='beta'",
                  (iso(),))

    plan = o.plan_wakes()
    assert [(r["role"], r["reason_kind"], r["outcome"]) for r in plan["resolved"]] \
        == [("beta", "meeting_wake", "acked")], \
        "alpha's still-pending wake request on T must not hold beta's attempt open"


def test_another_roles_queued_notifications_do_not_hold_my_inbox_demand_open(desk):
    """Same rule on the inbox demand: it is resolved once THIS role has nothing
    undelivered, not once the whole desk is quiet.

    Beta's notification reaches beta; alpha's is still queued. Counting the
    inbox without a role predicate means beta's attempt stays open until every
    OTHER role's queue drains too — beta's wake is never credited, and the
    busiest desk never resolves anything.
    """
    o.inbox_enqueue("alpha", "alert", "for-alpha", priority="urgent")
    for_beta = o.inbox_enqueue("beta", "alert", "for-beta", priority="urgent")

    first = o.plan_wakes()["changed"]
    assert sorted(c["role"] for c in first) == ["alpha", "beta"]
    assert {c["reason_kind"] for c in first} == {"inbox"}

    # The driver put beta's item in front of beta. Alpha's is untouched.
    o.inbox_mark_delivered([for_beta])

    plan = o.plan_wakes()
    assert [(r["role"], r["reason_kind"]) for r in plan["resolved"]] == [("beta", "inbox")], \
        "alpha's queued notification is not beta's: it must not hold beta's demand open"


# --- overdue vs escalated ----------------------------------------------------

def test_past_sla_and_unread_with_something_reacting_is_escalated(conn):
    """Past SLA + unread + a per-role wake request = the system is working on
    it. That is `escalated`, not a breach."""
    seed_meeting(conn, "T", ("alpha", "beta"))
    mid = seed_message(conn, "T", sender="alpha", recipient="beta",
                       created_at=iso(-9_999))
    o.sync_delivery(conn)
    seed_wake_request(conn, "T", "beta")

    state = o._delivery_state(ledger_row(conn, mid, "beta"), iso(), o._wake_keys(conn))
    assert state == "escalated"


def test_past_sla_and_unread_with_nothing_reacting_is_overdue(conn):
    """The invariant breach, surfaced red: the message is late and NOTHING is
    re-driving it. Reporting this as `escalated` would hide the one state the
    console exists to show.
    """
    seed_meeting(conn, "T", ("alpha", "beta"))
    mid = seed_message(conn, "T", sender="alpha", recipient="beta",
                       created_at=iso(-9_999))
    o.sync_delivery(conn)

    state = o._delivery_state(ledger_row(conn, mid, "beta"), iso(), o._wake_keys(conn))
    assert state == "overdue"
    assert [d["reason_kind"] for d in o.collect_wake_demand(conn)] == ["stuck_delivery"]


def test_a_wake_request_for_another_role_does_not_make_it_escalated(conn):
    """`_wake_keys` is (thread, role), not a set of threads. A wake request that
    re-drives gamma says nothing about whether beta's message is being handled.
    """
    seed_meeting(conn, "T", ("alpha", "beta", "gamma"))
    mid = seed_message(conn, "T", sender="alpha", recipient="beta",
                       created_at=iso(-9_999))
    o.sync_delivery(conn)
    seed_wake_request(conn, "T", "gamma")

    assert ("T", "gamma") in o._wake_keys(conn)
    state = o._delivery_state(ledger_row(conn, mid, "beta"), iso(), o._wake_keys(conn))
    assert state == "overdue"


# --- _wake_keys asks the PRESENT tense ---------------------------------------

def test_an_acknowledged_wake_request_does_not_make_a_later_message_escalated(conn):
    """`_wake_keys` must ask "is something reacting NOW?", not "has this role ever
    been woken on this thread?".

    Without the `status='pending'` filter the set answers the past-tense question,
    and since the table is keyed (thread, role) the acknowledged row never leaves.
    So the FIRST successful wake on a thread pins every later unread message for
    that pair to `escalated` forever; `collect_wake_demand` only raises on
    `overdue`, so that role can never be woken about that thread again. The
    delivery guarantee — this package's headline — dies silently for that pair.

    Found on a live desk, not in review: nine messages past SLA (one nine hours
    old), every wake request `acknowledged`, plan_wakes returning zero actions.
    """
    seed_meeting(conn, "T", ("alpha", "beta"))
    mid = seed_message(conn, "T", sender="alpha", recipient="beta",
                       created_at=iso(-9_999))
    o.sync_delivery(conn)
    seed_wake_request(conn, "T", "beta", status="acknowledged")

    assert ("T", "beta") not in o._wake_keys(conn), \
        "a wake that already succeeded is not something reacting now"
    state = o._delivery_state(ledger_row(conn, mid, "beta"), iso(), o._wake_keys(conn))
    assert state == "overdue"
    assert [d["reason_kind"] for d in o.collect_wake_demand(conn)] == ["stuck_delivery"]


def test_a_pending_wake_request_still_reads_as_reacting(conn):
    """The positive control for the test above: the `status='pending'` filter must
    narrow the tense, not delete the concept. A live wake request IS something
    reacting, so the same shape is `escalated` and raises no stuck_delivery — only
    the pending request's own meeting_wake demand.
    """
    seed_meeting(conn, "T", ("alpha", "beta"))
    mid = seed_message(conn, "T", sender="alpha", recipient="beta",
                       created_at=iso(-9_999))
    o.sync_delivery(conn)
    seed_wake_request(conn, "T", "beta")

    assert ("T", "beta") in o._wake_keys(conn)
    state = o._delivery_state(ledger_row(conn, mid, "beta"), iso(), o._wake_keys(conn))
    assert state == "escalated"
    assert [d["reason_kind"] for d in o.collect_wake_demand(conn)] == ["meeting_wake"], \
        "nothing is stuck: the wake request itself is the only live demand"


def test_an_acknowledged_wake_does_not_supersede_a_stuck_delivery_demand(desk):
    """Guards the OTHER copy of the same predicate. `_demand_resolved`'s
    stuck_delivery branch asks the same "is something reacting?" question as
    `_delivery_state`, and a hand-rolled copy of the query is exactly how one of
    them drifted into the past tense.

    The two must agree: `collect_wake_demand` raises this demand precisely when
    `_delivery_state` says `overdue`, so if `_demand_resolved` alone treats an
    acknowledged wake as "superseded", every tick closes the attempt and re-opens
    it at the start rung — the demand looks busy, never climbs, and the stuck
    message is never surfaced. Driving a full rung proves they still agree.
    """
    with o.connect(write=True) as c:
        seed_meeting(c, "T", ("alpha", "beta"))
        seed_message(c, "T", sender="alpha", recipient="beta",
                     created_at=iso(-9_999), body="for-beta")
        o.sync_delivery(c)
        seed_wake_request(c, "T", "beta", status="acknowledged")

    first = o.plan_wakes()["changed"]
    assert [c["role"] for c in first] == ["beta"]
    start_level = first[0]["level"]

    backdate_attempts(iso(-10_000))

    plan = o.plan_wakes()
    assert plan["resolved"] == [], \
        "an already-acknowledged wake is not another channel taking over"
    assert [c["escalated"] for c in plan["changed"]] == [True]
    assert plan["changed"][0]["level"] > start_level


# --- a closed thread is over: no wake, but honest history --------------------

def test_a_closed_threads_unread_message_raises_no_wake(conn):
    """`collect_wake_demand` scanned message_delivery with no thread-status
    filter, so unread messages in a CLOSED thread raised stuck_delivery every
    tick. Nothing an agent does can resolve them — the conversation is over — so
    the demand regenerates forever: a permanent wake loop over dead threads.
    """
    seed_meeting(conn, "T", ("alpha", "beta"))
    seed_message(conn, "T", sender="alpha", recipient="beta",
                 created_at=iso(-9_999))
    o.sync_delivery(conn)
    set_thread_status(conn, "T", "closed")

    assert o.collect_wake_demand(conn) == []


def test_a_closed_thread_still_reports_its_unread_message_as_overdue(desk):
    """The other half, and the one a careless fix breaks: suppressing the WAKE
    must not rewrite the RECORD.

    A message that was never read stays `overdue` even though the thread is
    closed — that is honest history, and `overdue` is the one state the console
    exists to show. Fixing the wake loop by dropping the projection for closed
    threads, or by dressing the row up as handled, would erase the evidence that
    the guarantee was ever broken. Read through the public ledger: that is the
    surface the console renders, and it re-syncs the projection first, so a closed
    thread's rows must survive that too.
    """
    with o.connect(write=True) as c:
        seed_meeting(c, "T", ("alpha", "beta"))
        mid = seed_message(c, "T", sender="alpha", recipient="beta",
                           created_at=iso(-9_999))
        o.sync_delivery(c)
        set_thread_status(c, "T", "closed")

    assert o.delivery_ledger("T")[str(mid)]["beta"]["state"] == "overdue", \
        "the record that nobody read it must survive the thread closing"


@pytest.mark.parametrize("status", ["paused", "escalated"])
def test_a_resumable_threads_unread_message_still_raises_a_wake(conn, status):
    """The over-correction guard on the closed-thread fix: only `closed` is over.

    `paused` and `escalated` threads can still resume, so their unread messages
    are genuinely undelivered and must keep waking. Excluding anything broader
    than `status='closed'` — filtering to `status='open'`, say — would silently
    drop the delivery guarantee for every escalated thread, which is exactly the
    population that most needs it.
    """
    seed_meeting(conn, "T", ("alpha", "beta"))
    seed_message(conn, "T", sender="alpha", recipient="beta",
                 created_at=iso(-9_999))
    o.sync_delivery(conn)
    set_thread_status(conn, "T", status)

    assert [d["reason_kind"] for d in o.collect_wake_demand(conn)] == ["stuck_delivery"]


# --- time-dependent state is computed at read time ---------------------------

def test_state_is_derived_at_read_time_not_stored(conn):
    """The SAME row reports different states as time passes — because state is
    computed from the row, never written into it. A stored state goes stale the
    moment nothing ticks, which is exactly when it matters.
    """
    seed_meeting(conn, "T", ("alpha", "beta"), sla=300)
    mid = seed_message(conn, "T", sender="alpha", recipient="beta",
                       created_at=iso())
    o.sync_delivery(conn)
    row = ledger_row(conn, mid, "beta")
    wake = o._wake_keys(conn)

    assert "state" not in row, "state must not be a column"
    assert o._delivery_state(row, iso(-1), wake) == "queued"
    assert o._delivery_state(row, iso(+280), wake) == "queued"   # still inside SLA
    assert o._delivery_state(row, iso(+400), wake) == "overdue"  # same row, later

    # Same row again, now with a notification hop — still within SLA.
    conn.execute("INSERT INTO mailbox_notifications (message_id, role, notified_at)"
                 " VALUES (?, 'beta', ?)", (mid, iso()))
    o.sync_delivery(conn)
    row = ledger_row(conn, mid, "beta")
    assert o._delivery_state(row, iso(+10), wake) == "notified"
    assert o._delivery_state(row, iso(+400), wake) == "overdue"


# --- projection covers every registered role, from the registry --------------

def test_sync_delivery_projects_for_every_registered_role(conn):
    """A two-role suite passes happily against a two-role hardcoding. Three
    roles, and every one of them must get its own ledger row from a broadcast.
    """
    seed_meeting(conn, "T", ("alpha", "beta", "gamma"))
    bcast = seed_message(conn, "T", sender="alpha", recipient=mailbox.BROADCAST,
                         created_at=iso(-10), body="all-hands")
    direct = seed_message(conn, "T", sender="beta", recipient="alpha",
                          created_at=iso(-10), body="to-alpha")
    o.sync_delivery(conn)

    assert {r[0] for r in rows(
        conn, "SELECT recipient_role FROM message_delivery WHERE message_id=?",
        (bcast,))} == {"beta", "gamma"}, "the sender is not a recipient of its own broadcast"
    assert {r[0] for r in rows(
        conn, "SELECT recipient_role FROM message_delivery WHERE message_id=?",
        (direct,))} == {"alpha"}


def test_a_role_added_via_configure_gets_projection_too(desk):
    """The registry is the source of truth, not a role list baked into the
    engine. A host that adds a fourth role mid-flight must get delivery
    obligations for it — the original bug was a third role silently getting no
    projection, and therefore no wakes, forever.
    """
    with o.connect(write=True) as c:
        seed_meeting(c, "T", ("alpha", "beta", "gamma"))
        bcast = seed_message(c, "T", sender="alpha", recipient=mailbox.BROADCAST,
                             created_at=iso(-10), body="all-hands")
        o.sync_delivery(c)
        assert scalar(c, "SELECT COUNT(*) FROM message_delivery") == 2

    # The host registers a fourth role and invites it to the running thread.
    cfg_mod.configure(roles=ROLES + (RoleSpec("delta", "Delta"),))
    with o.connect(write=True) as c:
        assert "delta" in o._known_roles(c), "connect() must seed the new role"
        c.execute("INSERT INTO meeting_attendees (thread_id, role, required,"
                  " invited_at) VALUES ('T', 'delta', 1, ?)", (iso(),))
        o.sync_delivery(c)
        assert {r[0] for r in rows(
            c, "SELECT recipient_role FROM message_delivery WHERE message_id=?",
            (bcast,))} == {"beta", "gamma", "delta"}


def test_supervisor_message_without_verified_auth_creates_no_obligation(conn):
    """An unauthenticated message claiming to be from the supervisor is not a
    real message. Projecting it would let anything that can write a row
    manufacture a delivery obligation — and an escalation — against an agent.
    """
    seed_meeting(conn, "T", ("alpha", "beta"))
    forged = seed_message(conn, "T", sender=CONFIG.supervisor_role, recipient="beta",
                          created_at=iso(-10), body="forged")
    o.sync_delivery(conn)
    assert rows(conn, "SELECT * FROM message_delivery WHERE message_id=?",
                (forged,)) == []
