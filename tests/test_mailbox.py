"""The mailbox's on-disk vocabulary: which words the engine owns, which it doesn't.

Two rules pull in opposite directions here, and both are pinned below because
the module used to state one of them and practise the other.

The engine's OWN vocabulary — thread kind, thread status, review stage — is
closed. Every literal names a branch in `mailbox.py` (`review` selects the phase
machine; `report`/`review`/`final` *are* the keys of `_STAGE_PHASE`), so a host
word like `incident` would name a state the engine has no code for. Those sets
may live in a DDL CHECK, and the CHECK must never drift from the constant.

The HOST's vocabulary — roles, senders, recipients — is not the engine's to
enumerate. A CHECK naming one would freeze a single host's words into every
host's database file. Those never reach a CHECK.

The roles are deliberately nonsense and there are three of them (see conftest).
That is load-bearing for the broadcast tests: a two-party fossil like
`recipient='both'` reads perfectly well to a two-role suite and is only
obviously wrong once a third role is in the thread.
"""

from __future__ import annotations

import re

import pytest

from conftest import iso
from deskd import mailbox


# --- reading the constraints the database actually stores --------------------

def _table_sql(conn, table: str) -> str:
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]


def _check_set(conn, table: str, column: str) -> frozenset[str] | None:
    """The literals `CHECK (column IN (...))` enumerates, or None if no CHECK.

    Read back from `sqlite_master` rather than from the SCHEMA string: the
    stored DDL is what an existing database file carries, and that file is the
    thing a constraint would freeze a vocabulary into.
    """
    match = re.search(rf"CHECK\s*\(\s*{column}\s+IN\s*\(([^)]*)\)",
                      _table_sql(conn, table), re.IGNORECASE)
    if not match:
        return None
    return frozenset(v.strip().strip("'") for v in match.group(1).split(","))


# --- engine vocabulary: closed, and the DDL agrees with the constant ---------

@pytest.mark.parametrize("table, column, constant", [
    ("mailbox_threads", "kind", mailbox.THREAD_KINDS),
    ("mailbox_threads", "status", mailbox.THREAD_STATUSES),
    ("review_artifacts", "stage", mailbox.REVIEW_STAGES),
])
def test_engine_vocabulary_ddl_matches_its_python_constant(conn, table, column,
                                                           constant):
    """These sets are enforced twice — in Python and by a CHECK 25 lines away.

    Two enforcement points and one meaning is only safe while they agree. Drift
    is silent in the direction that matters: widening the constant without the
    DDL leaves a word that `open_thread` accepts and the INSERT then rejects
    with a bare IntegrityError, from a table the caller never named.
    """
    assert _check_set(conn, table, column) == frozenset(constant)


def test_no_check_freezes_a_host_vocabulary(conn):
    """The rule the CHECKs above are the *exception* to, not evidence against.

    A role-bearing column is filled with the host's words. Enumerating one in
    DDL would bake this host's roles into the database file, where no later
    `configure()` can reach them — that is the failure the registry lookup in
    `_role()` exists to avoid.
    """
    host_columns = {
        "mailbox_threads": ("owner_role", "stopped_by"),
        "mailbox_messages": ("sender", "recipient"),
        "mailbox_receipts": ("role",),
        "thread_agreements": ("role",),
        "review_artifacts": ("role",),
    }
    frozen = {f"{table}.{column}"
              for table, columns in host_columns.items() for column in columns
              if _check_set(conn, table, column) is not None}

    assert frozen == set()


def test_a_host_domain_word_is_refused_as_a_thread_kind(desk):
    """The closed set, seen from a host that wanted `incident`.

    Refusing in Python — naming the offending word — is the whole reason the
    Python guard leads and the CHECK only backstops it. The engine has no phase
    machine for `incident`, and saying so is more useful than either a bare
    IntegrityError or a thread that silently behaves like `live`.
    """
    with pytest.raises(ValueError, match="invalid thread kind: incident"):
        mailbox.open_thread("pager fired", kind="incident")


def test_a_host_domain_word_is_refused_as_a_review_stage(desk, tmp_path):
    """Same, on the stage vocabulary: `_STAGE_PHASE` has no `postmortem` key,
    so accepting the word would only defer the failure to a KeyError."""
    artifact = tmp_path / "postmortem.md"
    artifact.write_text("what went wrong")
    thread = mailbox.open_thread("outage review", kind="review")

    with pytest.raises(ValueError, match="invalid review stage: postmortem"):
        mailbox.submit_review_artifact(thread["id"], role="alpha",
                                       stage="postmortem", path=artifact)


# --- the broadcast token ----------------------------------------------------

def test_broadcast_token_is_not_a_two_party_word(desk):
    """`BROADCAST` is stored verbatim in `mailbox_messages.recipient`, so the
    word itself is the on-disk contract — and `both` is a claim about how many
    participants a thread has. The mailbox's own docstring says the review
    workflow generalized past two parties; the recipient column has to say the
    same thing.
    """
    assert mailbox.BROADCAST == "all"


def test_a_broadcast_in_a_three_role_thread_stores_all(desk):
    """The fossil, made visible by the third role: `alpha` addressing both
    `beta` and `gamma` is not addressing `both` of anything."""
    thread = mailbox.open_thread("who has the pager")
    message = mailbox.send_message(thread["id"], sender="alpha", recipient="all",
                                   kind="question", body="who is on call tonight?")

    assert message["recipient"] == "all"
    # And it is a real broadcast, not just a relabelled one.
    assert [m["id"] for m in mailbox.inbox("beta")] == [message["id"]]
    assert [m["id"] for m in mailbox.inbox("gamma")] == [message["id"]]


def test_both_is_accepted_on_input_but_never_written(desk):
    """`both` stays a READ alias. A host script still holding the old word must
    not start getting `invalid recipient` — but nothing stores it any more, so
    the alias can never reintroduce the fossil.
    """
    thread = mailbox.open_thread("old spelling")
    message = mailbox.send_message(thread["id"], sender="alpha", recipient="both",
                                   kind="note", body="written with the old word")

    assert message["recipient"] == "all"
    assert "both" in mailbox.BROADCAST_ALIASES


def test_legacy_both_rows_are_migrated_and_keep_delivering(desk):
    """The rename is only safe if it takes the existing rows with it.

    A database written by the two-role engine spells every broadcast `both`.
    After the rename nothing looks for that word, so an unmigrated row is a
    message that durably addresses nobody — the mailbox's one guarantee
    ("prove the message landed") failing silently, on rows that already landed.
    """
    thread = mailbox.open_thread("legacy rotation")
    with mailbox.connect() as conn:
        legacy_id = conn.execute(
            """INSERT INTO mailbox_messages
                   (thread_id, sender, recipient, kind, body, body_hash, created_at)
               VALUES (?, 'alpha', 'both', 'note', 'written by the old engine',
                       'deadbeefcafe', ?)""",
            (thread["id"], iso()),
        ).lastrowid

    # Any connect is the migration point: no entry point is privileged, because
    # any of them can be the first one a cold-started session opens.
    with mailbox.connect() as conn:
        assert conn.execute("SELECT recipient FROM mailbox_messages WHERE id=?",
                            (legacy_id,)).fetchone()[0] == "all"

    assert legacy_id in [m["id"] for m in mailbox.inbox("beta")]
    assert legacy_id in [m["id"] for m in mailbox.inbox("gamma")]


def test_a_writing_entry_point_survives_the_connect_that_migrates(desk):
    """The migration must not strand the transaction of whoever triggers it.

    sqlite3 holds an implicit transaction open for the UPDATE, so the connect
    that finds legacy rows hands the caller a connection already in a
    transaction — and every mailbox writer opens `BEGIN IMMEDIATE`. The failure
    is nastier than it looks: it fires on exactly one connect (the first writer
    to touch a legacy database, i.e. the upgrade itself) and never again, so a
    suite that migrates through a read path is green while the real upgrade
    path raises.
    """
    thread = mailbox.open_thread("legacy then write")
    with mailbox.connect() as conn:
        conn.execute(
            """INSERT INTO mailbox_messages
                   (thread_id, sender, recipient, kind, body, body_hash, created_at)
               VALUES (?, 'alpha', 'both', 'note', 'old broadcast', 'b17e', ?)""",
            (thread["id"], iso()),
        )

    # This single call both migrates the legacy row and opens a write txn.
    message = mailbox.send_message(thread["id"], sender="beta", recipient="all",
                                   kind="note", body="first write after the rename")

    assert message["recipient"] == "all"


def test_migrating_a_legacy_row_does_not_disturb_directed_rows(desk):
    """The migration is a rewrite of live data, so its blast radius is the
    point: only the token moves. A directed message to a role that happened to
    be named `both` would be a different row entirely — the WHERE clause must
    key on the recipient column's *token*, not on anything fuzzier.
    """
    thread = mailbox.open_thread("mixed rotation")
    directed = mailbox.send_message(thread["id"], sender="alpha", recipient="beta",
                                    kind="note", body="just for beta")
    with mailbox.connect() as conn:
        conn.execute(
            """INSERT INTO mailbox_messages
                   (thread_id, sender, recipient, kind, body, body_hash, created_at)
               VALUES (?, 'alpha', 'both', 'note', 'old broadcast', 'f00d', ?)""",
            (thread["id"], iso()),
        )

    with mailbox.connect() as conn:
        assert conn.execute("SELECT recipient FROM mailbox_messages WHERE id=?",
                            (directed["id"],)).fetchone()[0] == "beta"
        assert conn.execute(
            "SELECT COUNT(*) FROM mailbox_messages WHERE recipient='both'"
        ).fetchone()[0] == 0
    # gamma was never a recipient of the directed row and must not become one.
    assert directed["id"] not in [m["id"] for m in mailbox.inbox("gamma")]


# --- the transport's own reply bounds ---------------------------------------

def test_the_transport_refuses_stacked_requests_and_says_which_one(desk):
    """design.md: "The transport rejects duplicates and stacked unresolved
    questions." This is that claim's only real coverage.

    It used to be asserted one layer up, by a meetings test named for the
    transport — but meetings never passes `requires_reply`, so that test was
    green off the one_to_one turn-taking gate and the bound below never ran.
    When the gate went, the claim was left with nothing testing it at all.
    Meetings deliberately does not opt in (it tracks obligations instead, which
    a host may stack and settle in batches); this bound guards the callers who
    DO ask for it, so it is pinned where it actually lives.
    """
    thread = mailbox.open_thread("one open question at a time")
    first = mailbox.send_message(thread["id"], sender="alpha", recipient="beta",
                                 kind="question", body="does the thesis hold",
                                 requires_reply=True)

    with pytest.raises(ValueError, match=f"#{first['id']}"):
        mailbox.send_message(thread["id"], sender="alpha", recipient="beta",
                             kind="question", body="and what is the downside",
                             requires_reply=True)

    # The bound is per-recipient, not per-thread: gamma holds no open question,
    # so alpha asking gamma is a different conversation, not a stacked one.
    mailbox.send_message(thread["id"], sender="alpha", recipient="gamma",
                         kind="question", body="and what is the downside",
                         requires_reply=True)

    # A request that does not demand a reply was never bounded by this.
    mailbox.send_message(thread["id"], sender="alpha", recipient="beta",
                         kind="note", body="no answer needed on this one")
