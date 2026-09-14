import time
import unittest

from tests.helpers import unit, world
from agent.actions import Action, ActionCompiler
from agent.planning import Candidate, PlanArbiter, Value, combat_utility
from agent.world import Pos, RuleProfile


class PlanningTests(unittest.TestCase):
    def choose(self, state, proposals, **settings):
        return PlanArbiter(ActionCompiler(RuleProfile()), **settings).choose(
            state, tuple(proposals), previous={}, deadline=time.monotonic() + 1)

    def test_joint_budget_selects_one_build_with_25_gold(self):
        state = world([unit(10013, "station", (5, 6)), unit(), unit(10012, pos=(7, 6))], gold=25)
        candidates = [Candidate(str(index), "test", frozenset({actor}), (Action(actor, "build", (target,), "gatling"),),
                                Value(readiness=10), "build")
                      for index, (actor, target) in enumerate((("10010", Pos(4, 6)), ("10012", Pos(7, 5))))]
        self.assertEqual(len(self.choose(state, candidates).actions), 1)

    def test_future_reservations_also_use_shared_budget(self):
        state = world([unit(), unit(10012, pos=(10, 10))], gold=25)
        candidates = [Candidate(actor, "test", frozenset({actor}), (Action(actor, "move", (target,)),),
                                Value(readiness=10), "approach", reserved_gold=25, reserved_weapon_slots=1)
                      for actor, target in (("10010", Pos(3, 5)), ("10012", Pos(9, 10)))]
        self.assertEqual(len(self.choose(state, candidates).actions), 1)

    def test_approaches_cannot_claim_same_weapon(self):
        state = world([unit(), unit(10012, pos=(10, 10))])
        candidates = [Candidate(actor, "test", frozenset({actor}), (Action(actor, "move", (target,)),),
                                Value(readiness=10), "approach", resources=frozenset({"weapon:x"}))
                      for actor, target in (("10010", Pos(3, 5)), ("10012", Pos(9, 10)))]
        self.assertEqual(len(self.choose(state, candidates).actions), 1)

    def test_multi_actor_plan_and_wait_alternative(self):
        state = world([unit(), unit(10012, pos=(10, 10))])
        coalition = Candidate("coalition", "test", frozenset({"10010", "10012"}), (), Value(readiness=5), "hold")
        negative = Candidate("bad", "test", frozenset({"10010"}), (), Value(readiness=-1), "hold")
        self.assertEqual(self.choose(state, [coalition, negative]).candidates, (coalition,))
        self.assertEqual(self.choose(state, [negative]).candidates, ())

    def test_shared_damage_and_kill_reward_not_double_counted(self):
        state = world([], robots=[unit(30001, "smallRobot", (8, 8), health=15)])
        shots = tuple(Candidate(str(i), "test", frozenset({str(i)}), (), Value(), "fire", damage=(("30001", 10),)) for i in range(2))
        self.assertEqual(combat_utility(state, shots), 4.0)

    def test_goal_diversity_survives_candidate_pruning(self):
        state = world([unit(), unit(10012, pos=(10, 10))])
        shared = [Candidate("shared", "test", frozenset({actor}), (), Value(readiness=10), "hold", resources=frozenset({"same"}))
                  for actor in ("10010", "10012") for _ in range(5)]
        other = Candidate("other", "test", frozenset({"10012"}), (), Value(readiness=5), "hold", resources=frozenset({"other"}))
        self.assertEqual(self.choose(state, shared + [other], per_actor=2).utility, 15)

    def test_node_budget_returns_valid_fallback(self):
        plan = self.choose(world(), [], max_nodes=0)
        self.assertTrue(plan.exhausted)
        self.assertEqual(plan.actions, ())
