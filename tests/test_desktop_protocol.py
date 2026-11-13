import json
import os
import socket
import unittest

from orchestrator.desktop_protocol import ProtocolError, peer_uid, validate_request


class ProtocolTests(unittest.TestCase):
    def test_unknown_version_command_and_malformed_message_refused(self):
        fixtures = [b"oops", b"[]", b"x" * 65537,
                    json.dumps({"version": 2, "request_id": "1", "command": "status", "params": {}}).encode(),
                    json.dumps({"version": 1, "request_id": "1", "command": "shell", "params": {}}).encode()]
        for raw in fixtures:
            with self.assertRaises(ProtocolError):
                validate_request(raw)

    def test_status_request_validated_and_peer_uid_verified(self):
        result = validate_request(b'{"version":1,"request_id":"a","command":"status","params":{}}\n')
        self.assertEqual(result["command"], "status")
        left, right = socket.socketpair()
        try:
            self.assertEqual(peer_uid(left), os.getuid())
        finally:
            left.close()
            right.close()

    def test_arbitrary_params_not_accepted(self):
        for params in ({"enabled": "false"}, {"enabled": False, "shell": "evil"}):
            with self.assertRaises(ProtocolError):
                validate_request(json.dumps({"version": 1, "request_id": "a", "command": "remote_access", "params": params}).encode())
