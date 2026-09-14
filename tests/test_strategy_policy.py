import time
import unittest

from tests.helpers import unit, world
from agent.actions import Action, ActionCompiler, InvalidAction
from agent.planning import Candidate, PlanArbiter, Value
from agent.playbooks import CashInventory, OperateDefense, PlaybookLibrary
from agent.strategy import StrategyEngine
from agent.strategy_policy import DefensivePostPolicy
from agent.world import Pos, RuleProfile


class StrategyPolicyTests(unittest.TestCase):
    def state(self, *, nearby=True):
        return world([unit(pos=(3, 5)), unit(10012, pos=(2, 5)), unit(10030, "railgun", (4, 6)),
                      unit(10013, "station", (5, 6), health=1500)], round_no=71,
                     robots=[unit(30001, "largeRobot", (4, 10 if nearby else 19), health=500)])

    def test_departure_is_restricted_only_under_nearby_pressure(self):
        action = Action("10010", "move", (Pos(2, 4),))
        for nearby, expected in ((True, 1), (False, 0)):
            policy = DefensivePostPolicy(self.state(nearby=nearby), RuleProfile())
            self.assertEqual(policy.lost_posts((action,)), expected)

    def test_joint_replacement_allows_economic_departure(self):
        state = self.state()
        departure = Candidate("cash-route", "economy", frozenset({"10010"}), (Action("10010", "move", (Pos(2, 4),)),),
                              Value(gold=1000), "approach")
        replacement = Candidate("take-over", "defense", frozenset({"10012"}), (Action("10012", "move", (Pos(3, 5),)),),
                                Value(readiness=1), "approach")
        policy = DefensivePostPolicy(state, RuleProfile())
        self.assertEqual(policy.lost_posts(departure.actions), 1)
        self.assertEqual(policy.lost_posts(departure.actions + replacement.actions), 0)
        arbiter = PlanArbiter(ActionCompiler(RuleProfile()))
        self.assertEqual(arbiter.choose(state, (departure,), previous={}, deadline=time.monotonic() + 1).actions, ())
        plan = arbiter.choose(state, (departure, replacement), previous={}, deadline=time.monotonic() + 1)
        self.assertEqual(len(plan.actions), 2)
        self.assertEqual(plan.threatened_posts_lost, 0)

    def test_one_operator_does_not_staff_multiple_weapons_at_once(self):
        state = world([unit(pos=(3, 5)), unit(10030, "railgun", (4, 6)), unit(10031, "gatling", (4, 5))])
        self.assertEqual(DefensivePostPolicy(state, RuleProfile()).staffed(()), 1)

    def test_policy_can_be_disabled_and_does_not_claim_current_fire(self):
        state = self.state()
        policy = DefensivePostPolicy(state, RuleProfile(preserve_threatened_posts=False))
        self.assertEqual(policy.lost_posts((Action("10010", "move", (Pos(2, 4),)),)), 0)
        active = DefensivePostPolicy(state, RuleProfile())
        self.assertEqual(active.staffed((Action("10010", "use", name="Medicine"),)), 1)
        # Staffing only describes the next observation's positioning. The
        # planner still needs a separate valid attack to claim actual damage.

    def test_real_playbooks_can_handover_a_single_cell_post_without_deadlock(self):
        roles = [unit(pos=(3, 5), backpack=["copper"] * 20), unit(10012, pos=(2, 5)),
                 unit(10030, "railgun", (4, 6)), unit(10013, "station", (5, 6), health=1500)]
        roles += [unit(10040 + index, "wall", position) for index, position in enumerate(((3, 6), (3, 7), (4, 5), (4, 7), (5, 7)))]
        state = world(roles, round_no=71, robots=[unit(30001, "largeRobot", (4, 10), health=500)],
                      zones=[{"neutralType": "vendor", "pos": {"x": 1, "y": 2}}])
        decision = StrategyEngine(library=PlaybookLibrary((CashInventory(), OperateDefense()))).propose(state)
        self.assertEqual(len(decision.plan.actions), 2)
        moves = {action.actor_id: action.targets[0] for action in decision.plan.actions}
        self.assertEqual(moves["10012"], Pos(3, 5))
        self.assertNotEqual(moves["10010"], Pos(2, 5))
        self.assertEqual(decision.plan.threatened_posts_lost, 0)
        ActionCompiler(RuleProfile()).validate(state, decision.plan.actions)
        with self.assertRaises(InvalidAction):
            ActionCompiler(RuleProfile(joint_follow_moves=False)).validate(state, decision.plan.actions)
