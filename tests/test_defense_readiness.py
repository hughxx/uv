import copy
import unittest

from tests.helpers import packet, unit
from agent.application import AgentApplication
from agent.playbooks import OperateDefense, PlaybookLibrary
from agent.strategy import StrategyEngine


class DefenseReadinessTests(unittest.TestCase):
    def application(self):
        return AgentApplication(StrategyEngine(library=PlaybookLibrary((OperateDefense(),))))

    def test_positioned_operator_holds_for_ten_empty_night_rounds(self):
        app = self.application()
        roles = [unit(pos=(4, 5)), unit(10030, "railgun", (3, 6)), unit(10020, "gatling", (6, 6))]
        for round_no in range(71, 81):
            response = app.handle_turn(packet(copy.deepcopy(roles), round_no=round_no))
            self.assertEqual(response["roleCommandMap"], {}, f"unnecessary move in round {round_no}")
            self.assertEqual(app.engine.last_decision.plan.candidates[0].stage, "hold-weapon")

    def test_hold_transitions_to_fire_on_first_observed_target(self):
        app = self.application()
        roles = [unit(pos=(4, 5)), unit(10030, "railgun", (3, 6)), unit(10020, "gatling", (6, 6))]
        app.handle_turn(packet(roles, round_no=71))
        response = app.handle_turn(packet(roles, round_no=72, robots=[unit(30001, "largeRobot", (3, 10), health=500)]))
        self.assertEqual(response["roleCommandMap"]["10030"]["action"], "attack")
        self.assertEqual(app.engine.last_decision.plan.candidates[0].stage, "fire")

    def test_three_operators_can_fill_three_distinct_posts(self):
        app = self.application()
        raw = packet([unit(pos=(2, 5)), unit(10012, pos=(8, 5)), unit(10011, "pioneer", (12, 5)),
                      unit(10030, "railgun", (3, 6)), unit(10020, "gatling", (7, 6)), unit(10031, "railgun", (11, 6))], round_no=71)
        self.assertEqual(app.handle_turn(raw)["roleCommandMap"], {})
        selected = app.engine.last_decision.plan.candidates
        self.assertEqual(len(selected), 3)
        self.assertEqual(len({item.key for item in selected}), 3)
        raw = copy.deepcopy(raw)
        raw.update(roundNo=72, robot={"roles": [unit(30001, "largeRobot", (7, 8), health=500)]})
        response = app.handle_turn(raw)
        self.assertEqual(len(response["roleCommandMap"]), 3)
        self.assertTrue(all(action["action"] == "attack" for action in response["roleCommandMap"].values()))

    def test_one_operator_cannot_claim_three_posts_or_three_shots(self):
        app = self.application()
        roles = [unit(pos=(4, 5)), unit(10030, "railgun", (3, 6)), unit(10020, "gatling", (5, 6)),
                 unit(10031, "railgun", (4, 6))]
        app.handle_turn(packet(roles, round_no=71))
        self.assertEqual(len(app.engine.last_decision.plan.candidates), 1)
        response = app.handle_turn(packet(roles, round_no=72, robots=[unit(30001, "largeRobot", (4, 8), health=500)]))
        self.assertEqual(len(response["roleCommandMap"]), 1)

    def test_cooldown_and_no_valid_target_group_still_offer_hold(self):
        for weapon in (unit(10040, "rocket", (3, 6), cooldown=2), unit(10020, "gatling", (3, 6), level=3)):
            with self.subTest(kind=weapon["roleType"]):
                app = self.application()
                response = app.handle_turn(packet([unit(pos=(4, 5)), weapon, unit(10030, "railgun", (7, 6))], round_no=71))
                self.assertEqual(response["roleCommandMap"], {})
                self.assertEqual(app.engine.last_decision.plan.candidates[0].stage, "hold-weapon")

    def test_distant_idle_pioneer_can_approach_but_active_task_cannot_be_abandoned(self):
        roles = [unit(10011, "pioneer", (2, 5)), unit(10030, "railgun", (8, 6))]
        app = self.application()
        self.assertEqual(app.handle_turn(packet(roles, round_no=71))["roleCommandMap"]["10011"]["action"], "move")
        active = self.application().handle_turn(packet(roles, round_no=71, phaseTask="Private task"))
        self.assertEqual(active["roleCommandMap"], {})
        self.assertTrue(active["prompt"])


if __name__ == "__main__":
    unittest.main()
