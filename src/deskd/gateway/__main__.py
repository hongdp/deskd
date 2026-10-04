"""Isolated experimental entry point; never loads the legacy host configuration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import sys
import uuid

from .bridge import BridgeError, _decode, run_bridge
from .events import canonical_json
from .preflight import preflight
from .wire import MAX_FRAME_BYTES, MAX_RESPONSE_BYTES


def _manifest(path: Path, *, protected: bool = False) -> dict:
    # This explicitly selected file must be a non-secret installation manifest.
    # Never discover files in a home directory or read inherited host config.
    if protected:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("unprotected_manifest")
        for parent in path.parents:
            info = parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
            ):
                raise ValueError("unprotected_manifest_parent")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("invalid_manifest_file")
        if protected and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o644):
            raise ValueError("unprotected_manifest")
        raw = stream.read(MAX_FRAME_BYTES + 1)
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError("manifest_too_large")
    return _decode(raw)


def control(socket_path: Path, gateway_uid: int, method: str, params: dict) -> dict:
    """One authenticated administrative RPC; no reconnection or automatic retry."""
    if (
        not hasattr(socket, "SO_PEERCRED")
        or type(gateway_uid) is not int
        or gateway_uid <= 0
        or gateway_uid == os.geteuid()
    ):
        raise ValueError("distinct_gateway_uid_required")
    mid = uuid.uuid4().hex
    request = (
        canonical_json({"id": mid, "method": method, "params": params}).encode() + b"\n"
    )
    if len(request) > MAX_FRAME_BYTES:
        raise ValueError("request_too_large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(30)
        sock.connect(str(socket_path))
        _, uid, _ = struct.unpack(
            "3i",
            sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            ),
        )
        if uid != gateway_uid:
            raise ValueError("untrusted_gateway")
        sock.sendall(request)
        with sock.makefile("rb") as stream:
            raw = stream.readline(MAX_RESPONSE_BYTES + 2)
    if not raw or len(raw) > MAX_RESPONSE_BYTES + 1 or not raw.endswith(b"\n"):
        raise OSError("unknown_admin_outcome")
    try:
        reply = _decode(raw, response=True)
        if reply.get("id") != mid or type(reply.get("ok")) is not bool:
            raise ValueError("invalid_admin_response")
        if set(reply) != {"id", "ok", "result" if reply["ok"] else "error"}:
            raise ValueError("invalid_admin_response")
        if not reply["ok"]:
            error = reply["error"]
            if (
                type(error) is not dict
                or set(error) != {"code"}
                or type(error["code"]) is not str
            ):
                raise ValueError("invalid_admin_response")
            if error["code"] in {
                "response_encoding_error",
                "storage_error",
                "internal_error",
            }:
                raise OSError("unknown_admin_outcome")
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise OSError("unknown_admin_outcome") from exc
    return reply


def _serve_memo(manifest_path: Path) -> None:
    from .actions import MemoWorkflow, WORKFLOW_ACTIONS
    from .commands import GatewayCommands
    from .events import GatewayEventStore
    from .identity import ActionPolicy
    from .registry import Registry
    from .transport import GatewayTransport

    manifest = _manifest(manifest_path, protected=True)

    def check_metadata():
        report = preflight(manifest)
        if not report.metadata_ok:
            raise ValueError("installation_metadata_rejected")
        # Binding/configuration is a privileged administrative action. The file
        # containing this inventory must itself belong to the protected policy.
        info = manifest_path.lstat()
        if (
            manifest_path != Path(manifest["paths"]["policy"]) / "manifest.json"
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o644
            or info.st_nlink != 1
        ):
            raise ValueError("unprotected_manifest")
        if _manifest(manifest_path, protected=True) != manifest:
            raise ValueError("manifest_changed")

    check_metadata()
    if os.geteuid() != manifest["gateway_uid"]:
        raise ValueError("wrong_gateway_uid")
    path = Path(manifest["paths"]["gateway_state"]) / "memo.db"
    # The protected directory is checked above. Refuse imported state aliases.
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError("unsafe_gateway_state")
    os.umask(0o077)
    registry = Registry(
        path,
        harness_uid=manifest["harness_uid"],
        actions={**WORKFLOW_ACTIONS, "state.read": ActionPolicy("state.read")},
    )
    events = GatewayEventStore(path)
    workflow = MemoWorkflow(path)
    commands = GatewayCommands(registry, events, handlers=workflow.handlers())
    service = GatewayTransport(
        registry,
        commands,
        business_path=Path(manifest["paths"]["gateway"]) / "business.sock",
        admin_path=Path(manifest["paths"]["admin"]) / "admin.sock",
        business_gid=manifest["business_gid"],
        activation_check=check_metadata,
    )

    # This executable exposes memo-only operations. Metadata checks do not
    # unlock credentials, external effects, or a claim of verified isolation.
    def stop(signum, frame):
        raise KeyboardInterrupt

    old_handler = signal.signal(signal.SIGTERM, stop)
    try:
        service.start()
        print(
            json.dumps(
                {"mode": "memo-only", "fenced": True, "ready_for_credentials": False}
            ),
            flush=True,
        )
        service.serve_forever()
    finally:
        service.close()
        signal.signal(signal.SIGTERM, old_handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Experimental credential-free deskd workflow"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser(
        "demo", help="create a fresh offline mock workflow and board"
    )
    demo.add_argument("--output", type=Path, required=True)
    check = commands.add_parser(
        "preflight", help="check explicit installation manifest metadata"
    )
    check.add_argument("manifest", type=Path)
    bridge = commands.add_parser(
        "bridge", help="fixed stdio MCP bridge for the trusted harness"
    )
    bridge.add_argument("--socket", type=Path, required=True)
    bridge.add_argument("--gateway-uid", type=int, required=True)
    serve = commands.add_parser(
        "serve-memo", help="run the fenced memo-only gateway after host preparation"
    )
    serve.add_argument("--manifest", type=Path, required=True)
    admin = commands.add_parser(
        "control", help="one privileged admin call (never automatically retried)"
    )
    admin.add_argument("--socket", type=Path, required=True)
    admin.add_argument("--gateway-uid", type=int, required=True)
    admin.add_argument(
        "method",
        choices=(
            "status",
            "connections",
            "bind",
            "revoke",
            "activate",
            "fence",
            "lease",
        ),
    )
    admin.add_argument(
        "--params", default="{}", help="non-secret JSON administration parameters"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            from .demo import run_demo

            report = run_demo(args.output)
            print(
                json.dumps(
                    {
                        "mode": report["mode"],
                        "checks": len(report["checks"]),
                        "published_memos": len(report["snapshot"]["memos"]),
                        "board": str(args.output.absolute() / "board.html"),
                        "isolation_verified": False,
                        "ready_for_credentials": False,
                    }
                )
            )
        elif args.command == "preflight":
            report = preflight(_manifest(args.manifest))
            print(json.dumps(report.as_dict(), indent=2))
            return 0 if report.metadata_ok else 2
        elif args.command == "bridge":
            run_bridge(
                args.socket,
                gateway_uid=args.gateway_uid,
                source=sys.stdin.buffer,
                target=sys.stdout.buffer,
            )
        elif args.command == "serve-memo":
            _serve_memo(args.manifest)
        else:
            reply = control(
                args.socket,
                args.gateway_uid,
                args.method,
                _decode(args.params.encode()),
            )
            print(json.dumps(reply))
            return 0 if reply["ok"] else 2
    except KeyboardInterrupt:
        return 130
    except FileExistsError:
        print('{"error":"output_or_socket_already_exists"}', file=sys.stderr)
        return 2
    except OSError:
        # After sending a request, even a transport error may follow commit.
        print('{"error":"io_failure_outcome_may_be_unknown"}', file=sys.stderr)
        return 2
    except (ValueError, TypeError, KeyError, RuntimeError, BridgeError):
        print('{"error":"request_or_configuration_rejected"}', file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
