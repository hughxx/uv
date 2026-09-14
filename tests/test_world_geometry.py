import unittest

from tests.helpers import packet, unit, world
from agent.geometry import interaction_cells, shortest_route
from agent.protocol import InvalidObservation, Observation
from agent.world import Pos, RuleProfile, World


class WorldGeometryTests(unittest.TestCase):
    def test_base_footprint_and_construction_rings(self):
        state = world()
        self.assertEqual(state.station.cells, {Pos(5, 6), Pos(6, 6), Pos(5, 5), Pos(6, 5)})
        self.assertEqual(len(state.build_ring("gatling")), 12)
        self.assertEqual(len(state.build_ring("wall")), 20)

    def test_enemy_buildings_and_neutrals_block_all_footprint_cells(self):
        state = world(enemy=[unit(20013, "station", (15, 16)), unit(41000, "wall", (14, 14))],
                      zones=[{"neutralType": "stone", "pos": {"x": 8, "y": 8}}])
        self.assertTrue({Pos(15, 15), Pos(16, 16), Pos(14, 14), Pos(8, 8)} <= state.blockers())
        self.assertNotIn(Pos(4, 5), state.blockers(exclude_actors=frozenset({"10010"})))

    def test_unknown_is_not_zero_and_dead_units_do_not_block(self):
        raw = unit()
        raw.pop("backPackCapability")
        state = world([raw], enemy=[unit(20010, health=0)])
        self.assertIsNone(state.actors[0].free_slots)
        self.assertIsNone(state.actors[0].cooldown)
        self.assertNotIn(Pos(4, 5), state.blockers(exclude_actors=frozenset({"10010"})))

    def test_duplicate_ids_and_outside_positions_rejected(self):
        for roles in ([unit(), unit()], [unit(pos=(-1, 0))]):
            with self.subTest(roles=roles), self.assertRaises(InvalidObservation):
                world(roles)

    def test_start_equals_goal_unreachable_and_diagonal_corner(self):
        state = world([])
        start, goal = Pos(0, 0), Pos(1, 1)
        self.assertIsNone(shortest_route(state, start, frozenset({start}), frozenset()).next_step)
        path = shortest_route(state, start, frozenset({goal}), frozenset({Pos(1, 0), Pos(0, 1)}))
        self.assertEqual(path.cells, (start, goal))
        self.assertIsNone(shortest_route(state, start, frozenset({goal}), frozenset(start.neighbors())))

    def test_interaction_goals_exclude_blockers(self):
        state = world([])
        goals = interaction_cells(state, frozenset({Pos(0, 0)}), frozenset({Pos(1, 0)}))
        self.assertEqual(goals, {Pos(0, 1), Pos(1, 1)})

    def test_round_origin_is_explicit(self):
        one, zero = RuleProfile(), RuleProfile(round_origin=0)
        self.assertTrue(one.daytime(70))
        self.assertFalse(one.daytime(71))
        self.assertTrue(one.daytime(131))
        self.assertEqual(one.daylight_left(70), 1)
        self.assertTrue(zero.daytime(0))
        self.assertFalse(zero.daytime(70))
