"""Typed views of known fields; the original observation remains available."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .protocol import InvalidObservation, Observation

ACTORS = frozenset({"worker", "pioneer"})
WEAPONS = frozenset({"gatling", "railgun", "rocket"})
MINERALS = frozenset({"stone", "iron", "copper"})


def integer(value: Any) -> int | None:
    return value if type(value) is int else None


@dataclass(frozen=True, order=True)
class Pos:
    x: int
    y: int

    @classmethod
    def parse(cls, raw: Any) -> Pos:
        if not isinstance(raw, dict) or integer(raw.get("x")) is None or integer(raw.get("y")) is None:
            raise InvalidObservation("invalid position")
        return cls(raw["x"], raw["y"])

    def distance(self, other: Pos) -> int:
        return max(abs(self.x - other.x), abs(self.y - other.y))

    def neighbors(self) -> tuple[Pos, ...]:
        return tuple(Pos(self.x + dx, self.y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy)

    def payload(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True)
class Unit:
    id: str
    kind: str
    pos: Pos
    health: int | None
    level: int | None
    backpack: tuple[str, ...] | None
    capacity: int | None
    attack_range: int | None
    cooldown: int | None
    target_team: str | None
    abnormal_state: str | None

    @classmethod
    def parse(cls, raw: Any) -> Unit:
        if not isinstance(raw, dict) or integer(raw.get("id")) is None or not isinstance(raw.get("roleType"), str):
            raise InvalidObservation("invalid unit")
        backpack = raw.get("backpack")
        if backpack is not None and (not isinstance(backpack, list) or not all(isinstance(item, str) for item in backpack)):
            raise InvalidObservation("invalid backpack")
        return cls(
            str(raw["id"]), raw["roleType"], Pos.parse(raw.get("pos")), integer(raw.get("health")),
            integer(raw.get("level")), tuple(backpack) if backpack is not None else None,
            integer(raw.get("backPackCapability")), integer(raw.get("attackRange")), integer(raw.get("cooldown")),
            raw.get("targetTeam") if isinstance(raw.get("targetTeam"), str) else None,
            raw.get("abnormalState") if isinstance(raw.get("abnormalState"), str) else None,
        )

    @property
    def alive(self) -> bool:
        return self.health is not None and self.health > 0

    @property
    def cells(self) -> frozenset[Pos]:
        if self.kind == "station":
            return frozenset(Pos(self.pos.x + dx, self.pos.y - dy) for dx in (0, 1) for dy in (0, 1))
        return frozenset({self.pos})

    @property
    def free_slots(self) -> int | None:
        if self.backpack is None or self.capacity is None:
            return None
        return max(0, self.capacity - len(self.backpack))


@dataclass(frozen=True)
class Zone:
    kind: str
    pos: Pos


@dataclass(frozen=True)
class RuleProfile:
    round_origin: int = 1
    day_length: int = 70
    night_length: int = 60
    max_weapons: int = 3
    max_walls: int = 20
    weapon_cost: int = 25
    inferred_build_rings: bool = True
    repeated_attack_targets: bool = False
    guard_projected_role_deaths: bool = True
    preserve_build_access: bool = True
    preserve_threatened_posts: bool = True
    joint_follow_moves: bool = True

    def day(self, round_no: int) -> int:
        return (round_no - self.round_origin) // (self.day_length + self.night_length) + 1

    def daytime(self, round_no: int) -> bool:
        return round_no >= self.round_origin and (round_no - self.round_origin) % (self.day_length + self.night_length) < self.day_length

    def daylight_left(self, round_no: int) -> int:
        if not self.daytime(round_no):
            return 0
        return self.day_length - (round_no - self.round_origin) % (self.day_length + self.night_length)


@dataclass(frozen=True)
class World:
    observation: Observation
    width: int
    height: int
    side: str
    team_id: str
    gold: int | None
    our: tuple[Unit, ...]
    enemy: tuple[Unit, ...]
    robots: tuple[Unit, ...]
    zones: tuple[Zone, ...]
    prices: Mapping[str, int]
    sale_prices: Mapping[str, int]

    @classmethod
    def from_observation(cls, observation: Observation) -> World:
        raw = observation.raw
        map_info, our = raw.get("mapInfo"), raw.get("teamOur")
        if not isinstance(map_info, dict) or not isinstance(our, dict):
            raise InvalidObservation("mapInfo and teamOur are required for planning")
        width, height = integer(map_info.get("width")), integer(map_info.get("height"))
        if width is None or height is None or not 1 <= width <= 256 or not 1 <= height <= 256:
            raise InvalidObservation("invalid map dimensions")
        if our.get("type") not in {"challenger", "defender"}:
            raise InvalidObservation("unknown team side")

        def units(team: Any) -> tuple[Unit, ...]:
            if not isinstance(team, dict) or not isinstance(team.get("roles", []), list):
                raise InvalidObservation("invalid team units")
            return tuple(Unit.parse(unit) for unit in team.get("roles", []))

        def prices(key: str) -> Mapping[str, int]:
            result = {}
            for item in raw.get(key, []):
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    price = integer(item.get("price"))
                    if price is not None and price >= 0:
                        result[item["name"]] = price
            return MappingProxyType(result)

        zones = tuple(Zone(zone["neutralType"], Pos.parse(zone.get("pos"))) for zone in map_info.get("zones", []))
        world = cls(observation, width, height, our["type"], str(our.get("teamId", "")), integer(our.get("goldNum")),
                    units(our), units(raw.get("teamEnemy", {})), units(raw.get("robot", {})), zones,
                    prices("weaponShopList"), prices("vendorShopList"))
        ids = [unit.id for unit in (*world.our, *world.enemy, *world.robots)]
        if len(ids) != len(set(ids)):
            raise InvalidObservation("duplicate unit IDs")
        if any(not world.inside(cell) for unit in (*world.our, *world.enemy, *world.robots) for cell in unit.cells):
            raise InvalidObservation("unit outside map")
        if any(not world.inside(zone.pos) for zone in zones):
            raise InvalidObservation("zone outside map")
        return world

    def inside(self, pos: Pos) -> bool:
        return 0 <= pos.x < self.width and 0 <= pos.y < self.height

    @property
    def actors(self) -> tuple[Unit, ...]:
        return tuple(sorted((unit for unit in self.our if unit.kind in ACTORS and unit.alive), key=lambda unit: unit.id))

    @property
    def weapons(self) -> tuple[Unit, ...]:
        return tuple(unit for unit in self.our if unit.kind in WEAPONS and unit.alive)

    @property
    def station(self) -> Unit | None:
        return next((unit for unit in self.our if unit.kind == "station" and unit.alive), None)

    @property
    def phase_task(self) -> str:
        value = self.observation.raw.get("phaseTask")
        return value if isinstance(value, str) else ""

    def neutral(self, kind: str) -> tuple[Pos, ...]:
        return tuple(zone.pos for zone in self.zones if zone.kind == kind)

    def task_cells(self, task: dict[str, Any]) -> frozenset[Pos]:
        anchor = Pos.parse(task.get("taskPosition"))
        kind = next((zone.kind for zone in self.zones if zone.pos == anchor and zone.kind.startswith(self.side + "TaskPoint")), None)
        return frozenset(self.neutral(kind)) if kind else frozenset({anchor})

    def task_stay_cells(self, actor: Unit) -> frozenset[Pos]:
        """Safe task-preserving stands; ambiguous nearby points use intersection."""
        possible = [self.task_cells(task) for task in self.observation.raw["teamOur"].get("playerTasks", [])]
        possible = [cells for cells in possible if min(actor.pos.distance(cell) for cell in cells) == 1]
        if not self.phase_task or actor.kind != "pioneer" or not possible:
            return frozenset()
        stands = [{cell for target in cells for cell in target.neighbors()
                   if self.inside(cell) and cell not in cells} for cells in possible]
        return frozenset(set.intersection(*stands))

    def blockers(self, *, exclude_actors: frozenset[str] = frozenset()) -> frozenset[Pos]:
        cells = {zone.pos for zone in self.zones}
        for unit in (*self.our, *self.enemy, *self.robots):
            if unit.health is not None and unit.health <= 0:
                continue
            if unit in self.our and unit.kind in ACTORS and unit.id in exclude_actors:
                continue
            cells.update(unit.cells)
        return frozenset(cells)

    def build_ring(self, kind: str) -> frozenset[Pos]:
        base = self.station
        if base is None:
            return frozenset()
        distance = 2 if kind == "wall" else 1
        return frozenset(Pos(x, y) for x in range(base.pos.x - distance, base.pos.x + 2 + distance)
                         for y in range(base.pos.y - 1 - distance, base.pos.y + 1 + distance)
                         if self.inside(Pos(x, y)) and min(Pos(x, y).distance(cell) for cell in base.cells) == distance)
