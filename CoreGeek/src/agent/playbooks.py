"""Registered, stateless opportunity generators and observable plan progress."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from typing import Protocol

from .actions import Action, ActionCompiler, InvalidAction, weapon_range
from .combat import AreaConsumables
from .economy import InvestUpgrades, RestockMedicine, upgrade_item, upgrade_value
from .forecast import ThreatEnvelope, full_health
from .geometry import Route, interaction_cells, shortest_route
from .layout import FortifyBase
from .planning import Candidate, Value, valid_candidate
from .world import MINERALS, Pos, RuleProfile, Unit, World


@dataclass
class Context:
    world: World
    rules: RuleProfile
    deadline: float
    horizon: int = 40
    routes: dict[tuple[str, Pos, frozenset[Pos]], Route | None] | None = None

    def __post_init__(self) -> None:
        self.horizon = max(0, min(self.horizon, self.rules.round_origin + 1300 - self.world.observation.round_no))
        self.routes = {}

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def route(self, actor: Unit, targets: frozenset[Pos]) -> Route | None:
        return self.route_from(actor, actor.pos, targets)

    def route_from(self, actor: Unit, start: Pos, targets: frozenset[Pos]) -> Route | None:
        key = actor.id, start, targets
        if key not in self.routes:
            if self.expired:
                return None
            blocked = self.world.blockers(exclude_actors=frozenset({actor.id}))
            self.routes[key] = shortest_route(self.world, start, interaction_cells(self.world, targets, blocked), blocked, deadline=self.deadline)
            if self.routes[key] is None and not self.expired and self.rules.joint_follow_moves:
                # A narrow post may be blocked only by a teammate who can
                # leave in this same joint plan. Offer the conditional route;
                # the compiler accepts its first step ONLY if that teammate
                # actually has a compatible move in the selected plan.
                cooperative = self.world.blockers(exclude_actors=frozenset(unit.id for unit in self.world.actors))
                if cooperative != blocked:
                    self.routes[key] = shortest_route(self.world, start, interaction_cells(self.world, targets, cooperative),
                                                       cooperative, deadline=self.deadline)
        return self.routes[key]

    def risk(self, pos: Pos) -> float:
        return sum(12.0 / max(1, pos.distance(robot.pos)) for robot in self.world.robots
                   if robot.alive and robot.target_team in {None, self.world.side} and robot.abnormal_state != "dizzy"
                   and pos.distance(robot.pos) <= 5)


class Playbook(Protocol):
    id: str

    def propose(self, context: Context) -> tuple[Candidate, ...]: ...


class PlaybookLibrary:
    def __init__(self, definitions: tuple[Playbook, ...]) -> None:
        if len({definition.id for definition in definitions}) != len(definitions):
            raise ValueError("duplicate playbook ID")
        self.definitions = definitions

    def propose(self, context: Context) -> tuple[tuple[Candidate, ...], tuple[str, ...]]:
        candidates: list[Candidate] = []
        diagnostics: list[str] = []
        for definition in self.definitions:
            if context.expired:
                diagnostics.append(f"budget:{definition.id}")
                continue
            try:
                for candidate in definition.propose(context):
                    if valid_candidate(candidate):
                        candidates.append(candidate)
                    else:
                        diagnostics.append(f"invalid-candidate:{definition.id}")
            except Exception as error:
                diagnostics.append(f"error:{definition.id}:{type(error).__name__}")
        return tuple(candidates), tuple(diagnostics)


def approach_or_act(context: Context, actor: Unit, targets: frozenset[Pos], action: Action) -> tuple[Action, int] | None:
    route = context.route(actor, targets)
    if route is None or route.distance + 1 > context.horizon:
        return None
    if route.next_step is not None:
        if context.world.phase_task and actor.kind == "pioneer":
            return None
        return Action(actor.id, "move", (route.next_step,)), route.distance + 1
    return action, 1


class CashInventory:
    id = "cash-inventory"

    def propose(self, context: Context) -> tuple[Candidate, ...]:
        world = context.world
        proposals = []
        for actor in world.actors:
            for name, amount in Counter(actor.backpack or ()).items():
                if name not in MINERALS or name not in world.sale_prices or context.expired:
                    continue
                step = approach_or_act(context, actor, frozenset(world.neutral("vendor")), Action(actor.id, "sell", name=name, amount=amount))
                if step:
                    action, eta = step
                    proposals.append(Candidate(f"cash:{actor.id}:{name}", self.id, frozenset({actor.id}), (action,),
                                              Value(gold=amount * world.sale_prices[name], occupied_turns=eta, risk=context.risk(actor.pos)),
                                              "sell" if action.kind == "sell" else "approach-vendor", ("inventory-observed", "sale-price-observed")))
        return tuple(proposals)


class BuildDefense:
    id = "build-defense"

    def propose(self, context: Context) -> tuple[Candidate, ...]:
        world = context.world
        if not context.rules.daytime(world.observation.round_no) or world.gold is None or world.gold < context.rules.weapon_cost:
            return ()
        if len(world.weapons) >= context.rules.max_weapons or not context.rules.inferred_build_rings:
            return ()
        kind = "railgun" if not any(unit.kind == "railgun" for unit in world.weapons) else "gatling"
        available = world.build_ring(kind) - world.blockers()
        proposals = []
        for actor in world.actors:
            if actor.kind != "worker":
                continue
            for target in sorted(available, key=lambda pos: (actor.pos.distance(pos), pos))[:6]:
                if context.expired:
                    break
                step = approach_or_act(context, actor, frozenset({target}), Action(actor.id, "build", (target,), kind))
                if not step:
                    continue
                action, eta = step
                if eta > context.rules.daylight_left(world.observation.round_no):
                    continue
                proposals.append(Candidate(f"build:{target.x}:{target.y}", self.id, frozenset({actor.id}), (action,),
                                          Value(gold=-context.rules.weapon_cost, readiness=32.0 / eta, occupied_turns=eta,
                                                risk=context.risk(action.targets[0])), "build" if action.kind == "build" else "approach-site",
                                          ("gold-observed", "inferred-build-mask"), frozenset({f"site:{target.x}:{target.y}"}),
                                          deadline=world.observation.round_no + context.rules.daylight_left(world.observation.round_no) - 1,
                                          reserved_gold=context.rules.weapon_cost, reserved_weapon_slots=1))
        return tuple(proposals)


def estimated_damage(world: World, weapon: Unit, targets: tuple[Pos, ...]) -> tuple[tuple[str, float], ...]:
    damage: dict[str, float] = {}
    for target in targets:
        if weapon.kind == "rocket":
            for robot in world.robots:
                if robot.alive and robot.pos.distance(target) <= 1:
                    damage[robot.id] = damage.get(robot.id, 0) + (20 if robot.pos == target else 10)
        else:
            # Exact collinear robots are modeled. Off-line raster intersections,
            # obstacles and PvP remain uncalibrated rather than fabricated.
            dx, dy = target.x - weapon.pos.x, target.y - weapon.pos.y
            on_ray = [robot for robot in world.robots if robot.alive
                      and (robot.pos.x - weapon.pos.x) * dy == (robot.pos.y - weapon.pos.y) * dx
                      and 0 < (robot.pos.x - weapon.pos.x) * dx + (robot.pos.y - weapon.pos.y) * dy <= dx * dx + dy * dy]
            energy = 10 * weapon.level if weapon.kind == "railgun" else 10
            for robot in sorted(on_ray, key=lambda unit: weapon.pos.distance(unit.pos)):
                dealt = min(robot.health, energy)
                damage[robot.id] = damage.get(robot.id, 0) + dealt
                energy -= dealt
                if weapon.kind == "gatling" or energy <= 0:
                    break
    return tuple(sorted(damage.items()))


class OperateDefense:
    id = "operate-defense"
    # One common readiness baseline for approaching, holding and firing.
    # Arrival must not destroy the very value that paid for the approach.
    # This is a heuristic positioning value, not score or predicted damage.
    post_readiness = 18.0

    def propose(self, context: Context) -> tuple[Candidate, ...]:
        world = context.world
        proposals = []
        night = not context.rules.daytime(world.observation.round_no)
        compiler = ActionCompiler(context.rules)
        for actor in world.actors:
            for weapon in world.weapons:
                if context.expired:
                    break
                route = context.route(actor, weapon.cells)
                if route is None:
                    continue
                if route.next_step and (night or context.rules.daylight_left(world.observation.round_no) <= route.distance + 2):
                    if world.phase_task and actor.kind == "pioneer":
                        continue
                    action = Action(actor.id, "move", (route.next_step,))
                    proposals.append(Candidate(f"defend:{weapon.id}", self.id, frozenset({actor.id}), (action,),
                                              Value(readiness=self.post_readiness / max(1, route.distance), risk=context.risk(route.next_step), occupied_turns=route.distance),
                                              "approach-weapon", ("weapon-observed", "defense-deadline"), frozenset({f"weapon:{weapon.id}"})))
                    continue
                if route.distance or not (night or context.rules.daylight_left(world.observation.round_no) <= 2):
                    continue
                # Holding reserves both this actor and this weapon in the
                # joint plan, without inventing an unsupported HTTP idle action.
                # It is an alternative, not a hard rule: healing, tasks, escape
                # and a genuinely more valuable joint handover can still win.
                proposals.append(Candidate(f"defend:{weapon.id}", self.id, frozenset({actor.id}), (),
                                          Value(readiness=self.post_readiness, risk=context.risk(actor.pos)), "hold-weapon",
                                          ("weapon-observed", "operator-positioned"), frozenset({f"weapon:{weapon.id}"})))
                if not night or weapon.level not in (1, 2, 3):
                    continue
                attack_range = weapon_range(weapon)
                if attack_range is None or (weapon.kind == "rocket" and weapon.cooldown != 0):
                    continue
                targets = sorted((robot.pos for robot in world.robots if robot.alive and robot.target_team in {None, world.side}
                                  and robot.pos != weapon.pos and weapon.pos.distance(robot.pos) <= attack_range),
                                 key=lambda pos: (weapon.pos.distance(pos), pos))[:8]
                count = 1 if weapon.kind == "railgun" else weapon.level
                for group in combinations(targets, count):
                    if context.expired:
                        break
                    action = Action(actor.id, "attack", tuple(group), weapon_id=weapon.id)
                    try:
                        compiler.validate(world, (action,))
                    except InvalidAction:
                        continue
                    proposals.append(Candidate(f"defend:{weapon.id}", self.id, frozenset({actor.id}), (action,),
                                              Value(readiness=self.post_readiness, risk=context.risk(actor.pos)), "fire", ("target-observed", "cooldown-ready", "estimated-ballistics"),
                                              frozenset({f"weapon:{weapon.id}"}), estimated_damage(world, weapon, tuple(group))))
        return tuple(proposals)


class HarvestMinerals:
    id = "harvest-minerals"

    def propose(self, context: Context) -> tuple[Candidate, ...]:
        world = context.world
        if not context.rules.daytime(world.observation.round_no):
            return ()
        proposals = []
        for actor in world.actors:
            if actor.kind != "worker" or actor.free_slots is None or actor.free_slots <= 0:
                continue
            for zone in world.zones:
                if zone.kind not in world.sale_prices or zone.kind not in MINERALS or context.expired:
                    continue
                step = approach_or_act(context, actor, frozenset({zone.pos}), Action(actor.id, "collect", (zone.pos,)))
                if not step:
                    continue
                action, eta = step
                stand = context.route(actor, frozenset({zone.pos})).cells[-1]
                delivery = context.route_from(actor, stand, frozenset(world.neutral("vendor")))
                if delivery is None:
                    continue
                defense = context.route_from(actor, stand, frozenset(weapon.pos for weapon in world.weapons)) if world.weapons else None
                if world.weapons and (defense is None or eta + defense.distance + 2 >= context.rules.daylight_left(world.observation.round_no)):
                    continue
                capacity = min(actor.free_slots, 10, max(0, context.horizon - eta - delivery.distance - 1))
                if capacity <= 0:
                    continue
                # Raw mineral value is discounted for the full delivery cycle;
                # this is a rate estimate, not guaranteed remaining vein stock.
                value = capacity * world.sale_prices[zone.kind] / max(1, eta + capacity + delivery.distance + 1)
                proposals.append(Candidate(f"harvest:{actor.id}:{zone.pos.x}:{zone.pos.y}", self.id, frozenset({actor.id}), (action,),
                                          Value(gold=value * min(10, context.horizon), risk=context.risk(action.targets[0]), occupied_turns=eta),
                                          "collect" if action.kind == "collect" else "approach-mineral", ("free-space-observed", "price-observed", "vein-stock-unknown")))
        return tuple(proposals)


class UseInventory:
    id = "use-inventory"

    def propose(self, context: Context) -> tuple[Candidate, ...]:
        world = context.world
        threat = ThreatEnvelope(world)
        proposals = []
        for actor in world.actors:
            if "Medicine" in (actor.backpack or ()) and actor.health < full_health(actor) and (actor.health < full_health(actor) / 2
                                                                                          or threat.damage_at(actor.pos) >= actor.health):
                proposals.append(Candidate(f"heal:{actor.id}", self.id, frozenset({actor.id}), (Action(actor.id, "use", name="Medicine"),),
                                          Value(readiness=20 + context.risk(actor.pos)), "heal", ("low-health", "medicine-owned")))
            for building in world.our:
                if not building.alive or building.level not in (1, 2):
                    continue
                item = upgrade_item(building)
                if item is None:
                    continue
                if item not in (actor.backpack or ()) or context.expired:
                    continue
                action = Action(actor.id, "use", (building.pos,), item)
                step = approach_or_act(context, actor, building.cells, action)
                if step:
                    next_action, eta = step
                    proposals.append(Candidate(f"upgrade:{building.id}", self.id, frozenset({actor.id}), (next_action,),
                                              Value(readiness=upgrade_value(building), occupied_turns=eta, risk=context.risk(actor.pos)),
                                              "upgrade" if next_action.kind == "use" else "deliver-upgrade", ("voucher-owned", "level-observed"),
                                              frozenset({f"upgrade:{building.id}"})))
            for wall in world.our:
                maximum = full_health(wall)
                if wall.kind != "wall" or not wall.alive or maximum is None or wall.health >= maximum / 2 or "WallFixer" not in (actor.backpack or ()):
                    continue
                step = approach_or_act(context, actor, wall.cells, Action(actor.id, "use", (wall.pos,), "WallFixer"))
                if step:
                    action, eta = step
                    proposals.append(Candidate(f"repair:{wall.id}", self.id, frozenset({actor.id}), (action,),
                                              Value(readiness=(maximum - wall.health) / 50, occupied_turns=eta),
                                              "repair" if action.kind == "use" else "deliver-repair", ("damaged-wall", "repair-item-owned"),
                                              frozenset({f"upgrade:{wall.id}"})))
        return tuple(proposals)


class EvadeLethalThreat:
    id = "evade-lethal-threat"

    def propose(self, context: Context) -> tuple[Candidate, ...]:
        world, threat = context.world, ThreatEnvelope(context.world)
        proposals = []
        for actor in world.actors:
            incoming = threat.damage_at(actor.pos)
            if incoming < actor.health:
                continue
            blocked = world.blockers(exclude_actors=frozenset({actor.id}))
            for target in actor.pos.neighbors():
                if context.expired:
                    break
                if not world.inside(target) or target in blocked or threat.damage_at(target) >= incoming:
                    continue
                if world.phase_task and actor.kind == "pioneer" and target not in world.task_stay_cells(actor):
                    continue
                proposals.append(Candidate(f"evade:{actor.id}:{target.x}:{target.y}", self.id, frozenset({actor.id}),
                                          (Action(actor.id, "move", (target,)),),
                                          Value(readiness=1, risk=threat.damage_at(target) / max(1, actor.health)),
                                          "evade", ("observed-health", "one-step-one-attack-envelope-unverified")))
        return tuple(proposals)


def default_library() -> PlaybookLibrary:
    from .tasks import AcquireTask

    return PlaybookLibrary((EvadeLethalThreat(), UseInventory(), OperateDefense(), AreaConsumables(), BuildDefense(), AcquireTask(),
                            RestockMedicine(), InvestUpgrades(), FortifyBase(), CashInventory(), HarvestMinerals()))
