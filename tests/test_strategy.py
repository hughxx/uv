import copy
import time
import unittest

from tests.helpers import packet, unit, world
from agent.actions import ActionCompiler
from agent.application import AgentApplication
from agent.playbooks import BuildDefense, CashInventory, Context, OperateDefense, PlaybookLibrary
from agent.protocol import Observation
from agent.strategy import StrategyEngine
from agent.world import Pos, RuleProfile, World


class StrategyTests(unittest.TestCase):
    def test_builds_are_budgeted_and_completion_is_observed(self):
        app = AgentApplication(StrategyEngine(library=PlaybookLibrary((BuildDefense(),))))
        raw = packet(gold=25)
        response = app.handle_turn(raw)
        action = response["roleCommandMap"]["10010"]
        self.assertEqual(action["action"], "build")
        updated = copy.deepcopy(raw)
        updated["roundNo"] = 2
        updated["teamOur"]["goldNum"] = 0
        pos = action["targetPos"][0]
        updated["teamOur"]["roles"].append(unit(10030, action["name"], (pos["x"], pos["y"])))
        app.handle_turn(updated)
        self.assertIn("objective-observed", [transition.kind for transition in app.engine.last_decision.transitions])
        self.assertEqual(app.engine.last_decision.observed_gold_delta, -25)

    def test_cash_playbook_approaches_sells_and_confirms_actual_result(self):
        app = AgentApplication(StrategyEngine(library=PlaybookLibrary((CashInventory(),))))
        raw = packet([unit(pos=(2, 5), backpack=["copper"] * 2)], gold=0,
                     zones=[{"neutralType": "vendor", "pos": {"x": 6, "y": 5}}])
        started = None
        for _ in range(8):
            response = app.handle_turn(raw)
            run = app.engine.runs[0]
            started = run.started if started is None else started
            self.assertEqual(run.started, started)
            command = response["roleCommandMap"]["10010"]
            updated = copy.deepcopy(raw)
            updated["roundNo"] += 1
            if command["action"] == "sell":
                self.assertEqual(command["num"], 2)
                updated["teamOur"]["roles"][0]["backpack"] = []
                updated["teamOur"]["goldNum"] = 10
                app.handle_turn(updated)
                self.assertEqual(app.engine.runs, ())
                self.assertEqual(app.engine.last_decision.observed_gold_delta, 10)
                self.assertEqual(app.engine.last_decision.transitions[0].kind, "objective-observed")
                break
            updated["teamOur"]["roles"][0]["pos"] = command["targetPos"][0]
            raw = updated
        else:
            self.fail("cash playbook did not reach a sale")

    def test_new_threat_replaces_cash_plan_and_hands_over_to_defense(self):
        app = AgentApplication(StrategyEngine(library=PlaybookLibrary((CashInventory(), OperateDefense()))))
        roles = [unit(backpack=["copper"] * 20), unit(10013, "station", (5, 6)), unit(10030, "railgun", (4, 6))]
        raw = packet(roles, round_no=71, robots=[unit(30001, "smallRobot", (4, 13), health=40)],
                     zones=[{"neutralType": "vendor", "pos": {"x": 12, "y": 10}}])
        response = app.handle_turn(raw)
        self.assertEqual(response["roleCommandMap"]["10010"]["action"], "move")
        updated = copy.deepcopy(raw)
        updated["roundNo"] = 72
        updated["lastRoundRoleActionResults"] = {"10010": False}
        updated["robot"]["roles"][0]["pos"] = {"x": 4, "y": 12}
        response = app.handle_turn(updated)
        self.assertEqual(response["roleCommandMap"]["10030"]["action"], "attack")
        self.assertTrue(any(transition.key.startswith("cash:") and transition.kind == "replaced"
                            for transition in app.engine.last_decision.transitions))
        self.assertEqual(updated["teamOur"]["roles"][0]["backpack"], ["copper"] * 20)

    def test_two_positioned_operators_fire_instead_of_id_zip_movement(self):
        app = AgentApplication(StrategyEngine(library=PlaybookLibrary((OperateDefense(),))))
        roles = [unit(pos=(8, 6)), unit(10012, pos=(2, 6)), unit(10020, "railgun", (3, 6)), unit(10030, "railgun", (7, 6))]
        response = app.handle_turn(packet(roles, round_no=71, robots=[unit(30001, "largeRobot", (5, 6), health=500)]))
        self.assertEqual(set(response["roleCommandMap"]), {"10020", "10030"})
        self.assertEqual(response["roleCommandMap"]["10020"]["controllerId"], "10012")
        self.assertEqual(response["roleCommandMap"]["10030"]["controllerId"], "10010")

    def test_new_definition_requires_only_registration_and_failure_is_local(self):
        class Failing:
            id = "broken"
            def propose(self, context):
                raise RuntimeError("private text")

        library = PlaybookLibrary((Failing(), BuildDefense()))
        app = AgentApplication(StrategyEngine(library=library))
        self.assertTrue(app.handle_turn(packet())["roleCommandMap"])
        self.assertEqual(app.engine.last_decision.diagnostics, ("error:broken:RuntimeError",))
        with self.assertRaises(ValueError):
            PlaybookLibrary((BuildDefense(), BuildDefense()))

    def test_zero_origin_is_inferred_only_from_an_actual_zero_observation(self):
        app = AgentApplication()
        self.assertTrue(app.handle_turn(packet(round_no=0))["roleCommandMap"])
        self.assertEqual(app.engine.rules.round_origin, 0)

    def test_time_alone_can_expire_a_build_window(self):
        app = AgentApplication(StrategyEngine(library=PlaybookLibrary((BuildDefense(),))))
        app.handle_turn(packet(round_no=70))
        self.assertTrue(app.engine.runs)
        response = app.handle_turn(packet(round_no=71))
        self.assertEqual(response["roleCommandMap"], {})
        self.assertEqual(app.engine.last_decision.transitions[0].kind, "premise-invalidated")
