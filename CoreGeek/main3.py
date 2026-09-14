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


def main() -> None:
    parser = argparse.ArgumentParser(description="CoreGeek competition agent")
    parser.add_argument("port", type=parse_port)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    from agent.server import serve

    serve(args.port)


if __name__ == "__main__":
    main()
