import unittest

from tests.helpers import packet, unit, world
from agent.actions import Action, ActionCompiler, InvalidAction
from agent.protocol import Observation
from agent.world import Pos, RuleProfile, World


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.compiler = ActionCompiler(RuleProfile())

    def rejects(self, state, *actions):
        with self.assertRaises(InvalidAction):
            self.compiler.compile(state, actions)

    def test_joint_gold_budget_and_global_cap(self):
        roles = [unit(10013, "station", (5, 6)), unit(), unit(10012, pos=(7, 6))]
        actions = (Action("10010", "build", (Pos(4, 6),), "gatling"), Action("10012", "build", (Pos(7, 5),), "railgun"))
        self.rejects(world(roles, gold=25), *actions)
        self.assertEqual(len(self.compiler.compile(world(roles, gold=50), actions)["roleCommandMap"]), 2)
        roles += [unit(10020, "gatling", (5, 7)), unit(10030, "railgun", (6, 7)), unit(10040, "rocket", (7, 7))]
        self.rejects(world(roles), actions[0])

    def test_full_bag_and_missing_inventory_cannot_collect(self):
        zones = [{"neutralType": "stone", "pos": {"x": 3, "y": 5}}]
        action = Action("10010", "collect", (Pos(3, 5),))
        self.rejects(world([unit(backpack=["copper"] * 100)], zones=zones), action)
        raw = unit()
        raw.pop("backpack")
        self.rejects(world([raw], zones=zones), action)

    def test_move_collision_swap_and_legal_following(self):
        state = world([unit(pos=(2, 2)), unit(10012, pos=(3, 2))])
        self.rejects(state, Action("10010", "move", (Pos(3, 3),)), Action("10012", "move", (Pos(3, 3),)))
        self.rejects(state, Action("10010", "move", (Pos(3, 2),)), Action("10012", "move", (Pos(2, 2),)))
        response = self.compiler.compile(state, (Action("10010", "move", (Pos(3, 2),)), Action("10012", "move", (Pos(4, 2),))))
        self.assertEqual(len(response["roleCommandMap"]), 2)
        self.rejects(state, Action("10010", "move", (Pos(3, 2),)))

    def test_operator_cannot_move_and_fire_or_control_two_towers(self):
        state = world([unit(), unit(10020, "gatling", (4, 6)), unit(10030, "railgun", (3, 5))], round_no=71,
                      robots=[unit(30001, "smallRobot", (3, 6), health=40)])
        fire = Action("10010", "attack", (Pos(3, 6),), weapon_id="10020")
        self.rejects(state, fire, Action("10010", "move", (Pos(5, 4),)))
        self.rejects(state, fire, Action("10010", "attack", (Pos(3, 6),), weapon_id="10030"))
        response = self.compiler.compile(state, (fire,))
        self.assertEqual(response["roleCommandMap"]["10020"]["controllerId"], "10010")

    def test_weapon_count_cone_range_and_rocket_unknown_cooldown(self):
        roles = [unit(), unit(10020, "gatling", (4, 6), level=2)]
        robots = [unit(30001, "smallRobot", (3, 6)), unit(30002, "smallRobot", (5, 6))]
        state = world(roles, round_no=71, robots=robots)
        self.rejects(state, Action("10010", "attack", (Pos(3, 6),), weapon_id="10020"))
        self.rejects(state, Action("10010", "attack", (Pos(3, 6), Pos(5, 6)), weapon_id="10020"))
        roles[1].update(roleType="rocket", id=10040, level=1)
        self.rejects(world(roles, round_no=71, robots=robots), Action("10010", "attack", (Pos(3, 6),), weapon_id="10040"))

    def test_sell_income_is_not_available_for_same_turn_purchase(self):
        roles = [unit(backpack=["copper"] * 10), unit(10012, pos=(8, 8))]
        zones = [{"neutralType": "vendor", "pos": {"x": 3, "y": 5}}, {"neutralType": "weaponShop", "pos": {"x": 9, "y": 8}}]
        self.rejects(world(roles, gold=0, zones=zones), Action("10010", "sell", name="copper", amount=10), Action("10012", "buy", name="Medicine"))

    def test_task_residency_and_second_tile_interaction(self):
        actor = unit(10011, "pioneer", (8, 5))
        state = world([actor], phaseTask="active")
        self.rejects(state, Action("10011", "move", (Pos(8, 6),)))
        zones = [{"neutralType": "challengerTaskPoint2", "pos": {"x": x, "y": 5}} for x in (6, 7)]
        raw = packet([actor], zones=zones)
        raw["teamOur"]["playerTasks"] = [{"taskPosition": {"x": 6, "y": 5}, "isValid": True, "coldDownRounds": 0}]
        self.compiler.compile(World.from_observation(Observation.from_payload(raw)), (Action("10011", "acceptTask"),))

    def test_upgrade_level_and_private_inventory(self):
        roles = [unit(backpack=["StationUpgradeVoucher1"]), unit(10013, "station", (5, 6), level=1)]
        action = Action("10010", "use", (Pos(5, 6),), "StationUpgradeVoucher1")
        self.compiler.compile(world(roles), (action,))
        roles[1]["level"] = 2
        self.rejects(world(roles), action)

    def test_explicit_build_mask_disable_and_tool_guard(self):
        compiler = ActionCompiler(RuleProfile(inferred_build_rings=False))
        with self.assertRaises(InvalidAction):
            compiler.compile(world(), (Action("10010", "build", (Pos(4, 6),), "gatling"),))
        with self.assertRaises(InvalidAction):
            self.compiler.compile(world(), (), execute_cmd="echo test")
