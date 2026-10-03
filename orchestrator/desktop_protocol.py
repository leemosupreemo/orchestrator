"""Small, bounded protocol for native clients on a same-user Unix socket."""
from __future__ import annotations

import ctypes
import json
import os
import socket
import struct
import sys

MAX_MESSAGE = 65536
COMMAND_PARAMS = {
    "status": (), "pair_start": ("name",), "pair_cancel": (), "browser_grant": ("route",),
    "remote_access": ("enabled",), "diagnostics": (), "stop_if_idle": (),
    "prepare_update": (), "cancel_update": (), "legacy_status": (), "legacy_migrate": ("consent",),
}


class ProtocolError(ValueError):
    def __init__(self, message: str, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code


def peer_uid(connection: socket.socket) -> int:
    if sys.platform == "darwin":
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise OSError(ctypes.get_errno(), "Cannot verify socket peer")
        return uid.value
    _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return uid


def validate_request(raw: bytes) -> dict:
    if len(raw) > MAX_MESSAGE:
        raise ProtocolError("Message is too large.")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ProtocolError("Invalid JSON message.") from None
    if not isinstance(data, dict) or set(data) != {"version", "request_id", "command", "params"}:
        raise ProtocolError("Invalid message envelope.")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ProtocolError("Update the menu app to match the agent.", "version_mismatch")
    request_id = data["request_id"]
    command, params = data["command"], data["params"]
    if not isinstance(request_id, str) or not request_id or len(request_id) > 100:
        raise ProtocolError("Invalid request ID.")
    if not isinstance(command, str) or command not in COMMAND_PARAMS:
        raise ProtocolError("Unknown command.")
    if not isinstance(params, dict) or any(key not in COMMAND_PARAMS[command] for key in params):
        raise ProtocolError("Unknown command parameter.")
    if command == "remote_access" and type(params.get("enabled")) is not bool:
        raise ProtocolError("Remote access needs a boolean preference.")
    if command == "legacy_migrate" and type(params.get("consent")) is not bool:
        raise ProtocolError("Moving an older service needs an explicit yes or no.")
    if "name" in params and (not isinstance(params["name"], str) or len(params["name"]) > 100):
        raise ProtocolError("Invalid computer name.")
    if "route" in params and (not isinstance(params["route"], str) or len(params["route"]) > 8192):
        raise ProtocolError("Invalid browser route.")
    return data
