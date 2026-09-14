from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "CoreGeek" / "src"))

from agent.application import AgentApplication
from agent.protocol import InvalidObservation, Observation, encode_response, idle_response


class ProtocolTests(unittest.TestCase):
    def test_complete_idle_response_and_fresh_commands(self) -> None:
        response = idle_response()
        self.assertEqual(response, {"roleCommandMap": {}, "prompt": "", "executeCmd": ""})
        response["roleCommandMap"]["example"] = {}
        self.assertEqual(idle_response()["roleCommandMap"], {})

    def test_preserves_all_observation_fields(self) -> None:
        payload = {"roundNo": 0, "unknownFutureField": {"value": 42}}
        observation = Observation.from_payload(payload)
        self.assertEqual(observation.round_no, 0)
        self.assertIs(observation.raw, payload)
        self.assertEqual(observation.raw["unknownFutureField"], {"value": 42})

    def test_response_requires_complete_envelope(self) -> None:
        for payload in (None, {}, {"roleCommandMap": {}, "prompt": "", "executeCmd": None}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                encode_response(payload)
        self.assertEqual(json.loads(encode_response(idle_response())), idle_response())

    def test_rejects_invalid_round_numbers(self) -> None:
        for round_no in (None, True, -1, 1.2, "1"):
            with self.subTest(round_no=round_no), self.assertRaises(InvalidObservation):
                Observation.from_payload({"roundNo": round_no})

    def test_rejects_nonobject(self) -> None:
        for payload in (None, [], 1, "text"):
            with self.subTest(payload=payload), self.assertRaises(InvalidObservation):
                Observation.from_payload(payload)

    def test_official_request_and_repeat_are_idle(self) -> None:
        payload = json.loads((ROOT / "docs" / "request.txt").read_text(encoding="utf-8-sig"))
        application = AgentApplication()
        self.assertEqual(application.handle_turn(payload), idle_response())
        self.assertEqual(application.handle_turn(payload), idle_response())


if __name__ == "__main__":
    unittest.main()
