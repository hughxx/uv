from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "CoreGeek" / "src"))

from agent.protocol import Observation
from agent.world import World


def unit(identifier=10010, kind="worker", pos=(4, 5), **changes):
    raw = {"id": identifier, "roleType": kind, "pos": dict(zip(("x", "y"), pos)),
           "health": 220, "backpack": [], "backPackCapability": 100}
    if kind not in {"worker", "pioneer"}:
        raw.update(level=1, health=1000)
    raw.update(changes)
    return raw


def packet(roles=None, *, round_no=1, gold=75, zones=None, enemy=None, robots=None, **changes):
    if roles is None:
        roles = [unit(10013, "station", (5, 6), health=1500), unit()]
    raw = {"roundNo": round_no, "mapInfo": {"width": 20, "height": 20, "zones": zones or []},
           "teamOur": {"type": "challenger", "teamId": "test", "goldNum": gold, "roles": roles},
           "teamEnemy": {"roles": enemy or []}, "robot": {"roles": robots or []},
           "vendorShopList": [{"name": "stone", "price": 1}, {"name": "copper", "price": 5}],
           "weaponShopList": [{"name": "Medicine", "price": 10}], "phaseTask": ""}
    raw.update(changes)
    return raw


def world(*args, **kwargs):
    return World.from_observation(Observation.from_payload(packet(*args, **kwargs)))
