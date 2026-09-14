#!/usr/bin/env python3
"""Competition entry point; works without installation or a specific cwd."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def bounded_integer(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("value must be an integer") from error
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"value must be between {minimum} and {maximum}")
        return number
    return parse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CoreGeek competition agent")
    parser.add_argument("port", type=parse_port)
    parser.add_argument("--round-origin", type=int, choices=(0, 1), default=1)
    parser.add_argument("--disable-inferred-building", action="store_true", help="disable the unverified 12/20-cell construction mask")
    parser.add_argument("--no-role-death-guard", action="store_true", help="disable the precautionary one-step robot threat guard")
    parser.add_argument("--no-threatened-post-guard", action="store_true", help="allow loss of staffed posts under nearby pressure")
    parser.add_argument("--disable-joint-follow", action="store_true", help="forbid moves into teammates' currently occupied cells")
    parser.add_argument("--decision-budget-ms", type=bounded_integer(50, 3500), default=2500)
    parser.add_argument("--task-max-requests", type=bounded_integer(1, 100), default=12)
    parser.add_argument("--tool-wait-rounds", type=bounded_integer(1, 10), default=3)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    from agent.server import serve
    from agent.application import AgentApplication
    from agent.strategy import StrategyEngine
    from agent.world import RuleProfile

    rules = RuleProfile(round_origin=args.round_origin, inferred_build_rings=not args.disable_inferred_building,
                        guard_projected_role_deaths=not args.no_role_death_guard,
                        preserve_threatened_posts=not args.no_threatened_post_guard, joint_follow_moves=not args.disable_joint_follow)
    serve(args.port, AgentApplication(StrategyEngine(rules=rules, budget_seconds=args.decision_budget_ms / 1000,
                                                     task_max_requests=args.task_max_requests, tool_wait_rounds=args.tool_wait_rounds)))


if __name__ == "__main__":
    main()
