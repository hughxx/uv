#!/usr/bin/env python3
"""Offline observation replay. Does not execute tool commands or simulate robots."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Iterable, TextIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "CoreGeek" / "src"))

from agent.application import AgentApplication
from agent.diagnostics import decision_report
from agent.protocol import encode_response

MAX_LINE_BYTES = 4 * 1024 * 1024


def replay(lines: Iterable[str], output: TextIO, *, include_response: bool = False) -> tuple[int, int]:
    application = AgentApplication()
    count = mismatches = 0
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise ValueError(f"line {line_no}: oversized observation")
        try:
            row = json.loads(line)
            wrapped = isinstance(row, dict) and "request" in row
            request = row["request"] if wrapped else row
            response = application.handle_turn(request)
            report = decision_report(application)
            report["line"] = line_no
            if wrapped and "expected_response" in row:
                expected = json.loads(encode_response(row["expected_response"]))
                matched = response == expected
                report["matchesExpected"] = matched
                mismatches += int(not matched)
            if include_response:
                report["response"] = response
            output.write(json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
            count += 1
        except Exception as error:
            # No raw task/answer/command excerpt, even on malformed input.
            raise ValueError(f"line {line_no}: {type(error).__name__}") from None
    return count, mismatches


def bounded_lines(source: TextIO) -> Iterable[str]:
    while True:
        line = source.readline(MAX_LINE_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_LINE_BYTES:
            raise ValueError("oversized observation line")
        yield line


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSONL observations or {request, expected_response} records")
    parser.add_argument("--output", type=Path, help="new JSONL report file; default stdout; existing files are never overwritten")
    parser.add_argument("--include-response", action="store_true", help="include private task answers/prompts/commands; local use only")
    args = parser.parse_args()
    try:
        with args.input.open("r", encoding="utf-8-sig") as source:
            context = args.output.open("x", encoding="utf-8", newline="\n") if args.output else nullcontext(sys.stdout)
            with context as output:
                count, mismatches = replay(bounded_lines(source), output, include_response=args.include_response)
    except (OSError, ValueError) as error:
        print(f"replay failed: {type(error).__name__}" + (f": {error}" if isinstance(error, ValueError) else ""), file=sys.stderr)
        return 2
    print(f"replayed {count} observations; {mismatches} expected-response mismatches", file=sys.stderr)
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
