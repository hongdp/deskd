"""Screenshot the overnight-helpdesk demo into docs/images/.

Runs ``run_demo`` headlessly (auto-supervisor, screenshot holds enabled) and
captures the console at each story peak with the ``playwright`` CLI::

    python -m examples.support_desk.capture

Shots land in ``docs/images/`` at 1440x900, each in a light and a dark
variant (``--color-scheme`` emulation). NOTE: the current console pages pin
their own dark palette and ignore ``prefers-color-scheme`` / ``data-theme``,
so today the two variants render identically — they are captured anyway so
the file set (and this pipeline) is already right when the themed console
lands; recapture then.

Setup: ``pip install playwright && playwright install chromium`` if the CLI
is not already on PATH (override the binary with ``--playwright`` or
``$PLAYWRIGHT_BIN``). Do NOT substitute a snap-packaged chromium: its
AppArmor confinement cannot write the PNG outside $HOME and fails with
"Permission denied".

Screenshots are keyed off polled ``/api/*`` state, not sleeps: a loaded
machine shifts the timeline, but "the board says wakes_at_human_level >= 1"
is the moment the hero shot wants regardless of when it happens.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

if __package__ in (None, ""):  # `python examples/support_desk/capture.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.support_desk import desk  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGES = REPO_ROOT / "docs" / "images"
THEMES = ("light", "dark")

#: shot name -> (console path, wait-condition over the API, full page?)
SHOTS = [
    ("board-live", "/board", "beat2", False),
    ("meeting-live", "/meetings", "meeting_open", False),
    ("board-escalation", "/board", "human_rung", False),
    ("agent-auditor", "/agent/auditor", "human_rung", True),
]


def _get(port: int, path: str) -> dict | list:
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def _condition(port: int, name: str) -> bool:
    if name == "beat2":
        health = _get(port, "/api/board")["health"]
        return health["unroutable_demands"] >= 1
    if name == "meeting_open":
        # Open AND mid-story: an obligation row means the question was asked,
        # so the transcript + owed-reply panel are worth photographing. The
        # /meetings page hides closed meetings by default — this must be
        # caught live, which is what the scenario's `meeting` hold is for.
        return any(m["meeting"]["state"] in {"active", "consensus",
                                             "termination_pending"}
                   and m["response_obligations"]
                   for m in _get(port, "/api/meetings"))
    if name == "human_rung":
        return _get(port, "/api/board")["health"]["wakes_at_human_level"] >= 1
    raise ValueError(name)


def wait_for(port: int, condition: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            if _condition(port, condition):
                return
        except OSError:
            pass  # console not up yet
        time.sleep(1)
    raise SystemExit(f"gave up waiting for condition {condition!r} — "
                     f"did the demo die? check its output above")


def find_playwright(explicit: str | None) -> str:
    for candidate in (explicit, os.environ.get("PLAYWRIGHT_BIN"),
                      shutil.which("playwright"),
                      str(Path.home() / ".local/bin/playwright")):
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("no playwright CLI found: pip install playwright && "
                     "playwright install chromium (or pass --playwright)")


def shoot(playwright: str, port: int, page: str, out: Path, *,
          theme: str, full_page: bool) -> None:
    cmd = [playwright, "screenshot",
           "--viewport-size=1440,900",
           # The pages fetch /api/* after load; 2500ms is comfortably past it.
           "--wait-for-timeout=2500",
           f"--color-scheme={theme}"]
    if full_page:
        cmd.append("--full-page")
    cmd += [f"http://127.0.0.1:{port}{page}", str(out)]
    subprocess.run(cmd, check=True)
    print(f"  wrote {out.relative_to(REPO_ROOT)} "
          f"({out.stat().st_size // 1024} KB)")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="capture demo screenshots")
    ap.add_argument("--port", type=int, default=desk.DEFAULT_PORT)
    ap.add_argument("--playwright", default=None,
                    help="path to the playwright CLI")
    ap.add_argument("--timeout", type=float, default=420,
                    help="overall budget in seconds (default %(default)s)")
    args = ap.parse_args(argv)

    playwright = find_playwright(args.playwright)
    IMAGES.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), str(REPO_ROOT / "src"),
         env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    tmp = Path(tempfile.mkdtemp(prefix="deskd-capture-"))
    demo = subprocess.Popen(
        [sys.executable, "-m", "examples.support_desk.run_demo",
         "--port", str(args.port), "--db", str(tmp / "demo.db"),
         "--auto-supervisor", "--exit-when-done",
         # Generous holds so every shot has a window even on a slow box.
         "--pause-at", "board:20", "--pause-at", "meeting:30",
         "--pause-at", "escalation:25", "--pause-at", "outage:25"],
        cwd=REPO_ROOT, env=env)
    deadline = time.monotonic() + args.timeout
    try:
        for name, page, condition, full_page in SHOTS:
            print(f"waiting for {condition!r} ...")
            wait_for(args.port, condition, deadline)
            for theme in THEMES:
                shoot(playwright, args.port, page,
                      IMAGES / f"{name}-{theme}.png",
                      theme=theme, full_page=full_page)
        print("waiting for the demo to finish ...")
        demo.wait(timeout=max(10.0, deadline - time.monotonic()))
    finally:
        if demo.poll() is None:
            demo.terminate()
            try:
                demo.wait(timeout=10)
            except subprocess.TimeoutExpired:
                demo.kill()
    print(f"done: {len(SHOTS) * len(THEMES)} images in "
          f"{IMAGES.relative_to(REPO_ROOT)}/")


if __name__ == "__main__":
    main()
