#!/usr/bin/env python3
"""Offline, allowlisted log summary and paged evidence extraction (stdlib only)."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import sqlite3
import sys
from collections import Counter, deque
from contextlib import closing
from pathlib import Path

SCHEMA = "coregeek-online-v1"
CACHE_VERSION = "1"
MAX_OBJECT_CHARS = 256 * 1024
OVERVIEW_BYTES = 16 * 1024
EVENTS = {"listening", "request_received", "turn", "http_rejected", "telemetry_error", "fatal", "logs_dropped"}
KINDS = {"worker", "pioneer", "station", "wall", "gatling", "railgun", "rocket", "smallRobot", "middleRobot", "largeRobot", "bossRobot"}
ACTIONS = {"move", "build", "remove", "collect", "sell", "buy", "use", "drop", "attack", "acceptTask", "submitAnswer", "summonTreasure"}
PLAYBOOKS = {"acquire-task", "submit-task-answer", "area-consumables", "build-defense", "cash-inventory", "evade-lethal-threat",
             "fortify-base", "harvest-minerals", "invest-upgrades", "operate-defense", "restock-medicine", "use-inventory"}
STAGES = {"build", "approach-site", "approach-task", "accept", "submit", "approach-weapon", "hold-weapon", "fire", "evade",
          "collect", "approach-mineral", "sell", "approach-vendor", "buy-voucher", "buy-medicine", "approach-shop", "heal",
          "upgrade", "deliver-upgrade", "repair", "deliver-repair", "use-area-item", "build-wall", "approach-wall-site"}
PHASES = {"inactive", "need-llm", "waiting-llm", "waiting-command", "answer-ready", "submitted", "budget-exhausted"}
STATUSES = {"new", "new_session", "cached", "probe", "stale_session", "conflicting_or_stale", "error", "rejected", "not-started"}
REASONS = {"", "unsupported_transfer_encoding", "content_length_required", "invalid_content_length", "request_too_large",
           "request_timeout", "invalid_json", "decision_busy", "invalid_observation", "decision_exception",
           "missing_game_fields_treated_as_probe", "unsupported_method", "malformed_http", "event_size_limit"}
GATES = {"not_daytime", "construction_mask_disabled", "no_living_worker", "gold_unknown_or_insufficient", "weapon_cap", "no_living_station", "no_free_build_site"}
ITEMS = {"gatling", "railgun", "rocket", "wall", "stone", "iron", "copper", "Medicine", "medicine", "WallFixer", "Bomb", "DizzyWeapon"}
ITEMS |= {f"{kind}UpgradeVoucher{level}" for kind in ("Weapon", "Station", "Wall") for level in (1, 2)}
FIELDS = {"roundNo", "mapInfo", "teamOur", "teamEnemy", "robot", "phaseTask", "llmResp", "lastCmdResult", "errors",
          "lastRoundRoleActionResults", "worldNews", "vendorShopList", "weaponShopList", "lastSummonTreasureResult"}


def number(value):
    return value if type(value) in (int, float) and abs(value) < 10**15 and math.isfinite(value) else None


def identifier(value):
    text = str(value)
    return text if re.fullmatch(r"[0-9]{1,20}", text) else None


def choice(allowed):
    return lambda value: value if isinstance(value, str) and value in allowed else "unknown"


def boolean(value):
    return value if type(value) is bool else None


def sequence(rule, limit=32):
    return lambda value: [clean(item, rule) for item in value[:limit]] if isinstance(value, list) else None


def mapping(key_rule, value_rule, limit=64):
    def convert(value):
        if not isinstance(value, dict):
            return None
        return {key_rule(key): clean(item, value_rule) for key, item in list(value.items())[:limit] if key_rule(key) is not None}
    return convert


def clean(value, rule):
    if isinstance(rule, dict):
        return {key: clean(value[key], subrule) for key, subrule in rule.items() if key in value} if isinstance(value, dict) else None
    return rule(value)


def selected(value):
    result = []
    if isinstance(value, list):
        for row in value[:8]:
            if isinstance(row, list) and len(row) == 3:
                result.append([choice(PLAYBOOKS)(row[0]), choice(STAGES)(row[1]), sequence(identifier, 8)(row[2]) or []])
    return result


def rejection_key(key):
    allowed = {"invalid_or_expired_candidate", "shared_gold", "weapon_slots", "layout_or_deadline", "not_preferred_by_policy_or_utility"}
    if key in allowed or re.fullmatch(r"action_validation@actions\.py:\d{1,6}", str(key)):
        return key
    return None


def diagnostic(value):
    parts = str(value).split(":")
    if len(parts) == 2 and parts[0] == "execution-cooldown" and parts[1].isdigit():
        return ":".join(parts)
    if len(parts) >= 2 and parts[0] in {"budget", "invalid-candidate", "error"} and parts[1] in PLAYBOOKS:
        return ":".join(parts[:2])  # Exception messages or arbitrary suffixes are never copied.
    return "unknown"


POINT = {"x": number, "y": number}
TYPES = choice({"int", "float", "str", "bool", "NoneType", "dict", "list"})
UNIT = {"id": identifier, "idType": TYPES, "kind": choice(KINDS), "pos": sequence(number, 2), "health": number,
        "level": number, "bagSize": number, "baseDistance": number, "state": choice({"normal", "dizzy", ""})}
ACTION = {"action": choice(ACTIONS), "controllerId": identifier, "targetPos": sequence(POINT, 8),
          "name": choice(ITEMS), "num": number, "taskAnswerChars": number}
TASK = {**{key: number for key in ("started", "requestsUsed", "llmRequests", "commandRequests", "commandId", "commandChars", "sameCommandExecutions")},
        "startReason": choice({"new-active-task", "task-text-changed"}), "repeatedWithoutProgress": boolean,
        "lastEvent": choice({"started", "llm-requested", "command-requested", "answer-ready", "answer-submitted", "budget-exhausted",
                             "answer-budget-reserved", "repeated-command-blocked", "invalid-answer", "invalid-reply-fields",
                             "llm-correlation-timeout", "command-correlation-timeout"}),
        "replyStatus": choice({"not-observed", "empty", "invalid-json", "not-object", "request-id-mismatch", "same-round", "matched-execute", "matched-answer", "matched-unknown-kind"}),
        "result": {"status": choice({"awaiting", "exit", "timeout", "judger-error", "unclassified", "uncorrelated-timeout"}),
                   "exitCode": number, "truncated": boolean, "chars": number}}
INPUT = {"round": number, "roundType": TYPES, "keys": sequence(choice(FIELDS), 24), "missing": sequence(choice(FIELDS), 24),
         "map": {"width": number, "height": number, "zonesType": TYPES, "zoneCount": number},
         "our": {"side": choice({"challenger", "defender"}), "gold": number, "goldType": TYPES, "rolesType": TYPES, "roleCount": number,
                 "healthTypes": mapping(lambda key: key if key in {"int", "float", "str", "bool", "NoneType", "dict", "list"} else None, number), "units": sequence(UNIT, 8)},
         "tasksType": TYPES, "tasks": sequence({"pos": sequence(number, 2), "valid": boolean, "cooldown": number, "score": number, "gold": number}, 8),
         **{key: {"type": TYPES, "chars": number} for key in ("phaseTask", "llmResp", "lastCmdResult")}}
DEFENSE = {"weapon": identifier, "kind": choice(KINDS), "pos": sequence(number, 2), "level": number, "range": number, "cooldown": number,
           "adjacent": sequence(identifier, 8), "nextAdjacent": sequence(identifier, 8), "inRangeRobots": number,
           "offers": {"approach": number, "hold": number, "fire": number},
           "selected": sequence(lambda row: [sequence(identifier, 8)(row[0]), choice(STAGES)(row[1])] if isinstance(row, list) and len(row) == 2 else None, 8)}
DECISION = {**{key: number for key in ("revision", "day", "decisionMs", "utilityEstimate", "projectedRoleLosses", "threatenedPostsLost", "livingActors", "weapons", "robots", "candidates")},
            "status": choice(STATUSES), "daytime": boolean, "offers": mapping(lambda key: key if key in PLAYBOOKS else None, number),
            "buildCheck": {"gates": sequence(choice(GATES), 12), "ringCells": number, "freeSites": number}, "selected": selected,
            "search": {"visited": number, "exhausted": boolean, "rejections": mapping(rejection_key, number)},
            "emptyReason": choice({"", "selected_hold", "no_living_actors", "search_budget", "no_candidates", "no_feasible_action", "wait_preferred"}),
            "diagnostics": sequence(diagnostic), "failedPreviousActions": sequence(identifier), "errorCodes": sequence(number),
            "taskPhase": choice(PHASES), "task": TASK, "defense": sequence(DEFENSE, 3),
            "staffing": {"now": number, "next": number, "shotsIssued": number},
            "robotSnapshot": {"hostileLiving": number, "kinds": mapping(lambda key: key if key in KINDS else None, number), "nearest": sequence(UNIT, 6), "omitted": number}}
RULES = {**{key: number for key in ("round_origin", "day_length", "night_length", "max_weapons", "max_walls", "weapon_cost")},
         **{key: boolean for key in ("inferred_build_rings", "repeated_attack_targets", "guard_projected_role_deaths", "preserve_build_access", "preserve_threatened_posts", "joint_follow_moves")}}
EVENT_SPEC = {"event": choice(EVENTS), "id": identifier, "round": number, "http": number, "status": choice(STATUSES), "reason": choice(REASONS),
              "responseWritten": boolean, "elapsedMs": number, "actionCount": number, "promptChars": number, "executeCmdChars": number,
              "actions": mapping(identifier, ACTION, 8), "input": INPUT, "decision": DECISION,
              "build": lambda value: value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{16}", value) else "unknown",
              "python": lambda value: value if isinstance(value, str) and re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", value) else "unknown",
              "rules": RULES, "decisionBudgetMs": number, "taskMaxRequests": number, "count": number,
              "declaredBytes": number, "transferEncodingPresent": boolean,
              "method": choice({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "other"}),
              "error": {"type": choice({"InvalidObservation", "InvalidAction", "RuntimeError", "ValueError", "TypeError", "KeyError", "IndexError", "OSError", "TimeoutError", "RecursionError", "OverflowError"}),
                        "frames": sequence(lambda value: value if isinstance(value, str) and re.fullmatch(r"(?:application|actions|world|strategy|tasks|server|telemetry|planning|main3)\.py:\d{1,6}:[a-z_]{1,64}", value) else "[location-redacted]", 6)},
              "omittedFields": sequence(choice({"input", "decision", "actions", "error", "rules", "responseBytes", "promptChars", "executeCmdChars", "responseWritten"}))}


def objects(source, stats):
    """Recognize balanced objects across chunks/lines, including glued events.

    At a fresh schema start, recover from a truncated preceding object. Never
    evaluate Python reprs or shell content. The capture buffer is bounded.
    """
    buffer = []
    depth = 0
    quoted = escaped = False
    line = start = 1
    prefix = deque(maxlen=12)
    for chunk in iter(lambda: source.read(65536), ""):
        stats["characters"] += len(chunk)
        stats["replacementCharacters"] += chunk.count("\ufffd")
        for char in chunk:
            if not depth:
                if char == "{":
                    buffer, depth, start = [char], 1, line
                    quoted = escaped = False
                elif char == "\n":
                    line += 1
                continue
            buffer.append(char)
            prefix.append(char)
            if char == "\n":
                line += 1
            if len(buffer) > MAX_OBJECT_CHARS:
                stats["oversizedObjects"] += 1
                buffer, depth = [], 0
                prefix.clear()
                continue
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield start, "".join(buffer)
                    buffer = []
                    prefix.clear()
            # All online events begin with schema. A second such literal
            # prefix inside a broken object signals a new recoverable event.
            if char == ":" and depth and len(buffer) > 10 and "".join(prefix).endswith('{"schema":'):
                stats["recoveredTruncations"] += 1
                buffer, depth, start = list('{"schema":'), 1, line
                quoted = escaped = False
                prefix.clear()
    if depth:
        stats["incompleteObjects"] += 1
    stats["lines"] = line


def decode_events(text, stats, depth=0):
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        stats["invalidJsonObjects"] += 1
        return
    if isinstance(value, dict) and value.get("schema") == SCHEMA and isinstance(value.get("event"), str) and value["event"] in EVENTS:
        yield clean(value, EVENT_SPEC)
        return
    if isinstance(value, dict) and depth < 2:
        for key in ("message", "log", "content", "text"):
            wrapped = value.get(key)
            if isinstance(wrapped, str) and SCHEMA in wrapped:
                inner_stats = Counter()
                for _, nested in objects(io.StringIO(wrapped), inner_stats):
                    yield from decode_events(nested, stats, depth + 1)
                for key in ("oversizedObjects", "recoveredTruncations", "incompleteObjects"):
                    stats["wrapped_" + key] += inner_stats[key]
                return
    stats["unrecognizedObjects"] += 1


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def open_source(path, encoding):
    if encoding == "auto":
        with path.open("rb") as source:
            bom = source.read(4)
        encoding = "utf-16" if bom.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    return path.open("r", encoding=encoding, errors="replace", newline=""), encoding


def create_cache(source_path, directory, *, encoding="auto", order="auto"):
    if not source_path.is_file():
        raise ValueError("input is not a regular file")
    directory.mkdir(parents=True, exist_ok=False)
    connection = sqlite3.connect(directory / "events.sqlite3")
    try:
        connection.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE events(seq INTEGER PRIMARY KEY, line INTEGER, segment INTEGER, event TEXT, request_id TEXT, round INTEGER, payload TEXT);
            CREATE INDEX event_segment ON events(segment, seq);
            CREATE INDEX request_segment ON events(segment, request_id, event);
            CREATE TABLE segments(id INTEGER PRIMARY KEY, reason TEXT, config TEXT);
        """)
        stats = Counter()
        digest = hashlib.sha256()
        previous = None
        source, encoding = open_source(source_path, encoding)
        with source:
            for line, text in objects(source, stats):
                for event in decode_events(text, stats):
                    payload = dump(event)
                    digest.update(payload.encode("utf-8"))
                    connection.execute("INSERT INTO events(line,event,request_id,round,payload) VALUES (?,?,?,?,?)",
                                       (line, event["event"], event.get("id"), event.get("round"), payload))
                    stats["recognizedEvents"] += 1
                    if event["event"] == "turn" and event.get("id") is not None:
                        current = int(event["id"])
                        if previous is not None and current != previous:
                            stats["idAscending" if current > previous else "idDescending"] += 1
                        previous = current
        direction = ("reverse" if stats["idDescending"] > stats["idAscending"] else "forward") if order == "auto" else order
        metadata = {"version": CACHE_VERSION, "logId": digest.hexdigest()[:16], "encoding": encoding, "order": direction,
                    "orderRequested": order, "scan": dict(stats), "inputBytes": source_path.stat().st_size}
        connection.execute("INSERT INTO meta VALUES ('info',?)", (dump(metadata),))
        assign_segments(connection, direction)
        connection.commit()
        return metadata
    finally:
        connection.close()


def assign_segments(connection, direction):
    ordering = "DESC" if direction == "reverse" else "ASC"
    recent_first = "ASC" if direction == "reverse" else "DESC"
    segment = 0
    config = {}
    seen_game = False
    side = None
    for seq, event_type, payload in connection.execute(f"SELECT seq,event,payload FROM events ORDER BY seq {ordering}"):
        row = json.loads(payload)
        current_side = ((row.get("input") or {}).get("our") or {}).get("side")
        new_session = event_type == "turn" and row.get("status") == "new_session" and seen_game
        side_change = event_type == "turn" and current_side in {"challenger", "defender"} and side is not None and current_side != side and seen_game
        if segment == 0 or event_type == "listening" or new_session or side_change:
            old_segment = segment
            segment += 1
            reason = "listening" if event_type == "listening" else "new_session" if new_session else "side_change" if side_change else "missing_startup"
            if event_type == "listening":
                config = {key: row[key] for key in ("build", "python", "rules", "decisionBudgetMs", "taskMaxRequests") if key in row}
            connection.execute("INSERT INTO segments VALUES (?,?,?)", (segment, reason, dump(config)))
            if (new_session or side_change) and row.get("id") is not None:
                connection.execute(f"""UPDATE events SET segment=? WHERE seq=(SELECT seq FROM events
                    WHERE segment=? AND request_id=? AND event='request_received' ORDER BY seq {recent_first} LIMIT 1)""",
                                   (segment, old_segment, row["id"]))
            seen_game, side = False, None
        connection.execute("UPDATE events SET segment=? WHERE seq=?", (segment, seq))
        if event_type == "turn" and row.get("status") in {"new", "new_session"}:
            seen_game = True
            if current_side in {"challenger", "defender"}:
                side = current_side


def read_cache(directory):
    path = (directory / "events.sqlite3").resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        info = connection.execute("SELECT value FROM meta WHERE key='info'").fetchone()
        metadata = json.loads(info[0]) if info else None
        if not isinstance(metadata, dict) or metadata.get("version") != CACHE_VERSION:
            raise ValueError("unsupported or incomplete cache")
    except Exception:
        connection.close()
        raise
    return connection, metadata


def segment_summary(connection, segment, reason, config, direction):
    totals, actions, weapons, phases, statuses, http, coverage = (Counter() for _ in range(7))
    marks = {}
    paths = {}
    previous_round = previous_hp = previous_phase = previous_task_start = None
    quiet_start = quiet_end = None
    longest_quiet = None
    first_gold = last_gold = max_gold = None
    elapsed_total = elapsed_max = 0.0
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS analyzed_rounds(segment INTEGER, round INTEGER, PRIMARY KEY(segment,round))")

    def mark(kind, round_no, focus="all"):
        marks.setdefault(kind, {"kind": kind, "round": round_no, "focus": focus})

    def finish_quiet():
        nonlocal longest_quiet
        if quiet_start is not None and (longest_quiet is None or quiet_end - quiet_start > longest_quiet[1] - longest_quiet[0]):
            longest_quiet = [quiet_start, quiet_end]

    order = "DESC" if direction == "reverse" else "ASC"
    for line, event_type, payload in connection.execute(f"SELECT line,event,payload FROM events WHERE segment=? ORDER BY seq {order}", (segment,)):
        row = json.loads(payload)
        totals[event_type] += 1
        if event_type == "logs_dropped":
            totals["reportedDroppedLogs"] += row.get("count") or 0
        if event_type != "turn":
            continue
        statuses[row.get("status", "missing")] += 1
        http[str(row.get("http", "missing"))] += 1
        totals["responseNotWritten"] += row.get("responseWritten") is False
        totals["eventsWithOmittedFields"] += bool(row.get("omittedFields"))
        if row.get("reason"):
            totals["reason:" + row["reason"]] += 1
        elapsed = row.get("elapsedMs")
        if elapsed is not None:
            elapsed_total += elapsed
            elapsed_max = max(elapsed_max, elapsed)
            totals["timedTurns"] += 1
        round_no = row.get("round")
        if row.get("status") not in {"new", "new_session"}:
            continue
        if type(round_no) is not int:
            totals["newWithoutIntegerRound"] += 1
            continue
        inserted = connection.execute("INSERT OR IGNORE INTO analyzed_rounds VALUES (?,?)", (segment, round_no))
        if not inserted.rowcount:
            totals["duplicateNewRounds"] += 1
            continue
        totals["distinctNewRounds"] += 1
        if previous_round is not None and round_no < previous_round:
            totals["roundRegressions"] += 1
        decision = row.get("decision") or {}
        inputs = row.get("input") or {}
        our = inputs.get("our") or {}
        task = decision.get("task") or {}
        commands = row.get("actions") or {}
        living = decision.get("livingActors")
        action_count = row.get("actionCount")
        tools_known = row.get("promptChars") is not None and row.get("executeCmdChars") is not None
        has_tools = bool(row.get("promptChars") or row.get("executeCmdChars"))
        for key in ("selected", "offers", "robots", "robotSnapshot", "defense", "task", "failedPreviousActions", "errorCodes"):
            coverage[key] += key in decision and decision[key] is not None
        coverage["actions"] += "actions" in row and row["actions"] is not None
        coverage["ourUnits"] += "units" in our and our["units"] is not None
        totals["emptyRoleActions"] += action_count == 0
        totals["llmRequests"] += bool(row.get("promptChars"))
        totals["commandRequests"] += bool(row.get("executeCmdChars"))
        totals["noLivingActors"] += living == 0
        totals["aliveRounds"] += living is not None and living > 0
        totals["unknownLivingActors"] += living is None
        totals["actionsNotExpanded"] += action_count is not None and action_count > len(commands)
        held = any(item[1] == "hold-weapon" for item in decision.get("selected") or [])
        idle_alive = living is not None and living > 0 and action_count == 0 and tools_known and not has_tools and not held
        totals["idleWhileAliveExcludingHold"] += idle_alive
        totals["positionedHoldRounds"] += action_count == 0 and held
        if living == 0 and action_count == 0 and tools_known and not has_tools:
            if quiet_end is not None and round_no == quiet_end + 1:
                quiet_end = round_no
            else:
                finish_quiet()
                quiet_start = quiet_end = round_no
        else:
            finish_quiet()
            quiet_start = quiet_end = None
        if living == 0:
            mark("首次观测无存活角色", round_no, "defense")
        if decision.get("daytime") is False:
            mark("首次观测夜间", round_no, "defense")
        if (decision.get("robots") or 0) > 0:
            mark("首次记录机器人数量非零", round_no, "defense")
        if idle_alive:
            mark("存活期间无动作且无工具请求", round_no)
        if (decision.get("search") or {}).get("exhausted") is True:
            totals["searchExhausted"] += 1
            mark("搜索预算耗尽", round_no)
        failures = decision.get("failedPreviousActions") or []
        error_codes = decision.get("errorCodes") or []
        totals["explicitFailedActions"] += len(failures)
        totals["reportedErrorCodes"] += len(error_codes)
        if failures or error_codes:
            mark("首次明确失败回执或错误码", round_no)
        for key, command in commands.items():
            command = command or {}
            kind = command.get("action", "unknown")
            actions[kind] += 1
            if kind in {"build", "acceptTask", "submitAnswer", "attack", "buy"}:
                mark("首次输出 " + kind, round_no, "task" if kind in {"acceptTask", "submitAnswer"} else "defense")
            if kind == "attack" and (key in weapons or len(weapons) < 64):
                weapons[key] += 1
        phase = decision.get("taskPhase", "missing")
        phases[phase] += 1
        active = phase not in {"inactive", "missing", "unknown"}
        task_start = task.get("started")
        if active and (previous_phase in {None, "inactive", "missing", "unknown"} or
                       (task_start is not None and previous_task_start is not None and task_start != previous_task_start)):
            totals["observedTaskWindows"] += 1
            mark("首次观测任务状态激活", round_no, "task")
        if phase == "budget-exhausted" and previous_phase != phase:
            totals["taskBudgetExhaustionTransitions"] += 1
            mark("任务预算耗尽", round_no, "task")
        if task.get("repeatedWithoutProgress") is True:
            mark("重复命令且结果无变化", round_no, "task")
        result = task.get("result") or {}
        if result.get("status") in {"timeout", "judger-error", "uncorrelated-timeout"} or result.get("exitCode") not in (None, 0):
            mark("任务命令失败或结果未关联", round_no, "task")
        if row.get("promptChars"):
            mark("首次LLM请求", round_no, "task")
        gold = our.get("gold")
        if gold is not None:
            first_gold = gold if first_gold is None else first_gold
            last_gold = gold
            max_gold = gold if max_gold is None else max(max_gold, gold)
        units = our.get("units") or []
        station = next((item for item in units if item and item.get("kind") == "station"), {})
        hp = station.get("health")
        if hp is not None and previous_hp is not None and hp < previous_hp:
            mark("首次观测基地掉血", round_no, "defense")
        if hp == 0:
            mark("首次观测基地血量为零", round_no, "defense")
        previous_hp = hp if hp is not None else previous_hp
        chosen_approach = {actor for item in decision.get("selected") or [] if item[1] == "approach-weapon" for actor in item[2]}
        current_paths = {}
        for item in units:
            if not item or item.get("kind") not in {"worker", "pioneer"} or not isinstance(item.get("pos"), list) or len(item["pos"]) != 2:
                continue
            actor = item.get("id")
            history = paths.get(actor, deque(maxlen=8)) if previous_round is not None and round_no == previous_round + 1 else deque(maxlen=8)
            history.append((tuple(item["pos"]), actor in chosen_approach))
            current_paths[actor] = history
            positions = [point for point, _ in history]
            if len(history) >= 4 and all(approach for _, approach in history) and positions[-1] in positions[:-1] and sum(a != b for a, b in zip(positions, positions[1:])) >= 3:
                mark("接近炮位时反复回访位置（待核验）", round_no, "defense")
                totals["oscillationSignals"] += 1
        paths = current_paths
        previous_round, previous_phase, previous_task_start = round_no, phase, task_start
    finish_quiet()
    pairs = connection.execute("""SELECT request_id, SUM(event='request_received'), SUM(event='turn')
        FROM events WHERE segment=? AND request_id IS NOT NULL AND event IN ('request_received','turn') GROUP BY request_id""", (segment,))
    for _, received, completed in pairs:
        totals["requestIdsWithoutTurn"] += received > 0 and completed == 0
        totals["turnIdsWithoutRequest"] += completed > 0 and received == 0
        totals["duplicateRequestIds"] += received > 1 or completed > 1
    gaps, last_round = [], None
    missing_rounds = 0
    bounds = connection.execute("SELECT MIN(round),MAX(round) FROM analyzed_rounds WHERE segment=?", (segment,)).fetchone()
    for (round_no,) in connection.execute("SELECT round FROM analyzed_rounds WHERE segment=? ORDER BY round", (segment,)):
        if last_round is not None and round_no > last_round + 1:
            missing_rounds += round_no - last_round - 1
            if len(gaps) < 10:
                gaps.append([last_round + 1, round_no - 1])
        last_round = round_no
    return {"segment": segment, "startReason": reason, "config": config, "rounds": list(bounds), "totals": dict(totals),
            "statuses": dict(statuses), "http": dict(http), "actions": dict(actions), "weaponAttacks": dict(weapons), "taskPhases": dict(phases),
            "fieldCoverage": dict(coverage), "missingNewRounds": missing_rounds, "gapExamples": gaps,
            "longestNoLivingIdle": longest_quiet, "gold": {"first": first_gold, "last": last_gold, "max": max_gold},
            "elapsedMs": {"mean": round(elapsed_total / totals["timedTurns"], 3) if totals["timedTurns"] else None, "max": elapsed_max},
            "bookmarks": sorted(marks.values(), key=lambda mark: mark["round"])}


def overview_text(metadata, summaries):
    lines = ["# 对局日志总览（先发送本文件）", "", f"logId={metadata['logId']}；识别方向={metadata['order']}；编码={metadata['encoding']}。",
             "动作统计是输出次数，不代表执行成功。缺字段不按零处理；任务状态窗口数不等于接取次数。",
             "清洗只保留白名单摘要，不包含任务/答案/命令正文、认证信息或原始路径；缓存不能替代完整请求回放。", "",
             "扫描：" + dump(metadata["scan"]), f"运行/会话分析段：{len(summaries)}。round 回退本身不切段；自动方向按请求 ID 趋势判断，混合顺序请手动指定。"]
    if not summaries:
        lines += ["", "未识别结构化在线事件，不能认定程序没有行动。检查编码、导出格式或是否只有旧版 demo 文本；不要仅上传空统计。"]
    used = len("\n".join(lines).encode("utf-8"))
    for summary in summaries:
        total, coverage = summary["totals"], summary["fieldCoverage"]
        block = ["", f"## 段 {summary['segment']} / R{summary['rounds'][0]}–R{summary['rounds'][1]}", "",
                 "版本/边界：" + dump({"startReason": summary["startReason"], **summary["config"]}),
                 f"到达/完成={total.get('request_received',0)}/{total.get('turn',0)}；有效独立回合={total.get('distinctNewRounds',0)}；缺失回合数={summary['missingNewRounds']}。",
                 "HTTP=" + dump(summary["http"]) + "；status=" + dump(summary["statuses"]),
                 f"fatal/telemetry_error/http_rejected={total.get('fatal',0)}/{total.get('telemetry_error',0)}/{total.get('http_rejected',0)}；拒绝/回退原因=" + dump({key[7:]: value for key, value in total.items() if key.startswith("reason:")}),
                 f"只有到达/只有完成的请求ID={total.get('requestIdsWithoutTurn',0)}/{total.get('turnIdsWithoutRequest',0)}；重复ID组={total.get('duplicateRequestIds',0)}；重复new回合={total.get('duplicateNewRounds',0)}；回合回退={total.get('roundRegressions',0)}。",
                 f"空角色动作={total.get('emptyRoleActions',0)}；存活时无动作无工具（排除守位）={total.get('idleWhileAliveExcludingHold',0)}；守位空动作={total.get('positionedHoldRounds',0)}；无存活角色={total.get('noLivingActors',0)}；角色存活数未知={total.get('unknownLivingActors',0)}。",
                 "无存活角色空等最长连续区间=" + dump(summary["longestNoLivingIdle"]),
                 "动作=" + dump(summary["actions"]) + "；逐炮attack=" + dump(summary["weaponAttacks"]),
                 f"LLM/命令输出={total.get('llmRequests',0)}/{total.get('commandRequests',0)}；任务状态窗口={total.get('observedTaskWindows',0)}；首次进入预算耗尽次数={total.get('taskBudgetExhaustionTransitions',0)}。",
                 "任务阶段=" + dump(summary["taskPhases"]),
                 f"明确失败动作条数={total.get('explicitFailedActions',0)}；错误码条数={total.get('reportedErrorCodes',0)}；搜索耗尽={total.get('searchExhausted',0)}。失败反馈字段覆盖率见下，缺字段时不能把这些零当作无失败。",
                 "字段出现回合数/有效独立回合数=" + dump(coverage),
                 "金币=" + dump(summary["gold"]) + "；耗时ms=" + dump(summary["elapsedMs"]),
                 f"记录含omittedFields={total.get('eventsWithOmittedFields',0)}；平台日志自报丢弃={total.get('reportedDroppedLogs',0)}；动作未完整展开回合={total.get('actionsNotExpanded',0)}。",
                 "关键节点：" + "；".join(f"R{mark['round']} {mark['kind']}" for mark in summary["bookmarks"])]
        important = [mark for mark in summary["bookmarks"] if any(word in mark["kind"] for word in ("反复", "耗尽", "掉血", "失败", "无变化"))]
        for mark in (important or summary["bookmarks"][:1])[:3]:
            lo, hi = max(0, mark["round"] - 4), mark["round"] + 2
            block += [f"建议细查：段{summary['segment']} R{lo}:{hi} focus={mark['focus']}（{mark['kind']}）。"]
        content = "\n".join(block)
        if used + len(content.encode("utf-8")) > OVERVIEW_BYTES - 600:
            lines += ["", "总览达到16KiB上限，其余段未展开；完整分段统计在本地 summary.json。"]
            break
        lines.extend(block)
        used += len(content.encode("utf-8"))
    lines += ["", "下一步：先发送 overview.md；被要求细查时，用 extract 的 --segment / --rounds / --focus 导出证据，先发 INDEX.md，再按需发送 part-*.jsonl。",
              "机器人、命令结果等字段若原日志没有，清洗无法还原；本报告的往返/闲置信号不是根因或胜负结论。"]
    return "\n".join(lines) + "\n"


def summarize(source, directory, *, encoding="auto", order="auto"):
    metadata = create_cache(source, directory, encoding=encoding, order=order)
    connection, _ = read_cache(directory)
    with closing(connection):
        segments = connection.execute("SELECT id,reason,config FROM segments ORDER BY id").fetchall()
        summaries = [segment_summary(connection, segment, reason, json.loads(config), metadata["order"]) for segment, reason, config in segments]
    (directory / "summary.json").write_text(dump({"metadata": metadata, "segments": summaries}) + "\n", encoding="utf-8")
    (directory / "overview.md").write_text(overview_text(metadata, summaries), encoding="utf-8", newline="\n")
    return metadata, summaries


def focused(row, focus):
    if focus == "all":
        return row
    result = {key: row[key] for key in ("event", "id", "round", "http", "status", "reason", "responseWritten", "elapsedMs", "error", "omittedFields") if key in row}
    inputs, decision = row.get("input") or {}, row.get("decision") or {}
    if focus == "transport":
        result.update({key: row[key] for key in ("method", "declaredBytes", "transferEncodingPresent", "count", "build", "python") if key in row})
        result["input"] = {key: inputs[key] for key in ("roundType", "keys", "missing", "map") if key in inputs}
        return result
    result.update({key: row[key] for key in ("actionCount", "actions", "promptChars", "executeCmdChars") if key in row})
    common = {"day", "daytime", "livingActors", "selected", "offers", "emptyReason", "search", "diagnostics", "failedPreviousActions", "errorCodes"}
    keys = common | ({"weapons", "robots", "robotSnapshot", "defense", "staffing", "buildCheck", "projectedRoleLosses", "threatenedPostsLost"} if focus == "defense" else {"taskPhase", "task"})
    result["decision"] = {key: decision[key] for key in keys if key in decision}
    result["input"] = {key: inputs[key] for key in ({"our"} if focus == "defense" else {"our", "tasks", "tasksType", "phaseTask", "llmResp", "lastCmdResult"}) if key in inputs}
    return result


def fit_record(row, max_bytes):
    row = dict(row)
    while len((dump(row) + "\n").encode("utf-8")) > max_bytes:
        candidates = [key for key in ("input", "decision", "actions", "error") if key in row]
        if not candidates:
            raise ValueError("part size too small for evidence metadata")
        largest = max(candidates, key=lambda key: len(dump(row[key]).encode("utf-8")))
        del row[largest]
        row.setdefault("fieldsRemovedForSize", []).append(largest)
    return row


def extract(directory, output, *, segment, rounds=None, focus="all", part_bytes=12 * 1024, limit=2000):
    if not 4096 <= part_bytes <= 65536 or not 1 <= limit <= 100000:
        raise ValueError("invalid evidence size or count limit")
    connection, metadata = read_cache(directory)
    with closing(connection):
        config_row = connection.execute("SELECT config FROM segments WHERE id=?", (segment,)).fetchone()
        if config_row is None:
            raise ValueError("unknown segment; inspect overview.md or summary.json")
        config = json.loads(config_row[0])
        where = "e.segment=?"
        parameters = [segment]
        if focus != "transport":
            where += " AND e.event='turn'"
        if rounds is not None:
            where += " AND (e.round BETWEEN ? AND ?"
            parameters.extend(rounds)
            if focus == "transport":
                where += " OR (e.event='request_received' AND EXISTS (SELECT 1 FROM events t WHERE t.segment=e.segment AND t.request_id=e.request_id AND t.event='turn' AND t.round BETWEEN ? AND ?))"
                parameters.extend(rounds)
            where += ")"
        count = connection.execute("SELECT COUNT(*) FROM events e WHERE " + where, parameters).fetchone()[0]
        output.mkdir(parents=True, exist_ok=False)
        ordering = "DESC" if metadata["order"] == "reverse" else "ASC"
        parts = []
        content = bytearray()
        part_rows = []
        written = pruned = 0

        def start_part():
            header = {"schema": "coregeek-evidence-v1", "logId": metadata["logId"], "segment": segment,
                      "part": len(parts) + 1, "focus": focus, "roundsRequested": rounds, "config": config,
                      "note": "Allowlisted summaries; missing fields are unknown, not zero. Not a full replay."}
            return bytearray((dump(header) + "\n").encode("utf-8"))

        def finish_part():
            if not part_rows:
                return
            name = f"part-{len(parts) + 1:03d}.jsonl"
            (output / name).write_bytes(content)
            observed = [round_no for round_no in part_rows if round_no is not None]
            parts.append({"file": name, "bytes": len(content), "events": len(part_rows),
                          "rounds": [min(observed), max(observed)] if observed else None})

        content = start_part()
        for seq, line, payload in connection.execute("SELECT e.seq,e.line,e.payload FROM events e WHERE " + where + f" ORDER BY e.seq {ordering} LIMIT ?", (*parameters, limit)):
            row = focused(json.loads(payload), focus)
            row = fit_record({"sourceEvent": seq, "sourceLine": line, **row}, part_bytes - len(start_part()))
            encoded = (dump(row) + "\n").encode("utf-8")
            if len(content) + len(encoded) > part_bytes:
                finish_part()
                part_rows = []
                content = start_part()
                row = fit_record(row, part_bytes - len(content))
                encoded = (dump(row) + "\n").encode("utf-8")
            content.extend(encoded)
            part_rows.append(row.get("round"))
            written += 1
            pruned += bool(row.get("fieldsRemovedForSize"))
        finish_part()
    manifest = {"logId": metadata["logId"], "segment": segment, "focus": focus, "rounds": rounds,
                "matchingEvents": count, "writtenEvents": written, "notExportedByLimit": count - written,
                "recordsPrunedForSize": pruned, "partByteLimit": part_bytes, "parts": parts}
    (output / "manifest.json").write_text(dump(manifest) + "\n", encoding="utf-8")
    index = ["# 精简证据索引", "", f"logId={metadata['logId']}；段={segment}；focus={focus}；请求回合={rounds}。",
             f"匹配 {count} 条，导出 {written} 条，共 {len(parts)} 包，每包不超过 {part_bytes} 字节。",
             f"因事件数上限未导出={count-written}；因单条过大裁剪字段的记录={pruned}（记录内 fieldsRemovedForSize 标注）。",
             "先发送本索引，再按需发送指定 part 文件；不要发送整个缓存或原始大日志。", ""]
    for part in parts[:100]:
        index.append(f"- {part['file']}：R{part['rounds']}，{part['events']}条，{part['bytes']}字节")
    if len(parts) > 100:
        index.append("其余分包见 manifest.json；建议缩小回合范围。")
    if not count:
        index.append("没有匹配事件。检查段号和回合范围；此结果不表示游戏没有行动。")
    (output / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8", newline="\n")
    return manifest


def round_range(text):
    try:
        left, right = (int(item) for item in text.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError("use START:END, e.g. 65:100") from None
    if not 0 <= left <= right <= 10**9:
        raise argparse.ArgumentTypeError("invalid round range")
    return left, right


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    summary = commands.add_parser("summary", help="scan full log and create a <=16KiB overview plus a local sanitized cache")
    summary.add_argument("input", type=Path)
    summary.add_argument("--out", type=Path, required=True, help="new directory; existing output is never overwritten")
    summary.add_argument("--encoding", choices=("auto", "utf-8-sig", "utf-16", "gb18030"), default="auto")
    summary.add_argument("--order", choices=("auto", "forward", "reverse"), default="auto")
    detail = commands.add_parser("extract", help="export small evidence parts from an existing sanitized cache")
    detail.add_argument("cache", type=Path)
    detail.add_argument("--out", type=Path, required=True)
    detail.add_argument("--segment", type=int, required=True)
    detail.add_argument("--rounds", type=round_range)
    detail.add_argument("--focus", choices=("all", "defense", "task", "transport"), default="all")
    detail.add_argument("--part-kb", type=int, choices=range(4, 65), default=12)
    detail.add_argument("--limit", type=int, default=2000)
    args = parser.parse_args(argv)
    try:
        if args.command == "summary":
            metadata, _ = summarize(args.input, args.out, encoding=args.encoding, order=args.order)
            print(f"Send first: {args.out / 'overview.md'}")
            return 0 if metadata["scan"].get("recognizedEvents", 0) else 1
        manifest = extract(args.cache, args.out, segment=args.segment, rounds=args.rounds, focus=args.focus,
                           part_bytes=args.part_kb * 1024, limit=args.limit)
        print(f"Send index: {args.out / 'INDEX.md'}; parts={len(manifest['parts'])}")
        return 0 if manifest["writtenEvents"] else 1
    except (OSError, ValueError, sqlite3.Error) as error:
        # Do not echo paths or any malformed source text to diagnostics.
        print(f"log triage failed: {type(error).__name__}; check input/encoding and use a NEW output directory.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
