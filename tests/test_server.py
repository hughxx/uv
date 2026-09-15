from __future__ import annotations

import http.client
import json
import logging
import logging.handlers
import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "CoreGeek" / "src"))

from agent.application import AgentApplication
from agent.protocol import idle_response
from agent.server import AgentHTTPServer, MAX_REQUEST_BYTES
from tests.helpers import packet


class HTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logs = queue.Queue()
        handler = logging.handlers.QueueHandler(self.logs)
        logger = logging.getLogger("agent.server")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
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

    def logged(self, name="turn") -> dict:
        for _ in range(20):
            record = self.logs.get(timeout=3)
            event = json.loads(record.getMessage())
            if event["event"] == name:
                return event
        self.fail(f"missing event: {name}")

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
        self.assertEqual(self.request(json.dumps(packet()).encode()), (200, idle_response()))
        event = self.logged()
        self.assertEqual((event["http"], event["reason"], event["actionCount"]), (200, "decision_exception", 0))
        self.assertEqual(event["error"]["type"], "RuntimeError")
        self.assertTrue(event["error"]["frames"])
        self.assertNotIn("not logged", json.dumps(event))

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
            with self.subTest(response=bad_response):
                self.assertEqual(self.request(json.dumps(packet()).encode()), (200, idle_response()))
                self.assertEqual(self.logged()["reason"], "decision_exception")

    def test_request_and_turn_are_correlated_and_show_empty_reason(self) -> None:
        self.assertEqual(self.request(json.dumps(packet([])).encode())[0], 200)
        received = self.logged("request_received")
        turn = self.logged()
        self.assertEqual(received["id"], turn["id"])
        self.assertEqual((turn["round"], turn["http"], turn["actionCount"]), (1, 200, 0))
        self.assertEqual(turn["decision"]["emptyReason"], "no_living_actors")

    def test_rejected_requests_have_reason_and_input_shape(self) -> None:
        self.assertEqual(self.request(b'{"roundNo": true}')[0], 400)
        event = self.logged()
        self.assertEqual(event["reason"], "invalid_observation")
        self.assertEqual(event["input"]["roundType"], "bool")
        self.assertEqual(event["error"]["type"], "InvalidObservation")
        self.assertEqual(self.request(b"PRIVATE_INVALID_JSON")[0], 400)
        event = self.logged()
        self.assertEqual(event["reason"], "invalid_json")
        self.assertNotIn("PRIVATE_INVALID_JSON", json.dumps(event))

    def test_game_shaped_probe_is_visibly_flagged(self) -> None:
        self.assertEqual(self.request(b'{"roundNo": 1, "mapInfo": {}}')[0], 200)
        event = self.logged()
        self.assertEqual(event["status"], "probe")
        self.assertEqual(event["reason"], "missing_game_fields_treated_as_probe")
        self.assertIn("teamOur", event["input"]["missing"])

    def test_unsupported_method_does_not_log_path_or_credentials(self) -> None:
        client = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            client.request("GET", "/PRIVATE_PATH?token=PRIVATE_QUERY", headers={"Authorization": "PRIVATE_AUTH"})
            reply = client.getresponse()
            self.assertEqual(reply.status, 501)
            reply.read()
        finally:
            client.close()
        event = self.logged("http_rejected")
        self.assertEqual(event["method"], "GET")
        self.assertNotIn("PRIVATE_", json.dumps(event))

    def test_summary_failure_does_not_change_response(self) -> None:
        with patch("agent.server.input_outline", side_effect=RuntimeError("PRIVATE_DETAIL")):
            self.assertEqual(self.request(b'{"roundNo": 1}'), (200, idle_response()))
            error = self.logged("telemetry_error")
            self.assertNotIn("PRIVATE_DETAIL", json.dumps(error))
            self.assertEqual(self.logged()["http"], 200)


if __name__ == "__main__":
    unittest.main()
