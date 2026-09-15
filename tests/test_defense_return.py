"""Synthetic late-return regressions, not a replay of the hidden full map."""

import copy
import time
import unittest
from unittest.mock import patch

from tests.helpers import unit, world
from agent.actions import Action, ActionCompiler
from agent.planning import Candidate, PlanArbiter, Value
from agent.playbooks import CashInventory, OperateDefense, PlaybookLibrary
from agent.strategy import StrategyEngine
from agent.protocol import Observation
from agent.strategy_policy import DefenseReturnPolicy, DefensivePostPolicy, ReturnCommitment
from agent.world import Pos, RuleProfile, World


class DefenseReturnTests(unittest.TestCase):
    def state(self, round_no=68, **kwargs):
        return world([unit(pos=(10, 6), backpack=["copper"] * 3),
                      unit(10012, pos=(7, 19)), unit(10011, "pioneer", (14, 15)),
                      unit(10013, "station", (9, 22), health=1500),
                      unit(10030, "railgun", (8, 20)), unit(10031, "railgun", (9, 23)),
                      unit(10020, "gatling", (8, 23))], round_no=round_no,
                     mapInfo={"width": 41, "height": 32, "zones": [{"neutralType": "vendor", "pos": {"x": 14, "y": 10}}]},
                     **kwargs)

    def policy(self, state, **rules):
        return DefenseReturnPolicy(state, RuleProfile(**rules), deadline=time.monotonic() + 2)

    def test_cash_route_cannot_delay_a_late_second_worker(self):
        state = self.state(phaseTask="synthetic active task")
        library = PlaybookLibrary((CashInventory(), OperateDefense()))
        old = StrategyEngine(library=library, rules=RuleProfile(timely_defense_return=False)).propose(state)
        self.assertTrue(any(c.definition == "cash-inventory" and "10010" in c.actors for c in old.plan.candidates))
        new = StrategyEngine(library=library).propose(state)
        self.assertTrue(any(c.definition == "operate-defense" and "10010" in c.actors for c in new.plan.candidates))
        policy = self.policy(state)
        old_commitments = tuple(c for candidate in old.plan.candidates for c in candidate.return_commitments)
        self.assertLess(policy.cost(new.plan.actions), policy.cost(old.plan.actions, old_commitments))
        ActionCompiler(RuleProfile()).validate(state, new.plan.actions)

    def test_early_day_economy_has_no_return_debt(self):
        state = self.state(10)
        policy = self.policy(state)
        self.assertEqual(policy.cost(()), (0, 0))
        self.assertEqual(policy.cost((Action("10010", "move", (Pos(11, 7),)),)), (0, 0))

    def test_two_workers_return_before_night_across_replanning(self):
        raw = copy.deepcopy(self.state(45, phaseTask="synthetic active task").observation.raw)
        engine = StrategyEngine(library=PlaybookLibrary((CashInventory(), OperateDefense())))
        sold = False
        for round_no in range(45, 72):
            raw["roundNo"] = round_no
            state = World.from_observation(Observation.from_payload(raw))
            decision = engine.propose(state)
            engine.commit(decision)
            self.assertEqual(decision.plan.return_check.status, "ready")
            if round_no == 71:
                self.assertEqual(DefensivePostPolicy(state, RuleProfile()).staffed(()), 2)
            for action in decision.plan.actions:
                actor = next(r for r in raw["teamOur"]["roles"] if str(r["id"]) == action.actor_id)
                if action.kind == "move":
                    actor["pos"] = action.targets[0].payload()
                elif action.kind == "sell":
                    actor["backpack"] = []
                    sold = True
        self.assertTrue(sold, "A profitable early sale should still fit before return")

    def test_spare_operator_can_complete_a_sale_without_abandoning_the_only_post(self):
        state = world([unit(pos=(3, 5)), unit(10012, pos=(2, 5)), unit(10030, "railgun", (4, 6)),
                       unit(10013, "station", (5, 6))], round_no=71)
        policy = self.policy(state)
        long_sale = (ReturnCommitment("10012", Pos(15, 15), 20),)
        self.assertEqual(policy.cost((), long_sale), (0, 0))

    def test_unreachable_post_is_not_silently_counted_as_staffed(self):
        roles = [unit(pos=(2, 5)), unit(10030, "railgun", (6, 5)), unit(10013, "station", (9, 9))]
        roles += [unit(10040 + i, "wall", (p.x, p.y)) for i, p in enumerate(Pos(6, 5).neighbors())]
        self.assertEqual(self.policy(world(roles, round_no=71)).cost(()), (1, 0))

    def test_deadline_counts_this_turn_and_two_setup_turns(self):
        state = world([unit(pos=(2, 5)), unit(10030, "railgun", (6, 5)), unit(10013, "station", (8, 6))], round_no=66)
        policy = self.policy(state)
        self.assertEqual(policy.available_moves, 2)  # five daylight actions, minus current and two buffer turns
        self.assertEqual(policy.cost(()), (0, 1))
        self.assertEqual(policy.cost((Action("10010", "move", (Pos(3, 5),)),)), (0, 0))

    def test_matching_does_not_send_both_workers_to_one_weapon(self):
        state = world([unit(pos=(2, 5)), unit(10012, pos=(3, 5)), unit(10030, "railgun", (4, 6)),
                       unit(10031, "railgun", (12, 6)), unit(10013, "station", (9, 9))], round_no=71)
        policy = self.policy(state)
        self.assertGreater(policy.cost(())[1], 0)
        both_at_first = (Action("10010", "move", (Pos(3, 6),)),)
        toward_second = (Action("10010", "move", (Pos(3, 6),)), Action("10012", "move", (Pos(4, 5),)))
        self.assertLess(policy.cost(toward_second), policy.cost(both_at_first))

    def test_real_obstacles_not_only_chebyshev_distance(self):
        roles = [unit(pos=(2, 5)), unit(10030, "railgun", (6, 5)), unit(10013, "station", (8, 9))]
        roles += [unit(10040 + y, "wall", (4, y)) for y in range(9)]
        policy = self.policy(world(roles, round_no=71))
        self.assertGreater(policy.cost(())[1], 3)

    def test_active_task_pioneer_is_not_promised_as_a_returning_operator(self):
        policy = self.policy(self.state(71, phaseTask="active"))
        self.assertEqual(policy.required, 2)
        self.assertNotIn("10011", policy.actor_ids)
        self.assertEqual(self.policy(self.state(71)).required, 3)

    def test_incomplete_routes_and_absent_base_are_explicitly_disabled(self):
        state = self.state()
        policy = DefenseReturnPolicy(state, RuleProfile(), deadline=time.monotonic() - 1)
        self.assertFalse(policy.active)
        self.assertEqual(policy.status, "budget")
        self.assertEqual(policy.cost(()), (0, 0))
        self.assertEqual(self.policy(world([unit(), unit(10030, "railgun")])).status, "not-applicable")
        self.assertIsNone(policy.check(()).after)

    def test_unsupported_unit_count_cannot_expand_assignment_search_without_bound(self):
        state = world([unit(), unit(10013, "station", (8, 8))] +
                      [unit(10030 + i, "railgun", (i, 10)) for i in range(4)], round_no=71)
        self.assertEqual(self.policy(state).status, "size-limit")

    def test_single_operator_does_not_owe_three_posts_and_handover_is_allowed(self):
        state = world([unit(pos=(3, 5)), unit(10012, pos=(2, 5)), unit(10030, "railgun", (4, 6)),
                       unit(10013, "station", (5, 6))], round_no=71)
        policy = self.policy(state)
        self.assertEqual(policy.required, 1)
        actions = (Action("10010", "move", (Pos(2, 4),)), Action("10012", "move", (Pos(3, 5),)))
        self.assertEqual(policy.cost(actions), (0, 0))

    def test_immediate_survival_still_precedes_return(self):
        state = self.state(71)
        stay = Candidate("stay", "test", frozenset({"10010"}), (), Value(readiness=10000), "hold")
        escape = Candidate("escape", "test", frozenset({"10010"}), (Action("10010", "move", (Pos(11, 5),)),), Value(), "evade")
        with patch("agent.planning.ThreatEnvelope.projected_losses", side_effect=lambda actions: int(not any(a.actor_id == "10010" for a in actions))):
            plan = PlanArbiter(ActionCompiler(RuleProfile())).choose(state, (stay, escape), previous={}, deadline=time.monotonic() + 2)
        self.assertEqual(plan.actions, escape.actions)


if __name__ == "__main__":
    unittest.main()
