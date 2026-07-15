"""Supervisor authentication — the engine's trust boundary.

Every other module in deskd trusts a role string. This module is the only place
that decides whether a caller is *actually* the supervisor, so it is the one
file where paranoia beats convenience.

The threat model is specific: agents run as unprivileged sessions on this host,
with write access to the workspace and to the coordination DB. An agent must
never be able to manufacture a supervisor action — not by forging a payload,
not by pointing verification at a key it wrote itself, and not by replaying a
supervisor action it observed. That yields four invariants:

1. **The private key is never on this host.** The supervisor signs on a trusted
   device; deskd only ever holds the Ed25519 *public* key, at a path that is
   fixed in ``config`` and deliberately NOT environment-overridable (an
   agent-settable env var would let an agent swap in its own key). The key file
   must be root-owned and not group/world writable, otherwise an agent could
   rewrite it in place.
2. **Signatures cover the whole decision.** The signed bytes are the payload:
   action, target, and the action-specific fields (agenda, body, resolution,
   reason...). Downstream code must read those fields from the *signed* payload,
   never from an unsigned sidecar, or the signature guarantees nothing.
3. **Assertions are short-lived and single-use.** Validity is capped at
   ``SUPERVISOR_ASSERTION_MAX_SECONDS``, and every nonce is burned into the
   ``supervisor_nonces`` ledger by a PRIMARY KEY insert. The insert happens
   *before* the action runs: a nonce is spent whether or not the action
   succeeds, so a failed action can never be retried by replaying its assertion.
4. **The ledger row is the receipt.** Everything the supervisor does elsewhere
   in the engine carries an ``auth_nonce`` FK into ``supervisor_nonces``, and
   :func:`claim` re-checks that the row exists, authorises *this* action, and is
   bound to *this* meeting. A supervisor-attributed row without a matching
   ledger row is by definition forged.

There is no ``--role supervisor`` agent command anywhere in deskd, and
agent-facing APIs reject ``CONFIG.supervisor_role``. Supervisor actions enter
only through the authenticated Web adapter, in one of two modes:

* ``signed`` — short-lived Ed25519 assertions from a trusted device (strict).
* ``simple`` — a shared access code over a trusted local network. This mode
  trades cryptographic identity for convenience and is honest about it: it still
  mints a ledger row so the audit trail and single-use semantics survive, but it
  cannot prove *who* acted. ``hybrid`` allows both.

This module is domain-agnostic and self-contained: it owns its own schema and
connection so it never depends on the modules that depend on it. It knows no
action verbs at all — the caller passes the allowlist it is willing to honour
(and the fields each of its actions must sign), and the behaviour of an action
lives entirely in that caller. deskd's only caller today is the meetings module,
which owns the meeting-lifecycle verbs.
"""

from __future__ import annotations

import base64
import datetime as dt
import hmac
import json
import os
import secrets
import sqlite3
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from .config import (
    CONFIG,
    SUPERVISOR_ASSERTION_MAX_SECONDS,
    SUPERVISOR_KEY_REQUIRE_ROOT,
    SUPERVISOR_PUBLIC_KEY_PATH,
    env,
)

__all__ = [
    "AuthError",
    "SCHEMA",
    "VerifiedAssertion",
    "ensure_schema",
    "connect",
    "key_status",
    "auth_mode",
    "simple_auth_enabled",
    "signed_auth_enabled",
    "simple_access_code",
    "access_code_is_ephemeral",
    "verify_access_code",
    "verify_bytes",
    "verify_files",
    "mint_simple",
    "consume_nonce",
    "claim",
    "supervisor_role",
]


class AuthError(ValueError):
    """A supervisor assertion was absent, malformed, invalid, or replayed.

    Subclasses ValueError so hosts that already funnel ValueError into a 400/401
    keep working. Messages are safe to surface: they never echo key material,
    signatures, or the access code.
    """


# --- verification parameters ------------------------------------------------

#: Type of the caller-supplied action allowlist. An action outside the set the
#: caller passes is rejected at verification time, before the caller sees the
#: payload — so a privileged verb added downstream cannot silently widen the
#: trust boundary without also being added to the allowlist.
ActionAllowlist = Collection[str]

#: Type of the caller-supplied "fields this action must sign" map. This is
#: invariant (2) made explicit: a caller may only read action inputs that the
#: signature actually covered, so verification refuses a payload that lacks them
#: rather than letting the caller KeyError halfway through a mutation.
RequiredFields = Mapping[str, Sequence[str]]

#: Clock skew tolerated on ``issued_at`` for a trusted device whose clock runs
#: slightly ahead. Deliberately small — it widens the replay window.
_ISSUED_AT_SKEW_SECONDS = 60

_NONCE_MIN_CHARS = 16
_NONCE_MAX_CHARS = 128


# --- schema -----------------------------------------------------------------

#: The nonce ledger. Other engine tables carry ``auth_nonce TEXT REFERENCES
#: supervisor_nonces(nonce)`` FKs, so this table must exist before theirs — see
#: :func:`ensure_schema`.
#:
#: The PRIMARY KEY *is* the replay defence — do not add ``OR IGNORE`` to the
#: insert in :func:`consume_nonce`. The full payload and signature are retained
#: so an auditor can re-verify any historical supervisor action offline.
SCHEMA = """
CREATE TABLE IF NOT EXISTS supervisor_nonces (
    nonce                 TEXT PRIMARY KEY,
    action                TEXT NOT NULL,
    payload               TEXT NOT NULL,
    signature_b64         TEXT NOT NULL,
    verified_at           TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the nonce ledger if absent.

    Modules whose tables hold an ``auth_nonce`` FK into ``supervisor_nonces``
    must call this (or import this module's :func:`connect`) *before* creating
    those tables, so the referenced table always exists first.
    """
    conn.executescript(SCHEMA)


@contextmanager
def connect(db_path: Path | str | None = None, *,
            write: bool = False) -> Iterator[sqlite3.Connection]:
    """Open the coordination DB with the nonce ledger present.

    Self-contained on purpose: this module must not import the modules that
    import it. Callers that already hold a transaction should pass their own
    connection to :func:`consume_nonce` / :func:`claim` instead.

    ``write=True`` takes an IMMEDIATE transaction so a concurrent nonce insert
    fails fast rather than deadlocking at COMMIT.
    """
    path = Path(db_path or CONFIG.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        ensure_schema(conn)
        conn.commit()
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.commit()
    except BaseException:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()


# --- time -------------------------------------------------------------------

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime | None = None) -> str:
    return (value or _now()).astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: object, label: str) -> dt.datetime:
    """Parse a signed timestamp. Naive timestamps are rejected rather than
    guessed: a bare local time would silently shift the validity window by the
    host's UTC offset, which is a replay window, not a formatting nit."""
    if not isinstance(value, str) or not value:
        raise AuthError(f"supervisor assertion is missing {label}")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthError(f"supervisor assertion has an unparseable {label}") from exc
    if parsed.tzinfo is None:
        raise AuthError("supervisor assertion timestamps must include timezone")
    return parsed.astimezone(dt.timezone.utc)


def supervisor_role() -> str:
    """The identity a verified assertion speaks for. Never a valid agent role."""
    return CONFIG.supervisor_role


# --- verified assertion -----------------------------------------------------

@dataclass(frozen=True)
class VerifiedAssertion:
    """A supervisor claim that passed every check in :func:`verify_bytes`.

    Holding one means: the signature over ``raw`` verified against the trusted
    key, the action is allowlisted, the payload carries the fields that action
    needs, and the assertion is currently within its (<=10 minute) window. It
    does NOT mean the nonce has been spent — that is :func:`consume_nonce`.

    :func:`mint_simple` returns one of these too, for an action authenticated by
    the access code rather than a signature. Everything downstream — the ledger,
    single-use, and the :func:`claim` binding — then behaves identically; only
    the strength of the identity differs, which ``payload["auth_mode"]`` records.
    """

    payload: dict
    raw: bytes
    signature: bytes

    @property
    def action(self) -> str:
        return str(self.payload["action"])

    @property
    def nonce(self) -> str:
        return str(self.payload["nonce"])

    @property
    def meeting_id(self) -> str | None:
        value = self.payload.get("meeting_id")
        return str(value) if value else None

    @property
    def actor(self) -> str:
        """Verified identity. Always the supervisor — an assertion cannot speak
        for an agent role, so handlers must attribute actions to this, not to a
        role name taken from the payload."""
        return supervisor_role()


# --- trusted key ------------------------------------------------------------

def _load_trusted_key() -> Ed25519PublicKey:
    """Load the supervisor public key, refusing anything an agent could subvert.

    The path comes from ``config`` and is not env-overridable. Ownership and
    mode are re-checked on every verification, not cached: a key that becomes
    agent-writable after startup must stop being trusted immediately.
    """
    if not SUPERVISOR_PUBLIC_KEY_PATH.is_file():
        raise AuthError(
            "supervisor identity is disabled: trusted public key missing at "
            f"{SUPERVISOR_PUBLIC_KEY_PATH}"
        )
    stat = SUPERVISOR_PUBLIC_KEY_PATH.stat()
    # st_mode & 0o022 == group- or world-writable. Either would let an
    # unprivileged agent replace the key with one it holds the private half of.
    if SUPERVISOR_KEY_REQUIRE_ROOT and (stat.st_uid != 0 or stat.st_mode & 0o022):
        raise AuthError(
            "supervisor public key must be root-owned and not group/world writable"
        )
    try:
        key = load_pem_public_key(SUPERVISOR_PUBLIC_KEY_PATH.read_bytes())
    except Exception as exc:
        raise AuthError("supervisor public key is not a readable PEM public key") from exc
    # Not merely a type nit: an RSA/EC key here would mean verify() below runs a
    # different algorithm than the one this protocol's security rests on.
    if not isinstance(key, Ed25519PublicKey):
        raise AuthError("supervisor public key must be Ed25519")
    return key


def key_status() -> dict:
    """Non-secret description of the trusted key, for consoles/health checks.

    Reports only presence and whether the file passes the ownership/mode gate —
    never key bytes, and never the access code.
    """
    status: dict = {
        "path": str(SUPERVISOR_PUBLIC_KEY_PATH),
        "present": SUPERVISOR_PUBLIC_KEY_PATH.is_file(),
        "require_root": SUPERVISOR_KEY_REQUIRE_ROOT,
        "usable": False,
        "problem": None,
    }
    try:
        _load_trusted_key()
        status["usable"] = True
    except AuthError as exc:
        status["problem"] = str(exc)
    return status


def _decode_signature(signature_raw: bytes) -> bytes:
    """Accept a raw 64-byte Ed25519 signature or its base64 form.

    base64 is validated strictly: lenient decoding would let two distinct
    encodings map to one signature, and sloppy input should fail loudly here
    rather than become a confusing verification error.
    """
    if len(signature_raw) == 64:
        return signature_raw
    encoded = signature_raw.strip()
    try:
        signature = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise AuthError(
            "supervisor signature must be raw Ed25519 bytes or base64"
        ) from exc
    if len(signature) != 64:
        raise AuthError("supervisor Ed25519 signature must be 64 bytes")
    return signature


def _check_action(action: object, actions: ActionAllowlist) -> str:
    if not isinstance(action, str) or action not in actions:
        raise AuthError("unsupported supervisor assertion version/action")
    return action


def _check_nonce(payload: dict) -> str:
    nonce = str(payload.get("nonce", ""))
    if not _NONCE_MIN_CHARS <= len(nonce) <= _NONCE_MAX_CHARS:
        raise AuthError(
            f"supervisor assertion nonce must be {_NONCE_MIN_CHARS}.."
            f"{_NONCE_MAX_CHARS} characters"
        )
    return nonce


def _check_required_fields(payload: dict, action: str,
                           required_fields: RequiredFields | None) -> None:
    """Invariant (2): the action's inputs must be inside the signed payload."""
    for field in (required_fields or {}).get(action, ()):
        value = payload.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise AuthError(
                f"supervisor assertion for '{action}' must sign a non-empty '{field}'"
            )


def verify_bytes(raw: bytes, signature_raw: bytes, *, actions: ActionAllowlist,
                 required_fields: RequiredFields | None = None) -> VerifiedAssertion:
    """Verify a signed supervisor assertion. Raises :class:`AuthError` on any doubt.

    Order matters: the signature is checked against the trusted key *before* the
    payload is interpreted, so no unverified content ever steers a decision.

    Checks, in order: signed mode is enabled; key present + root-owned + not
    group/world writable + is Ed25519; signature is 64 raw bytes (or strict
    base64 of them) and verifies over ``raw``; payload is a JSON object with
    ``version == 1``; action is in ``actions``; the action's ``required_fields``
    are present and signed; the assertion is currently valid (``issued_at`` no
    more than 60s in the future, ``expires_at`` not past) with a lifetime <=
    ``SUPERVISOR_ASSERTION_MAX_SECONDS``; nonce is 16..128 characters. Single-use is
    enforced separately, by :func:`consume_nonce`.

    The mode gate is re-checked here as well as in the Web adapter (which needs
    it earlier, to answer 403 rather than 400): a host that forgets must not be
    able to open the signed path by accident.
    """
    if not signed_auth_enabled():
        raise AuthError("signed supervisor authentication is disabled")
    key = _load_trusted_key()
    signature = _decode_signature(signature_raw)
    try:
        key.verify(signature, raw)
    except InvalidSignature as exc:
        raise AuthError("invalid supervisor assertion signature") from exc

    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise AuthError("supervisor assertion must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise AuthError("supervisor assertion must be a JSON object")

    if payload.get("version") != 1:
        raise AuthError("unsupported supervisor assertion version/action")
    action = _check_action(payload.get("action"), actions)
    _check_required_fields(payload, action, required_fields)

    now = _now()
    issued = _parse_time(payload.get("issued_at"), "issued_at")
    expires = _parse_time(payload.get("expires_at"), "expires_at")
    if issued > now + dt.timedelta(seconds=_ISSUED_AT_SKEW_SECONDS) or expires < now:
        raise AuthError("supervisor assertion is not currently valid")
    if expires - issued > dt.timedelta(seconds=SUPERVISOR_ASSERTION_MAX_SECONDS):
        raise AuthError(
            "supervisor assertion lifetime exceeds "
            f"{SUPERVISOR_ASSERTION_MAX_SECONDS // 60} minutes"
        )
    _check_nonce(payload)
    return VerifiedAssertion(payload=payload, raw=raw, signature=signature)


def verify_files(assertion_path: str | Path, signature_path: str | Path, *,
                 actions: ActionAllowlist,
                 required_fields: RequiredFields | None = None) -> VerifiedAssertion:
    """Verify an assertion supplied as two files (payload JSON + signature)."""
    return verify_bytes(
        Path(assertion_path).read_bytes(), Path(signature_path).read_bytes(),
        actions=actions, required_fields=required_fields,
    )


# --- nonce ledger -----------------------------------------------------------

def consume_nonce(verified: VerifiedAssertion, *,
                  conn: sqlite3.Connection | None = None,
                  db_path: Path | str | None = None) -> None:
    """Burn the assertion's nonce. Raises :class:`AuthError` if already used.

    Single-use rests entirely on the PRIMARY KEY: the insert is the atomic
    test-and-set, so there is no check-then-act race between concurrent
    replays of the same assertion.

    Callers must invoke this *before* performing the action, never after.
    Consuming first means a nonce is spent whether the action succeeds or
    fails, which is the point: a rejected action must not leave a live
    assertion an observer could replay.
    """
    row = (
        verified.nonce,
        verified.action,
        verified.raw.decode("utf-8", "replace"),
        base64.b64encode(verified.signature).decode(),
        _iso(),
    )
    sql = """INSERT INTO supervisor_nonces
                 (nonce,action,payload,signature_b64,verified_at)
             VALUES (?,?,?,?,?)"""
    if conn is not None:
        try:
            conn.execute(sql, row)
        except sqlite3.IntegrityError as exc:
            raise AuthError("supervisor assertion nonce has already been used") from exc
        return
    with connect(db_path, write=True) as own:
        try:
            own.execute(sql, row)
        except sqlite3.IntegrityError as exc:
            raise AuthError("supervisor assertion nonce has already been used") from exc


def claim(conn: sqlite3.Connection, auth_nonce: str | None,
          actions: ActionAllowlist, *,
          meeting_id: str | None = None) -> dict:
    """Re-check a spent nonce against the ledger and return its signed payload.

    Invariant (4). Every engine write attributed to the supervisor must call
    this first: the ledger row is the proof that the action was authenticated,
    and this re-check is what stops a caller from attributing a row to the
    supervisor with a nonce that was never verified, was verified for a
    *different* action, or was verified for a *different* meeting.

    Keep this requirement even in ``simple`` mode. The row is weaker evidence
    there (a shared code, not a signature), but the binding checks still hold
    and the audit trail stays uniform.
    """
    if not auth_nonce:
        raise AuthError("supervisor action lacks a verified assertion")
    row = conn.execute(
        "SELECT action,payload FROM supervisor_nonces WHERE nonce=?", (auth_nonce,)
    ).fetchone()
    if not row or row["action"] not in set(actions):
        raise AuthError("supervisor assertion is not valid for this action")
    payload = json.loads(row["payload"])
    if meeting_id and payload.get("meeting_id") != meeting_id:
        raise AuthError("supervisor assertion is bound to a different meeting")
    return payload


# --- simple mode: minting an equivalent claim -------------------------------

def mint_simple(action_payload: dict, *, actions: ActionAllowlist,
                required_fields: RequiredFields | None = None) -> VerifiedAssertion:
    """Mint a claim for an action authenticated only by the access code.

    Explicitly the weak path, and only reachable when ``DESKD_SUPERVISOR_AUTH_MODE``
    is ``simple`` or ``hybrid``. The caller (the Web adapter) MUST have already
    checked the access code with :func:`verify_access_code`; the mode gate is
    re-checked here so a host that forgets cannot open the weak path by accident.

    There is no signature to verify, so this mints an equivalent payload
    server-side — short-lived, single-use, tagged ``auth_mode="simple"`` so an
    auditor can tell code-authenticated actions from cryptographically signed
    ones. The action allowlist and the required-field check are applied exactly
    as in :func:`verify_bytes`, and the returned claim still has to go through
    :func:`consume_nonce` and :func:`claim` — so the ledger, single-use, and
    binding checks behave identically in both modes.
    """
    if not simple_auth_enabled():
        raise AuthError("simplified supervisor authentication is disabled")
    if not isinstance(action_payload, dict):
        raise AuthError("supervisor action payload must be an object")
    payload = dict(action_payload)
    action = _check_action(payload.get("action"), actions)
    _check_required_fields(payload, action, required_fields)
    now = _now()
    # Server-minted: the client cannot choose its own nonce or window here, so a
    # captured request body cannot be replayed as a fresh action.
    payload.update({
        "version": 1,
        "nonce": f"simple-{os.urandom(16).hex()}",
        "issued_at": _iso(now),
        "expires_at": _iso(now + dt.timedelta(minutes=5)),
        "auth_mode": "simple",
    })
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return VerifiedAssertion(payload=payload, raw=raw, signature=b"simple-web-auth")


# --- simple mode: access code ----------------------------------------------

_VALID_MODES = ("simple", "signed", "hybrid")
_EPHEMERAL_CODE: str | None = None


def auth_mode() -> str:
    """``simple`` | ``signed`` | ``hybrid`` from ``DESKD_SUPERVISOR_AUTH_MODE``.

    One name runs through the whole trust boundary: the ``SUPERVISOR_`` env
    knobs, the ``config.SUPERVISOR_*`` constants, the role
    ``CONFIG.supervisor_role``, and the ``supervisor_nonces`` ledger. An
    operator reading a variable name never has to map it onto a different term
    to find what it controls. Note that only the *mode* and the access code are
    env-settable — ``config.SUPERVISOR_PUBLIC_KEY_PATH`` is fixed on purpose, so
    no env var can point verification at an agent-written key.

    Note the default (``simple``) leaves the signed path DISABLED. Set
    ``signed`` or ``hybrid`` to accept Ed25519 assertions.
    """
    mode = (env("SUPERVISOR_AUTH_MODE") or "simple").strip().lower()
    if mode not in _VALID_MODES:
        raise AuthError(
            "DESKD_SUPERVISOR_AUTH_MODE must be one of: " + ", ".join(_VALID_MODES)
        )
    return mode


def simple_auth_enabled() -> bool:
    return auth_mode() in {"simple", "hybrid"}


def signed_auth_enabled() -> bool:
    return auth_mode() in {"signed", "hybrid"}


def simple_access_code() -> str | None:
    """The shared access code for ``simple`` mode, or None if the mode is off.

    Sourced from ``DESKD_SUPERVISOR_ACCESS_CODE``. If simple mode is enabled but no
    code is configured, one random code is generated per process — never a
    literal default, because a checked-in default code is a published password.
    The caller decides how to surface it (typically printed once at startup);
    this module never logs it.
    """
    global _EPHEMERAL_CODE
    if not simple_auth_enabled():
        return None
    configured = env("SUPERVISOR_ACCESS_CODE")
    if configured:
        return configured
    if _EPHEMERAL_CODE is None:
        _EPHEMERAL_CODE = secrets.token_urlsafe(9)
    return _EPHEMERAL_CODE


def access_code_is_ephemeral() -> bool:
    """True when the code was generated for this process rather than configured
    — i.e. it dies on restart and should be shown to the operator."""
    return simple_auth_enabled() and not env("SUPERVISOR_ACCESS_CODE")


def verify_access_code(presented: str | None) -> bool:
    """Constant-time access-code check. Use this — never ``==``.

    A plain string compare leaks the code one character at a time through timing.
    An empty/absent presented code is always false, so a missing header can never
    match a missing configuration.
    """
    expected = simple_access_code()
    if not expected or not presented:
        return False
    return hmac.compare_digest(str(presented), str(expected))
