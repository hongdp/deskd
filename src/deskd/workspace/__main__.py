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
    board_source = board.add_mutually_exclusive_group(required=True)
    board_source.add_argument(
        "--state", type=Path, help="recorded ledger; live health unverified"
    )
    board_source.add_argument(
        "--deployment", type=Path, help="observe the installed gateway and workspace"
    )
    board.add_argument("--port", type=int, default=0)
    console = sub.add_parser("console", help="paired human workspace: tasks, correspondence and results")
    console_source = console.add_mutually_exclusive_group(required=True)
    console_source.add_argument("--deployment", type=Path, help="protected installed workspace")
    console_source.add_argument("--demo", type=Path, help="new scratchpad directory for a synthetic interactive demo")
    console.add_argument("--port", type=int, default=0)
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
            "workspace.console.extended",
            "workspace.goal.create",
            "workspace.goal.update",
            "workspace.goal.answer",
            "workspace.source.configure",
            "workspace.source.disable",
            "workspace.notification.configure",
            "workspace.notification.ack",
            "workspace.memory.search",
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
    backup = sub.add_parser("backup", help="export offline domain facts without runtime authority")
    backup.add_argument("--workspace-db", type=Path, required=True)
    backup.add_argument("--gateway-db", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--offline-confirmed", action="store_true")
    verify = sub.add_parser("verify-backup", help="verify a detached facts archive")
    verify.add_argument("--archive", type=Path, required=True)
    restore = sub.add_parser("restore-archive", help="restore detached facts into a fresh directory")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    unit = sub.add_parser("service-template", help="print an inert service unit for review")
    unit.add_argument("--python", required=True)
    unit.add_argument("--deployment", required=True)
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
        if args.command in {"backup", "verify-backup", "restore-archive", "service-template"}:
            from .operations import export_backup, verify_backup, restore_archive, render_service_unit

            if args.command == "backup":
                result = export_backup(args.workspace_db, args.gateway_db, args.output, offline_confirmed=args.offline_confirmed)
            elif args.command == "verify-backup":
                result = verify_backup(args.archive)
            elif args.command == "restore-archive":
                result = restore_archive(args.archive, args.output)
            else:
                print(render_service_unit(python_executable=args.python, deployment=args.deployment), end="")
                return 0
            print(json.dumps(result, ensure_ascii=False))
            return 0
        elif args.command == "model-auth":
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
            from functools import partial
            from .board import make_server, read_installed_snapshot, read_snapshot

            if args.deployment is not None:
                from .deployment import Deployment

                installation = Deployment(args.deployment)
                snapshot = partial(read_installed_snapshot, installation)
            else:
                if not args.state.is_file():
                    raise ValueError("existing_workspace_required")
                read_snapshot(args.state)
                snapshot = partial(read_snapshot, args.state)
            server = make_server(snapshot, port=args.port)
            print(f"http://127.0.0.1:{server.server_port}", flush=True)
            try:
                server.serve_forever(poll_interval=0.2)
            finally:
                server.server_close()
        elif args.command == "console":
            from .console import ConsoleBackend, serve_console

            if args.deployment is not None:
                from .deployment import Deployment

                installation = Deployment(args.deployment)
                installation.attest_console()
                backend = ConsoleBackend(
                    installation.admin,
                    attest=installation.attest_console,
                    static_root=installation.prefix / "lib/deskd/workspace/static",
                )
            else:
                from .console_demo import create_demo

                backend = create_demo(args.demo)
            serve_console(backend, port=args.port)
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
