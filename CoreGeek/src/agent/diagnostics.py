"""Allowlisted decision summaries: no task text, commands or raw observations."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .application import AgentApplication


def decision_report(application: AgentApplication) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": "coregeek-decision-v1", "status": application.last_status,
        "revision": application.memory.revision,
        "elapsedMs": round(application.last_elapsed_seconds * 1000, 3),
    }
    decision = application.engine.last_decision
    # A cached OLD request must not be explained using the current round plan.
    if application.last_status not in {"new", "new_session"} or decision is None:
        return report
    world, plan = decision.world, decision.plan
    action_results = world.observation.raw.get("lastRoundRoleActionResults", {})
    errors = world.observation.raw.get("errors", [])
    report.update({
        "round": world.observation.round_no, "day": decision.rules.day(world.observation.round_no),
        "rules": asdict(decision.rules), "candidateCount": decision.candidate_count,
        "search": {"visited": plan.visited, "exhausted": plan.exhausted, "rejections": dict(plan.rejections)},
        "offers": dict(decision.offers), "emptyReason": plan.empty_reason,
        "utilityEstimate": plan.utility, "projectedRoleLosses": plan.projected_role_losses,
        "threatenedPostsLost": plan.threatened_posts_lost,
        "actualNetGoldDelta": decision.observed_gold_delta,
        "failedPreviousActions": sorted(str(key) for key, value in action_results.items() if value is False) if isinstance(action_results, dict) else [],
        "errorCodes": [error["errorCode"] for error in errors if isinstance(error, dict) and type(error.get("errorCode")) is int] if isinstance(errors, list) else [],
        "events": [event.kind for event in application.memory.events],
        "diagnostics": list(decision.diagnostics),
        "executionCooldowns": [{"actor": failure.actor, "action": failure.kind, "streak": failure.streak,
                                "retryAfter": failure.retry_after} for failure in decision.failures
                               if failure.retry_after > world.observation.round_no],
        "transitions": [asdict(transition) for transition in decision.transitions],
        "selected": [{"key": candidate.key, "definition": candidate.definition, "stage": candidate.stage,
                      "actors": sorted(candidate.actors), "evidence": list(candidate.evidence),
                      "valueEstimate": asdict(candidate.value), "reservedGold": candidate.reserved_gold,
                      "actions": [{"actor": action.actor_id, "kind": action.kind,
                                   "targets": [target.payload() for target in action.targets],
                                   "requiresVacating": [actor.id for actor in world.actors if action.kind == "move"
                                                        and actor.id != action.actor_id and actor.pos in action.targets],
                                   "item": action.name, "weapon": action.weapon_id} for action in candidate.actions]}
                     for candidate in plan.candidates],
        "tools": {"promptPresent": bool(decision.response["prompt"]), "commandPresent": bool(decision.response["executeCmd"]),
                  "phase": decision.task_state.phase if decision.task_state else "inactive",
                  "requests": decision.task_state.step if decision.task_state else 0},
    })
    return report
