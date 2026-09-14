import copy
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import packet, unit, world
from scripts.replay import replay
from agent.application import AgentApplication
from agent.diagnostics import decision_report
from agent.geometry import shortest_route
from agent.protocol import idle_response
from agent.world import Pos


ROOT = Path(__file__).resolve().parents[1]


class ReplayTests(unittest.TestCase):
    def test_replay_preserves_receipts_and_explains_only_the_selected_round(self):
        raw = packet()
        first_response = AgentApplication().handle_turn(copy.deepcopy(raw))
        rows = [{"request": raw, "expected_response": first_response}, raw, packet(round_no=2, gold=0)]
        output = io.StringIO()
        self.assertEqual(replay((json.dumps(row) for row in rows), output), (3, 0))
        reports = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([report["revision"] for report in reports], [1, 1, 2])
        self.assertEqual(reports[1]["status"], "cached")
        self.assertNotIn("selected", reports[1])
        self.assertTrue(reports[0]["selected"])
        self.assertEqual(reports[2]["actualNetGoldDelta"], -75)

    def test_default_report_omits_task_contents_answers_and_error_descriptions(self):
        app = AgentApplication()
        raw = packet([unit(10011, "pioneer")], phaseTask="PRIVATE_TASK_CONTENT")
        token = json.loads(app.handle_turn(raw)["prompt"])["requestId"]
        raw.update(roundNo=2, llmResp=json.dumps({"requestId": token, "kind": "answer", "answer": "PRIVATE_ANSWER"}),
                   errors=[{"errorCode": 2, "description": "PRIVATE_ERROR"}], lastCmdResult="PRIVATE_COMMAND_OUTPUT")
        with patch("subprocess.run", side_effect=AssertionError("no local execution")):
            app.handle_turn(raw)
        report = decision_report(app)
        encoded = json.dumps(report)
        self.assertNotIn("PRIVATE_", encoded)
        self.assertEqual(report["errorCodes"], [2])
        self.assertEqual(report["tools"]["phase"], "submitted")

    def test_raw_responses_require_explicit_opt_in_and_mismatch_is_reported(self):
        output = io.StringIO()
        raw = packet([unit(10011, "pioneer")], phaseTask="PRIVATE_TASK_CONTENT")
        self.assertEqual(replay([json.dumps({"request": raw, "expected_response": idle_response()})], output), (1, 1))
        self.assertNotIn("PRIVATE_TASK_CONTENT", output.getvalue())
        output = io.StringIO()
        replay([json.dumps(raw)], output, include_response=True)
        self.assertIn("PRIVATE_TASK_CONTENT", output.getvalue())

    def test_malformed_input_errors_do_not_echo_private_text(self):
        for text in ('{"PRIVATE_SECRET":', json.dumps({"roundNo": "PRIVATE_SECRET"})):
            with self.assertRaises(ValueError) as captured:
                replay([text], io.StringIO())
            self.assertNotIn("PRIVATE_SECRET", str(captured.exception))

    def test_cli_runs_from_unrelated_directory_and_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="coregeek replay ") as temporary:
            directory = Path(temporary)
            source, target = directory / "input.jsonl", directory / "report.jsonl"
            source.write_text(json.dumps(packet()) + "\n", encoding="utf-8")
            command = [sys.executable, "-B", str(ROOT / "scripts" / "replay.py"), str(source), "--output", str(target)]
            completed = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["status"], "new")
            before = target.read_bytes()
            self.assertEqual(subprocess.run(command, cwd=directory, capture_output=True, timeout=10).returncode, 2)
            self.assertEqual(target.read_bytes(), before)

    def test_destroyed_base_disappearing_does_not_create_a_new_session(self):
        app = AgentApplication()
        app.handle_turn(packet())
        session = app.memory.session
        raw = packet([unit()], round_no=2)
        app.handle_turn(raw)
        self.assertEqual(app.last_status, "new")
        self.assertEqual(app.memory.session, session)
        self.assertEqual(app.memory.retired_sessions, set())
        self.assertEqual(app.memory.revision, 2)

    def test_transport_probe_cannot_reset_live_game_or_poison_its_round_counter(self):
        app = AgentApplication()
        app.handle_turn(packet(round_no=10))
        decision = app.engine.last_decision
        app.handle_turn({"roundNo": 9999})
        self.assertEqual(app.last_status, "probe")
        self.assertEqual(app.memory.last.round_no, 10)
        self.assertIs(app.engine.last_decision, decision)
        app.handle_turn(packet(round_no=11))
        self.assertEqual(app.memory.revision, 2)

    def test_path_search_honors_shared_deadline(self):
        state = world([])
        self.assertIsNone(shortest_route(state, Pos(0, 0), frozenset({Pos(19, 19)}), frozenset(), deadline=time.monotonic() - 1))

    def test_failed_decision_is_not_reported_as_committed(self):
        class Broken(AgentApplication):
            def decide(self, observation):
                raise RuntimeError("private diagnostic")
        app = Broken()
        with self.assertRaises(RuntimeError):
            app.handle_turn(packet())
        self.assertEqual(decision_report(app)["status"], "error")
        self.assertEqual(app.memory.revision, 0)
