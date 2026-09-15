"""Bounded, stdout-based online diagnostics; never log raw task/tool contents."""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import math
import queue
import sys
import traceback
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .application import AgentApplication

SCHEMA = "coregeek-online-v1"
MAX_EVENT_BYTES = 8192


class NonBlockingHandler(logging.handlers.QueueHandler):
    def __init__(self, records: queue.Queue) -> None:
        super().__init__(records)
        self.dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1
            return
        if self.dropped:
            notice = logging.LogRecord("agent.server", logging.WARNING, "", 0,
                                       json.dumps({"schema": SCHEMA, "event": "logs_dropped", "count": self.dropped}), (), None)
            try:
                self.queue.put_nowait(notice)
            except queue.Full:
                return
            self.dropped = 0


class RuntimeLogging:
    def __init__(self) -> None:
        records: queue.Queue = queue.Queue(maxsize=256)
        self.handler = NonBlockingHandler(records)
        output = logging.StreamHandler(sys.stdout)
        output.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        self.listener = logging.handlers.QueueListener(records, output)
        logging.basicConfig(handlers=[self.handler], level=logging.INFO, force=True)
        self.listener.start()

    def close(self) -> None:
        # Do not hang process shutdown if the platform has stopped draining its
        # stdout pipe. Normal StreamHandler emits flush after every record.
        try:
            self.listener.enqueue_sentinel()
        except queue.Full:
            return
        thread = self.listener._thread
        if thread is not None:
            thread.join(timeout=0.5)


def configure_logging() -> RuntimeLogging:
    return RuntimeLogging()


def runtime_fingerprint() -> str:
    try:
        source = Path(__file__).resolve().parent
        project = source.parent.parent
        paths = sorted(source.rglob("*.py")) + [project / name for name in ("main3.py", "run.sh", "pyproject.toml")]
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.relative_to(project).as_posix().encode("utf-8"))
            digest.update(path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").encode("utf-8"))
        return digest.hexdigest()[:16]
    except OSError:
        return "unavailable"


def exception_location(error: BaseException) -> dict[str, Any]:
    return {"type": type(error).__name__,
            "frames": [f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                       for frame in traceback.extract_tb(error.__traceback__)[-6:]]}


def clipped(value: Any, depth: int = 6) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if type(value) in (int, float):
        try:
            return value if math.isfinite(value) else "nonfinite"
        except OverflowError:
            return "oversized-number"
    if isinstance(value, str):
        return value[:192] + ("...[clipped]" if len(value) > 192 else "")
    if depth <= 0:
        return "[depth-limit]"
    if isinstance(value, dict):
        result = {str(key)[:64]: clipped(item, depth - 1) for key, item in list(value.items())[:40]}
        if len(value) > 40:
            result["_omittedFields"] = len(value) - 40
        return result
    if isinstance(value, (tuple, list)):
        result = [clipped(item, depth - 1) for item in value[:32]]
        if len(value) > 32:
            result.append({"omittedItems": len(value) - 32})
        return result
    return {"type": type(value).__name__}


def event_text(event: str, **fields: Any) -> str:
    record = {"schema": SCHEMA, "event": event, **clipped(fields)}
    protected = {"schema", "event", "id", "round", "http", "status", "reason", "error", "elapsedMs", "actionCount", "omittedFields"}
    while True:
        encoded = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(encoded) <= MAX_EVENT_BYTES:
            return encoded
        removable = [key for key in record if key not in protected]
        if not removable:
            return json.dumps({"schema": SCHEMA, "event": event, "reason": "event_size_limit"})
        key = max(removable, key=lambda name: len(json.dumps(record[name], ensure_ascii=True)))
        del record[key]
        record.setdefault("omittedFields", []).append(key)


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    try:
        logger.log(level, "%s", event_text(event, **fields))
    except Exception:
        # Observability must not change a valid game response or its receipt.
        pass


def number(value: Any) -> int | float | bool | str | None:
    if type(value) in (int, float, bool):
        return clipped(value)
    return None


def position(value: Any) -> Any:
    return [number(value.get("x")), number(value.get("y"))] if isinstance(value, dict) else {"type": type(value).__name__}


def input_outline(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"rootType": type(payload).__name__}
    result: dict[str, Any] = {"round": number(payload.get("roundNo")), "roundType": type(payload.get("roundNo")).__name__,
                              "keys": list(payload)[:24], "missing": [key for key in ("roundNo", "mapInfo", "teamOur") if key not in payload],
                              "nestedKeys": {key: list(value)[:20] for key, value in list(payload.items())[:24] if isinstance(value, dict)}}
    board = payload.get("mapInfo")
    if isinstance(board, dict):
        zones = board.get("zones")
        result["map"] = {"width": number(board.get("width")), "height": number(board.get("height")),
                         "zonesType": type(zones).__name__, "zoneCount": len(zones) if isinstance(zones, list) else None}
    team = payload.get("teamOur")
    if isinstance(team, dict):
        rows = team.get("roles")
        roles = [row for row in rows[:64] if isinstance(row, dict)] if isinstance(rows, list) else []
        important = sorted(roles, key=lambda row: row.get("roleType") not in ("worker", "pioneer", "station"))[:8]
        result["our"] = {"side": team.get("type") if isinstance(team.get("type"), str) else None,
                         "gold": number(team.get("goldNum")), "goldType": type(team.get("goldNum")).__name__,
                         "rolesType": type(rows).__name__, "roleCount": len(rows) if isinstance(rows, list) else None,
                         "healthTypes": dict(Counter(type(row.get("health")).__name__ for row in roles)),
                         "units": [{"id": number(row.get("id")), "idType": type(row.get("id")).__name__,
                                    "kind": row.get("roleType") if isinstance(row.get("roleType"), str) else None,
                                    "pos": position(row.get("pos")), "health": number(row.get("health")), "level": number(row.get("level")),
                                    "bagSize": len(row["backpack"]) if isinstance(row.get("backpack"), list) else None} for row in important]}
        tasks = team.get("playerTasks")
        result["tasksType"] = type(tasks).__name__
        result["tasks"] = [{"pos": position(task.get("taskPosition")), "valid": number(task.get("isValid")),
                            "cooldown": number(task.get("coldDownRounds")), "score": number(task.get("scoreReward")),
                            "gold": number(task.get("goldReward"))} for task in tasks[:8] if isinstance(task, dict)] if isinstance(tasks, list) else []
    for name in ("phaseTask", "llmResp", "lastCmdResult"):
        value = payload.get(name)
        result[name] = {"type": type(value).__name__, "chars": len(value) if isinstance(value, str) else None}
    return result


def response_outline(response: dict[str, Any]) -> dict[str, Any]:
    commands = response.get("roleCommandMap", {})
    allowed = {"action", "controllerId", "targetPos", "name", "num", "item"}
    actions = {}
    if isinstance(commands, dict):
        for key, command in list(commands.items())[:8]:
            if isinstance(command, dict):
                actions[str(key)] = {name: value for name, value in command.items() if name in allowed}
                if "taskAnswer" in command:
                    actions[str(key)]["taskAnswerChars"] = len(command["taskAnswer"]) if isinstance(command["taskAnswer"], str) else None
    return {"actionCount": len(commands) if isinstance(commands, dict) else None, "actions": actions,
            "promptChars": len(response.get("prompt", "")), "executeCmdChars": len(response.get("executeCmd", ""))}


def decision_outline(application: AgentApplication) -> dict[str, Any]:
    from .actions import weapon_range
    from .strategy_policy import DefensivePostPolicy
    from .tasks import task_diagnostics

    result: dict[str, Any] = {"status": application.last_status, "revision": application.memory.revision}
    decision = application.engine.last_decision
    if application.last_status not in {"new", "new_session"} or decision is None:
        return result
    world, plan = decision.world, decision.plan
    build_cells = world.build_ring("gatling")
    free_sites = build_cells - world.blockers()
    build_gates = []
    if not decision.rules.daytime(world.observation.round_no):
        build_gates.append("not_daytime")
    if not decision.rules.inferred_build_rings:
        build_gates.append("construction_mask_disabled")
    if not any(actor.kind == "worker" for actor in world.actors):
        build_gates.append("no_living_worker")
    if world.gold is None or world.gold < decision.rules.weapon_cost:
        build_gates.append("gold_unknown_or_insufficient")
    if len(world.weapons) >= decision.rules.max_weapons:
        build_gates.append("weapon_cap")
    if not world.station:
        build_gates.append("no_living_station")
    if not free_sites:
        build_gates.append("no_free_build_site")
    feedback = world.observation.raw.get("lastRoundRoleActionResults", {})
    errors = world.observation.raw.get("errors", [])
    hostile = [robot for robot in world.robots if robot.alive and robot.target_team in {None, world.side}]
    station = world.station
    distance_to_base = lambda robot: min(robot.pos.distance(cell) for cell in station.cells) if station else None
    nearest = sorted(hostile, key=lambda robot: (distance_to_base(robot) if station else 0, robot.id))[:6]
    moves = {action.actor_id: action.targets[0] for action in plan.actions if action.kind == "move"}
    defense_offers = {identifier: {"approach": approach, "hold": hold, "fire": fire}
                      for identifier, approach, hold, fire in decision.defense_offers}
    defense = []
    for weapon in world.weapons[:3]:
        attack_range = weapon_range(weapon)
        defense.append({"weapon": weapon.id, "kind": weapon.kind, "pos": [weapon.pos.x, weapon.pos.y],
                        "level": weapon.level, "range": attack_range, "cooldown": weapon.cooldown,
                        "adjacent": [actor.id for actor in world.actors if actor.pos.distance(weapon.pos) == 1],
                        "nextAdjacent": [actor.id for actor in world.actors if moves.get(actor.id, actor.pos).distance(weapon.pos) == 1],
                        "inRangeRobots": sum(0 < robot.pos.distance(weapon.pos) <= attack_range for robot in hostile) if attack_range is not None else None,
                        "offers": defense_offers.get(weapon.id, {}),
                        "selected": [[sorted(candidate.actors), candidate.stage] for candidate in plan.candidates if candidate.key == f"defend:{weapon.id}"]})
    posts = DefensivePostPolicy(world, decision.rules)
    result.update({"day": decision.rules.day(world.observation.round_no), "daytime": decision.rules.daytime(world.observation.round_no),
                   "decisionMs": round(application.last_elapsed_seconds * 1000, 3), "utilityEstimate": plan.utility,
                   "projectedRoleLosses": plan.projected_role_losses, "threatenedPostsLost": plan.threatened_posts_lost,
                   "livingActors": len(world.actors), "weapons": len(world.weapons), "robots": len(world.robots),
                   "robotSnapshot": {"hostileLiving": len(hostile), "kinds": dict(Counter(robot.kind for robot in hostile)),
                                     "nearest": [{"id": robot.id, "kind": robot.kind, "pos": [robot.pos.x, robot.pos.y],
                                                  "health": robot.health, "baseDistance": distance_to_base(robot),
                                                  "state": robot.abnormal_state} for robot in nearest], "omitted": max(0, len(hostile) - len(nearest))},
                   "defense": defense, "staffing": {"now": posts.staffed(()), "next": posts.staffed(plan.actions),
                                                      "shotsIssued": sum(action.kind == "attack" for action in plan.actions)},
                   "candidates": decision.candidate_count, "offers": dict(decision.offers),
                   "buildCheck": {"gates": build_gates, "ringCells": len(build_cells), "freeSites": len(free_sites)},
                   "selected": [[candidate.definition, candidate.stage, sorted(candidate.actors)] for candidate in plan.candidates],
                   "search": {"visited": plan.visited, "exhausted": plan.exhausted, "rejections": dict(plan.rejections)},
                   "emptyReason": plan.empty_reason if not plan.actions else "",
                   "diagnostics": decision.diagnostics,
                   "failedPreviousActions": [str(key) for key, value in feedback.items() if value is False] if isinstance(feedback, dict) else [],
                   "errorCodes": [error["errorCode"] for error in errors if isinstance(error, dict) and type(error.get("errorCode")) is int] if isinstance(errors, list) else [],
                   "taskPhase": decision.task_state.phase if decision.task_state else "inactive",
                   "task": task_diagnostics(decision.task_state) if decision.task_state else None})
    return result


def startup_fields(application: AgentApplication) -> dict[str, Any]:
    return {"python": sys.version.split()[0], "build": runtime_fingerprint(), "logStream": "stdout",
            "rules": asdict(application.engine.rules), "decisionBudgetMs": application.engine.budget_seconds * 1000,
            "taskMaxRequests": application.engine.task_max_requests, "maxEventBytes": MAX_EVENT_BYTES}
