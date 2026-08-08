"""The provider seam: default providers work out of the box, custom ones plug
in by registration, per-role tuning is engine state, and the two driver
invariants (fail-closed grants, no cross-provider resume) hold.

Every assertion is a clause of the promise the README makes to a fresh
clone: `pip install deskd` → the claude provider launches with your role's
model/reasoning/grant; any other CLI becomes a provider via a template."""

from __future__ import annotations

import pytest

from deskd import orchestration as orch
from deskd.config import CONFIG
from deskd.orchestration import agent_run
from deskd.orchestration import runtime as rt
from deskd.providers import (ClaudeCodeProvider, CommandProvider, LaunchSpec,
                             get_provider, registry)

from conftest import ROLES  # noqa: F401  (fixture roles: alpha/beta/gamma)


def _spec(**kw):
    base = dict(role="alpha", mode="spawn", session_id="s-1", prompt="wake up")
    base.update(kw)
    return LaunchSpec(**base)


# --- built-in claude provider ------------------------------------------------

def test_claude_command_carries_model_session_and_grant():
    p = ClaudeCodeProvider()
    cmd = p.command(_spec(model="claude-opus-5",
                          allowed_tools=("Read", "Grep")))
    assert cmd[:3] == ["claude", "-p", "wake up"]
    assert ["--model", "claude-opus-5"] == cmd[3:5]
    assert ["--session-id", "s-1"] == cmd[5:7]
    assert ["--allowedTools", "Read,Grep"] == cmd[7:9]

    resume = p.command(_spec(mode="resume"))
    assert ["--resume", "s-1"] == resume[3:5]
    assert "--model" not in resume, "no pinned model → provider default → none"


def test_claude_reasoning_is_env_borne_not_argv():
    p = ClaudeCodeProvider()
    spec = _spec(reasoning="high")
    assert "MAX_THINKING_TOKENS" not in " ".join(p.command(spec))
    assert p.environment(spec) == {"MAX_THINKING_TOKENS": "24576"}
    assert p.environment(_spec()) == {}, "no tier → no override"


# --- template provider -------------------------------------------------------

def test_command_provider_fills_and_drops_missing_flags():
    p = CommandProvider(
        name="mycli",
        template=("mycli", "run", "--session", "{session_id}",
                  "--model", "{model}", "{prompt}"),
        env={"MYCLI_EFFORT": "{reasoning}"},
        resume_template=("mycli", "continue", "{session_id}", "{prompt}"))

    full = p.command(_spec(model="m1"))
    assert full == ["mycli", "run", "--session", "s-1",
                    "--model", "m1", "wake up"]

    # No model: BOTH the value element and its dangling flag vanish.
    bare = p.command(_spec())
    assert bare == ["mycli", "run", "--session", "s-1", "wake up"]

    assert p.command(_spec(mode="resume")) == \
        ["mycli", "continue", "s-1", "wake up"]
    assert p.environment(_spec(reasoning="low")) == {"MYCLI_EFFORT": "low"}
    assert p.environment(_spec()) == {}


def test_host_registration_wins_by_name(desk):
    CONFIG.providers = (CommandProvider(name="claude",
                                        template=("myclaude", "{prompt}")),)
    assert registry()["claude"].command(_spec())[0] == "myclaude", \
        "a host may replace the built-in without forking"
    with pytest.raises(ValueError, match="registered"):
        get_provider("nope")


# --- per-role runtime tuning -------------------------------------------------

def test_runtime_round_trip_validation_and_overview(desk):
    assert rt.role_runtime("alpha")["provider"] == "claude"

    rt.set_role_runtime("alpha", "model", "claude-opus-5")
    rt.set_role_runtime("alpha", "reasoning", "max")
    got = rt.role_runtime("alpha")
    assert (got["model"], got["reasoning"]) == ("claude-opus-5", "max")

    with pytest.raises(ValueError, match="reasoning tier"):
        rt.set_role_runtime("alpha", "reasoning", "ultra")
    with pytest.raises(ValueError, match="registered"):
        rt.set_role_runtime("alpha", "provider", "nope")

    rt.set_role_runtime("alpha", "model", None)
    assert rt.role_runtime("alpha")["model"] is None

    overview = rt.runtime_overview()
    rows = {r["role"]: r for r in overview["roles"]}
    assert rows["alpha"]["reasoning"] == "max"
    assert rows["beta"]["provider"] == "claude"
    claude_meta = overview["providers"]["claude"]
    assert claude_meta["models"], \
        "providers publish model hints for consoles to sync from"
    assert overview["reasoning_tiers"] == ["low", "medium", "high", "max"]


def test_model_and_reasoning_take_effect_next_turn_not_next_session(desk):
    """Sessions are turn-per-process: a resume relaunches the harness, so a
    pinned model/tier rides the very next wake of the EXISTING session. Only
    provider waits for a new session (the cross-provider guard)."""
    assert rt.set_role_runtime("alpha", "model",
                               "claude-opus-5")["takes_effect"] == "next turn"
    assert rt.set_role_runtime("alpha", "reasoning",
                               "high")["takes_effect"] == "next turn"
    assert rt.set_role_runtime("alpha", "provider",
                               "claude")["takes_effect"] == "next new session"

    orch.set_status("alpha", state="idle_standby", session_id="s-old",
                    harness="wake-alpha#claude")
    cmd, env, _ = agent_run.build_launch("alpha", "resume", "s-old", "p")
    assert ["--model", "claude-opus-5"] == cmd[3:5], \
        "the pinned model applies on RESUME of the existing session"
    assert env == {"MAX_THINKING_TOKENS": "24576"}


def test_missing_claude_binary_tells_the_user_what_to_do(desk, monkeypatch):
    """The first wall a fresh clone without Claude Code hits: the message
    must carry the way out (install link + the alternative-provider route),
    and it must be visible in `deskd runtime show`, not only in driver logs."""
    import shutil as _shutil

    from deskd import providers as providers_mod
    monkeypatch.setattr(providers_mod.shutil, "which", lambda _: None)
    health = rt.runtime_overview()["providers"]["claude"]
    assert health["ok"] is False
    assert "claude.com/claude-code" in health["message"]
    assert "set-provider" in health["message"]


# --- the driver arm ----------------------------------------------------------

def test_build_launch_resolves_registry_and_fails_closed_on_tools(desk):
    rt.set_role_runtime("alpha", "model", "claude-opus-5")
    rt.set_role_runtime("alpha", "reasoning", "low")
    cmd, env, provider = agent_run.build_launch(
        "alpha", "spawn", "s-1", "p",
        default_allowed_tools=("Read", "Grep"))
    assert provider == "claude"
    assert ["--model", "claude-opus-5"] == cmd[3:5]
    assert env == {"MAX_THINKING_TOKENS": "4096"}
    assert "Read,Grep" in cmd, \
        "a role with no declared grant gets the driver default, not the widest"


def test_resume_never_crosses_providers(desk):
    CONFIG.providers = (CommandProvider(name="other",
                                        template=("other", "{prompt}")),)
    orch.set_status("alpha", state="idle_standby", session_id="s-1",
                    harness="wake-alpha#other")
    # Registry now says claude, but the recorded session belongs to `other`:
    # the resume follows the session's owner, not the new preference.
    cmd, _, provider = agent_run.build_launch("alpha", "resume", "s-1", "p")
    assert provider == "other" and cmd[0] == "other"

    with pytest.raises(ValueError, match="refusing a blind resume"):
        agent_run.build_launch("alpha", "resume", "someone-elses-id", "p")


def test_legacy_harness_reads_as_the_default_provider(desk):
    orch.set_status("alpha", state="idle_standby", session_id="s-2",
                    harness="wake-alpha")          # pre-provider row: no suffix
    assert agent_run.session_provider("alpha", "s-2") == "claude"


def test_run_agent_executes_parks_and_reports_preflight(desk, tmp_path):
    marker = tmp_path / "ran.txt"
    CONFIG.providers = (CommandProvider(
        name="touchy", template=("touch", str(marker))),)
    rt.set_role_runtime("alpha", "provider", "touchy")

    code = agent_run.run_agent("alpha", "spawn", "s-9", "p")
    assert code == 0 and marker.exists()
    row = [r for r in orch.presence() if r["role"] == "alpha"][0]
    assert row["state"] == "idle_standby", "parked, not ended"
    assert row["harness"].endswith("#touchy")

    CONFIG.providers = (CommandProvider(
        name="ghost", template=("no-such-binary-xyz", "{prompt}")),)
    rt.set_role_runtime("alpha", "provider", "ghost")
    assert agent_run.run_agent("alpha", "spawn", "s-10", "p") == 75, \
        "a failed preflight is infrastructure (75), not a served wake"
