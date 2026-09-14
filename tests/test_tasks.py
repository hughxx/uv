import json
import os
import subprocess
import unittest
from unittest.mock import patch

from tests.helpers import packet, unit, world
from tests.test_package import find_bash
from agent.application import AgentApplication
from agent.tasks import parse_reply, prepare_task, sandbox_command


def active(round_no=1, **changes):
    return packet([unit(10011, "pioneer", (4, 5), health=200, backPackCapability=40)],
                  round_no=round_no, phaseTask="Read the sandbox value and return it.", **changes)


class TaskTests(unittest.TestCase):
    def test_full_mock_tool_cycle_and_request_deduplication(self):
        app = AgentApplication()
        raw = active()
        response = app.handle_turn(raw)
        prompt = json.loads(response["prompt"])
        self.assertEqual(app.engine.task_state.phase, "waiting-llm")
        self.assertEqual(app.handle_turn(raw), response)
        self.assertEqual(app.engine.task_state.step, 1)

        raw = active(2, llmResp=json.dumps({"requestId": prompt["requestId"], "kind": "execute", "command": "echo value=42"}))
        with patch("subprocess.run", side_effect=AssertionError("agent must not execute task commands locally")):
            response = app.handle_turn(raw)
        self.assertTrue(response["executeCmd"])
        self.assertFalse(response["prompt"])
        marker = app.engine.task_state.marker
        self.assertEqual(app.engine.task_state.phase, "waiting-command")

        raw = active(3, lastCmdResult="[exitCode:0]\n" + marker + "\nvalue=42")
        response = app.handle_turn(raw)
        prompt = json.loads(response["prompt"])
        self.assertIn("value=42", prompt["history"][-1])

        raw = active(4, llmResp=json.dumps({"requestId": prompt["requestId"], "kind": "answer", "answer": "42"}))
        response = app.handle_turn(raw)
        self.assertEqual(response["roleCommandMap"]["10011"], {"action": "submitAnswer", "taskAnswer": "42"})
        self.assertFalse(response["prompt"] or response["executeCmd"])
        self.assertEqual(app.engine.task_state.phase, "submitted")

        ended = active(5)
        ended["phaseTask"] = ""
        app.handle_turn(ended)
        self.assertIsNone(app.engine.task_state)

    def test_wrong_request_id_and_uncorrelated_command_result_are_ignored(self):
        app = AgentApplication()
        app.handle_turn(active())
        raw = active(2, llmResp=json.dumps({"requestId": "stale", "kind": "answer", "answer": "wrong"}))
        response = app.handle_turn(raw)
        self.assertEqual(response["roleCommandMap"], {})
        self.assertFalse(response["prompt"])
        self.assertEqual(app.engine.task_state.phase, "waiting-llm")
        response = app.handle_turn(active(4))
        self.assertTrue(response["prompt"])

    def test_partial_or_failed_answer_is_not_treated_as_task_completion(self):
        app = AgentApplication()
        token = json.loads(app.handle_turn(active())["prompt"])["requestId"]
        app.handle_turn(active(2, llmResp=json.dumps({"requestId": token, "kind": "answer", "answer": "partial"})))
        response = app.handle_turn(active(3, errors=[{"errorCode": 2, "description": "incomplete"}]))
        self.assertTrue(response["prompt"])
        self.assertIn("incomplete", json.loads(response["prompt"])["history"][-1])
        self.assertEqual(app.engine.task_state.phase, "waiting-llm")

    def test_request_budget_and_death_stop_tools(self):
        state_world = world([unit(10011, "pioneer")], phaseTask="test")
        work = prepare_task(state_world, None, max_requests=0)
        self.assertEqual(work.state.phase, "budget-exhausted")
        self.assertFalse(work.prompt or work.execute_cmd)
        dead = world([unit(10011, "pioneer", health=0)], phaseTask="test")
        self.assertIsNone(prepare_task(dead, work.state).state)

    def test_fenced_json_and_nonmatching_tokens(self):
        self.assertEqual(parse_reply('```json\n{"requestId":"x","kind":"answer","answer":"ok"}\n```', "x")["answer"], "ok")
        self.assertIsNone(parse_reply('{"requestId":"other"}', "x"))
        self.assertIsNone(parse_reply("not-json", "x"))

    def test_actual_sandbox_wrapper_syntax_with_benign_command(self):
        bash = find_bash()
        if bash is None:
            self.skipTest("Bash unavailable")
        result = subprocess.run([bash, "-c", sandbox_command("echo proof", "test_marker")],
                                capture_output=True, text=True, timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("test_marker", result.stdout)
        self.assertIn("proof", result.stdout)

    def test_available_task_is_accepted_without_extra_target_fields(self):
        raw = packet([unit(10011, "pioneer", (4, 5))],
                     zones=[{"neutralType": "challengerTaskPoint1", "pos": {"x": 5, "y": 5}}])
        raw["teamOur"]["playerTasks"] = [{"taskPosition": {"x": 5, "y": 5}, "isValid": True,
                                          "coldDownRounds": 0, "scoreReward": 50, "goldReward": 30}]
        response = AgentApplication().handle_turn(raw)
        self.assertEqual(response["roleCommandMap"]["10011"], {"action": "acceptTask"})
        self.assertFalse(response["prompt"] or response["executeCmd"])
