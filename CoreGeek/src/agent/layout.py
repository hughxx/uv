"""Structural access preservation for optional construction plans."""

from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING

from .actions import Action
from .world import MINERALS, WEAPONS, Pos, World

if TYPE_CHECKING:
    from .planning import Candidate
    from .playbooks import Context


def reachable(world: World, start: Pos, blocked: frozenset[Pos], deadline: float) -> frozenset[Pos] | None:
    frontier = deque([start])
    seen = {start}
    while frontier:
        if time.monotonic() >= deadline:
            return None
        current = frontier.popleft()
        for target in current.neighbors():
            if world.inside(target) and target not in blocked and target not in seen:
                seen.add(target)
                frontier.append(target)
    return frozenset(seen)


class LayoutGuard:
    """Preserve previously reachable essential interaction groups.

    Dynamic actors/robots are deliberately omitted: this is future structural
    access, not a promise that current traffic or enemies cannot block a route.
    """

    def __init__(self, world: World) -> None:
        self.world = world
        self.blocked = frozenset({zone.pos for zone in world.zones} |
                                 {cell for unit in (*world.our, *world.enemy)
                                  if unit.kind in WEAPONS | {"wall", "station"} and (unit.health is None or unit.health > 0)
                                  for cell in unit.cells})
        self.groups = [unit.cells for unit in world.our if unit.alive and unit.kind in WEAPONS | {"station"}]
        kinds = sorted({zone.kind for zone in world.zones if zone.kind in MINERALS | {"vendor", "weaponShop"}
                        or zone.kind.startswith(world.side + "TaskPoint")})
        self.groups += [frozenset(world.neutral(kind)) for kind in kinds]
        self.before: dict[Pos, frozenset[Pos]] = {}
        self.cache: dict[tuple, bool] = {}

    def preserves_access(self, actions: tuple[Action, ...], deadline: float) -> bool:
        placements = frozenset(action.targets[0] for action in actions if action.kind == "build")
        if not placements:
            return True
        moves = tuple(sorted((action.actor_id, action.targets[0]) for action in actions if action.kind == "move"))
        key = placements, moves
        if key in self.cache:
            return self.cache[key]
        after: dict[Pos, frozenset[Pos]] = {}
        destinations = dict(moves)
        new_groups = [frozenset(action.targets) for action in actions if action.kind == "build" and action.name in WEAPONS]

        def component(start: Pos, blocked: frozenset[Pos], cache: dict[Pos, frozenset[Pos]]) -> frozenset[Pos] | None:
            if start not in cache:
                result = reachable(self.world, start, blocked, deadline)
                if result is None:
                    return None
                cache.update((cell, result) for cell in result)
            return cache[start]

        def can_interact(cells: frozenset[Pos], target: frozenset[Pos]) -> bool:
            return any(neighbor in cells and neighbor not in target for cell in target for neighbor in cell.neighbors())

        usable_new = set()
        for actor in self.world.actors:
            before = component(actor.pos, self.blocked, self.before)
            current = component(destinations.get(actor.id, actor.pos), self.blocked | placements, after)
            if before is None or current is None:
                return False
            if any(can_interact(before, group) and not can_interact(current, group) for group in self.groups):
                self.cache[key] = False
                return False
            usable_new.update(index for index, group in enumerate(new_groups) if can_interact(current, group))
        accepted = len(usable_new) == len(new_groups)
        self.cache[key] = accepted
        return accepted


class FortifyBase:
    id = "fortify-base"

    def __init__(self, wall_budget: int = 6) -> None:
        self.wall_budget = wall_budget

    def propose(self, context: Context) -> tuple[Candidate, ...]:
        from .playbooks import approach_or_act
        from .planning import Candidate, Value

        world = context.world
        if not context.rules.daytime(world.observation.round_no) or not context.rules.inferred_build_rings or len(world.weapons) < 2:
            return ()
        wall_count = sum(unit.kind == "wall" and unit.alive for unit in world.our)
        cap = min(self.wall_budget, context.rules.max_walls)
        if wall_count >= cap:
            return ()
        sites = world.build_ring("wall") - world.blockers()
        proposals = []
        for actor in world.actors:
            if actor.kind != "worker" or "stone" not in (actor.backpack or ()):
                continue
            for target in sorted(sites, key=lambda pos: (actor.pos.distance(pos), pos))[:6]:
                if context.expired:
                    break
                step = approach_or_act(context, actor, frozenset({target}), Action(actor.id, "build", (target,), "wall"))
                if step:
                    action, eta = step
                    if eta > context.rules.daylight_left(world.observation.round_no):
                        continue
                    for slot in range(wall_count, cap):
                        proposals.append(Candidate(f"wall:{target.x}:{target.y}", self.id, frozenset({actor.id}), (action,),
                                                  Value(gold=-world.sale_prices.get("stone", 1), readiness=8 / eta,
                                                        risk=context.risk(actor.pos), occupied_turns=eta),
                                                  "build-wall" if action.kind == "build" else "approach-wall-site",
                                                  ("private-stone-owned", "limited-wall-investment", "inferred-build-mask"),
                                                  frozenset({f"site:{target.x}:{target.y}", f"wall-investment-slot:{slot}"})))
        return tuple(proposals)
