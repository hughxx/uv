import copy
import time
import unittest

from tests.helpers import packet, unit, world
from agent.actions import Action, ActionCompiler, InvalidAction
from agent.application import AgentApplication
from agent.economy import InvestUpgrades, RestockMedicine
from agent.forecast import ThreatEnvelope
from agent.planning import Candidate, PlanArbiter, Value
from agent.playbooks import Context, EvadeLethalThreat, PlaybookLibrary, UseInventory
from agent.strategy import StrategyEngine
from agent.world import Pos, RuleProfile


class EconomySafetyTests(unittest.TestCase):
    def test_buy_deliver_upgrade_uses_actual_inventory_and_gold(self):
        app = AgentApplication(StrategyEngine(library=PlaybookLibrary((InvestUpgrades(), UseInventory()))))
        raw = packet([unit(pos=(2, 5)), unit(10030, "railgun", (8, 5))], gold=100,
                     zones=[{"neutralType": "weaponShop", "pos": {"x": 3, "y": 5}}],
                     weaponShopList=[{"name": "WeaponUpgradeVoucher1", "price": 100}])
        stages = []
        for _ in range(12):
            response = app.handle_turn(raw)
            action = response["roleCommandMap"]["10010"]
            stages.append(action["action"])
            updated = copy.deepcopy(raw)
            updated["roundNo"] += 1
            actor = updated["teamOur"]["roles"][0]
            if action["action"] == "buy":
                self.assertEqual(action["num"], 1)
                actor["backpack"].append(action["name"])
                updated["teamOur"]["goldNum"] -= 100
            elif action["action"] == "move":
                actor["pos"] = action["targetPos"][0]
            elif action["action"] == "use":
                actor["backpack"].remove(action["name"])
                updated["teamOur"]["roles"][1].update(level=2, health=1500)
                app.handle_turn(updated)
                self.assertIn("objective-observed", [transition.kind for transition in app.engine.last_decision.transitions])
                break
            raw = updated
        else:
            self.fail("upgrade logistics did not finish")
        self.assertEqual(stages.count("buy"), 1)
        self.assertIn("move", stages)
        self.assertEqual(stages[-1], "use")

    def test_no_duplicate_procurement_or_same_turn_purchase_use(self):
        raw = packet([unit(pos=(2, 5)), unit(10012, pos=(2, 6)),
                      unit(10030, "railgun", (8, 5)), unit(10031, "gatling", (8, 8))], gold=300,
                     zones=[{"neutralType": "weaponShop", "pos": {"x": 3, "y": 5}}],
                     weaponShopList=[{"name": "WeaponUpgradeVoucher1", "price": 100}])
        app = AgentApplication(StrategyEngine(library=PlaybookLibrary((InvestUpgrades(),))))
        actions = list(app.handle_turn(raw)["roleCommandMap"].values())
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "buy")
        raw["roundNo"] = 2
        raw["teamOur"]["roles"][0]["backpack"] = ["WeaponUpgradeVoucher1"]
        self.assertEqual(app.handle_turn(raw)["roleCommandMap"], {})

    def test_full_bag_unaffordable_and_late_delivery_are_not_purchased(self):
        for bag, gold, turn in ((["stone"] * 100, 100, 1), ([], 99, 1), ([], 100, 70)):
            with self.subTest(bag=len(bag), gold=gold, turn=turn):
                state = world([unit(pos=(2, 5), backpack=bag), unit(10030, "railgun", (8, 5))], gold=gold, round_no=turn,
                              zones=[{"neutralType": "weaponShop", "pos": {"x": 3, "y": 5}}],
                              weaponShopList=[{"name": "WeaponUpgradeVoucher1", "price": 100}])
                self.assertEqual(InvestUpgrades().propose(Context(state, RuleProfile(), time.monotonic() + 1)), ())

    def test_medicine_buy_then_use_are_separate_turns(self):
        app = AgentApplication(StrategyEngine(library=PlaybookLibrary((RestockMedicine(), UseInventory()))))
        raw = packet([unit(health=50)], gold=10, zones=[{"neutralType": "weaponShop", "pos": {"x": 3, "y": 5}}])
        self.assertEqual(app.handle_turn(raw)["roleCommandMap"]["10010"]["action"], "buy")
        raw["roundNo"] = 2
        raw["teamOur"]["goldNum"] = 0
        raw["teamOur"]["roles"][0]["backpack"] = ["Medicine"]
        self.assertEqual(app.handle_turn(raw)["roleCommandMap"]["10010"], {"action": "use", "name": "Medicine"})

    def test_projected_lethal_threat_overrides_arbitrarily_large_cash_value(self):
        state = world([unit(health=5, backpack=["Medicine", "copper"])], round_no=71,
                      robots=[unit(30001, "smallRobot", (4, 8), health=40)],
                      zones=[{"neutralType": "vendor", "pos": {"x": 3, "y": 5}}])
        candidates = (Candidate("cash", "test", frozenset({"10010"}), (Action("10010", "sell", name="copper"),),
                                Value(gold=1e12), "sell"),
                      Candidate("heal", "test", frozenset({"10010"}), (Action("10010", "use", name="Medicine"),),
                                Value(readiness=1), "heal"))
        for guard, expected in ((True, "use"), (False, "sell")):
            compiler = ActionCompiler(RuleProfile(guard_projected_role_deaths=guard))
            plan = PlanArbiter(compiler, per_actor=1).choose(state, candidates, previous={}, deadline=time.monotonic() + 1)
            self.assertEqual(plan.actions[0].kind, expected)

    def test_evasion_is_legal_and_does_not_count_expected_kills_as_protection(self):
        state = world([unit(health=5)], round_no=71, robots=[unit(30001, "smallRobot", (4, 9), health=1)])
        engine = StrategyEngine(library=PlaybookLibrary((EvadeLethalThreat(),)))
        decision = engine.propose(state)
        self.assertEqual(decision.plan.actions[0].kind, "move")
        self.assertEqual(decision.plan.projected_role_losses, 0)
        self.assertEqual(ThreatEnvelope(state).projected_losses(()), 1)

    def test_task_reposition_must_preserve_every_possible_task_area(self):
        raw = packet([unit(10011, "pioneer", (4, 5))], phaseTask="active",
                     zones=[{"neutralType": "challengerTaskPoint1", "pos": {"x": 5, "y": 5}}])
        raw["teamOur"]["playerTasks"] = [{"taskPosition": {"x": 5, "y": 5}}]
        from agent.protocol import Observation
        from agent.world import World
        state = World.from_observation(Observation.from_payload(raw))
        compiler = ActionCompiler(RuleProfile())
        compiler.validate(state, (Action("10011", "move", (Pos(4, 6),)),))
        with self.assertRaises(InvalidAction):
            compiler.validate(state, (Action("10011", "move", (Pos(3, 5),)),))

    def test_current_purchase_and_future_reservation_share_one_ledger(self):
        state = world([unit(pos=(2, 5)), unit(10012, pos=(8, 8))], gold=25,
                      zones=[{"neutralType": "weaponShop", "pos": {"x": 3, "y": 5}}])
        candidates = (Candidate("buy", "test", frozenset({"10010"}), (Action("10010", "buy", name="Medicine"),), Value(readiness=10), "buy"),
                      Candidate("future", "test", frozenset({"10012"}), (), Value(readiness=10), "hold", reserved_gold=25))
        plan = PlanArbiter(ActionCompiler(RuleProfile())).choose(state, candidates, previous={}, deadline=time.monotonic() + 1)
        self.assertEqual(len(plan.candidates), 1)

    def test_invalid_definition_output_does_not_disable_other_playbooks(self):
        class Broken:
            id = "broken"
            def propose(self, context):
                return (object(), Candidate("nan", self.id, frozenset({"10010"}), (), Value(score=float("nan")), "hold"))
        app = AgentApplication(StrategyEngine(library=PlaybookLibrary((Broken(), UseInventory()))))
        response = app.handle_turn(packet([unit(health=50, backpack=["Medicine"])]))
        self.assertEqual(response["roleCommandMap"]["10010"]["name"], "Medicine")
        self.assertEqual(len(app.engine.last_decision.diagnostics), 2)

    def test_new_session_restores_configured_round_origin(self):
        app = AgentApplication()
        app.handle_turn(packet(round_no=0))
        self.assertEqual(app.engine.rules.round_origin, 0)
        raw = packet(round_no=1)
        raw["teamOur"]["teamId"] = "other"
        app.handle_turn(raw)
        self.assertEqual(app.engine.rules.round_origin, 1)
