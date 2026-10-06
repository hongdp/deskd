# Gemini provider

The protected workspace can use `gemini-3.8-flash` through an adapter hosted by
its existing gateway process. It retains the unmodified pinned Codex runtime,
one root per role and the same independent action authorization. This provider
is separate from the OpenAI API provider.

## Installation

Follow [the protected installation prerequisites](workspace-install.md), then
add these arguments to the fresh-prefix `install` command:

```text
--provider gemini --model gemini-3.8-flash --gemini-port PORT
```

Choose an unused local port from 1024 through 65535. The protected manifest binds
that port and the exact model. The gateway binds only `127.0.0.1` and refuses an
occupied port; it never connects to or adopts an old proxy. The adapter starts
before the gateway admits the harness. A gateway failure is handled by the
existing manager, which fences and restarts its own runtime group.

Provision the **Google Gemini API key** using the protected-file procedure in the
installation guide. No key belongs in command arguments, role configuration or
source control. The installer never discovers an existing key. Do not reuse the
old fork's runtime home, key paths or listener.

## Credentials and scope

The gateway authenticates the trusted harness over its existing Unix socket.
For this provider, the private model-auth helper returns a randomly generated
local capability for the current gateway process. The adapter requires that
capability and an active, attested service before accepting a request. The
Google key stays inside the gateway and is inserted only into a request to the
fixed Google HTTPS endpoint. Requests cannot choose another upstream or model.

Role shell commands retain their network restrictions. The trusted harness can
use the adapter; local access without its capability does not grant model usage.
This adds no per-role API spending account: role accounting and dispatch limits
remain those of the workspace, and API charges remain those of the operator's
Google project.

The protocol reuses the earlier fork's Responses-to-Gemini approach: ordinary
text, reasoning signatures, function tools, namespaced tools and custom text
tools are translated for the pinned harness. Unsupported surfaces are rejected
explicitly. Browser/computer tools and unrestricted model web search remain
disabled; research uses the workspace's approved source tools. This release
supports text, code and the existing research-brief tool workflow. Image, audio
and video input are rejected; it does not claim the full multimodal capability
of Gemini itself. Request compression is disabled and the protected default
reasoning effort is `medium`.

The adapter sends requests directly to Google's fixed `streamGenerateContent`
endpoint, without environment-configured proxies or redirects. It does not
persist prompts or upstream error bodies. Model output remains untrusted data;
the adapter does not issue action authorization.

## Acceptance boundaries

Tests use synthetic keys and an injected mock Google transport. The opt-in
`test_workspace_gemini_official.py` runs the pinned official CLI with a fresh
scratch home and a synthetic MCP tool, exercising an actual two-request tool
round trip and preserving a synthetic thought signature. It does not invoke a
real model or establish kernel role isolation. The separate privileged Linux
suite owns that isolation claim.

Real account access, Google availability, model output quality, billing and
long-running model behavior require an operator's first real task after
installation. Start with a single small research goal before scheduling repeats.

Protocol references: [Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash),
[function calling](https://ai.google.dev/gemini-api/docs/function-calling), and
[thought signatures](https://ai.google.dev/gemini-api/docs/thought-signatures).
