"""Optional realtime terminal client for a remote deskd control plane.

The transport and command parser are intentionally independent of Textual so
hosts can test integrations without installing a terminal framework.  Import
``deskd.tui.app`` only from :func:`run_tui`, after the optional dependency has
been checked by the CLI.
"""

from .client import (
    APIError,
    ClientError,
    ConnectionNotice,
    ControlPlaneClient,
    CursorExpired,
    DeskSnapshot,
    ProtocolError,
    SSEEvent,
    load_api_token,
    parse_sse,
)
from .commands import CommandRequest, LocalAction, command_help, parse_command

__all__ = [
    "APIError",
    "ClientError",
    "CommandRequest",
    "ConnectionNotice",
    "ControlPlaneClient",
    "CursorExpired",
    "DeskSnapshot",
    "LocalAction",
    "ProtocolError",
    "SSEEvent",
    "command_help",
    "load_api_token",
    "parse_command",
    "parse_sse",
]
