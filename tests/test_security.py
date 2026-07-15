"""The trust boundary.

Every other module in deskd trusts a role string; `auth` is the one place that
decides whether a caller is really the supervisor. The threat model is concrete:
agents run as unprivileged sessions on this host, with write access to the
workspace and to the coordination DB. So these tests are written from the
attacker's side — each one is an agent trying a specific move:

* point verification at a key it wrote itself (the env tests);
* act as the supervisor through an ordinary agent API (the rejection tests);
* replay, back-date, or forward-date an assertion it observed (the ledger tests).

The signing keypair is generated inside the tests, per the security docs: no
signing utility or private-key generator belongs in this package, and the real
supervisor's private key never touches this host.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deskd import auth
from deskd import config as cfg_mod
from deskd import meetings, orchestration
from deskd.config import CONFIG

#: Where the key path is allowed to be, and nowhere else. Spelled out literally
#: rather than imported, so a commit that "makes the path configurable" has to
#: change this line and face the reason in the diff.
FIXED_KEY_PATH = Path("/etc/deskd/supervisor_ed25519.pub")


# --- the fixed key path -----------------------------------------------------

def _config_reimported_from_source():
    """Re-execute config.py against the current environment.

    A fresh module object under a throwaway name, so this observes exactly what
    a deskd process *started* with a hostile environment would compute, without
    disturbing the real `deskd.config` that every other module holds a reference
    to. config.py imports only stdlib, so it loads standalone.

    The throwaway name has to be in sys.modules while the module body runs —
    @dataclass resolves its annotations through it — and is dropped afterwards.
    """
    name = "deskd_config_under_hostile_env"
    spec = importlib.util.spec_from_file_location(name, Path(cfg_mod.__file__))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_trusted_key_constants_are_what_they_are():
    """These four constants *are* the signed-mode security parameters. Each one
    is load-bearing, so each one is pinned here rather than read from the code
    it is supposed to constrain."""
    assert cfg_mod.SUPERVISOR_PUBLIC_KEY_PATH == FIXED_KEY_PATH
    assert cfg_mod.SUPERVISOR_KEY_REQUIRE_ROOT is True
    assert cfg_mod.SUPERVISOR_ASSERTION_MAX_SECONDS == 600
    # auth must verify against the same constant, not its own copy of the idea.
    assert auth.SUPERVISOR_PUBLIC_KEY_PATH == FIXED_KEY_PATH


#: Every env var name a plausible refactor might reach for. `DESKD_HOME` and
#: `DESKD_DB` are in here because they are *real* knobs: if the key path were
#: ever hung off `BASE_DIR`, `DESKD_HOME` would quietly become a key-path
#: override, and an agent can set `DESKD_HOME`.
HOSTILE_KEY_ENV = (
    "DESKD_SUPERVISOR_PUBLIC_KEY_PATH",
    "DESKD_SUPERVISOR_PUBLIC_KEY",
    "DESKD_SUPERVISOR_KEY_PATH",
    "DESKD_SUPERVISOR_KEY",
    "DESKD_SUPERVISOR_PUB",
    "DESKD_PUBLIC_KEY_PATH",
    "DESKD_KEY_PATH",
    "SUPERVISOR_PUBLIC_KEY_PATH",
    "DESKD_HOME",
    "DESKD_DB",
)


@pytest.mark.parametrize("var", HOSTILE_KEY_ENV)
def test_no_env_var_moves_the_trusted_key_path(monkeypatch, tmp_path, var):
    """The most important test in this file.

    An agent owns its environment. If any env var could move the verification
    key path, an agent would generate its own keypair in a workspace it can
    write, point deskd at the public half, and sign itself supervisor authority
    — defeating signed mode completely and silently. The path must be a
    constant, so this asserts the constant survives a hostile environment at
    import time, which is the only time it could be read.
    """
    attacker_key = tmp_path / "attacker.pub"
    attacker_key.write_text("attacker-controlled")
    monkeypatch.setenv(var, str(attacker_key))

    fresh = _config_reimported_from_source()

    assert fresh.SUPERVISOR_PUBLIC_KEY_PATH == FIXED_KEY_PATH


@pytest.mark.parametrize("var", ["DESKD_SUPERVISOR_KEY_REQUIRE_ROOT",
                                 "DESKD_SUPERVISOR_REQUIRE_ROOT",
                                 "SUPERVISOR_KEY_REQUIRE_ROOT"])
def test_no_env_var_disables_the_key_ownership_gate(monkeypatch, var):
    """Same attack, one step to the side: leaving the path fixed but switching
    off the root-ownership check would let an agent rewrite the key in place at
    the fixed path (if it could ever write there). The gate is not a knob."""
    monkeypatch.setenv(var, "0")

    assert _config_reimported_from_source().SUPERVISOR_KEY_REQUIRE_ROOT is True


@pytest.mark.parametrize("var", ["DESKD_SUPERVISOR_ASSERTION_MAX_SECONDS",
                                 "SUPERVISOR_ASSERTION_MAX_SECONDS"])
def test_no_env_var_widens_the_assertion_window(monkeypatch, var):
    """An assertion valid for a year is a replay token. The <=10 minute cap is a
    security parameter, not a preference."""
    monkeypatch.setenv(var, "31536000")

    assert _config_reimported_from_source().SUPERVISOR_ASSERTION_MAX_SECONDS == 600


def test_runtime_verification_ignores_a_hostile_environment(monkeypatch, tmp_path):
    """The import-time tests above prove the constant; this proves the running
    verifier actually uses it, with every hostile var set at once and a real
    attacker keypair behind them."""
    attacker = Ed25519PrivateKey.generate()
    attacker_key = tmp_path / "attacker.pub"
    attacker_key.write_bytes(attacker.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    for var in HOSTILE_KEY_ENV:
        monkeypatch.setenv(var, str(attacker_key))
    monkeypatch.setenv("DESKD_SUPERVISOR_AUTH_MODE", "signed")

    assert auth.key_status()["path"] == str(FIXED_KEY_PATH)

    # An assertion signed by the attacker's key must not verify, whether the
    # fixed path is absent (nothing to verify against) or present (wrong key).
    raw = json.dumps({"version": 1, "action": "call", "nonce": "a" * 20,
                      "agenda": "seize the desk"}).encode()
    with pytest.raises(auth.AuthError):
        auth.verify_bytes(raw, attacker.sign(raw), actions={"call"})


# --- signing fixtures -------------------------------------------------------

@pytest.fixture
def signer(monkeypatch, tmp_path, desk):
    """A throwaway supervisor keypair, trusted for the duration of one test.

    Redirecting `auth`'s key path here is a test seam, not a supported one: it
    is done by patching the module attribute, which requires already running
    code in the deskd process. That is precisely the capability an agent does
    not have — and the env tests above are what keep it that way.

    `SUPERVISOR_KEY_REQUIRE_ROOT` is off because the suite does not run as root; the
    gate itself is asserted in `test_agent_writable_key_fails_closed`.
    """
    private = Ed25519PrivateKey.generate()
    key_path = tmp_path / "supervisor_ed25519.pub"
    key_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    key_path.chmod(0o644)

    monkeypatch.setenv("DESKD_SUPERVISOR_AUTH_MODE", "signed")
    monkeypatch.setattr(auth, "SUPERVISOR_PUBLIC_KEY_PATH", key_path)
    monkeypatch.setattr(auth, "SUPERVISOR_KEY_REQUIRE_ROOT", False)
    return _Signer(private, key_path)


class _Signer:
    def __init__(self, private: Ed25519PrivateKey, key_path: Path):
        self.private = private
        self.key_path = key_path

    def assertion(self, **overrides) -> tuple[bytes, bytes]:
        """A currently-valid signed `call` assertion, plus overrides."""
        now = dt.datetime.now(dt.timezone.utc)
        payload = {
            "version": 1,
            "action": "call",
            "nonce": "n" * 24,
            "issued_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + dt.timedelta(minutes=5)).isoformat(timespec="seconds"),
            "agenda": "review the numbers",
        }
        payload.update(overrides)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return raw, self.private.sign(raw)


# --- agent-facing APIs reject the supervisor --------------------------------

def test_inbox_enqueue_rejects_the_supervisor(desk):
    """The supervisor has no inbox — nothing would ever read it, and accepting
    the enqueue would let an agent fabricate supervisor-addressed traffic."""
    with pytest.raises(ValueError, match="not an agent role"):
        orchestration.inbox_enqueue(CONFIG.supervisor_role, "alert", "x")


def test_task_add_rejects_the_supervisor_assignee(desk):
    """A task assigned to the supervisor would sit on a board row that no
    session can ever pick up."""
    with pytest.raises(ValueError, match="not an agent role"):
        orchestration.task_add("do this", assignee_role=CONFIG.supervisor_role)


@pytest.mark.parametrize("call", [
    pytest.param(lambda sup: meetings.call_meeting(
        agenda="a", called_by=sup, attendees=["alpha"]), id="call_meeting"),
    pytest.param(lambda sup: meetings.check_in("t1", role=sup), id="check_in"),
    pytest.param(lambda sup: meetings.send_update("t1", role=sup, body="b"),
                 id="send_update"),
    pytest.param(lambda sup: meetings.submit_position("t1", role=sup, body="b"),
                 id="submit_position"),
    pytest.param(lambda sup: meetings.leave_meeting("t1", role=sup, reason="r"),
                 id="leave_meeting"),
    pytest.param(lambda sup: meetings.propose_end("t1", role=sup, resolution="r"),
                 id="propose_end"),
    pytest.param(lambda sup: meetings.confirm_end("t1", role=sup), id="confirm_end"),
    pytest.param(lambda sup: meetings.reject_end("t1", role=sup, reason="r"),
                 id="reject_end"),
    pytest.param(lambda sup: meetings.discover(sup), id="discover"),
    pytest.param(lambda sup: meetings.meeting_updates("t1", role=sup),
                 id="meeting_updates"),
    pytest.param(lambda sup: meetings.pause_meeting("t1", role=sup, reason="r"),
                 id="pause_meeting"),
    pytest.param(lambda sup: meetings.escalate_meeting("t1", role=sup, reason="r"),
                 id="escalate_meeting"),
    pytest.param(lambda sup: meetings.wake_requests(sup), id="wake_requests"),
    pytest.param(lambda sup: meetings.acknowledge_wake("t1", role=sup),
                 id="acknowledge_wake"),
])
def test_meeting_agent_apis_reject_the_supervisor(desk, call):
    """There is no `--role supervisor` path anywhere. Every agent-facing meeting
    entry point must refuse the identity *before* it does anything else — the
    role check comes first, which is why a bogus thread id never gets reached.
    """
    with pytest.raises(ValueError, match="not an agent role"):
        call(CONFIG.supervisor_role)


def test_agents_cannot_invite_the_supervisor(desk):
    """Attendance is authority: a supervisor sitting in a meeting can be quoted
    as having endorsed its outcome. An agent must escalate, not conscript."""
    with pytest.raises(ValueError, match="cannot add|escalation"):
        meetings.call_meeting(agenda="a", called_by="alpha",
                              attendees=["beta", CONFIG.supervisor_role])


# --- the nonce ledger -------------------------------------------------------

def test_valid_assertion_verifies_and_speaks_only_for_the_supervisor(signer):
    """The baseline the rejection tests are measured against — and the check
    that a verified assertion can never speak for an agent role."""
    raw, sig = signer.assertion()

    verified = auth.verify_bytes(raw, sig, actions={"call"},
                                 required_fields={"call": ["agenda"]})

    assert verified.action == "call"
    assert verified.actor == CONFIG.supervisor_role
    assert verified.payload["agenda"] == "review the numbers"


def test_nonce_is_single_use(signer):
    """Replay defence. An agent can read the nonce ledger and the transport, so
    it can see a supervisor assertion go by; burning the nonce on first use is
    what stops it from being re-submitted."""
    raw, sig = signer.assertion()
    verified = auth.verify_bytes(raw, sig, actions={"call"})

    auth.consume_nonce(verified)

    with pytest.raises(auth.AuthError, match="already been used"):
        auth.consume_nonce(verified)


def test_expired_assertion_is_rejected(signer):
    """Expiry is what bounds the replay window to ten minutes."""
    now = dt.datetime.now(dt.timezone.utc)
    raw, sig = signer.assertion(
        issued_at=(now - dt.timedelta(minutes=20)).isoformat(timespec="seconds"),
        expires_at=(now - dt.timedelta(minutes=15)).isoformat(timespec="seconds"))

    with pytest.raises(auth.AuthError, match="not currently valid"):
        auth.verify_bytes(raw, sig, actions={"call"})


def test_future_dated_assertion_is_rejected(signer):
    """A far-future assertion is a replay token minted in advance: sign once,
    hold it, use it whenever. Only real clock skew is tolerated."""
    now = dt.datetime.now(dt.timezone.utc)
    raw, sig = signer.assertion(
        issued_at=(now + dt.timedelta(hours=1)).isoformat(timespec="seconds"),
        expires_at=(now + dt.timedelta(hours=1, minutes=5)).isoformat(timespec="seconds"))

    with pytest.raises(auth.AuthError, match="not currently valid"):
        auth.verify_bytes(raw, sig, actions={"call"})


def test_assertion_lifetime_is_capped(signer):
    """Not currently-valid, but valid for a year: without the cap, a single
    signature would be a standing supervisor credential."""
    now = dt.datetime.now(dt.timezone.utc)
    raw, sig = signer.assertion(
        issued_at=now.isoformat(timespec="seconds"),
        expires_at=(now + dt.timedelta(days=365)).isoformat(timespec="seconds"))

    with pytest.raises(auth.AuthError, match="lifetime exceeds"):
        auth.verify_bytes(raw, sig, actions={"call"})


def test_naive_timestamps_are_rejected(signer):
    """A bare local time silently shifts the validity window by the host's UTC
    offset, which is a replay window and not a formatting nit."""
    now = dt.datetime.now()
    raw, sig = signer.assertion(
        issued_at=now.isoformat(timespec="seconds"),
        expires_at=(now + dt.timedelta(minutes=5)).isoformat(timespec="seconds"))

    with pytest.raises(auth.AuthError, match="must include timezone"):
        auth.verify_bytes(raw, sig, actions={"call"})


def test_tampered_payload_fails_the_signature(signer):
    """Invariant (2): the signature covers the whole decision. Editing the
    agenda of a signed call must invalidate it, or the signature guarantees
    nothing about what was actually authorised."""
    raw, sig = signer.assertion()
    tampered = raw.replace(b"review the numbers", b"wire the funds!!!!")
    assert len(tampered) == len(raw)  # same-length edit: only content changed

    with pytest.raises(auth.AuthError, match="invalid supervisor assertion signature"):
        auth.verify_bytes(tampered, sig, actions={"call"})


def test_assertion_signed_by_another_key_is_rejected(signer):
    """The forgery attempt in its plainest form: a keypair an agent generated
    itself, used against the trusted key."""
    impostor = Ed25519PrivateKey.generate()
    raw, _ = signer.assertion()

    with pytest.raises(auth.AuthError, match="invalid supervisor assertion signature"):
        auth.verify_bytes(raw, impostor.sign(raw), actions={"call"})


def test_action_outside_the_caller_allowlist_is_rejected(signer):
    """The allowlist is the caller's, checked before it ever sees the payload:
    a privileged verb added downstream must not silently become reachable."""
    raw, sig = signer.assertion(action="force_close", meeting_id="t1")

    with pytest.raises(auth.AuthError, match="unsupported supervisor assertion"):
        auth.verify_bytes(raw, sig, actions={"call", "join"})


def test_required_fields_must_be_signed(signer):
    """Invariant (2) again, from the other end: a caller may only read action
    inputs the signature actually covered, so an assertion missing them is
    refused rather than allowed to KeyError halfway through a mutation."""
    raw, sig = signer.assertion(agenda="   ")

    with pytest.raises(auth.AuthError, match="must sign a non-empty 'agenda'"):
        auth.verify_bytes(raw, sig, actions={"call"},
                          required_fields={"call": ["agenda"]})


def test_claim_refuses_a_nonce_the_ledger_does_not_back(desk, signer):
    """Invariant (4): the ledger row is the receipt. This is the check that
    stops an agent writing a supervisor-attributed row directly into the DB —
    without a matching, correctly-bound ledger row, the row is forged.
    """
    raw, sig = signer.assertion(action="join", meeting_id="t1", nonce="j" * 24)
    verified = auth.verify_bytes(raw, sig, actions={"join"})
    auth.consume_nonce(verified)

    with auth.connect() as conn:
        # The happy path, so the failures below are attributable.
        assert auth.claim(conn, "j" * 24, {"join"}, meeting_id="t1")

        with pytest.raises(auth.AuthError, match="lacks a verified assertion"):
            auth.claim(conn, None, {"join"})
        with pytest.raises(auth.AuthError, match="not valid for this action"):
            auth.claim(conn, "never-minted-nonce", {"join"})
        with pytest.raises(auth.AuthError, match="not valid for this action"):
            auth.claim(conn, "j" * 24, {"force_close"})
        with pytest.raises(auth.AuthError, match="bound to a different meeting"):
            auth.claim(conn, "j" * 24, {"join"}, meeting_id="t2")


# --- fail closed ------------------------------------------------------------

def test_missing_key_fails_closed(monkeypatch, tmp_path):
    """No key means signed supervisor participation is DISABLED, not waved
    through. A missing-file default of "accept" would be the whole boundary."""
    monkeypatch.setenv("DESKD_SUPERVISOR_AUTH_MODE", "signed")
    monkeypatch.setattr(auth, "SUPERVISOR_PUBLIC_KEY_PATH", tmp_path / "absent.pub")

    status = auth.key_status()
    assert status["present"] is False
    assert status["usable"] is False
    assert status["problem"]

    private = Ed25519PrivateKey.generate()
    raw = json.dumps({"version": 1, "action": "call", "nonce": "z" * 20}).encode()
    with pytest.raises(auth.AuthError, match="identity is disabled|key missing"):
        auth.verify_bytes(raw, private.sign(raw), actions={"call"})


def test_agent_writable_key_fails_closed(monkeypatch, tmp_path):
    """The literal attack: a key file written by an unprivileged user — exactly
    what an agent is — must not be trusted. The suite runs as uid != 0, so the
    key file it just wrote is precisely the artefact the ownership gate exists
    to reject, world-writable mode on top.
    """
    private = Ed25519PrivateKey.generate()
    key_path = tmp_path / "supervisor_ed25519.pub"
    key_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    key_path.chmod(0o666)
    monkeypatch.setenv("DESKD_SUPERVISOR_AUTH_MODE", "signed")
    monkeypatch.setattr(auth, "SUPERVISOR_PUBLIC_KEY_PATH", key_path)
    monkeypatch.setattr(auth, "SUPERVISOR_KEY_REQUIRE_ROOT", True)

    assert auth.key_status()["usable"] is False

    raw = json.dumps({"version": 1, "action": "call", "nonce": "z" * 20}).encode()
    with pytest.raises(auth.AuthError, match="root-owned"):
        auth.verify_bytes(raw, private.sign(raw), actions={"call"})


def test_non_ed25519_key_fails_closed(monkeypatch, tmp_path):
    """An RSA key at the trusted path is not a type nit: verification would run
    a different algorithm than the one this protocol's security rests on."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "supervisor_ed25519.pub"
    key_path.write_bytes(rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setenv("DESKD_SUPERVISOR_AUTH_MODE", "signed")
    monkeypatch.setattr(auth, "SUPERVISOR_PUBLIC_KEY_PATH", key_path)
    monkeypatch.setattr(auth, "SUPERVISOR_KEY_REQUIRE_ROOT", False)

    assert auth.key_status()["usable"] is False

    raw = json.dumps({"version": 1, "action": "call", "nonce": "z" * 20}).encode()
    signature = rsa_key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    with pytest.raises(auth.AuthError, match="must be Ed25519"):
        auth.verify_bytes(raw, signature, actions={"call"})


def test_signed_mode_is_off_by_default(monkeypatch, signer):
    """Signed mode is opt-in (the default is `simple`), and `verify_bytes`
    re-checks the gate itself: a host that forgets must not be able to open the
    signed path by accident."""
    monkeypatch.delenv("DESKD_SUPERVISOR_AUTH_MODE", raising=False)
    raw, sig = signer.assertion()

    with pytest.raises(auth.AuthError, match="signed supervisor authentication is disabled"):
        auth.verify_bytes(raw, sig, actions={"call"})
