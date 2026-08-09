"""Agent providers: how a planned wake becomes a running session.

The engine plans; a PROVIDER knows how to launch one agent turn for one
harness. This module is the seam between them, extracted so that a fresh
clone works out of the box (Claude Code ships as the default) and any other
harness plugs in without forking the driver:

- :class:`ClaudeCodeProvider` — the built-in default; launches the
  ``claude`` CLI with per-role model, thinking budget, and tool grant.
- :class:`CommandProvider` — a template-driven adapter: describe ANY CLI as
  an argv template with placeholders and it becomes a provider, no Python
  subclassing required.
- subclass :class:`Provider` for full control (custom preflights, session
  bookkeeping, sandboxes) and register it via ``configure(providers=(...,))``.

What a provider is NOT: policy. Which role uses which provider/model/tier is
per-role runtime state on the registry (`deskd runtime set-*`,
orchestration.runtime); the engine stores and surfaces it, the driver hands
it to the provider, and the provider only translates it into argv + env.

Reasoning tiers are the engine's shared vocabulary; each provider maps them
onto its own knob (Claude: a thinking-token budget in the child env; your
harness: whatever it has). A provider that cannot express a tier should get
as close as it can rather than refuse — tuning is advice, launching is the
contract.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field

#: The engine-wide tier vocabulary. Providers map these onto their own knobs.
REASONING_TIERS = ("low", "medium", "high", "max")


@dataclass(frozen=True)
class LaunchSpec:
    """Everything a provider may need to launch one agent turn.

    ``model`` / ``reasoning`` / ``allowed_tools`` are None when the role has
    no override — the provider applies its own defaults then. ``prompt`` is
    the full wake/rollover prompt; providers must pass it through untouched.
    """
    role: str
    mode: str                       # "spawn" | "resume"
    session_id: str
    prompt: str
    model: str | None = None
    reasoning: str | None = None    # one of REASONING_TIERS, or None
    allowed_tools: tuple[str, ...] | None = None


class Provider:
    """Base provider. Subclass, set ``name``, implement :meth:`command`.

    ``environment`` returns ENV OVERRIDES (merged over the driver's child
    env), not a full environment — a provider should never need to copy
    os.environ. ``preflight`` runs before every launch; return ``ok=False``
    to refuse without burning a model turn (the demand stays pending and the
    wake ladder keeps climbing, which is the honest failure mode).
    """

    name: str = ""

    #: Does ``command()`` put newline-delimited JSON on stdout, in the Claude
    #: Code stream shape? False by default, because most CLIs do not, and
    #: parsing arbitrary output as if it were a known protocol invents
    #: structure that is not there. Setting it True is a promise the driver
    #: relies on — see docs/session-feed.md.
    streams: bool = False

    def command(self, spec: LaunchSpec) -> list[str]:
        raise NotImplementedError

    def environment(self, spec: LaunchSpec) -> dict[str, str]:
        return {}

    def preflight(self) -> dict:
        return {"ok": True, "provider": self.name}


@dataclass(frozen=True)
class ClaudeCodeProvider(Provider):
    """The built-in default: Claude Code's headless CLI.

    ``thinking_budgets`` maps the shared tiers onto MAX_THINKING_TOKENS in
    the child environment — env-borne on purpose, so the argv stays the
    deterministic artifact tests and humans inspect.
    """
    binary: str = "claude"
    default_model: str | None = None
    thinking_budgets: dict = field(default_factory=lambda: {
        "low": 4096, "medium": 12288, "high": 24576, "max": 49152})
    name: str = "claude"
    #: Off by default: turning it on changes the child's argv and therefore
    #: what every existing consumer of that stdout sees, which is not a thing
    #: to do behind a host's back. A host opts in with
    #: ``ClaudeCodeProvider(stream=True)``.
    stream: bool = False

    @property
    def streams(self) -> bool:            # type: ignore[override]
        return self.stream

    def command(self, spec: LaunchSpec) -> list[str]:
        cmd = [self.binary, "-p", spec.prompt]
        if self.stream:
            # --verbose is required by the CLI alongside stream-json under -p;
            # --include-partial-messages is what makes thinking observable as
            # it happens rather than only once the message completes. The
            # thinking payloads are empty either way (docs/session-feed.md) —
            # what the deltas buy is the live "still thinking" signal.
            cmd.extend(["--output-format", "stream-json", "--verbose",
                        "--include-partial-messages"])
        model = spec.model or self.default_model
        if model:
            cmd.extend(["--model", model])
        if spec.mode == "resume":
            cmd.extend(["--resume", spec.session_id])
        else:
            cmd.extend(["--session-id", spec.session_id])
        if spec.allowed_tools:
            cmd.extend(["--allowedTools", ",".join(spec.allowed_tools)])
        return cmd

    def environment(self, spec: LaunchSpec) -> dict[str, str]:
        if spec.reasoning and spec.reasoning in self.thinking_budgets:
            return {"MAX_THINKING_TOKENS": str(self.thinking_budgets[spec.reasoning])}
        return {}

    def preflight(self) -> dict:
        if shutil.which(self.binary):
            return {"ok": True, "provider": self.name}
        return {"ok": False, "provider": self.name,
                "code": "binary_missing",
                "message": f"{self.binary!r} not found on PATH"}


@dataclass(frozen=True)
class CommandProvider(Provider):
    """Any CLI as a provider, described as an argv template.

    Placeholders — ``{prompt}`` ``{session_id}`` ``{mode}`` ``{role}``
    ``{model}`` ``{reasoning}`` ``{allowed_tools}`` — are substituted per
    launch; an argv ELEMENT that references a value the spec does not carry
    is DROPPED whole (so ``"--model", "{model}"`` vanishes cleanly when no
    model is set, instead of passing a literal brace string or an empty
    argument). ``env`` values take the same placeholders.

    Example::

        CommandProvider(
            name="mycli",
            template=("mycli", "run", "--session", "{session_id}",
                      "--model", "{model}", "{prompt}"),
            env={"MYCLI_EFFORT": "{reasoning}"},
            resume_template=("mycli", "continue", "{session_id}", "{prompt}"),
        )
    """
    template: tuple[str, ...] = ()
    resume_template: tuple[str, ...] | None = None
    env: dict = field(default_factory=dict)
    name: str = "command"

    def _values(self, spec: LaunchSpec) -> dict[str, str | None]:
        return {
            "prompt": spec.prompt, "session_id": spec.session_id,
            "mode": spec.mode, "role": spec.role, "model": spec.model,
            "reasoning": spec.reasoning,
            "allowed_tools": ",".join(spec.allowed_tools)
            if spec.allowed_tools else None,
        }

    @staticmethod
    def _fill(parts, values) -> list[str]:
        out: list[str] = []
        filled: list[str | None] = []
        for part in parts:
            keys = [k for k in values if "{" + k + "}" in part]
            if any(values[k] is None for k in keys):
                filled.append(None)                 # this element has no value
                continue
            text = part
            for k in keys:
                text = text.replace("{" + k + "}", str(values[k]))
            filled.append(text)
        # Drop a dangling flag whose VALUE element vanished ("--model", None).
        for i, text in enumerate(filled):
            if text is None:
                if out and out[-1].startswith("-") and "{" not in parts[i - 1]:
                    out.pop()
                continue
            out.append(text)
        return out

    def command(self, spec: LaunchSpec) -> list[str]:
        parts = self.template
        if spec.mode == "resume" and self.resume_template is not None:
            parts = self.resume_template
        if not parts:
            raise ValueError(f"provider {self.name!r} has an empty template")
        return self._fill(parts, self._values(spec))

    def environment(self, spec: LaunchSpec) -> dict[str, str]:
        values = self._values(spec)
        out = {}
        for key, tmpl in self.env.items():
            keys = [k for k in values if "{" + k + "}" in tmpl]
            if any(values[k] is None for k in keys):
                continue
            text = tmpl
            for k in keys:
                text = text.replace("{" + k + "}", str(values[k]))
            out[key] = text
        return out

    def preflight(self) -> dict:
        binary = (self.template[0] if self.template else "")
        if binary and shutil.which(binary):
            return {"ok": True, "provider": self.name}
        return {"ok": False, "provider": self.name,
                "code": "binary_missing",
                "message": f"{binary!r} not found on PATH"}


def registry() -> dict[str, Provider]:
    """Name → provider: the built-in default plus everything the host
    registered via ``configure(providers=(...,))``. Host entries WIN on name
    collision, so a host may replace the built-in claude provider (custom
    binary path, different budgets) without a fork."""
    import os

    from .config import CONFIG
    # The built-in default honors the reference driver's historical env
    # contract: DESKD_WAKE_MODEL names the model when no role pins one.
    out: dict[str, Provider] = {"claude": ClaudeCodeProvider(
        default_model=os.environ.get("DESKD_WAKE_MODEL") or None)}
    for provider in getattr(CONFIG, "providers", ()) or ():
        if not getattr(provider, "name", ""):
            raise ValueError("every provider must carry a non-empty name")
        out[provider.name] = provider
    return out


def get_provider(name: str) -> Provider:
    try:
        return registry()[name]
    except KeyError:
        raise ValueError(
            f"unknown provider {name!r}; registered: {sorted(registry())}"
        ) from None
