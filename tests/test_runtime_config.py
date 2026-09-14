import contextlib
import io
import unittest
from unittest.mock import patch

from tests.helpers import packet
from CoreGeek.main3 import main, parse_args


class RuntimeConfigTests(unittest.TestCase):
    def test_default_platform_port_only_launch_keeps_baseline(self):
        args = parse_args(["9000"])
        self.assertEqual((args.port, args.decision_budget_ms, args.task_max_requests), (9000, 2500, 12))
        self.assertFalse(args.disable_inferred_building or args.no_role_death_guard)

    def test_flags_reach_engine_without_changing_protocol(self):
        with patch("agent.server.serve") as serve:
            main(["9000", "--round-origin", "0", "--disable-inferred-building", "--no-role-death-guard",
                  "--decision-budget-ms", "1000", "--task-max-requests", "20", "--tool-wait-rounds", "2"])
        port, app = serve.call_args.args
        self.assertEqual(port, 9000)
        self.assertEqual(app.engine.rules.round_origin, 0)
        self.assertFalse(app.engine.rules.inferred_build_rings or app.engine.rules.guard_projected_role_deaths)
        self.assertEqual((app.engine.budget_seconds, app.engine.task_max_requests, app.engine.tool_wait_rounds), (1, 20, 2))
        self.assertEqual(set(app.handle_turn(packet())), {"roleCommandMap", "prompt", "executeCmd"})

    def test_invalid_budget_and_quota_fail_before_server_starts(self):
        for option, value in (("--decision-budget-ms", "0"), ("--decision-budget-ms", "3501"),
                              ("--task-max-requests", "0"), ("--tool-wait-rounds", "-1"),
                              ("--round-origin", "2")):
            with self.subTest(option=option, value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                parse_args(["9000", option, value])
            self.assertNotEqual(error.exception.code, 0)
