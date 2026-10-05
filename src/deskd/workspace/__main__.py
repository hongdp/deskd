"""Explicit entry points for the installed workspace and an offline rehearsal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="deskd persistent multi-seat workspace"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    bridge = sub.add_parser("bridge", help="fixed trusted-harness MCP bridge")
    bridge.add_argument("--socket", type=Path, required=True)
    bridge.add_argument("--gateway-uid", type=int, required=True)
    auth = sub.add_parser("model-auth", help="private runtime authentication helper")
    auth.add_argument("--socket", type=Path, required=True)
    auth.add_argument("--gateway-uid", type=int, required=True)
    board = sub.add_parser(
        "board", help="read-only loopback status; private messages omitted"
    )
    board.add_argument("--state", type=Path, required=True)
    board.add_argument("--port", type=int, default=0)
    control = sub.add_parser(
        "control", help="independent administrative socket request"
    )
    control.add_argument("--socket", type=Path, required=True)
    control.add_argument("--gateway-uid", type=int, required=True)
    control.add_argument(
        "method",
        choices=(
            "workspace.status",
            "workspace.pause",
            "workspace.enqueue",
            "workspace.budget",
            "workspace.store.reconcile",
            "workspace.store.schedule_timer",
            "workspace.store.cancel_timer",
            "workspace.revoke",
            "status",
            "connections",
            "bind",
            "revoke",
            "activate",
            "fence",
            "lease",
        ),
    )
    control.add_argument("--params", default="{}")
    demo = sub.add_parser(
        "demo", help="rehearse durable collaboration with a fixed mock runtime"
    )
    demo.add_argument("--output", type=Path, required=True)
    for name, help_text in (
        (
            "install",
            "root-only fresh-prefix installation with a separately provisioned API key",
        ),
        (
            "install-mock",
            "root-only fresh-prefix credential-free rehearsal installation",
        ),
    ):
        installer = sub.add_parser(name, help=help_text)
        installer.add_argument("--prefix", type=Path, required=True)
        installer.add_argument("--python", type=Path)
        installer.add_argument("--binary", type=Path, required=True)
        installer.add_argument("--harness-uid", type=int, required=True)
        installer.add_argument("--gateway-uid", type=int, required=True)
        installer.add_argument("--business-gid", type=int, required=True)
        if name == "install-mock":
            installer.add_argument("--mock-port", type=int, required=True)
            installer.add_argument("--model", default="gpt-5.5")
        else:
            installer.add_argument("--model", required=True)
    up = sub.add_parser("up", help="supervise one protected installed workspace")
    up.add_argument("--deployment", type=Path, required=True)
    attach = sub.add_parser(
        "attach", help="attach the official native terminal to a registered seat"
    )
    attach.add_argument("--deployment", type=Path, required=True)
    attach.add_argument("--seat", required=True)
    for name in ("gateway", "controller", "bootstrap"):
        command = sub.add_parser(name, help="protected installed " + name)
        command.add_argument("--deployment", type=Path, required=True)
        if name == "controller":
            command.add_argument("--daemon-pid", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "model-auth":
            from .model_auth import helper_main

            return helper_main(args.socket, args.gateway_uid, sys.stdout.buffer)
        elif args.command == "bridge":
            from deskd.gateway.bridge import run_bridge
            from .exchange import tool_catalog

            run_bridge(
                args.socket,
                gateway_uid=args.gateway_uid,
                source=sys.stdin.buffer,
                target=sys.stdout.buffer,
                catalog=tool_catalog(),
                identify=True,
            )
        elif args.command == "control":
            from deskd.gateway.__main__ import control as request
            from deskd.gateway.bridge import _decode

            reply = request(
                args.socket,
                args.gateway_uid,
                args.method,
                _decode(args.params.encode()),
            )
            print(json.dumps(reply, ensure_ascii=False))
            return 0 if reply["ok"] else 1
        elif args.command == "board":
            from .board import make_server, read_snapshot

            if not args.state.is_file():
                raise ValueError("existing_workspace_required")
            read_snapshot(args.state)
            server = make_server(lambda: read_snapshot(args.state), port=args.port)
            print(f"http://127.0.0.1:{server.server_port}", flush=True)
            try:
                server.serve_forever(poll_interval=0.2)
            finally:
                server.server_close()
        elif args.command == "demo":
            from .demo import run_demo

            print(json.dumps(run_demo(args.output), ensure_ascii=False))
        elif args.command in {"install", "install-mock"}:
            from .deployment import install

            print(
                install(
                    args.prefix,
                    binary=args.binary,
                    harness_uid=args.harness_uid,
                    gateway_uid=args.gateway_uid,
                    business_gid=args.business_gid,
                    mock_port=getattr(args, "mock_port", None),
                    provider="api" if args.command == "install" else "mock",
                    model=args.model,
                    python=args.python,
                )
            )
        elif args.command == "up":
            from .manager import WorkspaceManager

            manager = WorkspaceManager(
                args.deployment,
                on_event=lambda event: print(json.dumps(event), flush=True),
            )
            manager.run()
        elif args.command == "attach":
            from .deployment import attach_seat

            attach_seat(args.deployment, args.seat)
        else:
            from .deployment import run_installed

            run_installed(
                args.command,
                args.deployment,
                daemon_pid=getattr(args, "daemon_pid", None),
            )
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        # Do not expose path values, request payloads, credentials or raw errors.
        print(
            json.dumps({"error": getattr(exc, "code", "workspace_command_failed")}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
