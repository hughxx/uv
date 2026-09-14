from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "CoreGeek" / "src"))

from agent.application import AgentApplication
from agent.protocol import idle_response
from agent.server import AgentHTTPServer, MAX_REQUEST_BYTES


class HTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = AgentHTTPServer(("127.0.0.1", 0))
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, body: bytes, headers: dict[str, str] | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request("POST", "/turn", body, headers or {"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertIn("application/json", response.getheader("Content-Type", ""))
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_valid_request(self) -> None:
        self.assertEqual(self.request(b'{"roundNo": 1}'), (200, idle_response()))

    def test_invalid_json_does_not_break_next_request(self) -> None:
        for body in (b"{", b"\xff", b'{"roundNo": NaN}'):
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        self.assertEqual(self.request(b'{"roundNo": 2}')[0], 200)

    def test_invalid_observation(self) -> None:
        for body in (b"[]", b"{}", b'{"roundNo": true}'):
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)

    def test_body_limit_and_invalid_length(self) -> None:
        self.assertEqual(self.request(b"", {"Content-Length": str(MAX_REQUEST_BYTES + 1)})[0], 413)
        for value in ("-1", "text"):
            with self.subTest(length=value):
                self.assertEqual(self.request(b"", {"Content-Length": value})[0], 400)

    def test_unsupported_transfer_encoding(self) -> None:
        self.assertEqual(self.request(b"", {"Transfer-Encoding": "chunked"})[0], 400)

    def test_application_failure_returns_complete_fallback(self) -> None:
        class FailingApplication(AgentApplication):
            def decide(self, observation):
                raise RuntimeError("not logged")

        self.server.application = FailingApplication()
        with self.assertLogs("agent.server", level="ERROR") as captured:
            self.assertEqual(self.request(b'{"roundNo": 1}'), (200, idle_response()))
        self.assertNotIn("not logged", " ".join(captured.output))

    def test_busy_application_has_bounded_wait(self) -> None:
        self.server.decision_lock.acquire()
        try:
            self.assertEqual(self.request(b'{"roundNo": 1}')[0], 503)
        finally:
            self.server.decision_lock.release()

    def test_invalid_application_output_falls_back_before_headers(self) -> None:
        for bad_response in ({}, {"roleCommandMap": {"x": object()}, "prompt": "", "executeCmd": ""}):
            class InvalidApplication(AgentApplication):
                def decide(self, observation):
                    return bad_response

            self.server.application = InvalidApplication()
            with self.subTest(response=bad_response), self.assertLogs("agent.server", level="ERROR"):
                self.assertEqual(self.request(b'{"roundNo": 1}'), (200, idle_response()))


if __name__ == "__main__":
    unittest.main()
