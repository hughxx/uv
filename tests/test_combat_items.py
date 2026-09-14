import time
import unittest

from tests.helpers import unit, world
from agent.actions import ActionCompiler
from agent.combat import AreaConsumables
from agent.planning import PlanArbiter
from agent.playbooks import Context, PlaybookLibrary
from agent.strategy import StrategyEngine
from agent.world import Pos, RuleProfile


class CombatItemTests(unittest.TestCase):
    def proposals(self, state):
        return AreaConsumables().propose(Context(state, RuleProfile(), time.monotonic() + 2))

    def test_empty_center_covers_cluster_and_no_local_distance_limit(self):
        state = world([unit(pos=(1, 1), backpack=["Bomb"])], round_no=71,
                      robots=[unit(30001, "largeRobot", (10, 10), health=500), unit(30002, "largeRobot", (12, 10), health=500)])
        decision = StrategyEngine(library=PlaybookLibrary((AreaConsumables(),))).propose(state)
        self.assertEqual(decision.plan.actions[0].name, "Bomb")
        center = decision.plan.actions[0].targets[0]
        self.assertEqual(center.x, 11)
        self.assertNotIn(center, {robot.pos for robot in state.robots})
        self.assertEqual(set(dict(decision.plan.candidates[0].damage)), {"30001", "30002"})
        ActionCompiler(RuleProfile()).validate(state, decision.plan.actions)

    def test_two_bombs_do_not_double_count_dead_robots_or_kill_rewards(self):
        state = world([unit(backpack=["Bomb"]), unit(10012, pos=(8, 5), backpack=["Bomb"])], round_no=71,
                      robots=[unit(30001, "middleRobot", (10, 10), health=60), unit(30002, "middleRobot", (11, 10), health=60)])
        plan = PlanArbiter(ActionCompiler(RuleProfile())).choose(state, self.proposals(state), previous={}, deadline=time.monotonic() + 2)
        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(plan.utility, 8)

    def test_not_used_on_single_cheap_robot_or_enemy_wave_alone(self):
        for target_team in ("challenger", "defender"):
            state = world([unit(backpack=["Bomb"])], round_no=71,
                          robots=[unit(30001, "smallRobot", (12, 12), health=40, targetTeam=target_team)])
            decision = StrategyEngine(library=PlaybookLibrary((AreaConsumables(),))).propose(state)
            self.assertEqual(decision.plan.actions, ())

    def test_stun_exclusive_claim_prevents_duplicate_control(self):
        state = world([unit(backpack=["DizzyWeapon"]), unit(10012, pos=(8, 5), backpack=["DizzyWeapon"]),
                       unit(10013, "station", (5, 6), health=1500)], round_no=71,
                      robots=[unit(30001, "bossRobot", (5, 10), health=800), unit(30002, "bossRobot", (6, 10), health=800)])
        decision = StrategyEngine(library=PlaybookLibrary((AreaConsumables(),))).propose(state)
        self.assertEqual(len(decision.plan.actions), 1)
        self.assertEqual(decision.plan.actions[0].name, "DizzyWeapon")

    def test_already_stunned_robots_do_not_trigger_another_stun(self):
        state = world([unit(backpack=["DizzyWeapon"]), unit(10013, "station", (5, 6))], round_no=71,
                      robots=[unit(30001, "bossRobot", (5, 10), health=800, abnormalState="dizzy")])
        self.assertEqual(self.proposals(state), ())

    def test_effect_on_enemy_wave_is_an_explicit_cost(self):
        state = world([unit(backpack=["Bomb"])], round_no=71,
                      robots=[unit(30001, "largeRobot", (10, 10), health=500),
                              unit(30002, "largeRobot", (11, 10), health=500, targetTeam="defender")])
        covering_both = [candidate for candidate in self.proposals(state) if candidate.actions[0].targets[0].distance(Pos(11, 10)) <= 1]
        self.assertTrue(covering_both)
        self.assertTrue(all(candidate.value.readiness == -40 for candidate in covering_both))
