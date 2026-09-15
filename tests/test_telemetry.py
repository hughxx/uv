from __future__ import annotations

import json
import logging
import queue
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "CoreGeek" / "src"))

from agent.application import AgentApplication
from agent.telemetry import (MAX_EVENT_BYTES, NonBlockingHandler, decision_outline, event_text,
                             exception_location, input_outline, log_event, response_outline)
from tests.helpers import packet, unit


class TelemetryTests(unittest.TestCase):
    def test_event_is_bounded_single_line_json(self):
        encoded = event_text("turn", id=12, round=3, input={str(i): "中文\n\r\x00" * 200 for i in range(60)},
                             other=[float("nan"), float("inf"), 10 ** 1000])
        self.assertLessEqual(len(encoded.encode("utf-8")), MAX_EVENT_BYTES)
        self.assertNotIn("\n", encoded)
        self.assertNotIn("\r", encoded)
        event = json.loads(encoded)
        self.assertEqual((event["id"], event["round"]), (12, 3))
        self.assertIn("input", event["omittedFields"])
        self.assertEqual(event["other"], ["nonfinite", "nonfinite", "oversized-number"])

    def test_input_redacts_task_tool_and_identity_values(self):
        payload = packet(phaseTask="PRIVATE_TASK", llmResp="PRIVATE_LLM", lastCmdResult="PRIVATE_CMD",
                         news={"message": "PRIVATE_NEWS"})
        payload["teamOur"].update(teamId="PRIVATE_ID", teamName="PRIVATE_NAME")
        payload["teamOur"]["roles"][1]["health"] = "220"
        outline = input_outline(payload)
        self.assertNotIn("PRIVATE_", json.dumps(outline))
        self.assertEqual(outline["phaseTask"]["chars"], len("PRIVATE_TASK"))
        self.assertEqual(outline["our"]["healthTypes"], {"int": 1, "str": 1})
        self.assertEqual(outline["our"]["units"][1]["health"], None)
        self.assertEqual(input_outline([]), {"rootType": "list"})
        self.assertIn("teamOur", input_outline({"roundNo": 1})["missing"])

    def test_response_preserves_actions_but_redacts_sensitive_strings(self):
        response = {"roleCommandMap": {"1": {"action": "submitAnswer", "taskAnswer": "PRIVATE_ANSWER"},
                                       "2": {"action": "build", "name": "gatling", "targetPos": {"x": 4, "y": 5}}},
                    "prompt": "PRIVATE_PROMPT", "executeCmd": "PRIVATE_COMMAND"}
        outline = response_outline(response)
        self.assertNotIn("PRIVATE_", json.dumps(outline))
        self.assertEqual(outline["actionCount"], 2)
        self.assertEqual(outline["actions"]["1"]["taskAnswerChars"], len("PRIVATE_ANSWER"))
        self.assertEqual(outline["actions"]["2"], response["roleCommandMap"]["2"])

    def test_empty_action_and_construction_gates_are_visible(self):
        app = AgentApplication()
        app.handle_turn(packet([unit(10013, "station")], gold=0))
        outline = decision_outline(app)
        self.assertEqual(outline["emptyReason"], "no_living_actors")
        self.assertIn("no_living_worker", outline["buildCheck"]["gates"])
        self.assertIn("gold_unknown_or_insufficient", outline["buildCheck"]["gates"])
        self.assertIn("offers", outline)
        self.assertIn("rejections", outline["search"])

    def test_cached_or_probe_summary_does_not_reuse_previous_plan(self):
        app = AgentApplication()
        payload = packet()
        app.handle_turn(payload)
        self.assertIn("selected", decision_outline(app))
        app.handle_turn(payload)
        self.assertEqual(decision_outline(app), {"status": "cached", "revision": 1})
        app.handle_turn({"roundNo": 3})
        self.assertEqual(decision_outline(app), {"status": "probe", "revision": 1})

    def test_defense_summary_distinguishes_operator_target_and_action_availability(self):
        app = AgentApplication()
        roles = [unit(pos=(4, 5)), unit(10030, "railgun", (3, 6)), unit(10020, "gatling", (6, 6))]
        app.handle_turn(packet(roles, round_no=71, robots=[unit(30001, "largeRobot", (3, 10), health=500)]))
        outline = decision_outline(app)
        defense = {row["weapon"]: row for row in outline["defense"]}
        self.assertEqual(defense["10030"]["adjacent"], ["10010"])
        self.assertEqual(defense["10030"]["inRangeRobots"], 1)
        self.assertGreater(defense["10030"]["offers"]["fire"], 0)
        self.assertEqual(defense["10020"]["adjacent"], [])
        self.assertEqual(defense["10020"]["offers"]["fire"], 0)
        self.assertGreater(defense["10020"]["offers"]["approach"], 0)
        self.assertEqual(outline["robotSnapshot"]["hostileLiving"], 1)
        self.assertEqual(outline["robotSnapshot"]["nearest"][0]["pos"], [3, 10])
        self.assertEqual(outline["staffing"]["shotsIssued"], 1)
        # Exercise the actual wire encoding, not only the pre-clipping outline.
        wire = json.loads(event_text("turn", decision=outline))
        encoded_defense = {row["weapon"]: row for row in wire["decision"]["defense"]}
        self.assertEqual(encoded_defense["10030"]["selected"], [[["10010"], "fire"]])

    def test_official_sample_keeps_input_decision_and_actions_within_event_limit(self):
        app = AgentApplication()
        raw = json.loads((Path(__file__).resolve().parents[1] / "docs" / "request.txt").read_text(encoding="utf-8-sig"))
        response = app.handle_turn(raw)
        encoded = event_text("turn", round=raw["roundNo"], input=input_outline(raw), decision=decision_outline(app), **response_outline(response))
        event = json.loads(encoded)
        self.assertLessEqual(len(encoded), MAX_EVENT_BYTES)
        for key in ("input", "decision", "actions"):
            self.assertIn(key, event)

    def test_task_diagnostics_preserve_privacy_and_expose_budget(self):
        app = AgentApplication()
        app.handle_turn(packet([unit(10011, "pioneer")], phaseTask="PRIVATE_TASK"))
        outline = decision_outline(app)
        self.assertEqual(outline["task"]["requestsUsed"], 1)
        self.assertEqual(outline["task"]["commandRequests"], 0)
        self.assertEqual(outline["task"]["lastEvent"], "llm-requested")
        self.assertNotIn("PRIVATE_TASK", json.dumps(outline))

    def test_queue_saturation_drops_without_blocking_and_reports_later(self):
        records = queue.Queue(maxsize=2)
        handler = NonBlockingHandler(records)
        record = logging.LogRecord("agent.server", logging.INFO, "", 0, "safe", (), None)
        handler.enqueue(record)
        handler.enqueue(record)
        start = time.monotonic()
        for _ in range(1000):
            handler.enqueue(record)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual((records.qsize(), handler.dropped), (2, 1000))
        records.get_nowait()
        records.get_nowait()
        handler.enqueue(record)
        self.assertEqual(records.get_nowait().msg, "safe")
        notice = json.loads(records.get_nowait().msg)
        self.assertEqual((notice["event"], notice["count"], handler.dropped), ("logs_dropped", 1000, 0))

    def test_logging_failure_does_not_escape(self):
        logger = Mock()
        logger.log.side_effect = OSError("PRIVATE_ERROR")
        log_event(logger, "turn", round=1)
        logger.log.assert_called_once()

    def test_exception_location_excludes_message_source_and_absolute_path(self):
        try:
            raise RuntimeError("PRIVATE_ERROR")
        except RuntimeError as error:
            outline = exception_location(error)
        self.assertEqual(outline["type"], "RuntimeError")
        encoded = json.dumps(outline)
        self.assertNotIn("PRIVATE_ERROR", encoded)
        self.assertNotIn(str(Path(__file__).resolve().parent), encoded)
        self.assertRegex(outline["frames"][-1], r"^test_telemetry.py:\d+:test_exception_location")


if __name__ == "__main__":
    unittest.main()
