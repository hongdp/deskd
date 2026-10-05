"""Linux UDS boundary for the gateway's fixed, credential-free local protocol.

Business peers cannot invoke management verbs. SO_PEERCRED proves a process UID,
not a role: authentic MCP metadata still depends on the sandboxed harness trust
closure. Connection IDs and service generations are assigned here, never taken
from request arguments. Administrative sockets require a separate OS principal.

Wire: {id: str, method: str, params: object}; response is {id, ok, result} or
{id, ok: false, error: {code}}. Business methods: hello, execute, status.
execute.params = {request_id, mcp: {name, arguments, _meta}}.
Management methods: status, connections, bind, revoke, activate, fence, lease.
There is no general SQL, process execution, remote URL or upstream tool proxy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import socket
import sqlite3
import stat
import struct
import sys
import threading
from typing import Any, Callable
import uuid

from .commands import GatewayCommands
from .actions import ActionError
from .events import EventConflict, EventValidationError
from .identity import (
    IdentityError,
    PrincipalId,
    TransportEvidence,
    identifier,
    parse_metadata,
)
from .registry import Registry
from .wire import JsonLineReader, WireError, encode_frame

BUSINESS_METHODS = frozenset({"hello", "execute", "status", "read", "identify", "model.auth"})
ADMIN_METHODS = frozenset(
    {"status", "connections", "bind", "revoke", "activate", "fence", "lease"}
)


class TransportError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int


def peer_credentials(connection: socket.socket) -> PeerCredentials:
    if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
        raise TransportError("linux_peer_credentials_required")
    raw = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    pid, uid, gid = struct.unpack("3i", raw)
    if pid <= 0 or uid < 0 or gid < 0:
        raise TransportError("invalid_peer_credentials")
    return PeerCredentials(pid, uid, gid)


def validate_service_domains(
    gateway_uid: int, harness_uid: int, admin_uids: frozenset[int]
) -> None:
    """Production gate: distinct service identities and management principal."""
    if (
        type(gateway_uid) is not int
        or gateway_uid <= 0
        or type(harness_uid) is not int
        or harness_uid <= 0
    ):
        raise TransportError("service_uid_must_be_nonroot")
    if gateway_uid == harness_uid:
        raise TransportError("gateway_harness_uid_must_differ")
    if not admin_uids or any(type(uid) is not int or uid < 0 for uid in admin_uids):
        raise TransportError("invalid_admin_uids")
    if gateway_uid in admin_uids or harness_uid in admin_uids:
        raise TransportError("admin_uid_must_be_independent")


class _Endpoint:
    """Own exactly one freshly bound filesystem inode; never remove a predecessor."""

    def __init__(
        self, path: Path | str, *, gid: int, directory_mode: int, socket_mode: int
    ):
        self.path = Path(path)
        self.gid = gid
        self.directory_mode = directory_mode
        self.socket_mode = socket_mode
        self.listener: socket.socket | None = None
        self.parent_fd: int | None = None
        self.identity: tuple[int, int] | None = None

    def bind(self) -> None:
        if not self.path.is_absolute() or self.path.name in ("", ".", ".."):
            raise TransportError("socket_path_must_be_absolute")
        if len(os.fsencode(self.path)) > 107:
            raise TransportError("socket_path_too_long")
        for ancestor in reversed(self.path.parent.parents):
            self._check_ancestor(ancestor)
        self._check_ancestor(self.path.parent)
        info = self.path.parent.lstat()
        if (
            info.st_uid != os.geteuid()
            or info.st_gid != self.gid
            or stat.S_IMODE(info.st_mode) != self.directory_mode
        ):
            raise TransportError("unsafe_socket_directory")
        self.parent_fd = os.open(
            self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            try:
                os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise TransportError("socket_path_exists")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener = listener
            listener.bind(str(self.path))
            created = os.stat(
                self.path.name, dir_fd=self.parent_fd, follow_symlinks=False
            )
            self.identity = (created.st_dev, created.st_ino)
            os.chown(
                self.path.name,
                os.geteuid(),
                self.gid,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            os.chmod(self.path.name, self.socket_mode, dir_fd=self.parent_fd)
            self.check()
            listener.listen(16)
            listener.settimeout(0.2)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check_ancestor(path: Path) -> None:
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid not in (0, os.geteuid())
            or info.st_mode & 0o022
        ):
            raise TransportError("unsafe_socket_ancestor")

    def check(self) -> None:
        if self.parent_fd is None or self.identity is None:
            raise TransportError("socket_not_bound")
        parent = os.fstat(self.parent_fd)
        current_parent = self.path.parent.lstat()
        info = os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False)
        if (
            (parent.st_dev, parent.st_ino)
            != (current_parent.st_dev, current_parent.st_ino)
            or not stat.S_ISDIR(current_parent.st_mode)
            or current_parent.st_uid != os.geteuid()
            or current_parent.st_gid != self.gid
            or stat.S_IMODE(current_parent.st_mode) != self.directory_mode
            or not stat.S_ISSOCK(info.st_mode)
            or (info.st_dev, info.st_ino) != self.identity
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_gid != self.gid
            or stat.S_IMODE(info.st_mode) != self.socket_mode
        ):
            raise TransportError("socket_identity_changed")

    def close(self) -> None:
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.parent_fd is not None:
            try:
                info = os.stat(
                    self.path.name, dir_fd=self.parent_fd, follow_symlinks=False
                )
                if (
                    stat.S_ISSOCK(info.st_mode)
                    and (info.st_dev, info.st_ino) == self.identity
                ):
                    os.unlink(self.path.name, dir_fd=self.parent_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(self.parent_fd)
                self.parent_fd = None


@dataclass
class _Accepted:
    connection: socket.socket
    peer: PeerCredentials
    evidence: TransportEvidence
    requested_root: str | None = None


class GatewayTransport:
    """Threaded, bounded local service; production start never permits same UID.

    Parent directories must already exist: business gateway-owned 0750 with
    business_gid; admin gateway-owned 0700 (root management). Optional admin_gid
    permits a separately provisioned human group directory 0750/socket 0660.
    No directories, users, groups, AppArmor rules or running daemons are altered.

    activation_check is fixed trusted controller code, not a client-provided
    health flag. It must independently verify deployment/manifest health. This
    service alone does not establish the harness's full lifecycle/sandbox proof.
    """

    def __init__(
        self,
        registry: Registry,
        commands: GatewayCommands,
        *,
        business_path: Path | str,
        admin_path: Path | str,
        business_gid: int,
        activation_check: Callable[[], None],
        admin_uids: frozenset[int] = frozenset({0}),
        admin_gid: int | None = None,
        max_connections: int = 16,
        idle_timeout: float = 300,
        frame_timeout: float = 5,
        readers: dict[str, Callable] | None = None,
        admin_handlers: dict[str, Callable] | None = None,
        allow_identify: bool = False,
        auth_provider: Callable[[], str] | None = None,
    ):
        if not callable(activation_check):
            raise TransportError("activation_check_required")
        if (
            type(business_gid) is not int
            or business_gid < 0
            or (admin_gid is not None and (type(admin_gid) is not int or admin_gid < 0))
        ):
            raise TransportError("invalid_socket_group")
        if (
            type(max_connections) is not int
            or not 1 <= max_connections <= 128
            or type(idle_timeout) not in (int, float)
            or not 0 < idle_timeout <= 3600
            or type(frame_timeout) not in (int, float)
            or not 0 < frame_timeout <= 60
        ):
            raise TransportError("invalid_transport_limits")
        if registry.harness_uid in admin_uids:
            raise TransportError("admin_uid_must_be_independent")
        if not admin_uids or any(type(uid) is not int or uid < 0 for uid in admin_uids):
            raise TransportError("invalid_admin_uids")
        if Path(business_path) == Path(admin_path):
            raise TransportError("socket_paths_must_differ")
        self.registry = registry
        self.commands = commands
        self.allow_identify = allow_identify
        if auth_provider is not None and not callable(auth_provider):
            raise TransportError("invalid_auth_provider")
        self.auth_provider = auth_provider
        # Fixed installation code only; neither map can be populated by a peer.
        self.readers = dict(readers or {})
        self.admin_handlers = dict(admin_handlers or {})
        for name, handler in self.readers.items():
            identifier(name, "reader")
            if not callable(handler):
                raise TransportError("invalid_reader")
        for name, handler in self.admin_handlers.items():
            identifier(name, "admin_handler")
            if not name.startswith("workspace.") or not callable(handler):
                raise TransportError("invalid_admin_handler")
        self.admin_uids = frozenset(admin_uids)
        self.activation_check = activation_check
        self.idle_timeout = idle_timeout
        self.frame_timeout = frame_timeout
        self.service_generation: str | None = None
        self._business = _Endpoint(
            business_path, gid=business_gid, directory_mode=0o750, socket_mode=0o660
        )
        self._admin = _Endpoint(
            admin_path,
            gid=os.getegid() if admin_gid is None else admin_gid,
            directory_mode=0o700 if admin_gid is None else 0o750,
            socket_mode=0o600 if admin_gid is None else 0o660,
        )
        self._slots = {
            "business": threading.BoundedSemaphore(max_connections),
            "admin": threading.BoundedSemaphore(4),
        }
        self._connections: dict[str, _Accepted] = {}
        self._sockets: set[socket.socket] = set()
        self._threads: set[threading.Thread] = set()
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_version = 0
        self._stopping = threading.Event()
        self._closed = False

    def start(self) -> "GatewayTransport":
        if sys.platform != "linux":
            raise TransportError("linux_peer_credentials_required")
        validate_service_domains(
            os.geteuid(), self.registry.harness_uid, self.admin_uids
        )
        return self._start_listeners()

    def _start_listeners(self) -> "GatewayTransport":
        """Internal listener seam; tests exercise sockets without claiming UID setup."""
        if self.service_generation is not None or self._closed:
            raise TransportError("transport_already_started")
        # Even a failed socket startup leaves previously persisted authority fenced.
        self.service_generation = self.registry.start_service()
        try:
            self._business.bind()
            self._admin.bind()
            for domain, endpoint in (
                ("business", self._business),
                ("admin", self._admin),
            ):
                thread = threading.Thread(
                    target=self._run_accept, args=(domain, endpoint), daemon=True
                )
                with self._lock:
                    if self._closed:
                        raise TransportError("transport_stopping")
                    self._threads.add(thread)
                    try:
                        thread.start()
                    except BaseException:
                        self._threads.discard(thread)
                        raise
        except BaseException:
            self.close()
            raise
        return self

    def serve_forever(self) -> None:
        if self.service_generation is None:
            self.start()
        try:
            while not self._stopping.wait(0.5):
                pass
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stopping.set()
        with self._lifecycle_lock:
            self._lifecycle_version += 1
            if self.service_generation is not None:
                try:
                    self.registry.fence(
                        expected_service_generation=self.service_generation
                    )
                except (IdentityError, sqlite3.Error):
                    pass  # A replacement process may already own and fence the registry.
        self._business.close()
        self._admin.close()
        with self._lock:
            connections = list(self._sockets)
            threads = list(self._threads)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1)

    def _run_accept(self, domain: str, endpoint: _Endpoint) -> None:
        try:
            self._accept(domain, endpoint)
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())

    def _accept(self, domain: str, endpoint: _Endpoint) -> None:
        while not self._stopping.is_set():
            try:
                endpoint.check()
                assert endpoint.listener is not None
                connection, _ = endpoint.listener.accept()
            except TimeoutError:
                continue
            except (OSError, TransportError):
                if not self._stopping.is_set():
                    self.close()
                return
            if not self._slots[domain].acquire(blocking=False):
                self._reply(
                    connection,
                    {"id": None, "ok": False, "error": {"code": "server_busy"}},
                )
                connection.close()
                continue
            with self._lock:
                if self._closed:
                    connection.close()
                    self._slots[domain].release()
                    return
                self._sockets.add(connection)
                thread = threading.Thread(
                    target=self._handle,
                    args=(domain, endpoint, connection),
                    daemon=True,
                )
                self._threads.add(thread)
                try:
                    thread.start()  # close() must never observe an unstarted worker.
                except BaseException:
                    self._threads.discard(thread)
                    self._sockets.discard(connection)
                    connection.close()
                    self._slots[domain].release()
                    raise

    @staticmethod
    def _reply(connection: socket.socket, response: dict[str, Any]) -> None:
        try:
            failed_encoding = False
            try:
                frame = encode_frame(response, response=True)
            except ValueError:
                failed_encoding = True
                frame = encode_frame(
                    {
                        "id": response.get("id"),
                        "ok": False,
                        "error": {"code": "response_encoding_error"},
                    },
                    response=True,
                )
            connection.settimeout(5)
            connection.sendall(frame)
            if failed_encoding:
                # The mutation may already have committed. End this transport;
                # clients must resolve the same request_id, never invent a retry.
                connection.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass  # A committed command is retried with the same request_id after a lost response.

    def _handle(
        self, domain: str, endpoint: _Endpoint, connection: socket.socket
    ) -> None:
        channel: str | None = None
        try:
            peer = peer_credentials(connection)
            if (domain == "business" and peer.uid != self.registry.harness_uid) or (
                domain == "admin" and peer.uid not in self.admin_uids
            ):
                self._reply(
                    connection,
                    {"id": None, "ok": False, "error": {"code": "untrusted_peer"}},
                )
                return
            if domain == "business":
                channel = uuid.uuid4().hex
                assert self.service_generation is not None
                accepted = _Accepted(
                    connection,
                    peer,
                    TransportEvidence(peer.uid, channel, self.service_generation),
                )
                with self._lock:
                    self._connections[channel] = accepted
            reader = JsonLineReader(
                connection,
                idle_timeout=self.idle_timeout,
                frame_timeout=self.frame_timeout,
            )
            for _ in range(1024):
                request_id = None
                try:
                    request = reader.read()
                    if request is None:
                        return
                    endpoint.check()
                    if set(request) != {"id", "method", "params"}:
                        raise WireError("invalid_request_fields")
                    request_id = identifier(request["id"], "wire_request_id")
                    method = identifier(request["method"], "wire_method")
                    if type(request["params"]) is not dict:
                        raise WireError("invalid_request_params")
                    if domain == "business":
                        result = self._business_call(
                            method, request["params"], accepted
                        )
                    else:
                        result = self._admin_call(method, request["params"])
                    self._reply(
                        connection, {"id": request_id, "ok": True, "result": result}
                    )
                except (WireError, IdentityError, TransportError, ActionError) as exc:
                    self._reply(
                        connection,
                        {"id": request_id, "ok": False, "error": {"code": exc.code}},
                    )
                    if isinstance(exc, (WireError, TransportError)):
                        return
                except EventConflict:
                    self._reply(
                        connection,
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {"code": "request_conflict"},
                        },
                    )
                except (EventValidationError, TypeError, ValueError):
                    self._reply(
                        connection,
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {"code": "invalid_request"},
                        },
                    )
                except sqlite3.Error:
                    self._reply(
                        connection,
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {"code": "storage_error"},
                        },
                    )
                    return  # Commit outcome may be unknown to this process.
                except Exception:
                    self._reply(
                        connection,
                        {
                            "id": request_id,
                            "ok": False,
                            "error": {"code": "internal_error"},
                        },
                    )
                    return
        except OSError:
            pass
        finally:
            with self._lock:
                if channel is not None:
                    self._connections.pop(channel, None)
                self._sockets.discard(connection)
                self._threads.discard(threading.current_thread())
            connection.close()
            self._slots[domain].release()

    @staticmethod
    def _fields(params: dict[str, Any], fields: set[str]) -> None:
        if set(params) != fields:
            raise WireError("invalid_method_params")

    def _business_call(
        self, method: str, params: dict[str, Any], accepted: _Accepted
    ) -> Any:
        if method not in BUSINESS_METHODS:
            raise IdentityError("unknown_business_method")
        if method == "hello":
            self._fields(params, set())
            return {
                "connection_id": accepted.evidence.connection_id,
                "service_generation": accepted.evidence.service_generation,
            }
        if method == "model.auth":
            self._fields(params, set())
            if self.auth_provider is None:
                raise IdentityError("model_auth_not_configured")
            return {"token": self.auth_provider()}
        if method == "execute":
            self._fields(params, {"request_id", "mcp"})
            request_id = identifier(params["request_id"], "request_id")
            return asdict(
                self.commands.execute(request_id, params["mcp"], accepted.evidence)
            )
        if method == "identify":
            if not self.allow_identify:
                raise IdentityError("unknown_business_method")
            self._fields(params, {"_meta"})
            root, _ = parse_metadata(params)
            with self._lock:
                if accepted.requested_root not in (None, root):
                    raise IdentityError("channel_root_changed")
                accepted.requested_root = root
            # An observation is not authority. Only the independent controller
            # can grant a lease after checking the registered root and process.
            try:
                self.registry.authorize("state.read", params, accepted.evidence)
                return {"ready": True}
            except IdentityError as exc:
                if exc.code in {"inactive_channel", "service_fenced"}:
                    return {"ready": False}
                raise
        if method == "read":
            self._fields(params, {"name", "arguments", "_meta"})
            name = identifier(params["name"], "reader")
            if name not in self.readers or type(params["arguments"]) is not dict:
                raise IdentityError("unknown_reader")
            identity = self.registry.authorize(name, params, accepted.evidence)
            return self.readers[name](identity, params["arguments"])
        self._fields(params, {"_meta"})
        identity = self.registry.authorize("state.read", params, accepted.evidence)
        return {
            "principal_id": identity.principal.value,
            "root_session_id": identity.root_session_id,
            "binding_generation": identity.binding_generation,
            "manifest_hash": identity.manifest_hash,
            "service_generation": identity.service_generation,
        }

    def _admin_call(self, method: str, params: dict[str, Any]) -> Any:
        if method in self.admin_handlers:
            return self.admin_handlers[method](params)
        if method not in ADMIN_METHODS:
            raise IdentityError("unknown_admin_method")
        assert self.service_generation is not None
        if method in {"status", "connections", "activate", "fence"}:
            self._fields(params, set())
            if method == "activate":
                with self._lifecycle_lock:
                    if self._stopping.is_set():
                        raise IdentityError("transport_stopping")
                    self.registry.fence(
                        expected_service_generation=self.service_generation
                    )
                    self._lifecycle_version += 1
                    version = self._lifecycle_version
                try:
                    self.activation_check()
                except Exception as exc:
                    raise IdentityError("activation_check_failed") from exc
                with self._lifecycle_lock:
                    if self._stopping.is_set() or version != self._lifecycle_version:
                        raise IdentityError("activation_superseded")
                    self.registry.trusted_activate_service(
                        expected_service_generation=self.service_generation
                    )
                return {"activated": True}
            if method == "fence":
                with self._lifecycle_lock:
                    self._lifecycle_version += 1
                    self.registry.fence(
                        expected_service_generation=self.service_generation
                    )
                return {"fenced": True}
            if method == "connections":
                with self._lock:
                    return [
                        {
                            "connection_id": k,
                            "pid": v.peer.pid,
                            "uid": v.peer.uid,
                            "gid": v.peer.gid,
                            "service_generation": v.evidence.service_generation,
                            **(
                                {"requested_root": v.requested_root}
                                if self.allow_identify
                                else {}
                            ),
                        }
                        for k, v in self._connections.items()
                    ]
            with sqlite3.connect(self.registry.db_path) as conn:
                row = conn.execute(
                    "SELECT generation,active FROM service WHERE singleton=1"
                ).fetchone()
            return {
                "service_generation": self.service_generation,
                "fenced": not row
                or row[0] != self.service_generation
                or not bool(row[1]),
            }
        base = {"desk_id", "seat_id"}
        if method == "bind":
            self._fields(
                params,
                base
                | {
                    "root_session_id",
                    "manifest_hash",
                    "capabilities",
                    "expected_binding_generation",
                },
            )
            caps = params["capabilities"]
            if (
                type(caps) is not list
                or any(type(c) is not str for c in caps)
                or len(caps) != len(set(caps))
            ):
                raise WireError("invalid_capabilities")
            result = self.registry.trusted_bind(
                PrincipalId(params["desk_id"], params["seat_id"]),
                root_session_id=params["root_session_id"],
                manifest_hash=params["manifest_hash"],
                capabilities=frozenset(caps),
                expected_binding_generation=params["expected_binding_generation"],
            )
            return {
                "principal_id": result.principal.value,
                "root_session_id": result.root_session_id,
                "binding_generation": result.binding_generation,
                "manifest_hash": result.manifest_hash,
            }
        if method == "revoke":
            self._fields(params, base | {"expected_binding_generation"})
            result = self.registry.trusted_revoke(
                PrincipalId(params["desk_id"], params["seat_id"]),
                expected_binding_generation=params["expected_binding_generation"],
            )
            return {
                "principal_id": result.principal.value,
                "binding_generation": result.binding_generation,
                "status": result.status,
            }
        self._fields(
            params,
            base
            | {
                "connection_id",
                "root_session_id",
                "binding_generation",
                "manifest_hash",
                "ttl_seconds",
            },
        )
        channel = identifier(params["connection_id"], "connection_id")
        with self._lock:
            accepted = self._connections.get(channel)
            if (
                accepted is None
                or accepted.peer.uid != self.registry.harness_uid
                or accepted.evidence.service_generation != self.service_generation
            ):
                raise IdentityError("unknown_live_connection")
            self.registry.trusted_grant_lease(
                PrincipalId(params["desk_id"], params["seat_id"]),
                connection_id=channel,
                service_generation=self.service_generation,
                root_session_id=params["root_session_id"],
                binding_generation=params["binding_generation"],
                manifest_hash=params["manifest_hash"],
                ttl_seconds=params["ttl_seconds"],
            )
        return {"connection_id": channel, "lease_granted": True}
