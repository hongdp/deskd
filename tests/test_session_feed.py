"""The session feed: what a headless turn said while it worked.

Layer 1 (test_tool_trace) answers "on what, right now". This is layer 2 — the
narration between tool calls, which in a headless run goes to a terminal nobody
is attached to.

The event fixtures below are REAL: captured from `claude -p … --output-format
stream-json` on 2026-08-09, not written from the documentation. That matters
for one clause in particular — the harness emits the structure of thinking with
its content redacted to '' — because a fixture invented from the docs would
have had thinking text in it and the code would have been built to read
something that never arrives.
"""

from __future__ import annotations

import json

import pytest

from deskd import orchestration as orch
from deskd.orchestration import agent_run
from deskd.orchestration.presence import FEED_MAX_ROWS_PER_SESSION
from deskd.providers import ClaudeCodeProvider, CommandProvider, LaunchSpec


# --- captured verbatim, trimmed only of ids/usage ---------------------------

ASSISTANT_WITH_TEXT = {
    "type": "assistant",
    "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "", "signature": "x" * 2880},
        {"type": "text", "text": "**Step by step**\n\n1. **Start:** 17 sheep."},
    ]},
}
THINKING_START = {
    "type": "stream_event",
    "event": {"type": "content_block_start", "index": 0,
              "content_block": {"type": "thinking", "thinking": ""}},
}
THINKING_DELTA = {
    "type": "stream_event",
    "event": {"type": "content_block_delta", "index": 0,
              "delta": {"type": "thinking_delta", "thinking": ""}},
}
TEXT_DELTA = {
    "type": "stream_event",
    "event": {"type": "content_block_delta", "index": 1,
              "delta": {"type": "text_delta", "text": "1729 is"}},
}
SYSTEM_INIT = {"type": "system", "subtype": "init", "model": "claude-opus-5"}
RESULT = {"type": "result", "subtype": "success", "is_error": False}


class TestEventVocabulary:
    """`_feed_lines` is pure, so the vocabulary is testable without a child."""

    def test_visible_narration_is_captured(self):
        assert list(agent_run._feed_lines(ASSISTANT_WITH_TEXT)) == [
            ("narration", "**Step by step**\n\n1. **Start:** 17 sheep.")]

    def test_a_thinking_block_is_recorded_as_a_marker_not_content(self):
        """The one clause the captured fixture exists to protect: the harness
        redacts thinking, so the honest record is that it happened."""
        assert list(agent_run._feed_lines(THINKING_START)) == [("thinking", "")]

    def test_empty_thinking_deltas_do_not_each_become_a_row(self):
        """Five empty payloads dressed up as five entries would read like five
        things worth looking at. One marker per block says what is known."""
        assert list(agent_run._feed_lines(THINKING_DELTA)) == []

    @pytest.mark.parametrize("event", [SYSTEM_INIT, RESULT, TEXT_DELTA,
                                       {"type": "rate_limit_event"},
                                       {"type": "brand_new_kind_of_event"},
                                       {}, {"type": "assistant"},
                                       {"type": "assistant", "message": None},
                                       {"type": "stream_event", "event": None}])
    def test_unknown_or_empty_shapes_yield_nothing_and_never_raise(self, event):
        assert list(agent_run._feed_lines(event)) == []

    def test_whitespace_only_narration_is_not_filed(self):
        blank = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "   \n  "}]}}
        assert list(agent_run._feed_lines(blank)) == []


class TestFeedStore:

    def test_append_and_tail_in_order(self, desk):
        for i in range(3):
            orch.feed_append("alpha", "s1", "narration", f"line {i}",
                             )
        rows = orch.session_feed("s1")
        assert [r["text"] for r in rows] == ["line 0", "line 1", "line 2"]
        assert [r["seq"] for r in rows] == [1, 2, 3]

    def test_after_seq_returns_only_what_is_new(self, desk):
        """How a console tails without re-reading what it already showed."""
        for i in range(4):
            orch.feed_append("alpha", "s1", "narration", f"l{i}")
        assert [r["text"] for r in
                orch.session_feed("s1", after_seq=2)] == ["l2", "l3"]

    def test_sequences_are_per_session_not_global(self, desk):
        orch.feed_append("alpha", "s1", "narration", "a")
        orch.feed_append("beta", "s2", "narration", "b")
        assert orch.session_feed("s2")[0]["seq"] == 1

    def test_the_ring_keeps_the_most_recent_and_bounds_the_table(self, desk):
        n = FEED_MAX_ROWS_PER_SESSION + 25
        for i in range(n):
            orch.feed_append("alpha", "s1", "narration", f"line {i}",
                             )
        rows = orch.session_feed("s1", after_seq=0, limit=10_000)
        assert len(rows) == FEED_MAX_ROWS_PER_SESSION
        assert rows[-1]["text"] == f"line {n - 1}"
        assert rows[0]["text"] == f"line {n - FEED_MAX_ROWS_PER_SESSION}"

    def test_one_session_cannot_evict_another(self, desk):
        """The bound is per session on purpose: a chatty turn must not push a
        quiet one's history out of the window."""
        orch.feed_append("beta", "quiet", "narration", "the only thing I said",
                         )
        for i in range(FEED_MAX_ROWS_PER_SESSION + 50):
            orch.feed_append("alpha", "chatty", "narration", f"{i}",
                             )
        assert len(orch.session_feed("quiet")) == 1

    def test_an_unknown_kind_is_refused_rather_than_stored(self, desk):
        assert orch.feed_append("alpha", "s1", "speculation", "...",
                                ) is None
        assert orch.session_feed("s1") == []

    def test_a_write_failure_never_reaches_the_caller(self):
        """It describes a running session; it must not be why one dies."""
        assert orch.feed_append("alpha", "s1", "narration", "x",
                                db_path="/nonexistent/dir/deskd.db") is None


class TestProviderOptIn:

    def test_streaming_is_off_by_default(self):
        assert ClaudeCodeProvider().streams is False
        spec = LaunchSpec(role="alpha", mode="spawn", session_id="s", prompt="p")
        assert "--output-format" not in ClaudeCodeProvider().command(spec)

    def test_opting_in_adds_the_stream_flags(self):
        p = ClaudeCodeProvider(stream=True)
        spec = LaunchSpec(role="alpha", mode="spawn", session_id="s", prompt="p")
        cmd = p.command(spec)
        assert p.streams is True
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        # the CLI requires --verbose alongside stream-json under -p
        assert "--verbose" in cmd and "--include-partial-messages" in cmd

    def test_a_template_provider_does_not_claim_to_stream(self):
        """An arbitrary CLI makes no promise about its stdout, so parsing it as
        a known protocol would be inventing structure."""
        assert CommandProvider(name="x", template=("echo", "{prompt}")).streams \
            is False


class TestStreamingRun:

    def test_stdout_is_forwarded_verbatim_and_narration_is_filed(
            self, desk, capfd):
        """Forwarding is the compatibility contract: whoever reads this
        process's stdout must see what they saw before capture existed."""
        events = [SYSTEM_INIT, THINKING_START, THINKING_DELTA,
                  ASSISTANT_WITH_TEXT, RESULT]
        payload = "".join(json.dumps(e) + "\n" for e in events)
        cmd = ["python3", "-c",
               f"import sys; sys.stdout.write({payload!r})"]

        code = agent_run._run_streaming(cmd, None, 30, "alpha", "s9", None)

        assert code == 0
        assert capfd.readouterr().out == payload
        rows = orch.session_feed("s9")
        assert [(r["kind"], r["text"]) for r in rows] == [
            ("thinking", ""),
            ("narration", "**Step by step**\n\n1. **Start:** 17 sheep."),
        ]

    def test_non_json_output_is_forwarded_and_ignored(self, desk, capfd):
        """A provider that mislabels itself, or a CLI that prints a warning
        line, must not take the turn down."""
        cmd = ["python3", "-c",
               "import sys; sys.stdout.write('not json at all\\n{\"type\":\"result\"}\\n')"]
        code = agent_run._run_streaming(cmd, None, 30, "alpha", "s10", None)
        assert code == 0
        assert "not json at all" in capfd.readouterr().out
        assert orch.session_feed("s10") == []

    def test_the_child_exit_code_survives(self, desk, capfd):
        cmd = ["python3", "-c", "import sys; sys.exit(3)"]
        assert agent_run._run_streaming(cmd, None, 30, "alpha", "s11",
                                        None) == 3


class TestConsoleFeedEndpoint:
    """The page has to be able to tail it, and an idle agent is not an error."""

    def _client(self):
        from fastapi.testclient import TestClient
        from deskd.web.app import create_app
        return TestClient(create_app())

    def test_an_idle_agent_returns_an_empty_feed_not_a_404(self, desk):
        """A console that 404s on a normal state teaches people to ignore its
        errors, and then to miss a real one."""
        r = self._client().get("/api/agent/alpha/feed")
        assert r.status_code == 200
        assert r.json() == {"role": "alpha", "session_id": None, "lines": []}

    def test_an_unknown_role_is_still_a_404(self, desk):
        assert self._client().get("/api/agent/nobody/feed").status_code == 404

    def test_the_live_session_feed_tails_from_a_sequence(self, desk):
        orch.set_status("alpha", state="working", session_id="s-live",
                        harness="wake-alpha")
        for i in range(3):
            orch.feed_append("alpha", "s-live", "narration", f"line {i}")
        body = self._client().get("/api/agent/alpha/feed?after_seq=1").json()
        assert body["session_id"] == "s-live"
        assert [l["text"] for l in body["lines"]] == ["line 1", "line 2"]
