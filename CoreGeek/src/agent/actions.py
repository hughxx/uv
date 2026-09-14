"""Action descriptions and joint legality checks, independent of strategy scores."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from .protocol import idle_response
from .world import ACTORS, MINERALS, WEAPONS, Pos, RuleProfile, Unit, World


class InvalidAction(ValueError):
    pass


@dataclass(frozen=True)
class Action:
    actor_id: str
    kind: str
    targets: tuple[Pos, ...] = ()
    name: str = ""
    amount: int = 1
    weapon_id: str = ""
    answer: str = ""
    items: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.weapon_id if self.kind == "attack" else self.actor_id

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {"action": self.kind}
        if self.targets:
            result["targetPos"] = [pos.payload() for pos in self.targets]
        if self.name:
            result["name"] = self.name
        if self.kind in {"buy", "sell"}:
            result["num"] = self.amount
        if self.kind == "attack":
            result["controllerId"] = self.actor_id
        if self.kind == "submitAnswer":
            result["taskAnswer"] = self.answer
        if self.kind == "summonTreasure":
            result["item"] = list(self.items)
        return result


def weapon_range(weapon: Unit) -> int | None:
    if weapon.attack_range is not None:
        return weapon.attack_range if weapon.attack_range >= 0 else None
    if weapon.level not in (1, 2, 3):
        return None
    table = {"gatling": (3, 5, 7), "railgun": (6, 8, 10), "rocket": (10, 15, 2**31 - 1)}
    return table[weapon.kind][weapon.level - 1] if weapon.kind in table else None


class ActionCompiler:
    def __init__(self, rules: RuleProfile) -> None:
        self.rules = rules

    def compile(self, world: World, actions: tuple[Action, ...], *, prompt: str = "", execute_cmd: str = "") -> dict[str, Any]:
        self.validate(world, actions)
        if execute_cmd and not world.phase_task:
            raise InvalidAction("sandbox commands require an active task")
        if prompt and not world.phase_task:
            raise InvalidAction("out-of-task LLM calls are disabled until a quota policy is provided")
        response = idle_response()
        response["roleCommandMap"] = {action.key: action.payload() for action in actions}
        response["prompt"], response["executeCmd"] = prompt, execute_cmd
        return response

    def validate(self, world: World, actions: tuple[Action, ...]) -> None:
        if actions and world.observation.round_no < self.rules.round_origin:
            raise InvalidAction("round precedes configured origin")
        actors = {unit.id: unit for unit in world.actors}
        if len({action.actor_id for action in actions}) != len(actions):
            raise InvalidAction("actor used more than once")
        if len({action.key for action in actions}) != len(actions):
            raise InvalidAction("command key used more than once")
        moving = frozenset(action.actor_id for action in actions if action.kind == "move")
        blocked = world.blockers(exclude_actors=moving)
        destinations: set[Pos] = set()
        edges: set[tuple[Pos, Pos]] = set()
        builds: set[Pos] = set()
        gold = weapons = walls = 0
        for action in actions:
            actor = actors.get(action.actor_id)
            if actor is None:
                raise InvalidAction("actor is not a living friendly role")
            if any(not world.inside(pos) for pos in action.targets):
                raise InvalidAction("target outside map")
            if action.kind in {"move", "build", "collect", "remove", "summonTreasure"} and len(action.targets) != 1:
                raise InvalidAction("action requires one target")
            target = action.targets[0] if action.targets else None
            if action.kind == "move":
                if actor.pos.distance(target) != 1 or target in blocked:
                    raise InvalidAction("move is blocked or not one step")
                if world.phase_task and actor.kind == "pioneer":
                    raise InvalidAction("active task pins the pioneer until an explicit safe exit")
                if target in destinations or (target, actor.pos) in edges:
                    raise InvalidAction("move collision or swap")
                destinations.add(target)
                edges.add((actor.pos, target))
            elif action.kind == "collect":
                if actor.kind != "worker" or target not in {zone.pos for zone in world.zones if zone.kind in MINERALS}:
                    raise InvalidAction("collect requires a worker and a mineral")
                if actor.pos.distance(target) != 1 or actor.free_slots is None or actor.free_slots < 1:
                    raise InvalidAction("collect needs adjacent space and a free inventory slot")
            elif action.kind == "build":
                if actor.kind != "worker" or not self.rules.daytime(world.observation.round_no):
                    raise InvalidAction("building requires a worker during daytime")
                if action.name not in WEAPONS | {"wall"} or actor.pos.distance(target) != 1:
                    raise InvalidAction("invalid building or distance")
                if not self.rules.inferred_build_rings or target not in world.build_ring(action.name):
                    raise InvalidAction("building outside enabled construction mask")
                if target in world.blockers() or target in builds:
                    raise InvalidAction("building target occupied")
                builds.add(target)
                if action.name == "wall":
                    if not actor.backpack or "stone" not in actor.backpack:
                        raise InvalidAction("wall needs stone in the builder inventory")
                    walls += 1
                else:
                    gold += self.rules.weapon_cost
                    weapons += 1
            elif action.kind == "attack":
                self.validate_attack(world, actor, action)
            elif action.kind in {"sell", "buy"}:
                if type(action.amount) is not int or action.amount < 1:
                    raise InvalidAction("trade quantity must be positive")
                shop = "vendor" if action.kind == "sell" else "weaponShop"
                if not any(actor.pos.distance(pos) == 1 for pos in world.neutral(shop)):
                    raise InvalidAction("trade requires an adjacent shop")
                if action.kind == "sell":
                    if action.name not in MINERALS or actor.backpack is None or actor.backpack.count(action.name) < action.amount:
                        raise InvalidAction("not enough minerals to sell")
                    if action.name not in world.sale_prices:
                        raise InvalidAction("sale price unknown")
                else:
                    if action.name not in world.prices or actor.free_slots is None or actor.free_slots < action.amount:
                        raise InvalidAction("purchase item or available space unknown")
                    gold += world.prices[action.name] * action.amount
            elif action.kind == "remove":
                if actor.kind != "worker" or actor.pos.distance(target) != 1:
                    raise InvalidAction("removal requires an adjacent worker")
                if not any(unit.kind == "wall" and unit.alive and unit.pos == target for unit in world.our):
                    raise InvalidAction("can only remove a friendly wall")
            elif action.kind == "acceptTask":
                tasks = world.observation.raw["teamOur"].get("playerTasks", [])
                available = [task for task in tasks if task.get("isValid") is True and task.get("coldDownRounds") == 0]
                if actor.kind != "pioneer" or world.phase_task or not any(min(actor.pos.distance(pos) for pos in world.task_cells(task)) == 1 for task in available):
                    raise InvalidAction("no adjacent available task")
            elif action.kind == "submitAnswer":
                if actor.kind != "pioneer" or not world.phase_task or not isinstance(action.answer, str) or not action.answer:
                    raise InvalidAction("submission needs an active task and answer")
            elif action.kind == "summonTreasure":
                if actor.kind != "pioneer" or actor.pos.distance(target) != 1 or actor.backpack is None or not action.items:
                    raise InvalidAction("invalid treasure attempt")
                if Counter(action.items) - Counter(actor.backpack):
                    raise InvalidAction("treasure materials missing")
            elif action.kind == "drop":
                if not actor.backpack or action.name not in actor.backpack:
                    raise InvalidAction("drop item missing")
            elif action.kind == "use":
                self.validate_use(world, actor, action)
            else:
                raise InvalidAction("unknown action")
        if gold and (world.gold is None or gold > world.gold):
            raise InvalidAction("joint gold budget exceeded")
        if weapons and len(world.weapons) + weapons > self.rules.max_weapons:
            raise InvalidAction("global tower cap exceeded")
        if walls and sum(unit.kind == "wall" and unit.alive for unit in world.our) + walls > self.rules.max_walls:
            raise InvalidAction("global wall cap exceeded")
        if builds & destinations:
            raise InvalidAction("building blocks a selected move")

    def validate_attack(self, world: World, actor: Unit, action: Action) -> None:
        weapon = next((unit for unit in world.weapons if unit.id == action.weapon_id), None)
        if weapon is None or actor.pos.distance(weapon.pos) != 1 or self.rules.daytime(world.observation.round_no):
            raise InvalidAction("weapon requires an adjacent operator at night")
        if weapon.level not in (1, 2, 3):
            raise InvalidAction("weapon level unknown")
        if weapon.kind == "rocket" and weapon.cooldown != 0:
            raise InvalidAction("rocket cooldown not confirmed ready")
        count = 1 if weapon.kind == "railgun" else weapon.level
        if len(action.targets) != count:
            raise InvalidAction("wrong target count for weapon level")
        if not self.rules.repeated_attack_targets and len(set(action.targets)) != count:
            raise InvalidAction("repeated target semantics unverified")
        attack_range = weapon_range(weapon)
        if attack_range is None or any(weapon.pos.distance(target) > attack_range for target in action.targets):
            raise InvalidAction("attack outside range")
        robot_positions = {robot.pos for robot in world.robots if robot.alive}
        if any(target not in robot_positions or target == weapon.pos for target in action.targets):
            raise InvalidAction("only visible robot targets are enabled")
        if weapon.kind == "gatling":
            directions = [(target.x - weapon.pos.x, target.y - weapon.pos.y) for target in action.targets]
            if any(ax * bx + ay * by < 0 for (ax, ay), (bx, by) in combinations(directions, 2)):
                raise InvalidAction("gatling targets exceed a 90 degree cone")

    def validate_use(self, world: World, actor: Unit, action: Action) -> None:
        if actor.backpack is None or action.name not in actor.backpack:
            raise InvalidAction("item missing from actor inventory")
        if action.name == "Medicine" and not action.targets:
            return
        if action.name in {"Bomb", "DizzyWeapon"} and len(action.targets) == 1:
            return
        target = action.targets[0] if len(action.targets) == 1 else None
        building = next((unit for unit in world.our if unit.alive and target == unit.pos), None)
        if building is None or min(actor.pos.distance(pos) for pos in building.cells) != 1:
            raise InvalidAction("item requires an adjacent friendly building")
        if action.name == "WallFixer" and building.kind == "wall":
            return
        for prefix, kinds in (("WeaponUpgradeVoucher", WEAPONS), ("WallUpgradeVoucher", {"wall"}), ("StationUpgradeVoucher", {"station"})):
            if action.name in {prefix + "1", prefix + "2"} and building.kind in kinds and building.level == int(action.name[-1]):
                return
        raise InvalidAction("unsupported item or wrong upgrade level")
