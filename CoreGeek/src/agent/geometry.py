"""Eight-neighbor paths to interaction cells; diagonal corner cutting is legal."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import time

from .world import Pos, World


@dataclass(frozen=True)
class Route:
    cells: tuple[Pos, ...]

    @property
    def distance(self) -> int:
        return len(self.cells) - 1

    @property
    def next_step(self) -> Pos | None:
        return self.cells[1] if len(self.cells) > 1 else None


def interaction_cells(world: World, targets: frozenset[Pos], blocked: frozenset[Pos]) -> frozenset[Pos]:
    return frozenset(pos for target in targets for pos in target.neighbors() if world.inside(pos) and pos not in blocked)


def shortest_route(world: World, start: Pos, goals: frozenset[Pos], blocked: frozenset[Pos], *, deadline: float | None = None) -> Route | None:
    if not world.inside(start):
        return None
    goals = frozenset(goal for goal in goals if world.inside(goal) and (goal not in blocked or goal == start))
    if start in goals:
        return Route((start,))
    if not goals:
        return None
    previous: dict[Pos, Pos | None] = {start: None}
    frontier = deque([start])
    while frontier:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        current = frontier.popleft()
        for neighbor in current.neighbors():
            if not world.inside(neighbor) or neighbor in blocked or neighbor in previous:
                continue
            previous[neighbor] = current
            if neighbor in goals:
                path = [neighbor]
                while previous[path[-1]] is not None:
                    path.append(previous[path[-1]])
                return Route(tuple(reversed(path)))
            frontier.append(neighbor)
    return None


def route_to_interact(world: World, actor_id: str, targets: frozenset[Pos]) -> Route | None:
    actor = next(unit for unit in world.actors if unit.id == actor_id)
    blocked = world.blockers(exclude_actors=frozenset({actor_id}))
    return shortest_route(world, actor.pos, interaction_cells(world, targets, blocked), blocked)
