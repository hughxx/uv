import time
import unittest

from tests.helpers import packet, unit, world
from agent.actions import Action, ActionCompiler
from agent.layout import FortifyBase, LayoutGuard
from agent.planning import Candidate, PlanArbiter, Value
from agent.playbooks import Context, PlaybookLibrary
from agent.protocol import Observation
from agent.strategy import StrategyEngine
from agent.world import Pos, RuleProfile, World


class LayoutTests(unittest.TestCase):
    def choke(self, gaps=(3,)):
        raw = packet([unit(10013, "station", (1, 4), health=1500), unit(pos=(3, 3), backpack=["stone"])],
                     zones=[{"neutralType": "unknown-obstacle", "pos": {"x": 4, "y": y}} for y in range(7) if y not in gaps]
                     + [{"neutralType": "vendor", "pos": {"x": 6, "y": 3}}])
        raw["mapInfo"].update(width=7, height=7)
        return World.from_observation(Observation.from_payload(raw))

    def test_legal_wall_can_be_rejected_as_a_bad_layout_not_an_illegal_action(self):
        state = self.choke()
        action = Action("10010", "build", (Pos(4, 3),), "wall")
        ActionCompiler(RuleProfile()).validate(state, (action,))
        self.assertFalse(LayoutGuard(state).preserves_access((action,), time.monotonic() + 1))
        candidate = Candidate("seal", "test", frozenset({"10010"}), (action,), Value(readiness=100), "build-wall")
        for guard, expected in ((True, 0), (False, 1)):
            plan = PlanArbiter(ActionCompiler(RuleProfile(preserve_build_access=guard))).choose(
                state, (candidate,), previous={}, deadline=time.monotonic() + 1)
            self.assertEqual(len(plan.actions), expected)

    def test_two_individually_open_layouts_can_jointly_seal_access(self):
        state = self.choke(gaps=(2, 3))
        guard = LayoutGuard(state)
        first = Action("10010", "build", (Pos(4, 3),), "wall")
        second = Action("10012", "build", (Pos(4, 2),), "wall")
        self.assertTrue(guard.preserves_access((first,), time.monotonic() + 1))
        self.assertTrue(guard.preserves_access((second,), time.monotonic() + 1))
        self.assertFalse(guard.preserves_access((first, second), time.monotonic() + 1))

    def test_open_layout_and_deadline(self):
        state = world()
        action = Action("10010", "build", (Pos(3, 5),), "wall")
        self.assertTrue(LayoutGuard(state).preserves_access((action,), time.monotonic() + 1))
        self.assertFalse(LayoutGuard(state).preserves_access((action,), time.monotonic() - 1))
        self.assertTrue(LayoutGuard(state).preserves_access((), time.monotonic() - 1))

    def test_wall_budget_is_shared_even_when_two_workers_have_stone(self):
        roles = [unit(10013, "station", (5, 6), health=1500), unit(backpack=["stone"]),
                 unit(10012, pos=(7, 5), backpack=["stone"]), unit(10030, "railgun", (4, 6)), unit(10031, "gatling", (7, 6))]
        state = world(roles)
        library = PlaybookLibrary((FortifyBase(wall_budget=1),))
        decision = StrategyEngine(library=library).propose(state)
        self.assertEqual(len(decision.plan.candidates), 1)
        self.assertTrue(all(action.name in {"", "wall"} for action in decision.plan.actions))

    def test_no_wall_without_private_stone_or_before_basic_weapons_exist(self):
        for roles in ([unit(backpack=["stone"])], [unit(), unit(10030, "railgun", (4, 6)), unit(10031, "gatling", (7, 6))]):
            state = world(roles)
            self.assertEqual(FortifyBase().propose(Context(state, RuleProfile(), time.monotonic() + 1)), ())
