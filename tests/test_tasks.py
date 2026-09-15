import json
import os
import subprocess
import unittest
from unittest.mock import patch

from tests.helpers import packet, unit, world
from tests.test_package import find_bash
from agent.application import AgentApplication
from agent.strategy import StrategyEngine
from agent.tasks import CommandAttempt, TaskState, parse_reply, prepare_task, record_command_result, sandbox_command, task_diagnostics


def active(round_no=1, **changes):
    return packet([unit(10011, "pioneer", (4, 5), health=200, backPackCapability=40)],
                  round_no=round_no, phaseTask="Read the sandbox value and return it.", **changes)


class TaskTests(unittest.TestCase):
    def test_command_error_hints_are_fixed_labels_not_exception_messages(self):
        state = TaskState("PRIVATE_TASK", 1, marker="PRIVATE_MARKER", attempts=(CommandAttempt("PRIVATE_HASH", 21),))
        raw = "[exitCode:1]\nPRIVATE_MARKER\nTraceback (most recent call last):\n  File /PRIVATE_PATH, line 1\nFileNotFoundError: PRIVATE_FILENAME\nPrivateCustomError: PRIVATE_SECRET"
        metadata = task_diagnostics(record_command_result(state, raw))
        self.assertEqual(metadata["result"]["errorHints"], ("FileNotFoundError",))
        self.assertTrue(metadata["result"]["markerSeen"])
        self.assertNotIn("PRIVATE", json.dumps(metadata))
        self.assertNotIn("PrivateCustomError", json.dumps(metadata))
        absent = task_diagnostics(record_command_result(state, "[exitCode:1]\nno classification available"))
        self.assertEqual(absent["result"]["errorHints"], ())
        self.assertFalse(absent["result"]["markerSeen"])

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

    def execute_cycle(self, app, response, round_no, command, output):
        token = json.loads(response["prompt"])["requestId"]
        requested = app.handle_turn(active(round_no, llmResp=json.dumps({"requestId": token, "kind": "execute", "command": command})))
        self.assertTrue(requested["executeCmd"])
        result = "[exitCode:0]\n" + app.engine.task_state.marker + "\n" + output
        return app.handle_turn(active(round_no + 1, lastCmdResult=result))

    def test_repeated_command_with_unchanged_result_is_blocked_but_new_command_is_allowed(self):
        app = AgentApplication()
        response = app.handle_turn(active())
        for round_no in (2, 4):
            response = self.execute_cycle(app, response, round_no, "echo PRIVATE_VALUE", "PRIVATE_VALUE")
        token = json.loads(response["prompt"])["requestId"]
        response = app.handle_turn(active(6, llmResp=json.dumps({"requestId": token, "kind": "execute", "command": "echo PRIVATE_VALUE"})))
        self.assertFalse(response["executeCmd"])
        self.assertTrue(response["prompt"])
        diagnostic = task_diagnostics(app.engine.task_state)
        self.assertEqual(diagnostic["lastEvent"], "repeated-command-blocked")
        self.assertEqual((diagnostic["commandRequests"], diagnostic["commandId"], diagnostic["sameCommandExecutions"]), (2, 1, 2))
        self.assertTrue(diagnostic["repeatedWithoutProgress"])
        self.assertNotIn("PRIVATE_VALUE", json.dumps(diagnostic))
        response = self.execute_cycle(app, response, 7, "echo changed", "changed")
        self.assertEqual(task_diagnostics(app.engine.task_state)["commandId"], 2)

    def test_same_command_with_changing_output_is_not_blocked(self):
        app = AgentApplication()
        response = app.handle_turn(active())
        for round_no, output in ((2, "value1"), (4, "value2"), (6, "value3")):
            response = self.execute_cycle(app, response, round_no, "inspect-progress", output)
        self.assertFalse(task_diagnostics(app.engine.task_state)["repeatedWithoutProgress"])
        self.assertEqual(len(app.engine.task_state.attempts), 3)

    def test_same_length_different_commands_are_not_treated_as_repeats(self):
        app = AgentApplication()
        response = app.handle_turn(active())
        for round_no, command in ((2, "echo a"), (4, "echo b"), (6, "echo c")):
            response = self.execute_cycle(app, response, round_no, command, "same")
        self.assertEqual(task_diagnostics(app.engine.task_state)["commandId"], 3)

    def test_budget_reserves_final_llm_request_and_still_accepts_its_answer(self):
        app = AgentApplication(StrategyEngine(task_max_requests=4))
        response = self.execute_cycle(app, app.handle_turn(active()), 2, "echo 42", "42")
        self.assertEqual(app.engine.task_state.step, 3)
        token = json.loads(response["prompt"])["requestId"]
        response = app.handle_turn(active(4, llmResp=json.dumps({"requestId": token, "kind": "execute", "command": "echo more"})))
        self.assertFalse(response["executeCmd"])
        self.assertTrue(response["prompt"])
        self.assertEqual(app.engine.task_state.last_event, "answer-budget-reserved")
        prompt = json.loads(response["prompt"])
        self.assertFalse(prompt["commandAndFollowupAffordable"])
        self.assertEqual(app.engine.task_state.step, 4)
        response = app.handle_turn(active(5, llmResp=json.dumps({"requestId": prompt["requestId"], "kind": "answer", "answer": "42"})))
        self.assertEqual(response["roleCommandMap"]["10011"]["taskAnswer"], "42")
        self.assertEqual(app.engine.task_state.step, 4)
        self.assertEqual(task_diagnostics(app.engine.task_state)["llmRequests"], 3)

    def test_result_metadata_redacts_payload_and_distinguishes_failures(self):
        state = TaskState("PRIVATE_TASK", 1, marker="private_marker", attempts=(CommandAttempt("private_hash", 8),))
        for result, status, code, truncated in (("[exitCode:0]\nprivate_marker\nPRIVATE_OUTPUT", "exit", 0, False),
                                                ("[exitCode:1]\nprivate_marker\nPRIVATE_ERROR\n[TRUNCATED]", "exit", 1, True),
                                                ("[TIMEOUT]\nPRIVATE_PARTIAL", "timeout", None, False),
                                                ("[JUDGER_ERROR]\nPRIVATE_DETAIL", "judger-error", None, False)):
            with self.subTest(status=status, code=code):
                updated = record_command_result(state, result)
                metadata = task_diagnostics(updated)
                self.assertEqual(metadata["result"]["status"], status)
                self.assertEqual(metadata["result"]["exitCode"], code)
                self.assertEqual(metadata["result"]["truncated"], truncated)
                self.assertNotIn("PRIVATE_", json.dumps(metadata))
                self.assertNotIn("private_", json.dumps(metadata))

    def test_task_text_changes_and_reply_mismatch_are_visible(self):
        app = AgentApplication()
        app.handle_turn(active())
        app.handle_turn(active(2, llmResp='{"requestId":"wrong","kind":"answer","answer":"private"}'))
        self.assertEqual(task_diagnostics(app.engine.task_state)["replyStatus"], "request-id-mismatch")
        raw = active(3)
        raw["phaseTask"] = "Different private task"
        app.handle_turn(raw)
        diagnostic = task_diagnostics(app.engine.task_state)
        self.assertEqual((diagnostic["started"], diagnostic["startReason"], diagnostic["requestsUsed"]), (3, "task-text-changed", 1))
