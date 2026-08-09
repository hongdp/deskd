"""Launch one agent turn through the provider seam — the built-in driver arm.

This is the piece that used to live only in the reference bash script (and
only for Claude Code): resolve the role's runtime tuning, hand it to the
registered provider, execute the command with the provider's env overlaid,
and keep presence honest around the launch. The bash driver still owns
cron-side concerns (per-role file locks, timeouts, log routing); it calls
``deskd agent run`` for everything harness-shaped.

Two invariants every driver must keep, so they live here and not in scripts:

- **Sessions never cross providers.** A session id minted by one harness is
  meaningless to another; resuming it there is corruption, not migration.
  The provider that owns a session is recorded in the presence ``harness``
  field as ``<base>#<provider>`` at spawn, and resume refuses on mismatch.
  Legacy rows without the suffix are read as the default provider — exactly
  the sessions that existed before providers did.
- **A failed launch must not look like a served wake.** Nothing here acks
  demands or marks deliveries; a non-zero exit leaves the demand pending and
  the wake ladder climbing, which is the honest outcome.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ..providers import LaunchSpec, get_provider
from .presence import feed_append, set_status
from .runtime import role_runtime
from .store import connect


def session_provider(role: str, session_id: str,
                     db_path: Path | str | None = None) -> str:
    """Provider that owns the role's recorded session, or raise if the
    recorded session is not the one the caller is about to resume."""
    from ..config import CONFIG
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT session_id, harness FROM agent_sessions WHERE role=?",
            (role,)).fetchone()
    if row is None or row["session_id"] != session_id:
        raise ValueError(
            f"role {role!r} has no recorded session {session_id!r}; "
            f"refusing a blind resume")
    harness = row["harness"] or ""
    if "#" in harness:
        return harness.rsplit("#", 1)[1]
    return CONFIG.default_provider


def build_launch(role: str, mode: str, session_id: str, prompt: str, *,
                 default_allowed_tools: tuple[str, ...] | None = None,
                 db_path: Path | str | None = None) -> tuple[list[str], dict, str]:
    """(argv, env_overrides, provider_name) for one launch — pure enough to
    test and to print (``deskd agent command``) without executing."""
    if mode not in {"spawn", "resume"}:
        raise ValueError(f"unsupported mode: {mode}")
    rt = role_runtime(role, db_path=db_path)
    provider_name = rt["provider"]
    if mode == "resume":
        owner = session_provider(role, session_id, db_path=db_path)
        if owner != provider_name:
            # The registry changed hands since this session was born. The
            # session still belongs to its creator: resume THERE; the new
            # provider takes over at the next cold spawn.
            provider_name = owner
    provider = get_provider(provider_name)
    # A role that declares no grant gets the DRIVER's default, never the
    # harness's widest one — the same fail-closed direction the reference
    # script has always taken (its DESKD_WAKE_ALLOWED_TOOLS default).
    allowed = rt["allowed_tools"] or default_allowed_tools
    spec = LaunchSpec(role=role, mode=mode, session_id=session_id,
                     prompt=prompt, model=rt["model"],
                     reasoning=rt["reasoning"],
                     allowed_tools=allowed)
    return provider.command(spec), provider.environment(spec), provider_name


def _feed_lines(event: dict):
    """The feed rows one stream event implies, as (kind, text) pairs.

    Pure, so the event vocabulary can be tested without launching anything.
    Unknown shapes yield nothing: a harness that grows a new event type must
    never be able to break a turn, and inventing a row from a shape we do not
    understand would be worse than silence.
    """
    kind = event.get("type")
    if kind == "assistant":
        message = event.get("message")
        if isinstance(message, dict):
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        yield "narration", text
    elif kind == "stream_event":
        inner = event.get("event") or {}
        if not isinstance(inner, dict):
            return
        block = inner.get("content_block") or {}
        # Only the START of a thinking block. The deltas arrive empty (the
        # harness redacts the content), so one marker per block says exactly
        # what is known — "it is thinking now" — while a row per delta would
        # dress five empty payloads up as five events worth reading.
        if (inner.get("type") == "content_block_start"
                and isinstance(block, dict)
                and block.get("type") == "thinking"):
            yield "thinking", ""


def _run_streaming(command, env, timeout, role, session_id, db_path) -> int:
    """Run the child, forward its stdout verbatim, and file what it narrates.

    Forwarding first is the compatibility contract: whoever reads this
    process's stdout today — a human tailing the cron log, the wake driver's
    capture — must see exactly what they saw before capture existed. Parsing
    is strictly additional, and every parse failure is ignored.
    """
    proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                            text=True, bufsize=1)
    try:
        for line in proc.stdout:            # type: ignore[union-attr]
            sys.stdout.write(line)
            sys.stdout.flush()
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue                    # not ours; already forwarded
            if not isinstance(event, dict):
                continue
            for kind, text in _feed_lines(event):
                feed_append(role, session_id, kind, text, db_path=db_path)
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    finally:
        if proc.stdout:
            proc.stdout.close()


def run_agent(role: str, mode: str, session_id: str, prompt: str, *,
              harness_base: str = "deskd-agent",
              timeout: int | None = None,
              default_allowed_tools: tuple[str, ...] | None = None,
              db_path: Path | str | None = None) -> int:
    """Execute one agent turn. Returns the child's exit code (75 = a
    preflight refusal: infrastructure, not the model — retry later)."""
    command, env_overrides, provider_name = build_launch(
        role, mode, session_id, prompt,
        default_allowed_tools=default_allowed_tools, db_path=db_path)
    health = get_provider(provider_name).preflight()
    if not health.get("ok"):
        print(f"[deskd-agent] preflight failed for provider={provider_name}: "
              f"{health.get('message', health)}")
        return 75
    harness = f"{harness_base}#{provider_name}"
    set_status(role, state="booting" if mode == "spawn" else "working",
               session_id=session_id, harness=harness, db_path=db_path)
    env = dict(os.environ)
    env.update(env_overrides)
    try:
        if get_provider(provider_name).streams:
            code = _run_streaming(command, env, timeout, role, session_id,
                                  db_path)
        else:
            proc = subprocess.run(command, env=env, timeout=timeout)
            code = proc.returncode
    except subprocess.TimeoutExpired:
        code = 124
    finally:
        # Park, never end: the session remains resumable; rollover retires it.
        set_status(role, state="idle_standby", session_id=session_id,
                   harness=harness, db_path=db_path)
    return code
