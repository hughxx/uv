import copy
import unittest

from tests.helpers import packet, unit, world
from agent.actions import Action
from agent.application import AgentApplication
from agent.feedback import FailedAttempt, reconcile_failures
from agent.planning import Candidate, Plan, Value
from agent.playbooks import PlaybookLibrary
from agent.strategy import StrategyEngine
from agent.world import Pos


class CollectOnly:
    id = "test-collect"
    def propose(self, context):
        return (Candidate("mine", self.id, frozenset({"10010"}), (Action("10010", "collect", (Pos(3, 5),)),),
                          Value(gold=1), "collect"),)


class FeedbackTests(unittest.TestCase):
    def test_repeated_failures_back_off_then_retry_without_inventing_resource_changes(self):
        app = AgentApplication(StrategyEngine(library=PlaybookLibrary((CollectOnly(),))))
        raw = packet([unit()], zones=[{"neutralType": "copper", "pos": {"x": 3, "y": 5}}])
        self.assertTrue(app.handle_turn(copy.deepcopy(raw))["roleCommandMap"])
        raw.update(roundNo=2, lastRoundRoleActionResults={"10010": False})
        self.assertTrue(app.handle_turn(copy.deepcopy(raw))["roleCommandMap"])
        raw["roundNo"] = 3
        self.assertEqual(app.handle_turn(copy.deepcopy(raw))["roleCommandMap"], {})
        self.assertEqual(app.engine.failures[0].streak, 2)
        self.assertIn("execution-cooldown:1", app.engine.last_decision.diagnostics)
        app.handle_turn(copy.deepcopy(raw))
        self.assertEqual(app.engine.failures[0].streak, 2)
        raw["roundNo"] = 6
        self.assertTrue(app.handle_turn(copy.deepcopy(raw))["roleCommandMap"])
        self.assertEqual(raw["teamOur"]["roles"][0]["backpack"], [])

    def test_map_change_releases_old_failure_evidence(self):
        old = world([unit()])
        new = world([unit()], round_no=2, zones=[{"neutralType": "stone", "pos": {"x": 1, "y": 1}}])
        failure = FailedAttempt("10010", "move", (Pos(3, 5),), "", 2, 1, 4)
        self.assertEqual(reconcile_failures(new, old, Plan(), (failure,)), ())

    def test_missing_or_skipped_result_does_not_increment_failure(self):
        actor_action = Action("10010", "move", (Pos(3, 5),))
        plan = Plan((Candidate("move", "test", frozenset({"10010"}), (actor_action,), Value(readiness=1), "move"),))
        old = world([unit()])
        for next_world in (world([unit()], round_no=2), world([unit()], round_no=3, lastRoundRoleActionResults={"10010": False})):
            self.assertEqual(reconcile_failures(next_world, old, plan, ()), ())

    def test_success_clears_failure_and_late_baseless_side_is_rejected(self):
        actor_action = Action("10010", "move", (Pos(3, 5),))
        plan = Plan((Candidate("move", "test", frozenset({"10010"}), (actor_action,), Value(readiness=1), "move"),))
        failure = FailedAttempt("10010", "move", actor_action.targets, "", 1, 1, 1)
        self.assertEqual(reconcile_failures(world([unit()], round_no=2, lastRoundRoleActionResults={"10010": True}),
                                            world([unit()]), plan, (failure,)), ())
        app = AgentApplication()
        app.handle_turn(packet())
        new_side = packet()
        new_side["teamOur"]["type"] = "defender"
        app.handle_turn(new_side)
        app.handle_turn(packet([unit()], round_no=2))
        self.assertEqual(app.last_status, "stale_session")
        self.assertEqual(app.memory.last.raw["teamOur"]["type"], "defender")
